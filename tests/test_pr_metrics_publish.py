"""Tests for ``forge-metrics publish`` (:mod:`forge.pr_metrics_publish`).

# MOCKING STRATEGY: ``gh`` goes through the module's ``Runner`` seam,
# replaced by the bench tests' ``FakeRunner``; ``gh repo clone`` becomes a
# real ``git clone`` of a bare repo in ``tmp_path``, so forge's real commit
# and push seams run against it. Rows come from a real in-memory database.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest

from forge import pr_metrics
from forge.git_utils import _FORGE_GITHUB_REPO
from forge.pr_metrics_bench import Completed
from forge.pr_metrics_publish import (
    ALLOWED_FILES,
    FORGE_LABEL,
    OTHER_LABEL,
    PAGE_FILES,
    Published,
    PublishError,
    enable_pages_command,
    pages_url,
    public_files,
    public_order,
    publish,
)
from tests.conftest import GIT_ENV
from tests.test_pr_metrics_bench import FakeRunner, _git
from tests.test_pr_metrics_report import _REPO_A, _REPO_B, _fixture_rows


if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path


MAINTAINER = _FORGE_GITHUB_REPO.partition("/")[0]
SLUG = f"{MAINTAINER}/forge-metrics"
PUBLIC = '{"private": false, "permissions": {"admin": true}}'
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}|W\d\d")


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No machine git config; data dir in *tmp_path*."""
    for name, value in GIT_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))


@pytest.fixture
def bare_origin(tmp_path: Path) -> Path:
    """An empty bare repo standing in for the GitHub page repo."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "--quiet", "--bare", "-b", "main", str(origin))
    return origin


def _runner(origin: Path, repo: str = PUBLIC, *, pages: bool = False) -> FakeRunner:
    """A runner whose ``gh`` sees *repo* and Pages on or off.

    Args:
        origin: The bare page repo.
        repo: ``gh api repos/<slug>`` JSON.
        pages: Whether ``gh api repos/<slug>/pages`` succeeds.

    Returns:
        The runner.
    """
    return FakeRunner(
        origin,
        answers=[
            (("gh", "api", "user"), Completed(0, f"{MAINTAINER}\n")),
            (("gh", "api", f"repos/{SLUG}/pages"), Completed(0 if pages else 1)),
            (("gh", "api", f"repos/{SLUG}"), Completed(0, repo)),
        ],
    )


def _files() -> dict[str, str]:
    """The public files of the fixture rows.

    Returns:
        File name → content.
    """
    return public_files(_fixture_rows(), schema=4, forge_repo_id=_REPO_A)


@dataclass
class _FailingClone:
    """A runner that answers like *inner* but fails ``gh repo clone``."""

    inner: FakeRunner

    def __call__(
        self, argv: Sequence[str], *, cwd: Path, timeout: float | None = None
    ) -> Completed:
        """Fail a clone; delegate anything else.

        Args:
            argv: The command.
            cwd: Its working directory.
            timeout: Passed through.

        Returns:
            The result.
        """
        if list(argv[:3]) == ["gh", "repo", "clone"]:
            return Completed(1, "", "clone exploded")
        return self.inner(argv, cwd=cwd, timeout=timeout)


def test_public_files_hold_no_dates_keys_or_repo_ids() -> None:
    """Behavior: no date, PR key or repo id (full or short) in either file."""
    for name, text in _files().items():
        assert not _DATE.search(text), name
        assert '"a1"' not in text, name
        for repo_id in (_REPO_A, _REPO_B):
            assert repo_id[:6] not in text, name
        assert FORGE_LABEL in text, name
        assert OTHER_LABEL in text, name
    assert "over time" not in _files()["index.html"]


def test_every_other_repo_merges_into_external() -> None:
    """Behavior: two different non-forge repos both show as 'external'."""
    files = public_files(_fixture_rows(), schema=4, forge_repo_id="not-recorded")

    repos = {pr["repo"] for pr in json.loads(files["metrics.json"])["prs"]}
    assert repos == {OTHER_LABEL}


def test_maintainer_only_refuses_another_gh_user(
    bare_origin: Path, tmp_path: Path
) -> None:
    """Behavior: a gh user other than forge's maintainer is refused first."""
    runner = _runner(bare_origin)
    runner.answers.insert(0, (("gh", "api", "user"), Completed(0, "someone-else\n")))

    with pytest.raises(PublishError, match="limited to forge's maintainer"):
        publish(SLUG, _files(), runner=runner, workdir=tmp_path)

    assert not runner.ran("gh", "repo", "clone")


