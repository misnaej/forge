"""Tests for ``forge-metrics publish`` (:mod:`forge.pr_metrics_publish`).

# MOCKING STRATEGY: ``gh`` goes through the module's ``Runner`` seam,
# replaced by the bench tests' ``FakeRunner``; ``gh repo clone`` becomes a
# real ``git clone`` of a bare repo in ``tmp_path``, so forge's real commit
# and push seams run against it. Rows come from a real in-memory database.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

import pytest

from forge import pr_metrics
from forge.pr_metrics_bench import Completed
from forge.pr_metrics_publish import (
    ALLOWED_FILES,
    PAGE_FILES,
    PublishError,
    public_files,
    publish,
)
from tests.conftest import GIT_ENV
from tests.test_pr_metrics_bench import FakeRunner, _git
from tests.test_pr_metrics_report import _REPO_A, _fixture_rows


if TYPE_CHECKING:
    from pathlib import Path


SLUG = "someone/forge-metrics"
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
            (("gh", "api", f"repos/{SLUG}/pages"), Completed(0 if pages else 1)),
            (("gh", "api", f"repos/{SLUG}"), Completed(0, repo)),
        ],
    )


def _files() -> dict[str, str]:
    """The public files of the fixture rows, each group reversed.

    Returns:
        File name → content.
    """
    return public_files(_fixture_rows(), schema=4, shuffle=list.reverse)


def test_public_files_hold_no_dates_or_full_repo_ids() -> None:
    """Behavior: neither file carries a date, a PR key or a full repo id."""
    for name, text in _files().items():
        assert not _DATE.search(text), name
        assert _REPO_A not in text, name
        assert '"a1"' not in text, name
        assert "aaaaaa" in text, name
    assert "over time" not in _files()["index.html"]


def test_public_rows_are_grouped_by_version_and_shuffled() -> None:
    """Behavior: rows follow release order by version, shuffled within each."""
    prs = json.loads(_files()["metrics.json"])["prs"]

    versions = [pr["forge_version"] for pr in prs]
    assert versions == ["1.2.0"] * 3 + ["1.10.0"] * 2 + [None]
    # Input order of the 1.2.0 group has rounds 1, 3, 2; the seam reversed it.
    assert [pr["review_rounds"] for pr in prs[:3]] == [2, 3, 1]


def test_public_page_escapes_injected_text() -> None:
    """Behavior: a version string carrying markup is escaped on the page."""
    page = public_files(_fixture_rows(version_override="<script>v</script>"), schema=4)[
        "index.html"
    ]

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
    assert published.url == "https://someone.github.io/forge-metrics/"
    again = publish(
        SLUG, _files(), runner=_runner(bare_origin, pages=True), workdir=tmp_path
    )
    assert not again.changed
    assert again.pages_on
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith("forge-")] == []


def test_dry_run_writes_the_two_files_only(tmp_path: Path) -> None:
    """Behavior: ``--dry-run DIR`` writes index.html and metrics.json, nothing else."""
    out = tmp_path / "preview"

    db = str(tmp_path / "m.sqlite")
    code = pr_metrics.main(
        ["--db", db, "publish", "--repo", SLUG, "--dry-run", str(out)]
    )

    assert code == 0
    assert sorted(p.name for p in out.iterdir()) == sorted(PAGE_FILES)
