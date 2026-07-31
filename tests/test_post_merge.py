"""Tests for forge.post_merge."""

# MOCKING STRATEGY: no real git-hook side effects — every external seam is stubbed.
#   - subprocess.run: replaced by `make_fake_run` (capturing argv) so the drift
#     check never spawns a child process.
#   - subprocess.Popen: stubbed so the backgrounded self-refresh is inert and its
#     argv can be asserted instead of executed.
#   - shutil.which: stubbed to fake/None to drive the CLI-present vs -missing paths.
#   - is_non_interactive: forced True/False to select the fast-exit vs active path
#     for forge's own drift-check/self-refresh/tag-advisory.
#   - is_ci: forced True/False to select whether consumer hook extensions run
#     (#177 — extensions must run on a non-tty local pull, only CI suppresses
#     them; is_ci and is_non_interactive are mocked independently since the
#     two now gate different behavior).

from __future__ import annotations

import pytest

from forge import _hook_helpers, post_merge
from tests.conftest import CapturedCalls, make_fake_run


@pytest.fixture(autouse=True)
def _silence_tag_staleness(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default the post-merge tag advisory to a no-op for every test.

    ``main()`` calls ``tag_staleness_warning(Path.cwd())``, which shells
    out to ``git`` — without this, the interactive-path tests would make a
    live git call whose result depends on the runner's branch. Tests that
    exercise the advisory re-patch it.
    """
    monkeypatch.setattr(post_merge, "tag_staleness_warning", lambda _root: None)


def test_no_op_in_genuine_ci(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Genuine CI (is_ci True) fast-exits before any subprocess call or extension.

    ``is_ci() == True`` implies ``is_non_interactive() == True`` too, so both
    forge's own drift-check/self-refresh AND consumer hook extensions are
    suppressed — CI is the one context where neither should run (#177).
    """
    monkeypatch.setattr(post_merge, "is_ci", lambda: True)
    monkeypatch.setattr(post_merge, "is_non_interactive", lambda: True)
    captured = CapturedCalls()
    monkeypatch.setattr(
        _hook_helpers.subprocess, "run", make_fake_run(captured=captured)
    )
    monkeypatch.setattr(_hook_helpers.shutil, "which", lambda _n: "/fake/bin")
    monkeypatch.setattr(post_merge.shutil, "which", lambda _n: "/fake/bin")
    calls: list[str] = []
    monkeypatch.setattr(post_merge, "run_hook_extensions", calls.append)
    assert post_merge.main([]) == 0
    assert captured.calls == []
    assert calls == []


def test_runs_hook_extensions_on_non_tty_local_pull(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-tty local pull runs consumer extensions but skips forge's own dev-loop aids.

    Regression for #177: ``is_ci() == False`` (no CI marker) but
    ``is_non_interactive() == True`` (e.g. stdin is not a tty — VS Code
    terminal, tmux, piped shell). Before the fix, gating
    ``run_hook_extensions`` on ``is_non_interactive()`` silently skipped
    consumer extensions in this exact context. Forge's own drift check /
    self-refresh / tag advisory remain interactive-only dev-loop aids and
    stay skipped (rc stays 0).
    """
    monkeypatch.setattr(post_merge, "is_ci", lambda: False)
    monkeypatch.setattr(post_merge, "is_non_interactive", lambda: True)
    captured = CapturedCalls()
    monkeypatch.setattr(
        _hook_helpers.subprocess, "run", make_fake_run(captured=captured)
    )
    monkeypatch.setattr(_hook_helpers.shutil, "which", lambda _n: "/fake/bin")
    monkeypatch.setattr(post_merge.shutil, "which", lambda _n: "/fake/bin")
    calls: list[str] = []
    monkeypatch.setattr(post_merge, "run_hook_extensions", calls.append)
    assert post_merge.main([]) == 0
    assert captured.calls == []
    assert calls == ["post-merge"]


def test_runs_drift_check_and_hook_extensions_when_interactive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fully interactive: both the drift check AND hook extensions run.

    ``is_ci() == False`` and ``is_non_interactive() == False`` (a human at
    a real terminal) — the third leg of the #177 truth table alongside
    genuine CI and non-tty local.
    """
    monkeypatch.setattr(post_merge, "is_ci", lambda: False)
    monkeypatch.setattr(post_merge, "is_non_interactive", lambda: False)
    monkeypatch.setattr(_hook_helpers.shutil, "which", lambda _n: "/fake/bin")
    monkeypatch.setattr(post_merge.shutil, "which", lambda _n: "/fake/bin")
    monkeypatch.setattr(post_merge.subprocess, "Popen", lambda *_a, **_kw: None)
    captured = CapturedCalls()
    monkeypatch.setattr(
        _hook_helpers.subprocess,
        "run",
        make_fake_run(returncode=0, captured=captured),
    )
    calls: list[str] = []
    monkeypatch.setattr(post_merge, "run_hook_extensions", calls.append)
    rc = post_merge.main([])
    assert rc == 0
    assert any(
        c[:3] == ["install-forge-claude-md", "--check", "--quiet"]
        for c in captured.calls
    )
    assert calls == ["post-merge"]


def test_accepts_git_squash_flag_positional(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Git passes post-merge a squash-status flag; the parser must accept it.

    Regression: a bare ``parse_args`` with no positional rejected git's
    ``$1`` with ``error: unrecognized arguments: 0`` (exit 2) on every
    merge, silently killing the drift check + self-refresh.
    """
    monkeypatch.setattr(post_merge, "is_ci", lambda: True)
    monkeypatch.setattr(post_merge, "is_non_interactive", lambda: True)
    for flag in ("0", "1"):
        assert post_merge.main([flag]) == 0


def test_hard_fail_when_install_forge_claude_md_missing(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Missing drift CLI → exit 1 with a clear remediation pointer (FOUNDATION §2)."""
    monkeypatch.setattr(post_merge, "is_ci", lambda: False)
    monkeypatch.setattr(post_merge, "is_non_interactive", lambda: False)
    monkeypatch.setattr(_hook_helpers.shutil, "which", lambda _n: None)
    monkeypatch.setattr(post_merge, "run_hook_extensions", lambda _h: None)
    with caplog.at_level("ERROR"):
        rc = post_merge.main([])
    assert rc == 1
    msgs = "\n".join(r.getMessage() for r in caplog.records)
    assert "install-forge-claude-md not on PATH" in msgs
    assert "pip install forge-scripts" in msgs
    assert "./dev/setup.sh" in msgs
    assert '-e ".[' not in msgs


def test_runs_drift_check_when_interactive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Interactive context → invokes ``install-forge-claude-md --check --quiet``."""
    monkeypatch.setattr(post_merge, "is_ci", lambda: False)
    monkeypatch.setattr(post_merge, "is_non_interactive", lambda: False)
    monkeypatch.setattr(_hook_helpers.shutil, "which", lambda _n: "/fake/bin")
    # Stub Popen so the self-refresh path (also keyed on shutil.which)
    # is harmless; we only assert the drift-check subprocess.run call here.
    monkeypatch.setattr(post_merge.subprocess, "Popen", lambda *_a, **_kw: None)
    monkeypatch.setattr(post_merge, "run_hook_extensions", lambda _h: None)
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
    monkeypatch.setattr(post_merge, "is_ci", lambda: False)
    monkeypatch.setattr(post_merge, "is_non_interactive", lambda: False)
    monkeypatch.setattr(_hook_helpers.shutil, "which", lambda _n: "/fake/bin")
    monkeypatch.setattr(post_merge.shutil, "which", lambda _n: "/fake/bin")
    monkeypatch.setattr(post_merge, "run_hook_extensions", lambda _h: None)
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


def test_runs_hook_extensions_even_when_drift_check_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Extensions are consumer logic — they run even if the drift check returns 1.

    Symmetric with post-checkout: a forge misconfiguration (drift CLI
    not on PATH) must not silently suppress the consumer's extensions.
    The drift rc is still propagated as the exit code.
    """
    monkeypatch.setattr(post_merge, "is_ci", lambda: False)
    monkeypatch.setattr(post_merge, "is_non_interactive", lambda: False)
    monkeypatch.setattr(post_merge, "run_foundation_drift_check", lambda _n: 1)
    calls: list[str] = []
    monkeypatch.setattr(post_merge, "run_hook_extensions", calls.append)
    assert post_merge.main([]) == 1
    assert calls == ["post-merge"]


def test_emits_tag_staleness_warning_when_owed(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The post-merge advisory is logged when a rolling-next tag is owed.

    MOCK SETUP: interactive; drift check passes; githooks CLI absent (skip
    self-refresh); hook extensions inert; tag_staleness_warning re-patched
    to return a warning string (overriding the autouse no-op).
    """
    monkeypatch.setattr(post_merge, "is_ci", lambda: False)
    monkeypatch.setattr(post_merge, "is_non_interactive", lambda: False)
    monkeypatch.setattr(post_merge, "run_foundation_drift_check", lambda _h: 0)
    monkeypatch.setattr(post_merge.shutil, "which", lambda _n: None)
    monkeypatch.setattr(post_merge, "run_hook_extensions", lambda _h: None)
    monkeypatch.setattr(
        post_merge, "tag_staleness_warning", lambda _root: "owed: tag v1.x"
    )
    with caplog.at_level("WARNING"):
        post_merge.main([])
    assert any("owed: tag v1.x" in r.getMessage() for r in caplog.records)
