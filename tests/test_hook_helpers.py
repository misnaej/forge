"""Tests for forge._hook_helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from forge import _hook_helpers
from tests.conftest import CapturedCalls, make_fake_run


if TYPE_CHECKING:
    import pytest


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