def test_maintainer_only_refuses_a_repo_under_another_owner(
    bare_origin: Path, tmp_path: Path
) -> None:
    """Behavior: the maintainer may publish only to a repo they own."""
    runner = _runner(bare_origin)

    with pytest.raises(PublishError, match="limited to forge's maintainer"):
        publish("someone-else/page", _files(), runner=runner, workdir=tmp_path)

    assert not runner.ran("gh", "repo", "clone")


def test_public_rows_are_grouped_by_version_in_key_hash_order() -> None:
    """Behavior: release order by version; within each, order by PR-key hash."""
    rows = _fixture_rows()

    ordered = public_order(rows)

    assert [r.forge_version for r in ordered] == ["1.2.0"] * 3 + ["1.10.0"] * 2 + [None]
    first = [r.pr_key for r in ordered[:3]]
    assert first == sorted(
        ("a1", "a2", "a3"), key=lambda k: hashlib.sha256(k.encode()).hexdigest()
    )


def test_unchanged_rows_give_byte_identical_files() -> None:
    """Regression: the scrambled order is stable, so a republish is a no-op."""
    rows = _fixture_rows()

    once = public_files(rows, schema=4, forge_repo_id=_REPO_A)
    assert once == public_files(rows[::-1], schema=4, forge_repo_id=_REPO_A)


def test_public_page_escapes_injected_text() -> None:
    """Behavior: a version string carrying markup is escaped on the page."""
    rows = _fixture_rows(version_override="<script>v</script>")
    page = public_files(rows, schema=4, forge_repo_id=None)["index.html"]

    assert "<script" not in page
    assert "&lt;script&gt;" in page
    for external in ("http://", "https://", "<link", "src="):
        assert external not in page


@pytest.mark.parametrize(
    "repo",
    [
        '{"private": true, "permissions": {"admin": true}}',
        '{"private": false, "permissions": {"admin": false}}',
    ],
    ids=["private", "not-admin"],
)
def test_publish_refuses_a_private_or_foreign_repo(
    bare_origin: Path, tmp_path: Path, repo: str
) -> None:
    """Behavior: a private or non-admin target is refused before any clone.

    Args:
        repo: The repo metadata JSON the fake GitHub reports.
    """
    runner = _runner(bare_origin, repo)

    with pytest.raises(PublishError):
        publish(SLUG, _files(), runner=runner, workdir=tmp_path)

    assert not runner.ran("gh", "repo", "clone")


def test_publish_refuses_a_repo_holding_other_files(
    bare_origin: Path, tmp_path: Path
) -> None:
    """Behavior: a repo tracking anything publish did not write stays untouched."""
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "--quiet", "-b", "main")
    (work / "app.py").write_text("x = 1\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "--quiet", "-m", "app")
    _git(work, "push", "--quiet", str(bare_origin), "main")
    before = _git(bare_origin, "rev-parse", "main")

    with pytest.raises(PublishError, match=r"app\.py"):
        publish(SLUG, _files(), runner=_runner(bare_origin), workdir=tmp_path)

    assert _git(bare_origin, "rev-parse", "main") == before


def test_publish_pushes_the_page_files_to_main(
    bare_origin: Path, tmp_path: Path
) -> None:
    """Behavior: an empty public repo gets the four files on main; Pages off noted."""
    published = publish(SLUG, _files(), runner=_runner(bare_origin), workdir=tmp_path)

    tracked = _git(bare_origin, "ls-tree", "-r", "--name-only", "main").splitlines()
    assert set(tracked) == ALLOWED_FILES
    assert published.changed
    assert not published.pages_on
    assert published.url == f"https://{MAINTAINER.lower()}.github.io/forge-metrics/"
    again = publish(
        SLUG, _files(), runner=_runner(bare_origin, pages=True), workdir=tmp_path
    )
    assert not again.changed
    assert again.pages_on
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith("forge-")] == []


