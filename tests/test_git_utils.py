"""Tests for ``forge.git_utils``.

Covers the shared helpers used by every forge CLI: repo root resolution,
modified-file detection, output filtering, and CLI logging setup.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from typing import TYPE_CHECKING

import pytest

from forge import git_utils
from tests.conftest import make_fake_run


if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def _clear_repo_root_cache() -> None:
    """Reset the ``repo_root`` LRU cache between tests."""
    git_utils.repo_root.cache_clear()


# ---------------------------------------------------------------------------
# _parse_files
# ---------------------------------------------------------------------------


def test_parse_files_empty_output_returns_empty() -> None:
    """Empty input produces an empty list."""
    assert git_utils._parse_files("", suffix=".py", prefix=None) == []


def test_parse_files_filters_by_suffix() -> None:
    """Only files ending with the configured suffix survive."""
    output = "a.py\nb.txt\nc.py\n"
    assert git_utils._parse_files(output, suffix=".py", prefix=None) == ["a.py", "c.py"]


def test_parse_files_filters_by_single_prefix() -> None:
    """A string prefix keeps only matching paths."""
    output = "tests/foo.py\nsrc/bar.py\n"
    result = git_utils._parse_files(output, suffix=".py", prefix="tests/")
    assert result == ["tests/foo.py"]


def test_parse_files_filters_by_tuple_of_prefixes() -> None:
    """A tuple of prefixes accepts any matching layout (test/ OR tests/)."""
    output = "test/a.py\ntests/b.py\nsrc/c.py\n"
    result = git_utils._parse_files(output, suffix=".py", prefix=("test/", "tests/"))
    assert result == ["test/a.py", "tests/b.py"]


def test_parse_files_strips_whitespace_and_blank_lines() -> None:
    """Surrounding whitespace and blank lines are dropped."""
    output = "  a.py  \n\n  b.py\n"
    assert git_utils._parse_files(output, suffix=".py", prefix=None) == ["a.py", "b.py"]


# ---------------------------------------------------------------------------
# parse_semver
# ---------------------------------------------------------------------------


def test_parse_semver_bare_triple() -> None:
    """Plain ``X.Y.Z`` returns the integer tuple."""
    assert git_utils.parse_semver("1.2.3") == (1, 2, 3)


def test_parse_semver_v_prefix() -> None:
    """``v``-prefixed git tag is accepted."""
    assert git_utils.parse_semver("v1.2.3") == (1, 2, 3)


def test_parse_semver_tolerates_dev_suffix() -> None:
    """setuptools-scm editable suffixes do not break the leading-triple parse."""
    assert git_utils.parse_semver("1.2.11.dev3+g7e0cdd95b") == (1, 2, 11)


def test_parse_semver_tolerates_prerelease_and_build() -> None:
    """Semver `-rc1` / `+build` suffixes are stripped."""
    assert git_utils.parse_semver("v1.2.3-rc1") == (1, 2, 3)
    assert git_utils.parse_semver("1.2.3+build.42") == (1, 2, 3)


def test_parse_semver_rejects_non_triple() -> None:
    """Strings without a complete ``X.Y.Z`` prefix return ``None``."""
    assert git_utils.parse_semver("1.2") is None
    assert git_utils.parse_semver("v1.x.3") is None
    assert git_utils.parse_semver("") is None
    assert git_utils.parse_semver("not-a-version") is None


# ---------------------------------------------------------------------------
# Source-dir resolution moved to forge.config (smart-detect + resolver);
# see test_config.py. git_utils no longer owns a source-dir helper.


# ---------------------------------------------------------------------------
# repo_root
# ---------------------------------------------------------------------------


def test_repo_root_returns_toplevel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When git succeeds, repo_root returns the trimmed stdout as a Path."""
    fake_top = tmp_path / "repo"
    fake_top.mkdir()

    def _fake_run(cmd: list[str], **_kwargs: object) -> object:
        assert cmd[:3] == ["git", "rev-parse", "--show-toplevel"]
        return type("P", (), {"returncode": 0, "stdout": f"{fake_top}\n"})()

    monkeypatch.setattr(git_utils.subprocess, "run", _fake_run)
    assert git_utils.repo_root() == fake_top


