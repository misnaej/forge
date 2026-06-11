"""Tests for ``forge.fix_ruff``."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from forge import fix_ruff


if TYPE_CHECKING:
    from pathlib import Path


def _stub_subprocess(
    monkeypatch: pytest.MonkeyPatch,
    *,
    diff_stdout: str = "",
    ruff_check_returncode: int = 0,
    calls: list[list[str]] | None = None,
) -> list[list[str]]:
    """Stub subprocess.run for fix_ruff: ruff format/check + git diff/add.

    Args:
        monkeypatch: pytest monkeypatch fixture.
        diff_stdout: What ``git diff --name-only`` should return.
        ruff_check_returncode: Exit code from ``ruff check --fix``.
        calls: Optional list to collect every argv.

    Returns:
        The list of captured argvs (the same list passed in via ``calls``,
        or a fresh one if none was given).
    """
    captured = calls if calls is not None else []

    def fake_run(cmd: list[str], **_kwargs: object) -> object:
        captured.append(cmd)
        if cmd[:2] == ["git", "diff"]:
            return type("P", (), {"returncode": 0, "stdout": diff_stdout})()
        if cmd[:2] == ["ruff", "check"]:
            return type(
                "P",
                (),
                {"returncode": ruff_check_returncode, "stdout": "", "stderr": ""},
            )()
        return type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(fix_ruff.subprocess, "run", fake_run)
    return captured


def test_main_runs_format_and_check_fix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """fix-forge-ruff runs ``ruff format`` then ``ruff check --fix --unsafe-fixes``."""
    (tmp_path / "src").mkdir()
    monkeypatch.chdir(tmp_path)
    calls = _stub_subprocess(monkeypatch)
    monkeypatch.setattr("sys.argv", ["fix-forge-ruff"])
    assert fix_ruff.main() == 0
    format_calls = [c for c in calls if c[:2] == ["ruff", "format"]]
    check_calls = [c for c in calls if c[:2] == ["ruff", "check"]]
    assert format_calls
    assert "--check" not in format_calls[0]
    assert check_calls
    assert "--fix" in check_calls[0]
    assert "--unsafe-fixes" in check_calls[0]


def test_main_writes_ruff_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """fix-forge-ruff writes ``code_health/ruff.log`` with the section header."""
    (tmp_path / "src").mkdir()
    monkeypatch.chdir(tmp_path)
    _stub_subprocess(monkeypatch)
    monkeypatch.setattr("sys.argv", ["fix-forge-ruff"])
    fix_ruff.main()
    log_path = tmp_path / "code_health" / "ruff.log"
    assert log_path.is_file()
    content = log_path.read_text()
    assert "$ ruff format" in content
    assert "$ ruff check --fix --unsafe-fixes" in content


def test_main_restages_modified_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Modified tracked files are re-staged via ``git add``."""
    (tmp_path / "src").mkdir()
    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)
    calls = _stub_subprocess(monkeypatch, diff_stdout="src/foo.py\n")
    monkeypatch.setattr("sys.argv", ["fix-forge-ruff"])
    fix_ruff.main()
    git_add_calls = [c for c in calls if c[:2] == ["git", "add"]]
    assert git_add_calls, "expected git add for modified files"
    assert "src/foo.py" in git_add_calls[0]


def test_main_propagates_nonzero_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When ``ruff check --fix`` exits non-zero (residue), main returns same code."""
    (tmp_path / "src").mkdir()
    monkeypatch.chdir(tmp_path)
    _stub_subprocess(monkeypatch, ruff_check_returncode=1)
    monkeypatch.setattr("sys.argv", ["fix-forge-ruff"])
    assert fix_ruff.main() == 1


def test_main_skipped_without_source_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No source dirs → exit 0 and log says (skipped)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["fix-forge-ruff"])
    assert fix_ruff.main() == 0
    log = (tmp_path / "code_health" / "ruff.log").read_text()
    assert "skipped" in log


def test_main_rejects_path_outside_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``args.dirs`` resolving outside repo root → SystemExit(2)."""
    (tmp_path / "src").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["fix-forge-ruff", "../escape"])
    with pytest.raises(SystemExit) as exc_info:
        fix_ruff.main()
    assert exc_info.value.code == 2


def test_restage_scoped_to_source_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Git diff is pathspec-scoped to source_dirs (no unrelated files folded in)."""
    (tmp_path / "src").mkdir()
    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)
    calls = _stub_subprocess(monkeypatch, diff_stdout="src/foo.py\n")
    monkeypatch.setattr("sys.argv", ["fix-forge-ruff"])
    fix_ruff.main()
    diff_calls = [c for c in calls if c[:2] == ["git", "diff"]]
    assert diff_calls
    cmd = diff_calls[0]
    assert "--" in cmd
    assert "src" in cmd[cmd.index("--") :]
