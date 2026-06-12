"""Tests for forge.post_checkout."""

# MOCKING STRATEGY: no real git-hook side effects — every external seam is stubbed.
#   - subprocess.run: replaced by `make_fake_run` (capturing argv) so the drift
#     check never spawns a child process.
#   - shutil.which: stubbed to fake/None to drive the CLI-present vs -missing paths.
#   - is_non_interactive: forced True/False to select the fast-exit vs active path.

from __future__ import annotations

from typing import TYPE_CHECKING

from forge import _hook_helpers, post_checkout
from tests.conftest import CapturedCalls, make_fake_run


if TYPE_CHECKING:
    import pytest


def test_no_op_when_branch_flag_is_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """File-level checkout (branch_flag != "1") → exit 0 before any work."""
    captured = CapturedCalls()
    monkeypatch.setattr(
        _hook_helpers.subprocess, "run", make_fake_run(captured=captured)
    )
    monkeypatch.setattr(_hook_helpers.shutil, "which", lambda _n: "/fake/bin")
    monkeypatch.setattr(post_checkout, "is_non_interactive", lambda: False)
    assert post_checkout.main(["prev", "new", "0"]) == 0
    assert captured.calls == []


def test_no_op_in_non_interactive_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``is_non_interactive() == True`` fast-exits even on a branch checkout."""
    monkeypatch.setattr(post_checkout, "is_non_interactive", lambda: True)
    captured = CapturedCalls()
    monkeypatch.setattr(
        _hook_helpers.subprocess, "run", make_fake_run(captured=captured)
    )
    monkeypatch.setattr(_hook_helpers.shutil, "which", lambda _n: "/fake/bin")
    assert post_checkout.main(["prev", "new", "1"]) == 0
    assert captured.calls == []


def test_hard_fail_when_install_forge_claude_md_missing(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Missing CLI on a branch checkout → exit 1 with remediation pointer."""
    monkeypatch.setattr(post_checkout, "is_non_interactive", lambda: False)
    monkeypatch.setattr(_hook_helpers.shutil, "which", lambda _n: None)
    with caplog.at_level("ERROR"):
        rc = post_checkout.main(["prev", "new", "1"])
    assert rc == 1
    msgs = "\n".join(r.getMessage() for r in caplog.records)
    assert "install-forge-claude-md not on PATH" in msgs
    assert 'pip install -e ".[dev]"' in msgs


def test_runs_drift_check_on_branch_checkout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Branch checkout (flag=1) + interactive → invokes the drift CLI."""
    monkeypatch.setattr(post_checkout, "is_non_interactive", lambda: False)
    monkeypatch.setattr(_hook_helpers.shutil, "which", lambda _n: "/fake/bin")
    captured = CapturedCalls()
    monkeypatch.setattr(
        _hook_helpers.subprocess,
        "run",
        make_fake_run(returncode=0, captured=captured),
    )
    rc = post_checkout.main(["prev", "new", "1"])
    assert rc == 0
    assert any(
        c[:3] == ["install-forge-claude-md", "--check", "--quiet"]
        for c in captured.calls
    )


def test_handles_short_argv_gracefully(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fewer than 3 args (defensive guard) → no-op exit 0."""
    captured = CapturedCalls()
    monkeypatch.setattr(
        _hook_helpers.subprocess, "run", make_fake_run(captured=captured)
    )
    monkeypatch.setattr(_hook_helpers.shutil, "which", lambda _n: "/fake/bin")
    monkeypatch.setattr(post_checkout, "is_non_interactive", lambda: False)
    assert post_checkout.main([]) == 0
    assert post_checkout.main(["only-one"]) == 0
    assert captured.calls == []


def test_runs_hook_extensions_on_branch_checkout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Branch checkout + interactive runs the ``post-checkout.d`` extensions."""
    monkeypatch.setattr(post_checkout, "is_non_interactive", lambda: False)
    monkeypatch.setattr(_hook_helpers.shutil, "which", lambda _n: "/fake/bin")
    monkeypatch.setattr(_hook_helpers.subprocess, "run", make_fake_run(returncode=0))
    calls: list[str] = []
    monkeypatch.setattr(post_checkout, "run_hook_extensions", calls.append)
    assert post_checkout.main(["prev", "new", "1"]) == 0
    assert calls == ["post-checkout"]


def test_skips_hook_extensions_on_file_checkout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """File-level checkout (flag=0) fast-exits before the extension runner."""
    calls: list[str] = []
    monkeypatch.setattr(post_checkout, "run_hook_extensions", calls.append)
    assert post_checkout.main(["prev", "new", "0"]) == 0
    assert calls == []
