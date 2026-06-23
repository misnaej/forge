"""Tests for forge.install_bootstrap."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

from forge import install_bootstrap
from tests.conftest import CapturedCalls, make_fake_run


if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_steps_in_expected_order() -> None:
    """STEPS list matches the documented dependency order."""
    slugs = [s.slug for s in install_bootstrap.STEPS]
    assert slugs == [
        "githooks",
        "claude-md",
        "claude-settings",
        "labels",
        "readme-badges",
        "api-digest",
        "cli-reference",
        "audit-deps",
        "doctor",
        "config",
    ]


def test_githooks_step_passes_refresh_flag() -> None:
    """The githooks step passes --refresh so a forge upgrade rewrites the hook.

    install-forge-githooks is idempotent and leaves managed files alone
    after the first install; without --refresh the hook's version marker
    stays stale until the next git pull triggers post-merge auto-refresh.
    """
    githooks = next(s for s in install_bootstrap.STEPS if s.slug == "githooks")
    assert "--refresh" in githooks.argv


def test_resolve_steps_drops_skipped() -> None:
    """--skip removes matching slugs without touching ordering."""
    out = install_bootstrap._resolve_steps(["claude-md", "doctor"])
    slugs = [s.slug for s in out]
    assert "claude-md" not in slugs
    assert "doctor" not in slugs
    assert "githooks" in slugs


def test_resolve_steps_warns_on_unknown_slug(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Unknown --skip slugs log a warning but do not raise."""
    with caplog.at_level("WARNING"):
        install_bootstrap._resolve_steps(["does-not-exist"])
    assert any("unknown --skip" in r.getMessage() for r in caplog.records)


def test_gate_labels_skips_when_gh_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The labels gate self-skips when `gh` is not on PATH."""
    monkeypatch.setattr(install_bootstrap.shutil, "which", lambda _name: None)
    reason = install_bootstrap._gate_labels(tmp_path)
    assert reason is not None
    assert "gh" in reason


def test_gate_labels_skips_when_no_remote(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The labels gate self-skips when the repo has no git remote."""
    monkeypatch.setattr(install_bootstrap.shutil, "which", lambda _name: "/usr/bin/gh")
    monkeypatch.setattr(
        install_bootstrap.subprocess,
        "run",
        make_fake_run(stdout=""),
    )
    reason = install_bootstrap._gate_labels(tmp_path)
    assert reason is not None
    assert "remote" in reason


