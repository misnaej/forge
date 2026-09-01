"""Tests for forge.pr_plan — /pr finalization-path classifier.

# MOCKING STRATEGY: most cases exercise `classify()` against a real,
# ephemeral git repo (no network/remote) built by the helpers below — the
# thresholds and glob decisions live in `forge.pr_delta` and are already
# unit-tested in `test_pr_delta.py`, so here the concern is `pr_plan`'s
# composition (diff extraction, mode precedence, `classified_at` stamping).
# Only the delta path's `gh` seam (`_latest_verified_sha`) and `main()`'s
# `repo_root` seam are monkeypatched, since those touch real subprocesses.
"""

from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING

from forge import pr_plan
from forge.pr_delta import PROVENANCE_GATE_STEPS
from tests.conftest import GIT_ENV, init_git_repo, make_fake_run


if TYPE_CHECKING:
    from pathlib import Path

    import pytest


# --- repo-building helpers ---------------------------------------------


def _init_feature_repo(tmp_path: Path) -> Path:
    """Create a real git repo with an empty ``main`` and a checked-out ``feature``."""
    repo = tmp_path / "repo"
    repo.mkdir()
    init_git_repo(repo)
    subprocess.run(
        ["git", "checkout", "-q", "-b", "feature"], cwd=repo, env=GIT_ENV, check=True
    )
    return repo


def _commit_files(repo: Path, files: dict[str, str | bytes], message: str) -> str:
    """Write *files* to *repo* and commit them, returning the new ``HEAD`` SHA.

    Args:
        repo: Git repo working tree.
        files: Mapping of repo-relative path to content — ``str`` for text
            files, ``bytes`` for a binary file.
        message: Commit message.

    Returns:
        The full (40-char) ``HEAD`` SHA after the commit.
    """
    for relpath, content in files.items():
        path = repo / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
        subprocess.run(["git", "add", relpath], cwd=repo, env=GIT_ENV, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", message], cwd=repo, env=GIT_ENV, check=True
    )
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        env=GIT_ENV,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _repo_with_branch_diff(tmp_path: Path, feature_files: dict[str, str]) -> Path:
    """Build a real repo with a ``main``/``feature`` diff of exactly *feature_files*.

    Shared by the ``classify()`` mode-precedence cases: a ``main`` branch
    with one empty commit, and a ``feature`` branch one commit ahead that
    adds *feature_files* — no remote needed since ``classify()`` diffs two
    local refs directly.

    Args:
        tmp_path: Pytest ``tmp_path`` fixture directory to build the repo under.
        feature_files: Mapping of repo-relative path to text content,
            committed together on ``feature``.

    Returns:
        The repo root, checked out on ``feature``.
    """
    repo = _init_feature_repo(tmp_path)
    _commit_files(repo, feature_files, "feature commit")
    return repo


