"""Tests for ``forge.smart_test.git_helpers`` — base resolution and change detection."""

# MOCKING STRATEGY: ``resolve_base_ref`` tests monkeypatch
# ``git_helpers.load_config`` (consuming namespace) to inject a ForgeConfig
# without a real ``pyproject.toml``.  Real git repos are created via
# ``init_git_repo`` / ``init_single_track_repo`` from conftest.
# ``changed_python_files`` tests use real git operations (commit, stage,
# unstaged edits, untracked files) so the git-plumbing layer is exercised
# end-to-end.  No subprocess mocking; the real git binary runs.
# Monkeypatch targets are always the consuming module namespace
# (``forge.smart_test.git_helpers.*``).

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

from forge.config import ForgeConfig
from forge.smart_test import git_helpers
from tests.conftest import GIT_ENV as _GIT_ENV
from tests.conftest import init_git_repo, init_single_track_repo


if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_resolve_base_ref_override_resolves(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An override ref that resolves is returned verbatim, skipping auto-detection.

    SCENARIO: caller supplies ``--base HEAD``.
    MOCK SETUP: git_helpers.load_config → ForgeConfig(base_branch="main");
        real git repo with a HEAD commit.
    EXPECTED BEHAVIOR: returns ``"HEAD"`` (override wins over candidates).
    """
    init_git_repo(tmp_path)
    monkeypatch.setattr(
        git_helpers,
        "load_config",
        lambda _root: ForgeConfig(base_branch="main"),
    )
    result = git_helpers.resolve_base_ref(tmp_path, override="HEAD")
    assert result == "HEAD"


def test_resolve_base_ref_override_missing_falls_through(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A supplied override that does not resolve is ignored; auto-detection proceeds.

    SCENARIO: caller supplies ``--base nonexistent-branch``; no remote.
    MOCK SETUP: git_helpers.load_config → ForgeConfig(base_branch="main");
        repo has only ``main``.
    EXPECTED BEHAVIOR: falls through to ``"main"`` (the ``base_branch``
        local candidate).
    """
    init_git_repo(tmp_path)
    monkeypatch.setattr(
        git_helpers,
        "load_config",
        lambda _root: ForgeConfig(base_branch="main"),
    )
    result = git_helpers.resolve_base_ref(tmp_path, override="nonexistent-branch-xyz")
    assert result == "main"


def test_resolve_base_ref_flag_shaped_override_falls_through(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `-`-prefixed override (option injection) is ignored, not passed to git.

    SCENARIO: caller supplies ``--base --output=pwned``; no remote.
    MOCK SETUP: git_helpers.load_config → ForgeConfig(base_branch="main");
        repo has only ``main``.
        git_helpers._ref_exists replaced by a spy recording every ref it
        is asked about, so the flag-shaped override never reaching git is
        directly observable rather than just inferred from the result.
    EXPECTED BEHAVIOR: falls through to ``"main"`` (the ``base_branch``
        local candidate) and the spy is never called with the flag value.
    """
    init_git_repo(tmp_path)
    monkeypatch.setattr(
        git_helpers,
        "load_config",
        lambda _root: ForgeConfig(base_branch="main"),
    )
    calls: list[str] = []

    def _spy(_root: Path, ref: str) -> bool:
        calls.append(ref)
        return True

    monkeypatch.setattr(git_helpers, "_ref_exists", _spy)
    result = git_helpers.resolve_base_ref(tmp_path, override="--output=pwned")
    assert result == "main"
    assert "--output=pwned" not in calls


def test_resolve_base_ref_prefers_origin_base_over_local(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``origin/<base_branch>`` is preferred over the local base branch.

    SCENARIO: repo with a remote where both ``origin/main`` and local
        ``main`` exist.
    MOCK SETUP: git_helpers.load_config → ForgeConfig(base_branch="main");
        ``init_single_track_repo`` pushes ``main`` to a bare origin.
    EXPECTED BEHAVIOR: returns ``"origin/main"``.
    """
    work, _bare = init_single_track_repo(tmp_path)
    # Fetch so origin/main is available in the work repo.
    subprocess.run(["git", "fetch", "origin"], cwd=work, env=_GIT_ENV, check=True)
    monkeypatch.setattr(
        git_helpers,
        "load_config",
        lambda _root: ForgeConfig(base_branch="main"),
    )
    result = git_helpers.resolve_base_ref(work)
    assert result == "origin/main"


def test_resolve_base_ref_falls_to_local_base_when_no_remote(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Falls back to the local base branch when ``origin/main`` does not exist.

    SCENARIO: repo without a remote; local ``main`` exists (checked out on a
        feature branch so the resolved ref is not merely HEAD's branch).
    MOCK SETUP: git_helpers.load_config → ForgeConfig(base_branch="main").
    EXPECTED BEHAVIOR: returns ``"main"`` (the local base candidate).
    """
    init_git_repo(tmp_path)
    subprocess.run(
        ["git", "checkout", "-q", "-b", "feature"],
        cwd=tmp_path,
        env=_GIT_ENV,
        check=True,
    )
    monkeypatch.setattr(
        git_helpers,
        "load_config",
        lambda _root: ForgeConfig(base_branch="main"),
    )
    result = git_helpers.resolve_base_ref(tmp_path)
    assert result == "main"


def test_resolve_base_ref_falls_to_head_when_nothing_resolves(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Returns ``"HEAD"`` as last resort when no candidate resolves.

    SCENARIO: fresh repo, no remote; inject a config whose base branch
        doesn't exist.
    MOCK SETUP: git_helpers.load_config →
        ForgeConfig(base_branch="also-nonexistent"); repo has only ``main``.
    EXPECTED BEHAVIOR: returns ``"HEAD"``.
    """
    init_git_repo(tmp_path)
    monkeypatch.setattr(
        git_helpers,
        "load_config",
        lambda _root: ForgeConfig(base_branch="also-nonexistent"),
    )
    result = git_helpers.resolve_base_ref(tmp_path)
    assert result == "HEAD"


def test_resolve_base_ref_routes_through_resolve_base_branch_ref(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`resolve_base_ref` routes through `resolve_base_branch_ref`.

    SCENARIO: the candidate is not checked against the filesystem — the
        resolution is stubbed, so no real repo state is needed.
    MOCK SETUP: git_helpers.load_config → ForgeConfig(base_branch="main");
        git_helpers.resolve_base_branch_ref replaced by a spy.
    EXPECTED BEHAVIOR: the configured base branch is the only candidate
        tried and the spy's return value is returned verbatim.
    """
    monkeypatch.setattr(
        git_helpers,
        "load_config",
        lambda _root: ForgeConfig(base_branch="main"),
    )
    calls: list[str] = []

    def _spy(_root: object, branch: str) -> str | None:
        calls.append(branch)
        return "sentinel/main" if branch == "main" else None

    monkeypatch.setattr(git_helpers, "resolve_base_branch_ref", _spy)
    result = git_helpers.resolve_base_ref(tmp_path)
    assert calls == ["main"]
    assert result == "sentinel/main"


def test_changed_python_files_committed_file_included(tmp_path: Path) -> None:
    """A .py file committed after the base ref is included in the result."""
    init_git_repo(tmp_path)
    (tmp_path / "foo.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "foo.py"], cwd=tmp_path, env=_GIT_ENV, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "add foo"],
        cwd=tmp_path,
        env=_GIT_ENV,
        check=True,
    )
    result = git_helpers.changed_python_files(tmp_path, "HEAD~1")
    assert "foo.py" in result


def test_changed_python_files_staged_file_included(tmp_path: Path) -> None:
    """A .py file in the index (staged but not committed) is included."""
    init_git_repo(tmp_path)
    (tmp_path / "staged.py").write_text("x = 2\n")
    subprocess.run(["git", "add", "staged.py"], cwd=tmp_path, env=_GIT_ENV, check=True)
    result = git_helpers.changed_python_files(tmp_path, "HEAD")
    assert "staged.py" in result


def test_changed_python_files_unstaged_file_included(tmp_path: Path) -> None:
    """A tracked .py file with unstaged edits is included."""
    init_git_repo(tmp_path)
    (tmp_path / "tracked.py").write_text("x = 0\n")
    subprocess.run(["git", "add", "tracked.py"], cwd=tmp_path, env=_GIT_ENV, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "add tracked"],
        cwd=tmp_path,
        env=_GIT_ENV,
        check=True,
    )
    # Modify without staging.
    (tmp_path / "tracked.py").write_text("x = 99\n")
    result = git_helpers.changed_python_files(tmp_path, "HEAD")
    assert "tracked.py" in result


def test_changed_python_files_untracked_file_included(tmp_path: Path) -> None:
    """An untracked .py file (never added to git) is included."""
    init_git_repo(tmp_path)
    (tmp_path / "brand_new.py").write_text("# new\n")
    result = git_helpers.changed_python_files(tmp_path, "HEAD")
    assert "brand_new.py" in result


def test_changed_python_files_non_py_filtered_out(tmp_path: Path) -> None:
    """Non-.py files (e.g. .txt, .toml) are excluded from the result."""
    init_git_repo(tmp_path)
    (tmp_path / "README.txt").write_text("hello\n")
    (tmp_path / "pyproject.toml").write_text("[tool]\n")
    subprocess.run(
        ["git", "add", "README.txt", "pyproject.toml"],
        cwd=tmp_path,
        env=_GIT_ENV,
        check=True,
    )
    result = git_helpers.changed_python_files(tmp_path, "HEAD")
    assert not any(f.endswith((".txt", ".toml")) for f in result)


def test_changed_python_files_merge_base_semantics(tmp_path: Path) -> None:
    """Merge-base semantics exclude base-branch-only commits from the diff.

    SCENARIO: commit A on main, then commit B (a .py file) on a feature branch.
        The base ref is ``main``; only B should appear, not any file from A.
    """
    init_git_repo(tmp_path)
    # Commit a file on main.
    (tmp_path / "base_only.py").write_text("base = 1\n")
    subprocess.run(
        ["git", "add", "base_only.py"], cwd=tmp_path, env=_GIT_ENV, check=True
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "base commit"],
        cwd=tmp_path,
        env=_GIT_ENV,
        check=True,
    )
    # Branch and commit a feature file.
    subprocess.run(
        ["git", "checkout", "-q", "-b", "feature"],
        cwd=tmp_path,
        env=_GIT_ENV,
        check=True,
    )
    (tmp_path / "feature.py").write_text("feature = 1\n")
    subprocess.run(["git", "add", "feature.py"], cwd=tmp_path, env=_GIT_ENV, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "feature commit"],
        cwd=tmp_path,
        env=_GIT_ENV,
        check=True,
    )
    result = git_helpers.changed_python_files(tmp_path, "main")
    assert "feature.py" in result
    assert "base_only.py" not in result


def test_changed_python_files_empty_result_when_nothing_changed(
    tmp_path: Path,
) -> None:
    """Returns an empty set when HEAD equals the base ref and the tree is clean."""
    init_git_repo(tmp_path)
    result = git_helpers.changed_python_files(tmp_path, "HEAD")
    assert result == set()


def test_changed_python_files_merge_base_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Falls back to ``base_ref`` directly when ``git merge-base`` returns empty.

    SCENARIO: monkeypatch ``run_git`` so merge-base returns ``""``; the diff
        then uses the base_ref itself and still surfaces changed files.
    MOCK SETUP: git_helpers.run_git is wrapped so calls with ``"merge-base"``
        as the first arg return ``""``; all other calls delegate to the real
        implementation so ls-files / diff still work against the real git repo.
    EXPECTED BEHAVIOR: at least one .py file appears in the result (the
        untracked one), demonstrating the fallback path runs without error.
    """
    init_git_repo(tmp_path)
    (tmp_path / "fallback.py").write_text("x = 1\n")
    original_run_git = git_helpers.run_git

    def _patched_run_git(*args: object, **kwargs: object) -> str:
        if args and args[0] == "merge-base":
            return ""
        return original_run_git(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(git_helpers, "run_git", _patched_run_git)
    result = git_helpers.changed_python_files(tmp_path, "HEAD")
    assert "fallback.py" in result


# ---------------------------------------------------------------------------
# head_commit_message — real git
# ---------------------------------------------------------------------------


def test_head_commit_message_returns_subject(tmp_path: Path) -> None:
    """``head_commit_message`` returns the subject of HEAD's commit message."""
    init_git_repo(tmp_path)
    # init_git_repo commits with message "initial"
    msg = git_helpers.head_commit_message(tmp_path)
    assert "initial" in msg


def test_head_commit_message_returns_subject_and_body(tmp_path: Path) -> None:
    """``head_commit_message`` returns both the subject and body of the commit."""
    init_git_repo(tmp_path)
    (tmp_path / "f.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "f.py"], cwd=tmp_path, env=_GIT_ENV, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "subject line\n\nbody text here"],
        cwd=tmp_path,
        env=_GIT_ENV,
        check=True,
    )
    msg = git_helpers.head_commit_message(tmp_path)
    assert "subject line" in msg
    assert "body text here" in msg


def test_head_commit_message_empty_on_no_commits(tmp_path: Path) -> None:
    """``head_commit_message`` returns an empty string when there are no commits.

    ``git log -1`` on an empty repo exits non-zero; ``run_git`` with
    ``check=False`` captures that as an empty string.
    """
    # Init without any commits (skip init_git_repo which always commits)
    subprocess.run(
        ["git", "init", "-q", "-b", "main"], cwd=tmp_path, env=_GIT_ENV, check=True
    )
    msg = git_helpers.head_commit_message(tmp_path)
    assert msg == ""


# ---------------------------------------------------------------------------
# changed_non_python_files — real git (four-source coverage already proven by
# the changed_python_files suite above; these cases exercise the non-.py
# filter and ignore_globs specifically)
# ---------------------------------------------------------------------------


def test_changed_non_python_files_untracked_included(tmp_path: Path) -> None:
    """An untracked non-``.py`` file (e.g. notes) is included by default."""
    init_git_repo(tmp_path)
    (tmp_path / "notes.txt").write_text("todo\n", encoding="utf-8")
    result = git_helpers.changed_non_python_files(tmp_path, "HEAD")
    assert "notes.txt" in result


def test_changed_non_python_files_py_file_excluded(tmp_path: Path) -> None:
    """A changed ``.py`` file never appears in the non-Python result."""
    init_git_repo(tmp_path)
    (tmp_path / "notes.txt").write_text("todo\n", encoding="utf-8")
    (tmp_path / "code.py").write_text("x = 1\n", encoding="utf-8")
    result = git_helpers.changed_non_python_files(tmp_path, "HEAD")
    assert "code.py" not in result
    assert "notes.txt" in result


def test_changed_non_python_files_ignore_globs_excludes_matching(
    tmp_path: Path,
) -> None:
    """A path matching ``ignore_globs`` (e.g. ``*.md``) is excluded."""
    init_git_repo(tmp_path)
    (tmp_path / "README.md").write_text("# hi\n", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("todo\n", encoding="utf-8")
    result = git_helpers.changed_non_python_files(
        tmp_path, "HEAD", ignore_globs=("*.md",)
    )
    assert "README.md" not in result
    assert "notes.txt" in result


def test_changed_non_python_files_empty_ignore_globs_only_filters_py(
    tmp_path: Path,
) -> None:
    """With no ``ignore_globs``, every changed non-``.py`` file is returned."""
    init_git_repo(tmp_path)
    (tmp_path / "README.md").write_text("# hi\n", encoding="utf-8")
    (tmp_path / "code.py").write_text("x = 1\n", encoding="utf-8")
    result = git_helpers.changed_non_python_files(tmp_path, "HEAD")
    assert result == {"README.md"}