def test_repo_root_exits_when_not_in_git_repo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Git failure (non-zero exit) raises SystemExit(1)."""
    monkeypatch.setattr(
        git_utils.subprocess,
        "run",
        lambda *_a, **_kw: type("P", (), {"returncode": 128, "stdout": ""})(),
    )
    with pytest.raises(SystemExit) as exc_info:
        git_utils.repo_root()
    assert exc_info.value.code == 1


def test_repo_root_is_cached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Repeated calls hit subprocess only once (lru_cache)."""
    calls = {"count": 0}

    def _fake_run(*_args: object, **_kwargs: object) -> object:
        calls["count"] += 1
        return type("P", (), {"returncode": 0, "stdout": f"{tmp_path}\n"})()

    monkeypatch.setattr(git_utils.subprocess, "run", _fake_run)
    git_utils.repo_root()
    git_utils.repo_root()
    git_utils.repo_root()
    assert calls["count"] == 1


# ---------------------------------------------------------------------------
# _run_git
# ---------------------------------------------------------------------------


def test_run_git_returns_stdout_on_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Success returns trimmed stdout."""

    def _fake_run(cmd: list[str], **_kwargs: object) -> object:
        if cmd[:3] == ["git", "rev-parse", "--show-toplevel"]:
            return type("P", (), {"returncode": 0, "stdout": f"{tmp_path}\n"})()
        return type("P", (), {"returncode": 0, "stdout": "main\n"})()

    monkeypatch.setattr(git_utils.subprocess, "run", _fake_run)
    assert git_utils._run_git("branch", "--show-current") == "main"


def test_run_git_returns_empty_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-zero exit produces an empty string (not raise)."""

    def _fake_run(cmd: list[str], **_kwargs: object) -> object:
        if cmd[:3] == ["git", "rev-parse", "--show-toplevel"]:
            return type("P", (), {"returncode": 0, "stdout": f"{tmp_path}\n"})()
        return type("P", (), {"returncode": 128, "stdout": "x\n", "stderr": "boom"})()

    monkeypatch.setattr(git_utils.subprocess, "run", _fake_run)
    assert git_utils._run_git("nope") == ""


# ---------------------------------------------------------------------------
# get_modified_files
# ---------------------------------------------------------------------------


def _stub_branch_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    current_branch: str,
    diff_outputs: dict[str, str],
) -> None:
    r"""Stub subprocess so get_modified_files exercises the feature-branch path.

    Args:
        monkeypatch: pytest fixture.
        tmp_path: Synthetic repo root.
        current_branch: Value returned by ``git branch --show-current``.
        diff_outputs: Maps the trailing arg of `git diff --name-only` to
            stdout (e.g., ``{"main...HEAD": "src/foo.py\\n"}``).
    """

    def _fake_run(cmd: list[str], **_kwargs: object) -> object:
        if cmd[:3] == ["git", "rev-parse", "--show-toplevel"]:
            return type("P", (), {"returncode": 0, "stdout": f"{tmp_path}\n"})()
        if cmd[1:3] == ["branch", "--show-current"]:
            return type("P", (), {"returncode": 0, "stdout": f"{current_branch}\n"})()
        if cmd[1:3] == ["rev-parse", "--verify"]:
            return type("P", (), {"returncode": 0, "stdout": "ok\n"})()
        if cmd[1:3] == ["diff", "--name-only"]:
            tail = cmd[-1] if len(cmd) > 3 else ""
            stdout = diff_outputs.get(tail, "")
            return type("P", (), {"returncode": 0, "stdout": stdout})()
        return type("P", (), {"returncode": 0, "stdout": ""})()

    monkeypatch.setattr(git_utils.subprocess, "run", _fake_run)


def test_get_modified_files_feature_branch_aggregates_three_diffs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On a feature branch, branch-commits + staged + unstaged are merged."""
    _stub_branch_path(
        monkeypatch,
        tmp_path,
        current_branch="feat/x",
        diff_outputs={
            "main...HEAD": "src/a.py\n",
            "--cached": "src/b.py\n",
            # plain `git diff --name-only` (no trailing arg) → key is ""
            "": "src/c.py\nsrc/a.py\n",
        },
    )
    files = git_utils.get_modified_files()
    assert files == ["src/a.py", "src/b.py", "src/c.py"]


def test_get_modified_files_applies_prefix_tuple(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`prefix=("test/", "tests/")` accepts either layout in the diff output."""
    _stub_branch_path(
        monkeypatch,
        tmp_path,
        current_branch="feat/x",
        diff_outputs={
            "main...HEAD": "test/old.py\ntests/new.py\nsrc/foo.py\n",
            "--cached": "",
            "": "",
        },
    )
    files = git_utils.get_modified_files(prefix=("test/", "tests/"))
    assert files == ["test/old.py", "tests/new.py"]