def test_publish_refuses_a_repo_whose_default_branch_is_not_main(
    tmp_path: Path,
) -> None:
    """Behavior: a page repo on another default branch is refused unchanged."""
    origin = tmp_path / "pages.git"
    _git(tmp_path, "init", "--quiet", "--bare", "-b", "gh-pages", str(origin))
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "--quiet", "-b", "gh-pages")
    (work / "index.html").write_text("old\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "--quiet", "-m", "page")
    _git(work, "push", "--quiet", str(origin), "gh-pages")

    with pytest.raises(PublishError, match="gh-pages"):
        publish(SLUG, _files(), runner=_runner(origin), workdir=tmp_path)

    assert _git(origin, "branch", "--list", "main") == ""


def test_publish_reports_a_failed_clone(bare_origin: Path, tmp_path: Path) -> None:
    """Behavior: a clone failure is a refusal naming gh's error; temp dir removed."""
    runner = _FailingClone(_runner(bare_origin))

    with pytest.raises(PublishError, match="clone exploded"):
        publish(SLUG, _files(), runner=runner, workdir=tmp_path)

    assert not [p for p in tmp_path.iterdir() if p.name.startswith("forge-")]


def test_publish_reports_a_rejected_push(bare_origin: Path, tmp_path: Path) -> None:
    """Behavior: a push the remote rejects is a refusal, not a silent success."""
    hook = bare_origin / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)

    with pytest.raises(PublishError, match="push failed"):
        publish(SLUG, _files(), runner=_runner(bare_origin), workdir=tmp_path)

    assert _git(bare_origin, "branch", "--list", "main") == ""


def _cli(tmp_path: Path, *extra: str) -> int:
    """Run ``forge-metrics publish`` on an empty database in *tmp_path*.

    Args:
        tmp_path: Where the database lives.
        *extra: Arguments after ``publish``.

    Returns:
        The exit code.
    """
    db = str(tmp_path / "m.sqlite")
    return pr_metrics.main(["--db", db, "publish", "--repo", SLUG, *extra])


def test_cli_prints_the_pages_enable_command_when_pages_is_off(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Behavior: Pages off → the page URL and the one-time enable command."""
    monkeypatch.setattr(pr_metrics, "require_cli", lambda *_a, **_k: None)
    monkeypatch.setattr(
        pr_metrics,
        "publish",
        lambda *_a, **_k: Published(pages_url(SLUG), pages_on=False, changed=True),
    )

    assert _cli(tmp_path) == 0

    out = capsys.readouterr().out
    assert pages_url(SLUG) in out
    assert enable_pages_command(SLUG) in out


def test_cli_refusal_exits_2(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Behavior: a refused target exits 2 without a traceback."""

    def refuse(*_a: object, **_k: object) -> Published:
        msg = "refused"
        raise PublishError(msg)

    monkeypatch.setattr(pr_metrics, "require_cli", lambda *_a, **_k: None)
    monkeypatch.setattr(pr_metrics, "publish", refuse)

    assert _cli(tmp_path) == 2


def test_dry_run_writes_the_two_files_only(tmp_path: Path) -> None:
    """Behavior: ``--dry-run DIR`` writes index.html and metrics.json, nothing else."""
    out = tmp_path / "preview"

    db = str(tmp_path / "m.sqlite")
    code = pr_metrics.main(
        ["--db", db, "publish", "--repo", SLUG, "--dry-run", str(out)]
    )

    assert code == 0
    assert sorted(p.name for p in out.iterdir()) == sorted(PAGE_FILES)