def _repo_with_modified_file(
    tmp_path: Path, path: str, initial: str, modified: str
) -> Path:
    """Build a repo where *path* is MODIFIED on ``feature``, never added.

    Seeds *path* with *initial* content on ``main`` before branching, so
    the ``main``/``feature`` diff is a pure modify (``--diff-filter=M``)
    with no entry in ``--diff-filter=A`` — the light-code path's
    added-file refusal must not fire on this diff.

    Args:
        tmp_path: Pytest ``tmp_path`` fixture directory to build the repo under.
        path: Repo-relative path to seed on ``main`` and modify on ``feature``.
        initial: Content committed to *path* on ``main``.
        modified: Content committed to *path* on ``feature``.

    Returns:
        The repo root, checked out on ``feature``.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    init_git_repo(repo)
    _commit_files(repo, {path: initial}, "seed on main")
    subprocess.run(
        ["git", "checkout", "-q", "-b", "feature"], cwd=repo, env=GIT_ENV, check=True
    )
    _commit_files(repo, {path: modified}, "modify on feature")
    return repo


def _git_short_sha(repo: Path, ref: str = "HEAD") -> str:
    """Return the short SHA of *ref* via a real ``git rev-parse``.

    Independent verification path for ``classified_at`` assertions —
    deliberately not reusing ``pr_plan``'s own git helper.

    Args:
        repo: Git repo root.
        ref: Commit-ish to resolve. Defaults to ``HEAD``.

    Returns:
        The short SHA string.
    """
    return subprocess.run(
        ["git", "rev-parse", "--short", ref],
        cwd=repo,
        env=GIT_ENV,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


# --- classify(): mode precedence ----------------------------------------


def test_classify_docs_only_diff_returns_light_docs(tmp_path: Path) -> None:
    """A diff of only markdown files takes the light-docs path."""
    repo = _repo_with_branch_diff(tmp_path, {"README.md": "# hi\n"})

    plan = pr_plan.classify(repo, "main", None)

    assert plan.mode == "light-docs"
    assert plan.reporters == list(pr_plan.DOCS_ONLY_REPORTERS)
    assert plan.precommit_scope == list(pr_plan.DOCS_ONLY_PRECOMMIT_STEPS)
    assert plan.classified_at == _git_short_sha(repo)


def test_classify_foundation_only_diff_returns_light_regen(tmp_path: Path) -> None:
    """A diff of only FOUNDATION.md takes the regen-verified light path.

    FOUNDATION.md is high-blast-radius (disqualifying light-docs) but is
    also a managed regen artifact, so it earns eligibility for light-regen
    instead — gated on the provenance steps, not skipped outright.
    """
    repo = _repo_with_branch_diff(tmp_path, {"FOUNDATION.md": "# Foundation\n"})

    plan = pr_plan.classify(repo, "main", None)

    assert plan.mode == "light-regen"
    assert plan.reporters == []
    assert plan.precommit_scope == list(PROVENANCE_GATE_STEPS)
    assert any("ELIGIBILITY ONLY" in r for r in plan.reasons)
    assert plan.classified_at == _git_short_sha(repo)


def test_classify_mixed_diff_returns_full(tmp_path: Path) -> None:
    """A diff mixing docs and source files falls through to the full round."""
    repo = _repo_with_branch_diff(
        tmp_path, {"README.md": "# hi\n", "src/foo.py": "x = 1\n"}
    )

    plan = pr_plan.classify(repo, "main", None)

    assert plan.mode == "full"
    assert plan.reporters == list(pr_plan.FULL_REPORTERS)
    assert plan.precommit_scope == []
    assert any("not docs-only:" in r for r in plan.reasons)
    assert any("not regen-only:" in r for r in plan.reasons)
    assert plan.classified_at == _git_short_sha(repo)


def test_classify_docs_glob_precedence_over_regen_glob(tmp_path: Path) -> None:
    """A file matching both docs and managed-regen globs takes light-docs first.

    ``docs/cli-reference.md`` is both doc-shaped (``*.md``) and a managed
    regen artifact (:data:`forge.pr_delta.MANAGED_REGEN_PATHS`) — pinning
    the docs-first branch order in :func:`pr_plan.classify`.
    """
    repo = _repo_with_branch_diff(tmp_path, {"docs/cli-reference.md": "content\n"})

    plan = pr_plan.classify(repo, "main", None)

    assert plan.mode == "light-docs"


# --- classify(): light-code path ------------------------------------------


def test_classify_small_non_source_modify_returns_light_code(tmp_path: Path) -> None:
    """A small, non-blast, non-source MODIFY takes the light-code path."""
    repo = _repo_with_modified_file(tmp_path, "tests/fixture.py", "x = 1\n", "x = 2\n")

    plan = pr_plan.classify(repo, "main", None)

    assert plan.mode == "light-code"
    assert plan.reporters == []
    assert plan.precommit_scope == []
    assert any(r.startswith("light-code:") for r in plan.reasons)


def test_classify_added_small_file_returns_full(tmp_path: Path) -> None:
    """A small added (non-doc) file disqualifies light-code — full round instead.

    The prior-art gate must run on any added file, however small, so
    `light_wrapup_decision` refuses on `added_paths` before the line
    threshold is even considered.
    """
    repo = _repo_with_branch_diff(tmp_path, {"src/new_module.py": "x = 1\n"})

    plan = pr_plan.classify(repo, "main", None)

    assert plan.mode == "full"
    reason = next(r for r in plan.reasons if r.startswith("not light-code:"))
    assert "src/new_module.py" in reason
    assert "prior-art" in reason


def test_classify_modified_source_path_returns_full(tmp_path: Path) -> None:
    """A small MODIFY under `src/` disqualifies light-code — full round instead."""
    repo = _repo_with_modified_file(
        tmp_path, "src/forge/existing.py", "x = 1\n", "x = 2\n"
    )

    plan = pr_plan.classify(repo, "main", None)

    assert plan.mode == "full"
    reason = next(r for r in plan.reasons if r.startswith("not light-code:"))
    assert "src/forge/existing.py" in reason


# --- classify(): delta path ----------------------------------------------


def test_classify_delta_eligible_small_diff_since_verified_sha(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A small, non-blast diff since the last verified SHA takes delta mode.

    MOCK SETUP: ``pr_plan._latest_verified_sha`` is monkeypatched to a real
    earlier commit in the tmp repo (not a fake `gh` round-trip) — the delta
    decision itself is exercised against real git.
    """
    repo = _init_feature_repo(tmp_path)
    verified_sha = _commit_files(repo, {"src/foo.py": "x = 1\n"}, "verified commit")
    _commit_files(repo, {"src/bar.py": "y = 2\nz = 3\n"}, "small follow-up")
    monkeypatch.setattr(pr_plan, "_latest_verified_sha", lambda _pr: verified_sha)

    plan = pr_plan.classify(repo, "main", 7)

    assert plan.mode == "delta"
    assert plan.reporters == []
    assert plan.precommit_scope == []