def test_gate_labels_allows_when_prereqs_met(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The labels gate returns None when gh + remote are present."""
    monkeypatch.setattr(install_bootstrap.shutil, "which", lambda _name: "/usr/bin/gh")
    monkeypatch.setattr(
        install_bootstrap.subprocess,
        "run",
        make_fake_run(stdout="origin\n"),
    )
    assert install_bootstrap._gate_labels(tmp_path) is None


def test_run_step_check_mode_emits_intent_for_unsupported_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """--check on a step without --check support prints intent, doesn't execute."""
    step = install_bootstrap.Step(slug="x", cli="forge-x", supports_check=False)
    monkeypatch.setattr(install_bootstrap.shutil, "which", lambda _name: "/usr/bin/x")
    captured = CapturedCalls()
    monkeypatch.setattr(
        install_bootstrap.subprocess,
        "run",
        make_fake_run(returncode=0, captured=captured),
    )
    with caplog.at_level("INFO"):
        rc = install_bootstrap._run_step(step, check_mode=True, root=tmp_path)
    assert rc == 0
    assert captured.calls == []  # never executed
    assert any("would run" in r.getMessage() for r in caplog.records)


def test_run_step_appends_check_flag_when_supported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Steps that support --check get the flag appended in check mode."""
    step = install_bootstrap.Step(slug="x", cli="forge-x", supports_check=True)
    monkeypatch.setattr(install_bootstrap.shutil, "which", lambda _name: "/usr/bin/x")
    captured = CapturedCalls()
    monkeypatch.setattr(
        install_bootstrap.subprocess,
        "run",
        make_fake_run(returncode=0, captured=captured),
    )
    install_bootstrap._run_step(step, check_mode=True, root=tmp_path)
    assert captured.calls[0][-1] == "--check"


def test_run_step_reports_missing_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A CLI absent from PATH returns 127, the shell convention."""
    step = install_bootstrap.Step(slug="x", cli="forge-x")
    monkeypatch.setattr(install_bootstrap.shutil, "which", lambda _name: None)
    rc = install_bootstrap._run_step(step, check_mode=False, root=tmp_path)
    assert rc == 127


def test_main_strict_aborts_on_first_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--strict stops after the first failing step (does not run the rest)."""
    monkeypatch.setattr(install_bootstrap.shutil, "which", lambda _name: "/usr/bin/x")
    monkeypatch.setattr(install_bootstrap, "repo_root", lambda: tmp_path)
    captured = CapturedCalls()
    monkeypatch.setattr(
        install_bootstrap.subprocess,
        "run",
        make_fake_run(returncode=2, captured=captured),
    )
    argv = ["install-forge-bootstrap", "--strict", "--skip", "labels"]
    with patch.object(install_bootstrap.sys, "argv", argv):
        rc = install_bootstrap.main()
    assert rc == 1  # exactly one failing step before abort
    # First step is githooks; under --strict the loop breaks immediately.
    cli_calls = [c[0] for c in captured.calls]
    assert cli_calls == ["install-forge-githooks"]


def test_main_continue_on_fail_runs_every_step(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default mode counts every failure across the whole sequence."""
    monkeypatch.setattr(install_bootstrap.shutil, "which", lambda _name: "/usr/bin/x")
    monkeypatch.setattr(install_bootstrap, "repo_root", lambda: tmp_path)
    # Force interactive context: the _gate_skip_in_ci gate (FOUNDATION
    # §15) self-skips `doctor` and `audit-deps` in non-TTY runs, which
    # would drop the failure count to 6.
    monkeypatch.setattr(install_bootstrap, "is_non_interactive", lambda: False)
    captured = CapturedCalls()
    monkeypatch.setattr(
        install_bootstrap.subprocess,
        "run",
        make_fake_run(returncode=1, captured=captured),
    )
    # Skip labels (its gate runs subprocess for `git remote`, which would
    # tangle with the always-returncode-1 fake) so we exercise the
    # remaining seven steps cleanly.
    argv = ["install-forge-bootstrap", "--skip", "labels"]
    with patch.object(install_bootstrap.sys, "argv", argv):
        rc = install_bootstrap.main()
    # Nine non-gated steps (incl. config), each fails with rc=1.
    assert rc == 9


def test_doctor_and_audit_deps_skip_in_ci(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In a non-interactive context, doctor + audit-deps self-skip cleanly.

    Asserts: the other four non-gated steps still execute, doctor and
    audit-deps CLIs are absent from the captured subprocess calls, and
    the failure count reflects only the non-gated steps. The bootstrap
    invocation succeeds without ``--skip doctor --skip audit-deps``
    flags because both gate on
    :func:`forge.run_context.is_non_interactive` per FOUNDATION §15.
    """
    monkeypatch.setattr(install_bootstrap.shutil, "which", lambda _name: "/usr/bin/x")
    monkeypatch.setattr(install_bootstrap, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(install_bootstrap, "is_non_interactive", lambda: True)
    captured = CapturedCalls()
    monkeypatch.setattr(
        install_bootstrap.subprocess,
        "run",
        make_fake_run(returncode=1, captured=captured),
    )
    argv = ["install-forge-bootstrap", "--skip", "labels"]
    with patch.object(install_bootstrap.sys, "argv", argv):
        rc = install_bootstrap.main()
    # Six failing steps: githooks, claude-md, claude-settings, readme-badges,
    # api-digest, cli-reference. `doctor` + `audit-deps` self-skip (CI gate).
    assert rc == 6
    invoked_clis = {call[0] for call in captured.calls}
    assert "forge-doctor" not in invoked_clis
    assert "forge-audit-deps" not in invoked_clis