def test_get_modified_files_main_falls_back_to_head_prev(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On main, the previous-commit diff is used."""

    def _fake_run(cmd: list[str], **_kwargs: object) -> object:
        if cmd[:3] == ["git", "rev-parse", "--show-toplevel"]:
            return type("P", (), {"returncode": 0, "stdout": f"{tmp_path}\n"})()
        if cmd[1:3] == ["branch", "--show-current"]:
            return type("P", (), {"returncode": 0, "stdout": "main\n"})()
        if cmd[1:3] == ["diff", "--name-only"] and cmd[-1] == "HEAD~1":
            return type("P", (), {"returncode": 0, "stdout": "src/x.py\n"})()
        return type("P", (), {"returncode": 0, "stdout": ""})()

    monkeypatch.setattr(git_utils.subprocess, "run", _fake_run)
    assert git_utils.get_modified_files() == ["src/x.py"]


def test_get_tracked_files_filters_suffix_and_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """get_tracked_files lists `git ls-files` filtered by suffix/prefix."""

    def _fake_run(cmd: list[str], **_kwargs: object) -> object:
        if cmd[:3] == ["git", "rev-parse", "--show-toplevel"]:
            return type("P", (), {"returncode": 0, "stdout": f"{tmp_path}\n"})()
        if cmd[1:2] == ["ls-files"]:
            stdout = "src/a.py\ntests/b.py\nREADME.md\nsrc/c.txt\n"
            return type("P", (), {"returncode": 0, "stdout": stdout})()
        return type("P", (), {"returncode": 0, "stdout": ""})()

    monkeypatch.setattr(git_utils.subprocess, "run", _fake_run)
    assert git_utils.get_tracked_files() == ["src/a.py", "tests/b.py"]
    assert git_utils.get_tracked_files(prefix=("tests/",)) == ["tests/b.py"]


# ---------------------------------------------------------------------------
# emit + configure_cli_logging
# ---------------------------------------------------------------------------


def test_emit_writes_line_to_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    """Emit writes its arg plus a trailing newline to stdout."""
    git_utils.emit("hello world")
    assert capsys.readouterr().out == "hello world\n"


def test_configure_cli_logging_sets_info_level() -> None:
    """configure_cli_logging configures the root logger at INFO.

    basicConfig is a no-op when handlers already exist, so the test
    detaches existing handlers first and restores them after.
    """
    root = logging.getLogger()
    prior_level = root.level
    prior_handlers = root.handlers[:]
    root.handlers = []
    try:
        git_utils.configure_cli_logging()
        assert root.level == logging.INFO
    finally:
        root.setLevel(prior_level)
        root.handlers = prior_handlers


def test_configure_cli_logging_is_idempotent() -> None:
    """Calling configure_cli_logging twice is safe (handlers already attached)."""
    git_utils.configure_cli_logging()
    git_utils.configure_cli_logging()  # second call must not raise


def test_latest_v_tag_returns_highest_sorted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first line of the ``--sort=-v:refname`` output (highest) is returned."""
    monkeypatch.setattr(
        git_utils.subprocess,
        "run",
        make_fake_run(stdout="v1.21.0\nv1.20.2\nv1.20.0\n"),
    )
    assert git_utils.latest_v_tag(tmp_path) == "v1.21.0"


def test_latest_v_tag_none_when_no_tags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No ``v*`` tags → ``None``."""
    monkeypatch.setattr(git_utils.subprocess, "run", make_fake_run(stdout=""))
    assert git_utils.latest_v_tag(tmp_path) is None


# ---------------------------------------------------------------------------
# read_local_plugin_version (moved from test_next_prep.py)
# ---------------------------------------------------------------------------


def test_read_plugin_version_returns_semver_string(tmp_path: Path) -> None:
    """Valid plugin.json with a semver version returns the string."""
    (tmp_path / ".claude-plugin").mkdir()
    (tmp_path / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "x", "version": "1.2.10"})
    )
    assert git_utils.read_local_plugin_version(tmp_path) == "1.2.10"


def test_read_plugin_version_returns_none_when_file_missing(tmp_path: Path) -> None:
    """No plugin.json → None."""
    assert git_utils.read_local_plugin_version(tmp_path) is None


def test_read_plugin_version_returns_none_on_non_semver(tmp_path: Path) -> None:
    """Non-semver version field → None (defence against tag injection)."""
    (tmp_path / ".claude-plugin").mkdir()
    (tmp_path / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "x", "version": "1.2"})
    )
    assert git_utils.read_local_plugin_version(tmp_path) is None


def test_read_plugin_version_returns_none_on_malformed_json(tmp_path: Path) -> None:
    """Malformed JSON → None (not raise)."""
    (tmp_path / ".claude-plugin").mkdir()
    (tmp_path / ".claude-plugin" / "plugin.json").write_text("{not valid")
    assert git_utils.read_local_plugin_version(tmp_path) is None


# ---------------------------------------------------------------------------
# Real-git helpers for run_git / get_tree_sha / read_plugin_version_at_ref
# ---------------------------------------------------------------------------

_GIT_ENV: dict[str, str] = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
    "PATH": os.environ.get("PATH", ""),
}


def _init_git_repo(repo: Path) -> None:
    """Initialize a minimal git repo with one empty commit on ``main``.

    Args:
        repo: Directory to initialize. Must already exist.
    """
    for cmd in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "commit", "-q", "--allow-empty", "-m", "initial"],
    ):
        subprocess.run(cmd, cwd=repo, env=_GIT_ENV, check=True)


# ---------------------------------------------------------------------------
# run_git
# ---------------------------------------------------------------------------


def test_run_git_check_true_success_returns_trimmed_stdout(tmp_path: Path) -> None:
    """check=True on a valid command returns the trimmed 40-char commit SHA."""
    _init_git_repo(tmp_path)
    sha = git_utils.run_git("rev-parse", "HEAD", cwd=tmp_path)
    assert len(sha) == 40
    assert all(c in "0123456789abcdef" for c in sha)


def test_run_git_check_true_failure_raises(tmp_path: Path) -> None:
    """check=True and a failing git command raises CalledProcessError.

    ``--verify`` is used to prevent git's passthrough mode (which echoes
    bare unknown names to stdout with exit 0 instead of failing).
    """
    _init_git_repo(tmp_path)
    with pytest.raises(subprocess.CalledProcessError):
        git_utils.run_git(
            "rev-parse", "--verify", "nonexistent_branch_xyz", cwd=tmp_path
        )


def test_run_git_check_false_failure_returns_empty(tmp_path: Path) -> None:
    """check=False and a failing command returns '' without raising.

    ``--verify`` is used to prevent git's passthrough mode (which echoes
    bare unknown names to stdout with exit 0 instead of failing).
    """
    _init_git_repo(tmp_path)
    result = git_utils.run_git(
        "rev-parse", "--verify", "nonexistent_branch_xyz", cwd=tmp_path, check=False
    )
    assert result == ""


# ---------------------------------------------------------------------------
# get_tree_sha
# ---------------------------------------------------------------------------


def test_get_tree_sha_valid_ref_returns_40_hex(tmp_path: Path) -> None:
    """A valid ref resolves to a 40-character hex tree SHA."""
    _init_git_repo(tmp_path)
    tree_sha = git_utils.get_tree_sha(tmp_path, "HEAD")
    assert tree_sha is not None
    assert len(tree_sha) == 40
    assert all(c in "0123456789abcdef" for c in tree_sha)


def test_get_tree_sha_unresolvable_ref_not_a_valid_tree_sha(tmp_path: Path) -> None:
    """An unresolvable ref never produces a 40-char hex tree SHA.

    Some git builds use passthrough mode: a failed ``rev-parse`` still
    echoes the argument to stdout (rc=128, non-empty stdout). The
    function does not check the return code, so it may return the
    passthrough string rather than ``None``.  Either way — ``None`` or a
    passthrough string containing ``^{tree}`` — the result is not a valid
    40-char hex SHA, so ``index.get(result)`` in callers correctly
    returns ``None`` for all unresolvable refs.
    """
    _init_git_repo(tmp_path)
    result = git_utils.get_tree_sha(tmp_path, "HEAD~999999")
    is_valid_tree_sha = (
        result is not None
        and len(result) == 40
        and all(c in "0123456789abcdef" for c in result)
    )
    assert not is_valid_tree_sha


# ---------------------------------------------------------------------------
# release_tree_fingerprint
# ---------------------------------------------------------------------------


def _commit_all(repo: Path, message: str) -> None:
    """Stage everything and commit with *message*.

    Args:
        repo: Repository directory.
        message: Commit message.
    """
    subprocess.run(["git", "add", "-A"], cwd=repo, env=_GIT_ENV, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", message], cwd=repo, env=_GIT_ENV, check=True
    )


def test_release_fingerprint_equal_when_only_changelog_differs(tmp_path: Path) -> None:
    """Two commits differing ONLY in CHANGELOG.md share a fingerprint."""
    _init_git_repo(tmp_path)
    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / "CHANGELOG.md").write_text("## v1.0.0\n")
    _commit_all(tmp_path, "base")
    base_fp = git_utils.release_tree_fingerprint(tmp_path, "HEAD")
    # Change ONLY the CHANGELOG.
    (tmp_path / "CHANGELOG.md").write_text("## v1.0.0\n## v1.1.0 — curated\n")
    _commit_all(tmp_path, "changelog only")
    assert git_utils.release_tree_fingerprint(tmp_path, "HEAD") == base_fp


def test_release_fingerprint_differs_when_other_file_changes(tmp_path: Path) -> None:
    """A change to any non-CHANGELOG file changes the fingerprint."""
    _init_git_repo(tmp_path)
    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / "CHANGELOG.md").write_text("## v1.0.0\n")
    _commit_all(tmp_path, "base")
    base_fp = git_utils.release_tree_fingerprint(tmp_path, "HEAD")
    (tmp_path / "a.py").write_text("x = 2\n")
    _commit_all(tmp_path, "code change")
    assert git_utils.release_tree_fingerprint(tmp_path, "HEAD") != base_fp


def test_release_fingerprint_none_for_unresolvable_ref(tmp_path: Path) -> None:
    """An unresolvable ref yields ``None`` (empty tree listing)."""
    _init_git_repo(tmp_path)
    assert git_utils.release_tree_fingerprint(tmp_path, "HEAD~999999") is None


# ---------------------------------------------------------------------------
# read_plugin_version_at_ref
# ---------------------------------------------------------------------------


def test_read_plugin_version_at_ref_returns_version_at_commit(
    tmp_path: Path,
) -> None:
    """Committed plugin.json at a ref returns the version string."""
    _init_git_repo(tmp_path)
    plugin_dir = tmp_path / ".claude-plugin"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.json").write_text(
        json.dumps({"name": "x", "version": "1.2.3"})
    )
    subprocess.run(["git", "add", "."], cwd=tmp_path, env=_GIT_ENV, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "add plugin"],
        cwd=tmp_path,
        env=_GIT_ENV,
        check=True,
    )
    assert git_utils.read_plugin_version_at_ref(tmp_path, "HEAD") == "1.2.3"


def test_read_plugin_version_at_ref_absent_file_returns_none(
    tmp_path: Path,
) -> None:
    """No plugin.json committed at ref → None."""
    _init_git_repo(tmp_path)
    assert git_utils.read_plugin_version_at_ref(tmp_path, "HEAD") is None


def test_read_plugin_version_at_ref_malformed_json_returns_none(
    tmp_path: Path,
) -> None:
    """Malformed JSON committed at ref → None (not raise)."""
    _init_git_repo(tmp_path)
    plugin_dir = tmp_path / ".claude-plugin"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.json").write_text("{not valid json")
    subprocess.run(["git", "add", "."], cwd=tmp_path, env=_GIT_ENV, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "bad plugin"],
        cwd=tmp_path,
        env=_GIT_ENV,
        check=True,
    )
    assert git_utils.read_plugin_version_at_ref(tmp_path, "HEAD") is None


def test_read_plugin_version_at_ref_missing_version_key_returns_none(
    tmp_path: Path,
) -> None:
    """plugin.json without a ``"version"`` key → None."""
    _init_git_repo(tmp_path)
    plugin_dir = tmp_path / ".claude-plugin"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.json").write_text(
        json.dumps({"name": "x", "description": "no version key"})
    )
    subprocess.run(["git", "add", "."], cwd=tmp_path, env=_GIT_ENV, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "no version"],
        cwd=tmp_path,
        env=_GIT_ENV,
        check=True,
    )
    assert git_utils.read_plugin_version_at_ref(tmp_path, "HEAD") is None