def test_classify_delta_ineligible_over_line_threshold_falls_back_to_full(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A follow-up diff over DELTA_LINE_THRESHOLD lines forces the full round."""
    repo = _init_feature_repo(tmp_path)
    verified_sha = _commit_files(repo, {"src/foo.py": "x = 1\n"}, "verified commit")
    big_content = "\n".join(f"line{i}" for i in range(60)) + "\n"
    _commit_files(repo, {"src/bar.py": big_content}, "large follow-up")
    monkeypatch.setattr(pr_plan, "_latest_verified_sha", lambda _pr: verified_sha)

    plan = pr_plan.classify(repo, "main", 7)

    assert plan.mode == "full"
    assert any("full re-check required" in r for r in plan.reasons)


def test_classify_delta_ineligible_blast_radius_path_falls_back_to_full(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A follow-up diff touching a high-blast-radius path forces the full round."""
    repo = _init_feature_repo(tmp_path)
    verified_sha = _commit_files(repo, {"src/foo.py": "x = 1\n"}, "verified commit")
    _commit_files(repo, {"agents/newfile.md": "docs\n"}, "hot-path follow-up")
    monkeypatch.setattr(pr_plan, "_latest_verified_sha", lambda _pr: verified_sha)

    plan = pr_plan.classify(repo, "main", 7)

    assert plan.mode == "full"
    assert any("high-blast-radius" in r for r in plan.reasons)


def test_classify_no_pr_number_marks_delta_ineligible(tmp_path: Path) -> None:
    """Omitting --pr (no existing PR) disqualifies delta with an exact reason."""
    repo = _repo_with_branch_diff(tmp_path, {"src/foo.py": "x = 1\n"})

    plan = pr_plan.classify(repo, "main", None)

    assert plan.mode == "full"
    assert "delta: no --pr given (no existing PR); ineligible" in plan.reasons


def test_classify_delta_no_verified_sha_falls_back_to_full(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No verified-at: SHA found on the PR degrades delta eligibility to full.

    MOCK SETUP: ``pr_plan._latest_verified_sha`` returns ``None``,
    simulating a PR with no reporter wrap-up comment yet (or an
    unauthenticated/missing ``gh``, already covered at the unit level
    below).
    """
    repo = _repo_with_branch_diff(tmp_path, {"src/foo.py": "x = 1\n"})
    monkeypatch.setattr(pr_plan, "_latest_verified_sha", lambda _pr: None)

    plan = pr_plan.classify(repo, "main", 7)

    assert plan.mode == "full"
    assert any("no verified-at: SHA found" in r for r in plan.reasons)


def test_classify_delta_unresolvable_sha_falls_back_to_full(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A verified-at SHA that no longer resolves degrades delta eligibility to full.

    MOCK SETUP: ``pr_plan._latest_verified_sha`` returns a SHA-shaped string
    absent from the tmp repo's object store — simulating a stale comment
    referencing a since-rewritten or since-deleted commit.
    """
    repo = _repo_with_branch_diff(tmp_path, {"src/foo.py": "x = 1\n"})
    monkeypatch.setattr(pr_plan, "_latest_verified_sha", lambda _pr: "deadbeef")

    plan = pr_plan.classify(repo, "main", 7)

    assert plan.mode == "full"
    assert any("does not resolve" in r for r in plan.reasons)


# --- _latest_verified_sha(): happy path -----------------------------------


def test_latest_verified_sha_returns_last_sha_across_multiple_comments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Among several ``verified-at:`` lines, the last one posted wins.

    MOCK SETUP: ``pr_plan.subprocess.run`` is replaced with
    ``make_fake_run`` returning ``gh``-shaped ``stdout`` carrying two
    ``verified-at:`` lines — simulating multiple reporter wrap-up comments
    on the same PR, newest last.
    """
    stdout = (
        "verified-at: aaaa111 first wrap-up\n"
        "some other comment body\n"
        "verified-at: bbbb222 second wrap-up\n"
    )
    monkeypatch.setattr(pr_plan.subprocess, "run", make_fake_run(stdout=stdout))

    assert pr_plan._latest_verified_sha(42) == "bbbb222"


# --- _latest_verified_sha(): gh failure modes ----------------------------


def test_latest_verified_sha_returns_none_on_called_process_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing `gh pr view` (non-zero exit) degrades to None, not a raise.

    MOCK SETUP: ``pr_plan.subprocess.run`` is replaced with a stub that
    raises ``subprocess.CalledProcessError``, simulating an
    unauthenticated or unknown-PR ``gh`` invocation.
    """

    def _raise(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.CalledProcessError(1, ["gh"])

    monkeypatch.setattr(pr_plan.subprocess, "run", _raise)

    assert pr_plan._latest_verified_sha(42) is None


def test_latest_verified_sha_returns_none_when_gh_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing `gh` binary (FileNotFoundError) also degrades to None.

    MOCK SETUP: ``pr_plan.subprocess.run`` raises ``FileNotFoundError``,
    simulating ``gh`` not being installed on PATH.
    """

    def _raise(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        msg = "gh not found"
        raise FileNotFoundError(msg)

    monkeypatch.setattr(pr_plan.subprocess, "run", _raise)

    assert pr_plan._latest_verified_sha(42) is None


# --- _line_count() --------------------------------------------------------


def test_line_count_ignores_binary_file_rows(tmp_path: Path) -> None:
    """Binary numstat rows ("-" counts) are skipped; only text lines count."""
    repo = tmp_path / "repo"
    repo.mkdir()
    init_git_repo(repo)
    before = _git_short_sha(repo, "HEAD")
    _commit_files(
        repo,
        {"notes.txt": "one\ntwo\nthree\n", "blob.bin": b"\x00\x01\x02binary"},
        "add text and binary files",
    )

    count = pr_plan._line_count(repo, f"{before}..HEAD")

    assert count == 3


# --- main() ----------------------------------------------------------------


def test_main_rejects_dash_prefixed_base(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A `--base` value shaped like a flag is rejected before any git call.

    Pure unit test — no repo needed; ``classify()`` never runs, so nothing
    reaches stdout.
    """
    rc = pr_plan.main(["--base=-evil"])

    assert rc == 2
    assert capsys.readouterr().out == ""


def test_main_rejects_nonexistent_base_ref(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A `--base` ref git cannot resolve exits 2 with no stdout, not a traceback.

    Locks the security-review fix: `classify()`'s `git diff` raises
    `subprocess.CalledProcessError` on an unresolvable ref; `main()` must
    catch it and return the documented exit code instead of leaking a
    traceback.
    """
    repo = _init_feature_repo(tmp_path)
    _commit_files(repo, {"src/foo.py": "x = 1\n"}, "initial commit")
    monkeypatch.setattr(pr_plan, "repo_root", lambda: repo)

    rc = pr_plan.main(["--base", "no-such-ref-xyz"])

    assert rc == 2
    assert capsys.readouterr().out == ""


def test_main_base_with_space_stays_one_argv_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A space-containing --base never splits into option-injecting argv tokens.

    SCENARIO: a base like ``main --output=<path>`` would, if shell-interpreted,
    let an attacker inject a second git flag. Because argv is always a list
    (never passed through a shell), the whole string stays ONE token inside
    the diff range and simply fails as an invalid revision.
    EXPECTED BEHAVIOR: exit 2 and no file created at the injected path.
    """
    repo = _init_feature_repo(tmp_path)
    _commit_files(repo, {"src/foo.py": "x = 1\n"}, "initial commit")
    monkeypatch.setattr(pr_plan, "repo_root", lambda: repo)
    pwned = tmp_path / "pwned.txt"

    rc = pr_plan.main(["--base", f"main --output={pwned}"])

    assert rc == 2
    assert not pwned.exists()


def test_main_happy_path_emits_full_plan_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`main()` on a mixed-diff feature branch emits the full-mode plan as JSON.

    MOCK SETUP: ``pr_plan.repo_root`` — an ``lru_cache``'d function bound
    into this module's namespace at import time — is monkeypatched
    directly on ``pr_plan``, not on ``forge.git_utils``: patching the
    origin module would not reach the name ``pr_plan.main`` already looked
    up.
    """
    repo = _repo_with_branch_diff(tmp_path, {"src/foo.py": "x = 1\n"})
    monkeypatch.setattr(pr_plan, "repo_root", lambda: repo)

    rc = pr_plan.main(["--base", "main"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "full"
    assert payload["reporters"] == list(pr_plan.FULL_REPORTERS)
    assert payload["precommit_scope"] == []
    assert any("not docs-only:" in r for r in payload["reasons"])
    assert payload["classified_at"] == _git_short_sha(repo)


# --- classified_at regression ---------------------------------------------


def test_classify_classified_at_changes_across_new_commit(tmp_path: Path) -> None:
    """classified_at tracks HEAD — a follow-up commit changes the stamped SHA."""
    repo = _repo_with_branch_diff(tmp_path, {"README.md": "# hi\n"})
    plan1 = pr_plan.classify(repo, "main", None)

    _commit_files(repo, {"README.md": "# hi again\n"}, "second commit")
    plan2 = pr_plan.classify(repo, "main", None)

    assert plan1.classified_at != plan2.classified_at
    assert plan1.classified_at == _git_short_sha(repo, "HEAD~1")
    assert plan2.classified_at == _git_short_sha(repo)
