"""Tests for forge.post_merge."""

# MOCKING STRATEGY: no real git-hook side effects — every external seam is stubbed.
#   - subprocess.run: replaced by `make_fake_run` (capturing argv) so the drift
#     check never spawns a child process.
#   - subprocess.Popen: stubbed so the backgrounded self-refresh is inert and its
#     argv can be asserted instead of executed.
#   - shutil.which: stubbed to fake/None to drive the CLI-present vs -missing paths.
#   - is_non_interactive: forced True/False to select the fast-exit vs active path.

from __future__ import annotations

from typing import TYPE_CHECKING

from forge import _hook_helpers, post_merge
from tests.conftest import CapturedCalls, make_fake_run


if TYPE_CHECKING:
    import pytest


def test_no_op_in_non_interactive_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``is_non_interactive() == True`` fast-exits before any subprocess call."""
    monkeypatch.setattr(post_merge, "is_non_interactive", lambda: True)
    captured = CapturedCalls()
    monkeypatch.setattr(
        _hook_helpers.subprocess, "run", make_fake_run(captured=captured)
    )
    monkeypatch.setattr(_hook_helpers.shutil, "which", lambda _n: "/fake/bin")
    monkeypatch.setattr(post_merge.shutil, "which", lambda _n: "/fake/bin")
    assert post_merge.main([]) == 0
    assert captured.calls == []


def test_accepts_git_squash_flag_positional(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Git passes post-merge a squash-status flag; the parser must accept it.

    Regression: a bare ``parse_args`` with no positional rejected git's
    ``$1`` with ``error: unrecognized arguments: 0`` (exit 2) on every
    merge, silently killing the drift check + self-refresh.
    """
    monkeypatch.setattr(post_merge, "is_non_interactive", lambda: True)
    for flag in ("0", "1"):
        assert post_merge.main([flag]) == 0


def test_hard_fail_when_install_forge_claude_md_missing(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Missing drift CLI → exit 1 with a clear remediation pointer (FOUNDATION §2)."""
    monkeypatch.setattr(post_merge, "is_non_interactive", lambda: False)
    monkeypatch.setattr(_hook_helpers.shutil, "which", lambda _n: None)
    with caplog.at_level("ERROR"):
        rc = post_merge.main([])
    assert rc == 1
    msgs = "\n".join(r.getMessage() for r in caplog.records)
    assert "install-forge-claude-md not on PATH" in msgs
    assert 'pip install -e ".[dev]"' in msgs


def test_runs_drift_check_when_interactive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Interactive context → invokes ``install-forge-claude-md --check --quiet``."""
    monkeypatch.setattr(post_merge, "is_non_interactive", lambda: False)
    monkeypatch.setattr(_hook_helpers.shutil, "which", lambda _n: "/fake/bin")
    # Stub Popen so the self-refresh path (also keyed on shutil.which)
    # is harmless; we only assert the drift-check subprocess.run call here.
    monkeypatch.setattr(post_merge.subprocess, "Popen", lambda *_a, **_kw: None)
    captured = CapturedCalls()
    monkeypatch.setattr(
        _hook_helpers.subprocess,
        "run",
        make_fake_run(returncode=0, captured=captured),
    )
    rc = post_merge.main([])
    assert rc == 0
    assert any(
        c[:3] == ["install-forge-claude-md", "--check", "--quiet"]
        for c in captured.calls
    )


def test_backgrounds_self_refresh_when_githooks_cli_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``install-forge-githooks`` is on PATH, the refresh is backgrounded.

    Verified by asserting ``post_merge.subprocess.Popen`` is invoked
    with the refresh argv. The CLI returns ``0`` regardless of
    whether the background process succeeds (auto-refresh is
    best-effort and must never fail a ``git pull``).
    """
    monkeypatch.setattr(post_merge, "is_non_interactive", lambda: False)
    monkeypatch.setattr(_hook_helpers.shutil, "which", lambda _n: "/fake/bin")
    monkeypatch.setattr(post_merge.shutil, "which", lambda _n: "/fake/bin")
    monkeypatch.setattr(
        _hook_helpers.subprocess,
        "run",
        make_fake_run(returncode=0),
    )
    popen_calls: list[list[str]] = []
    monkeypatch.setattr(
        post_merge.subprocess,
        "Popen",
        lambda argv, **_kw: popen_calls.append(argv),  # type: ignore[func-returns-value]
    )
    assert post_merge.main([]) == 0
    assert popen_calls == [["install-forge-githooks", "--refresh", "--quiet"]]
