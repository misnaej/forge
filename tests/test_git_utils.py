"""Tests for ``forge.git_utils``.

Covers the shared helpers used by every forge CLI: repo root resolution,
modified-file detection, output filtering, and CLI logging setup.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pytest

from forge import git_utils


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
# detect_existing_source_dirs
# ---------------------------------------------------------------------------


def test_detect_existing_source_dirs_returns_existing_subset(
    tmp_path: Path,
) -> None:
    """Only ``DEFAULT_SOURCE_DIRS`` entries that exist as dirs are returned."""
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "agents").mkdir()
    result = git_utils.detect_existing_source_dirs(tmp_path)
    assert result == ["src", "tests", "agents"]


def test_detect_existing_source_dirs_empty_when_no_dirs(tmp_path: Path) -> None:
    """No matching directories → empty list."""
    assert git_utils.detect_existing_source_dirs(tmp_path) == []


def test_detect_existing_source_dirs_ignores_files(tmp_path: Path) -> None:
    """A regular file named like a candidate dir is ignored."""
    (tmp_path / "src").write_text("not a dir")
    assert git_utils.detect_existing_source_dirs(tmp_path) == []


# ---------------------------------------------------------------------------
# repo_root
# ---------------------------------------------------------------------------


def test_repo_root_returns_toplevel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When git succeeds, repo_root returns the trimmed stdout as a Path."""
    fake_top = tmp_path / "repo"
    fake_top.mkdir()

    def fake_run(cmd: list[str], **_kwargs: object) -> object:
        assert cmd[:3] == ["git", "rev-parse", "--show-toplevel"]
        return type("P", (), {"returncode": 0, "stdout": f"{fake_top}\n"})()

    monkeypatch.setattr(git_utils.subprocess, "run", fake_run)
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

    def fake_run(*_args: object, **_kwargs: object) -> object:
        calls["count"] += 1
        return type("P", (), {"returncode": 0, "stdout": f"{tmp_path}\n"})()

    monkeypatch.setattr(git_utils.subprocess, "run", fake_run)
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

    def fake_run(cmd: list[str], **_kwargs: object) -> object:
        if cmd[:3] == ["git", "rev-parse", "--show-toplevel"]:
            return type("P", (), {"returncode": 0, "stdout": f"{tmp_path}\n"})()
        return type("P", (), {"returncode": 0, "stdout": "main\n"})()

    monkeypatch.setattr(git_utils.subprocess, "run", fake_run)
    assert git_utils._run_git("branch", "--show-current") == "main"


def test_run_git_returns_empty_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-zero exit produces an empty string (not raise)."""

    def fake_run(cmd: list[str], **_kwargs: object) -> object:
        if cmd[:3] == ["git", "rev-parse", "--show-toplevel"]:
            return type("P", (), {"returncode": 0, "stdout": f"{tmp_path}\n"})()
        return type("P", (), {"returncode": 128, "stdout": "x\n", "stderr": "boom"})()

    monkeypatch.setattr(git_utils.subprocess, "run", fake_run)
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

    def fake_run(cmd: list[str], **_kwargs: object) -> object:
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

    monkeypatch.setattr(git_utils.subprocess, "run", fake_run)


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

    def fake_run(cmd: list[str], **_kwargs: object) -> object:
        if cmd[:3] == ["git", "rev-parse", "--show-toplevel"]:
            return type("P", (), {"returncode": 0, "stdout": f"{tmp_path}\n"})()
        if cmd[1:3] == ["branch", "--show-current"]:
            return type("P", (), {"returncode": 0, "stdout": "main\n"})()
        if cmd[1:3] == ["diff", "--name-only"] and cmd[-1] == "HEAD~1":
            return type("P", (), {"returncode": 0, "stdout": "src/x.py\n"})()
        return type("P", (), {"returncode": 0, "stdout": ""})()

    monkeypatch.setattr(git_utils.subprocess, "run", fake_run)
    assert git_utils.get_modified_files() == ["src/x.py"]


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
