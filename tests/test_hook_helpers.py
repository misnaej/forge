"""Tests for forge._hook_helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from forge import _hook_helpers
from tests.conftest import CapturedCalls, make_fake_run


if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _make_ext(ext_dir: Path, name: str, body: str, *, executable: bool = True) -> Path:
    """Write an extension script into *ext_dir* and return its path.

    Args:
        ext_dir: The ``.githooks/<hook>.d`` directory.
        name: Script filename.
        body: Bash body (a shebang is prepended).
        executable: Whether to set the executable bit.

    Returns:
        The written script path.
    """
    ext_dir.mkdir(parents=True, exist_ok=True)
    script = ext_dir / name
    script.write_text(f"#!/usr/bin/env bash\n{body}\n")
    if executable:
        script.chmod(0o755)
    return script


def test_drift_check_hard_fails_with_remediation_when_cli_missing(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Missing ``install-forge-claude-md`` returns 1 + names the hook in the error."""
    monkeypatch.setattr(_hook_helpers.shutil, "which", lambda _n: None)
    with caplog.at_level("ERROR"):
        rc = _hook_helpers.run_foundation_drift_check("post-merge")
    assert rc == 1
    msgs = "\n".join(r.getMessage() for r in caplog.records)
    assert "post-merge" in msgs
    assert "install-forge-claude-md not on PATH" in msgs
    assert 'pip install -e ".[dev]"' in msgs


def test_drift_check_invokes_cli_when_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the CLI is on PATH, runs ``install-forge-claude-md --check --quiet``."""
    monkeypatch.setattr(_hook_helpers.shutil, "which", lambda _n: "/fake/bin")
    captured = CapturedCalls()
    monkeypatch.setattr(
        _hook_helpers.subprocess,
        "run",
        make_fake_run(returncode=0, captured=captured),
    )
    rc = _hook_helpers.run_foundation_drift_check("post-checkout")
    assert rc == 0
    assert captured.calls == [["install-forge-claude-md", "--check", "--quiet"]]


def test_drift_check_logs_hint_on_non_zero_exit_but_returns_zero(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Non-zero drift CLI exit → INFO hint but the helper still returns 0.

    The hook must not fail ``git pull`` over an advisory drift
    warning. The user sees the hint; git's exit code stays clean.
    """
    monkeypatch.setattr(_hook_helpers.shutil, "which", lambda _n: "/fake/bin")
    monkeypatch.setattr(
        _hook_helpers.subprocess,
        "run",
        make_fake_run(returncode=2),
    )
    with caplog.at_level("INFO"):
        rc = _hook_helpers.run_foundation_drift_check("post-merge")
    assert rc == 0
    msgs = "\n".join(r.getMessage() for r in caplog.records)
    assert "run `install-forge-claude-md` to sync" in msgs


def test_runs_executable_extension(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An executable .sh under <hook>.d/ runs."""
    monkeypatch.setattr(_hook_helpers, "repo_root", lambda: tmp_path)
    marker = tmp_path / "ran"
    _make_ext(tmp_path / ".githooks" / "post-merge.d", "10-x.sh", f"touch '{marker}'")
    _hook_helpers.run_hook_extensions("post-merge")
    assert marker.is_file()


def test_skips_non_executable_extension(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-executable .sh is skipped silently — no effect, no crash."""
    monkeypatch.setattr(_hook_helpers, "repo_root", lambda: tmp_path)
    marker = tmp_path / "ran"
    _make_ext(
        tmp_path / ".githooks" / "post-merge.d",
        "10-x.sh",
        f"touch '{marker}'",
        executable=False,
    )
    _hook_helpers.run_hook_extensions("post-merge")
    assert not marker.exists()


def test_runs_in_sorted_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scripts run in sorted filename order."""
    monkeypatch.setattr(_hook_helpers, "repo_root", lambda: tmp_path)
    log = tmp_path / "order.log"
    ext_dir = tmp_path / ".githooks" / "post-merge.d"
    _make_ext(ext_dir, "20-second.sh", f"echo second >> '{log}'")
    _make_ext(ext_dir, "10-first.sh", f"echo first >> '{log}'")
    _hook_helpers.run_hook_extensions("post-merge")
    assert log.read_text().split() == ["first", "second"]


def test_non_zero_exit_is_tolerated_and_logged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failing extension is logged and the next one still runs."""
    monkeypatch.setattr(_hook_helpers, "repo_root", lambda: tmp_path)
    marker = tmp_path / "second_ran"
    ext_dir = tmp_path / ".githooks" / "post-merge.d"
    _make_ext(ext_dir, "10-fail.sh", "exit 3")
    _make_ext(ext_dir, "20-ok.sh", f"touch '{marker}'")
    with caplog.at_level("WARNING"):
        _hook_helpers.run_hook_extensions("post-merge")
    assert marker.is_file()
    failed = [r for r in caplog.records if "10-fail.sh" in r.getMessage()]
    assert failed
    assert "ignored" in failed[0].getMessage()


def test_no_extension_dir_is_noop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No <hook>.d/ directory → silent no-op."""
    monkeypatch.setattr(_hook_helpers, "repo_root", lambda: tmp_path)
    _hook_helpers.run_hook_extensions("post-merge")  # must not raise


def test_outside_repo_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """repo_root() raising SystemExit (not in a repo) → silent no-op."""

    def _raise() -> None:
        raise SystemExit(1)

    monkeypatch.setattr(_hook_helpers, "repo_root", _raise)
    _hook_helpers.run_hook_extensions("post-merge")  # must not raise


def test_only_matches_dot_sh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only *.sh files are run; other extensions are ignored."""
    monkeypatch.setattr(_hook_helpers, "repo_root", lambda: tmp_path)
    marker = tmp_path / "ran"
    _make_ext(tmp_path / ".githooks" / "post-merge.d", "note.txt", f"touch '{marker}'")
    _hook_helpers.run_hook_extensions("post-merge")
    assert not marker.exists()
