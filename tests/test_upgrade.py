"""Tests for forge.upgrade."""

# MOCKING STRATEGY: the upgrade flow shells out and reads CI/auth context;
# all of it is stubbed so no real pip/git runs.
#   - subprocess.run: faked to capture the pip/git commands + return codes.
#   - _bootstrap_run / repo_root: neutralized (no real bootstrap; sandbox root).
#   - git_auth_mode / is_non_interactive: pinned to exercise the CI-gate and
#     auth-URL-form branches deterministically (see `_stub_run_context`).

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from forge import upgrade
from tests.conftest import FakeProc


if TYPE_CHECKING:
    from pathlib import Path


_BASE_PYPROJECT = """\
[project]
name = "example"
version = "0.1.0"

[project.optional-dependencies]
dev = [
    "forge-scripts @ git+https://github.com/misnaej/forge.git@v1.2.0",
    "pytest>=8.0",
]
"""


def test_find_pin_in_pyproject(tmp_path: Path) -> None:
    """A standard pin under [project.optional-dependencies] is located."""
    (tmp_path / "pyproject.toml").write_text(_BASE_PYPROJECT)
    pin = upgrade.find_pin(tmp_path)
    assert pin is not None
    assert pin.ref == "v1.2.0"
    assert pin.url == "https://github.com/misnaej/forge.git"
    assert pin.line_no == 7  # the forge-scripts line


def test_find_pin_returns_none_when_no_pyproject(tmp_path: Path) -> None:
    """No pyproject.toml → no pin to find."""
    assert upgrade.find_pin(tmp_path) is None


def test_find_pin_returns_none_when_no_pin_line(tmp_path: Path) -> None:
    """pyproject.toml without a forge-scripts pin → no pin."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\nversion = "0"\n',
    )
    assert upgrade.find_pin(tmp_path) is None


def test_find_pin_accepts_main_channel(tmp_path: Path) -> None:
    """A `@main` channel pin is recognised."""
    (tmp_path / "pyproject.toml").write_text(
        "[project.optional-dependencies]\n"
        'dev = ["forge-scripts @ git+https://github.com/misnaej/forge.git@main"]\n',
    )
    pin = upgrade.find_pin(tmp_path)
    assert pin is not None
    assert pin.ref == "main"


def test_rewrite_pin_changes_only_ref(tmp_path: Path) -> None:
    """Rewriting touches only the @ref portion of the pin line."""
    (tmp_path / "pyproject.toml").write_text(_BASE_PYPROJECT)
    pin = upgrade.find_pin(tmp_path)
    assert pin is not None
    new_text = upgrade._rewrite_pin(pin, "v1.3.0")
    # Only the ref changed; everything else byte-identical.
    assert "forge-scripts @ git+https://github.com/misnaej/forge.git@v1.3.0" in new_text
    assert "v1.2.0" not in new_text
    assert "pytest>=8.0" in new_text  # other deps untouched
    assert "[project]" in new_text  # other sections untouched


def test_find_pin_accepts_ssh_format(tmp_path: Path) -> None:
    """An SSH-format pin (`git+ssh://git@host/owner/repo.git@ref`) is parsed.

    Regression for #77: previously the URL group forbade ``@`` so the
    parser anchored on ``git@`` and dropped the hostname / owner / repo
    on rewrite. The url group now allows ``@`` and the ref-anchor finds
    the LAST ``@`` on the line.
    """
    (tmp_path / "pyproject.toml").write_text(
        "[project.optional-dependencies]\n"
        'ci = ["forge-scripts @ git+ssh://git@github.com/misnaej/forge.git@dev"]\n',
    )
    pin = upgrade.find_pin(tmp_path)
    assert pin is not None
    assert pin.url == "ssh://git@github.com/misnaej/forge.git"
    assert pin.ref == "dev"


def test_rewrite_pin_preserves_ssh_url(tmp_path: Path) -> None:
    """SSH-format pin rewrite keeps hostname / owner / repo / .git suffix.

    Regression for #77: the buggy rewriter produced
    ``git+ssh://git@<new-ref>`` (no host, no path) because url and ref
    groups split on the first ``@`` (in ``git@host``) instead of the
    last ``@`` (before the ref).
    """
    pp = tmp_path / "pyproject.toml"
    pp.write_text(
        "[project.optional-dependencies]\n"
        'ci = ["forge-scripts @ git+ssh://git@github.com/misnaej/forge.git@main"]\n',
    )
    pin = upgrade.find_pin(tmp_path)
    assert pin is not None
    new_text = upgrade._rewrite_pin(pin, "v1.7.0")
    assert (
        '"forge-scripts @ git+ssh://git@github.com/misnaej/forge.git@v1.7.0"'
        in new_text
    )
    # Catch the regression shape explicitly: the busted output drops the host.
    assert "git+ssh://git@v1.7.0" not in new_text


def test_rewrite_pin_preserves_quote_style(tmp_path: Path) -> None:
    """Single-quoted pin → single-quoted rewrite. Double-quoted → double."""
    (tmp_path / "pyproject.toml").write_text(
        "[project.optional-dependencies]\n"
        "dev = ['forge-scripts @ git+https://github.com/misnaej/forge.git@v1.0.0']\n",
    )
    pin = upgrade.find_pin(tmp_path)
    assert pin is not None
    new_text = upgrade._rewrite_pin(pin, "v2.0.0")
    assert (
        "'forge-scripts @ git+https://github.com/misnaej/forge.git@v2.0.0'" in new_text
    )


def test_pip_command_uses_https_no_deps_force_reinstall() -> None:
    """The printed pip command matches the documented shape."""
    cmd = upgrade.pip_command("main")
    assert cmd.startswith("pip install --upgrade --force-reinstall --no-deps")
    assert "git+https://github.com/misnaej/forge.git@main" in cmd


def test_phase1_check_mode_prints_without_writing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """--check reports current vs target and the pip command, never writes."""
    pp = tmp_path / "pyproject.toml"
    pp.write_text(_BASE_PYPROJECT)
    original = pp.read_text()

    monkeypatch.setattr(upgrade, "repo_root", lambda: tmp_path)
    argv = ["forge-upgrade", "--channel", "main", "--check"]
    with patch.object(upgrade.sys, "argv", argv), caplog.at_level("INFO"):
        rc = upgrade.main()
    assert rc == 0
    assert pp.read_text() == original  # untouched
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "current pin" in msgs
    assert "would upgrade to: main" in msgs
    assert "pip install --upgrade" in msgs


def test_phase1_rewrites_to_channel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--channel dev` rewrites the pin and prints the pip command."""
    pp = tmp_path / "pyproject.toml"
    pp.write_text(_BASE_PYPROJECT)

    monkeypatch.setattr(upgrade, "repo_root", lambda: tmp_path)
    argv = ["forge-upgrade", "--channel", "dev"]
    with patch.object(upgrade.sys, "argv", argv):
        rc = upgrade.main()
    assert rc == 0
    assert "@dev" in pp.read_text()
    assert "@v1.2.0" not in pp.read_text()


def test_phase1_rewrites_to_specific_tag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--to v1.3.0` pins to that exact tag."""
    pp = tmp_path / "pyproject.toml"
    pp.write_text(_BASE_PYPROJECT)

    monkeypatch.setattr(upgrade, "repo_root", lambda: tmp_path)
    argv = ["forge-upgrade", "--to", "v1.3.0"]
    with patch.object(upgrade.sys, "argv", argv):
        rc = upgrade.main()
    assert rc == 0
    assert "@v1.3.0" in pp.read_text()


def test_phase1_idempotent_when_already_at_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Re-running with the same target is a no-op."""
    pp = tmp_path / "pyproject.toml"
    pp.write_text(_BASE_PYPROJECT)

    monkeypatch.setattr(upgrade, "repo_root", lambda: tmp_path)
    argv = ["forge-upgrade", "--to", "v1.2.0"]  # already at v1.2.0
    with patch.object(upgrade.sys, "argv", argv), caplog.at_level("INFO"):
        rc = upgrade.main()
    assert rc == 0
    assert any("already at" in r.getMessage() for r in caplog.records)


def test_phase1_advisory_warns_on_installed_revision_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Phase 1 warns when the installed build's revision no longer matches the pin.

    None of the *other* phase-1 tests stub `_installed_revision`, so they
    exercise the real (dev-checkout) call, which returns None — editable /
    dir_info install, per its docstring — and stay silent on this path by
    construction; only a test that explicitly stubs `_installed_revision`
    (like this one) can trip it.
    """
    pp = tmp_path / "pyproject.toml"
    pp.write_text(_BASE_PYPROJECT)
    monkeypatch.setattr(upgrade, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(upgrade, "_installed_revision", lambda: "v1.1.0")
    argv = ["forge-upgrade"]  # no flags -> target_ref resolves to the pin's own ref
    with patch.object(upgrade.sys, "argv", argv), caplog.at_level("WARNING"):
        rc = upgrade.main()
    assert rc == 0
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "installed build is from" in msgs
    assert "pin now says" in msgs


def test_phase1_advisory_silent_when_installed_matches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No warning when the installed revision already matches the pin."""
    pp = tmp_path / "pyproject.toml"
    pp.write_text(_BASE_PYPROJECT)
    monkeypatch.setattr(upgrade, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(upgrade, "_installed_revision", lambda: "v1.2.0")
    argv = ["forge-upgrade"]
    with patch.object(upgrade.sys, "argv", argv), caplog.at_level("WARNING"):
        rc = upgrade.main()
    assert rc == 0
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "installed build is from" not in msgs


def test_phase1_advisory_silent_when_installed_revision_none(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No warning when the installed revision is unknowable (non-git install)."""
    pp = tmp_path / "pyproject.toml"
    pp.write_text(_BASE_PYPROJECT)
    monkeypatch.setattr(upgrade, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(upgrade, "_installed_revision", lambda: None)
    argv = ["forge-upgrade"]
    with patch.object(upgrade.sys, "argv", argv), caplog.at_level("WARNING"):
        rc = upgrade.main()
    assert rc == 0
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "installed build is from" not in msgs


def test_phase1_errors_when_no_pin_and_no_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No pin AND no --channel/--to → can't infer target; exit 2."""
    # No pyproject.toml at all.
    monkeypatch.setattr(upgrade, "repo_root", lambda: tmp_path)
    argv = ["forge-upgrade"]  # no target hint
    with (
        patch.object(upgrade.sys, "argv", argv),
        patch.object(upgrade.sys, "stderr") as _stderr,
    ):
        with pytest.raises(SystemExit) as exc_info:
            upgrade.main()
        assert exc_info.value.code == 2


def test_phase1_warns_when_pyproject_missing_but_target_given(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No pyproject pin but --channel given → print pip command + continue note."""
    monkeypatch.setattr(upgrade, "repo_root", lambda: tmp_path)
    argv = ["forge-upgrade", "--channel", "main"]
    with patch.object(upgrade.sys, "argv", argv), caplog.at_level("INFO"):
        rc = upgrade.main()
    assert rc == 0
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "no forge-scripts pin found" in msgs
    assert "pip install --upgrade" in msgs
    assert "forge-upgrade --continue" in msgs


def test_continue_rejects_other_flags(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--continue is exclusive with --channel / --to / --check."""
    monkeypatch.setattr(upgrade, "repo_root", lambda: tmp_path)
    argv = ["forge-upgrade", "--continue", "--channel", "main"]
    with patch.object(upgrade.sys, "argv", argv):
        rc = upgrade.main()
    assert rc == 2


def test_continue_calls_bootstrap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`--continue` invokes install-forge-bootstrap.main() and prints plugin hint."""
    monkeypatch.setattr(upgrade, "repo_root", lambda: tmp_path)
    captured: dict[str, int] = {"calls": 0}

    def _fake_bootstrap_run() -> int:
        captured["calls"] += 1
        return 0

    # Bootstrap is now imported at the top of the upgrade module as
    # ``_bootstrap_run`` — patch that symbol so the test substitutes
    # the right binding.
    monkeypatch.setattr(upgrade, "_bootstrap_run", _fake_bootstrap_run)
    argv = ["forge-upgrade", "--continue"]
    with patch.object(upgrade.sys, "argv", argv), caplog.at_level("INFO"):
        rc = upgrade.main()
    assert rc == 0
    assert captured["calls"] == 1
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "/plugin update forge@forge" in msgs


def test_pin_revision_mismatch_returns_pair_on_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pin present + installed revision known + differing -> the pair."""
    (tmp_path / "pyproject.toml").write_text(_BASE_PYPROJECT)
    monkeypatch.setattr(upgrade, "_installed_revision", lambda: "v1.1.0")
    assert upgrade.pin_revision_mismatch(tmp_path) == ("v1.2.0", "v1.1.0")


def test_pin_revision_mismatch_none_when_refs_match(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Installed revision equal to the pin ref -> no mismatch."""
    (tmp_path / "pyproject.toml").write_text(_BASE_PYPROJECT)
    monkeypatch.setattr(upgrade, "_installed_revision", lambda: "v1.2.0")
    assert upgrade.pin_revision_mismatch(tmp_path) is None


def test_pin_revision_mismatch_none_when_pin_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No pin to compare against -> no mismatch, regardless of install."""
    monkeypatch.setattr(upgrade, "_installed_revision", lambda: "v1.1.0")
    assert upgrade.pin_revision_mismatch(tmp_path) is None


def test_pin_revision_mismatch_none_when_installed_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-git install (revision unknowable) -> never a mismatch."""
    (tmp_path / "pyproject.toml").write_text(_BASE_PYPROJECT)
    monkeypatch.setattr(upgrade, "_installed_revision", lambda: None)
    assert upgrade.pin_revision_mismatch(tmp_path) is None


def test_phase2_gate_blocks_on_revision_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A provable installed/pin mismatch refuses to regenerate managed artifacts.

    SCENARIO: pin says v1.2.0 but the installed build's direct_url reads
        v1.1.0 — regenerating artifacts now would be the silent-downgrade
        case, so phase 2 must refuse.
    MOCK SETUP: `_installed_revision` -> "v1.1.0" (mismatches the pin
        ref); `_bootstrap_run` replaced with a spy that fails the test if
        it is ever called.
    EXPECTED BEHAVIOR: rc 1, an ERROR log naming the refusal and the exact
        `pip_command("v1.2.0")` fix command; bootstrap never runs.
    """
    (tmp_path / "pyproject.toml").write_text(_BASE_PYPROJECT)
    monkeypatch.setattr(upgrade, "_installed_revision", lambda: "v1.1.0")

    def _fail_if_called() -> int:
        msg = "_bootstrap_run must not be called when the gate blocks"
        raise AssertionError(msg)

    monkeypatch.setattr(upgrade, "_bootstrap_run", _fail_if_called)

    with caplog.at_level("ERROR"):
        rc = upgrade._run_phase2(tmp_path)
    assert rc == 1
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "refusing to regenerate managed artifacts from a stale install" in msgs
    assert upgrade.pip_command("v1.2.0") in msgs


def test_phase2_gate_proceeds_on_revision_match(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A matching installed revision lets bootstrap run."""
    (tmp_path / "pyproject.toml").write_text(_BASE_PYPROJECT)
    monkeypatch.setattr(upgrade, "_installed_revision", lambda: "v1.2.0")
    calls: dict[str, int] = {"n": 0}

    def _spy() -> int:
        calls["n"] += 1
        return 0

    monkeypatch.setattr(upgrade, "_bootstrap_run", _spy)
    rc = upgrade._run_phase2(tmp_path)
    assert rc == 0
    assert calls["n"] == 1


def test_phase2_gate_silent_when_pin_is_none(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No pin at all -> the gate can't compare, so it must not block."""
    monkeypatch.setattr(upgrade, "_installed_revision", lambda: "v1.1.0")
    calls: dict[str, int] = {"n": 0}

    def _spy() -> int:
        calls["n"] += 1
        return 0

    monkeypatch.setattr(upgrade, "_bootstrap_run", _spy)
    rc = upgrade._run_phase2(tmp_path)
    assert rc == 0
    assert calls["n"] == 1


def test_phase2_gate_silent_when_installed_revision_none(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unknowable installed revision (non-git install) never blocks."""
    (tmp_path / "pyproject.toml").write_text(_BASE_PYPROJECT)
    monkeypatch.setattr(upgrade, "_installed_revision", lambda: None)
    calls: dict[str, int] = {"n": 0}

    def _spy() -> int:
        calls["n"] += 1
        return 0

    monkeypatch.setattr(upgrade, "_bootstrap_run", _spy)
    rc = upgrade._run_phase2(tmp_path)
    assert rc == 0
    assert calls["n"] == 1


def test_check_with_no_pin_and_no_target_reports_gracefully(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`--check` alone with no pin succeeds and surfaces a target hint."""
    monkeypatch.setattr(upgrade, "repo_root", lambda: tmp_path)
    argv = ["forge-upgrade", "--check"]
    with patch.object(upgrade.sys, "argv", argv), caplog.at_level("INFO"):
        rc = upgrade.main()
    assert rc == 0  # graceful — not SystemExit(2)
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "no forge-scripts pin found" in msgs
    assert "no target" in msgs


def test_check_with_channel_but_no_pin_prints_pip_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`--check --channel main` with no pin still prints what would happen."""
    monkeypatch.setattr(upgrade, "repo_root", lambda: tmp_path)
    argv = ["forge-upgrade", "--check", "--channel", "main"]
    with patch.object(upgrade.sys, "argv", argv), caplog.at_level("INFO"):
        rc = upgrade.main()
    assert rc == 0
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "would upgrade to: main" in msgs
    assert "pip command" in msgs


def test_to_rejects_shell_metacharacters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`--to "v1.0; rm -rf"` is rejected by the argparse type validator."""
    monkeypatch.setattr(upgrade, "repo_root", lambda: tmp_path)
    argv = ["forge-upgrade", "--to", "v1.0; rm -rf /"]
    with patch.object(upgrade.sys, "argv", argv), pytest.raises(SystemExit) as exc:
        upgrade.main()
    # argparse exits with 2 on type-validator failure.
    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert "invalid --to ref" in captured.err


def _stub_run_context(monkeypatch: pytest.MonkeyPatch, auth_mode: str = "ssh") -> None:
    """Stub forge.run_context lookups so --apply tests don't hit the CI gate.

    The production guard in :func:`forge.upgrade._run_apply` aborts when
    ``git_auth_mode()`` returns ``"none"`` AND the run is non-interactive.
    Tests run under pytest (no TTY → non-interactive), so unless we stub
    the auth detection the guard would fire before the pip subprocess
    stub is exercised. The default ``"ssh"`` makes the URL form
    deterministic; pass ``"none"`` to exercise the guard itself.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        auth_mode: Value returned by the stubbed ``git_auth_mode``.
    """
    monkeypatch.setattr(upgrade, "git_auth_mode", lambda: auth_mode)
    monkeypatch.setattr(upgrade, "is_non_interactive", lambda: False)


def test_apply_runs_pip_and_bootstrap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`--apply` rewrites pin, runs pip force-reinstall, then bootstrap.

    SCENARIO: Happy-path ``--apply`` on a repo with a valid pin and usable
        git auth.
    MOCK SETUP: ``repo_root`` → sandbox; ``_stub_run_context`` pins
        ``git_auth_mode``/``is_non_interactive`` past the CI gate;
        ``subprocess.run`` captures the pip argv (returncode 0);
        ``_bootstrap_run`` counts its invocations.
    EXPECTED BEHAVIOR: pin rewritten to ``@main``, pip invoked with
        ``--force-reinstall --no-deps``, bootstrap run exactly once, rc 0.
    """
    pp = tmp_path / "pyproject.toml"
    pp.write_text(_BASE_PYPROJECT)
    monkeypatch.setattr(upgrade, "repo_root", lambda: tmp_path)
    _stub_run_context(monkeypatch)

    pip_calls: list[list[str]] = []

    def _fake_run(cmd: list[str], **_kw: object) -> FakeProc:
        pip_calls.append(cmd)
        return FakeProc(returncode=0)

    bootstrap_calls: dict[str, int] = {"n": 0}

    def _fake_bootstrap() -> int:
        bootstrap_calls["n"] += 1
        return 0

    monkeypatch.setattr(upgrade.subprocess, "run", _fake_run)
    monkeypatch.setattr(upgrade, "_bootstrap_run", _fake_bootstrap)

    argv = ["forge-upgrade", "--apply", "--channel", "main"]
    with patch.object(upgrade.sys, "argv", argv), caplog.at_level("INFO"):
        rc = upgrade.main()
    assert rc == 0
    assert bootstrap_calls["n"] == 1
    assert pip_calls
    assert pip_calls[0][0] == "pip"
    assert "--force-reinstall" in pip_calls[0]
    assert "--no-deps" in pip_calls[0]
    assert any("@main" in arg for arg in pip_calls[0])
    assert "@main" in pp.read_text()


def test_apply_warns_when_installed_still_mismatches_after_pip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`--apply` warns when pip reports success but the read-back still mismatches.

    SCENARIO: pip exits 0 but a re-read of the installed distribution still
        shows the old revision — the "multiple environments" case
        (`which pip` vs `which python`).
    MOCK SETUP: `_run_pip_install` -> 0 and `_run_phase2` -> 0 (isolates
        this assertion from the phase-2 gate, which would otherwise *also*
        fire on the same mismatched `_installed_revision` and mask it
        behind an early rc=1); `_installed_revision` -> "v1.1.0" against a
        `--channel main` target.
    EXPECTED BEHAVIOR: rc 0, a WARNING log carrying the apply-specific
        substring. The same mocked `_installed_revision` also trips the
        phase-1 advisory warning (a distinct message) earlier in the same
        run, so this asserts on the apply-specific substring rather than
        "any warning present" — keeps the two messages distinguishable if
        either wording drifts.
    """
    pp = tmp_path / "pyproject.toml"
    pp.write_text(_BASE_PYPROJECT)
    monkeypatch.setattr(upgrade, "repo_root", lambda: tmp_path)
    _stub_run_context(monkeypatch)
    monkeypatch.setattr(upgrade, "_run_pip_install", lambda *_a, **_k: 0)
    monkeypatch.setattr(upgrade, "_run_phase2", lambda _root: 0)
    monkeypatch.setattr(upgrade, "_installed_revision", lambda: "v1.1.0")

    argv = ["forge-upgrade", "--apply", "--channel", "main"]
    with patch.object(upgrade.sys, "argv", argv), caplog.at_level("WARNING"):
        rc = upgrade.main()
    assert rc == 0
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "pip reported success but the installed build still reads" in msgs


def test_apply_aborts_if_pip_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When pip fails, `--apply` reports failure and skips bootstrap.

    SCENARIO: ``--apply`` where the pip force-reinstall exits non-zero.
    MOCK SETUP: ``repo_root`` → sandbox; ``_stub_run_context`` clears the
        CI gate; ``subprocess.run`` returns ``FakeProc(returncode=1)`` to
        simulate a pip failure; ``_bootstrap_run`` counts invocations.
    EXPECTED BEHAVIOR: rc 1, bootstrap never called, a "pip install failed"
        log emitted.
    """
    pp = tmp_path / "pyproject.toml"
    pp.write_text(_BASE_PYPROJECT)
    monkeypatch.setattr(upgrade, "repo_root", lambda: tmp_path)
    _stub_run_context(monkeypatch)

    bootstrap_calls: dict[str, int] = {"n": 0}

    def _fake_run(_cmd: list[str], **_kw: object) -> FakeProc:
        return FakeProc(returncode=1)

    def _fake_bootstrap() -> int:
        bootstrap_calls["n"] += 1
        return 0

    monkeypatch.setattr(upgrade.subprocess, "run", _fake_run)
    monkeypatch.setattr(upgrade, "_bootstrap_run", _fake_bootstrap)

    argv = ["forge-upgrade", "--apply", "--channel", "main"]
    with patch.object(upgrade.sys, "argv", argv), caplog.at_level("INFO"):
        rc = upgrade.main()
    assert rc == 1
    assert bootstrap_calls["n"] == 0
    assert any("pip install failed" in r.getMessage() for r in caplog.records)


def test_apply_and_check_mutex(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--apply` and `--check` together are rejected with exit 2."""
    monkeypatch.setattr(upgrade, "repo_root", lambda: tmp_path)
    argv = ["forge-upgrade", "--apply", "--check"]
    with patch.object(upgrade.sys, "argv", argv):
        rc = upgrade.main()
    assert rc == 2


def test_atomic_write_preserves_other_lines(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Atomic rewrite preserves bytes outside the pin line."""
    pp = tmp_path / "pyproject.toml"
    pp.write_text(_BASE_PYPROJECT)

    monkeypatch.setattr(upgrade, "repo_root", lambda: tmp_path)
    argv = ["forge-upgrade", "--to", "v9.0.0"]
    with patch.object(upgrade.sys, "argv", argv):
        rc = upgrade.main()
    assert rc == 0
    content = pp.read_text()
    # Original lines outside the pin must be byte-identical.
    assert 'name = "example"' in content
    assert "pytest>=8.0" in content
    assert "@v9.0.0" in content
    # No leftover tempfiles.
    tmp_artifacts = list(tmp_path.glob("pyproject.toml.*.tmp"))
    assert tmp_artifacts == []


# ---------------------------------------------------------------------------
# #79 — run_context wiring (auth-mode URL form, timeout, abort-on-no-auth)
# ---------------------------------------------------------------------------


def test_pip_command_ssh_mode_uses_ssh_url() -> None:
    """auth_mode=ssh renders a ``git+ssh://git@github.com/...`` URL."""
    cmd = upgrade.pip_command("main", auth_mode="ssh")
    assert "git+ssh://git@github.com/" in cmd
    assert "git+https://" not in cmd


@pytest.mark.parametrize("mode", ["https-token", "https-anonymous", "none"])
def test_pip_command_non_ssh_modes_use_https_url(mode: str) -> None:
    """Every non-ssh auth_mode renders a plain ``git+https://...`` URL.

    Args:
        mode: Auth mode from the AuthMode Literal (excluding ``ssh``).
    """
    cmd = upgrade.pip_command("main", auth_mode=mode)
    assert "git+https://github.com/" in cmd
    assert "git+ssh://" not in cmd


def test_pip_command_default_auth_mode_is_https_anonymous() -> None:
    """Default ``auth_mode="https-anonymous"`` keeps the hint-display URL form."""
    cmd = upgrade.pip_command("main")
    assert "git+https://github.com/" in cmd


class _StubDistribution:
    """Stand-in for importlib.metadata.Distribution exposing only read_text."""

    def __init__(self, content: str | None) -> None:
        """Initialize with stubbed direct_url.json content.

        Args:
            content: The JSON content to return from read_text, or None.
        """
        self._content = content

    def read_text(self, _filename: str) -> str | None:
        """Return the stubbed direct_url.json content (or None if "missing").

        Args:
            _filename: Name of the file to read (unused in stub).

        Returns:
            The stubbed direct_url.json content, or None if "missing".
        """
        return self._content


def test_installed_revision_returns_ref_for_git_install(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A git install's direct_url.json yields the requested revision verbatim."""
    content = json.dumps(
        {
            "url": "https://github.com/misnaej/forge.git",
            "vcs_info": {
                "vcs": "git",
                "requested_revision": "dev",
                "commit_id": "abc123",
            },
        }
    )
    monkeypatch.setattr(
        upgrade.metadata, "distribution", lambda _name: _StubDistribution(content)
    )
    assert upgrade._installed_revision() == "dev"


def test_installed_revision_returns_none_for_editable_install(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dir_info-only (editable) install has no vcs_info -> None."""
    content = json.dumps({"url": "file:///repo", "dir_info": {"editable": True}})
    monkeypatch.setattr(
        upgrade.metadata, "distribution", lambda _name: _StubDistribution(content)
    )
    assert upgrade._installed_revision() is None


def test_installed_revision_returns_none_when_file_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """direct_url.json absent -- read_text returns None, not a raise."""
    monkeypatch.setattr(
        upgrade.metadata, "distribution", lambda _name: _StubDistribution(None)
    )
    assert upgrade._installed_revision() is None


def test_installed_revision_returns_none_on_malformed_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Malformed direct_url.json content degrades to None rather than crashing."""
    monkeypatch.setattr(
        upgrade.metadata,
        "distribution",
        lambda _name: _StubDistribution("{not valid json"),
    )
    assert upgrade._installed_revision() is None


@pytest.mark.parametrize(
    "content",
    ["null", "[]", "42", '"str"'],
    ids=["null", "list", "int", "str"],
)
def test_installed_revision_returns_none_for_non_dict_json(
    monkeypatch: pytest.MonkeyPatch,
    content: str,
) -> None:
    """Valid JSON whose top level is not a dict degrades to None, not a crash.

    Args:
        content: direct_url.json payload that parses but has the wrong shape.
    """
    monkeypatch.setattr(
        upgrade.metadata,
        "distribution",
        lambda _name: _StubDistribution(content),
    )
    assert upgrade._installed_revision() is None


def test_installed_revision_returns_none_when_package_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """forge-scripts distribution unavailable -> None, not a crash."""

    def _raise(_name: str) -> _StubDistribution:
        raise upgrade.metadata.PackageNotFoundError

    monkeypatch.setattr(upgrade.metadata, "distribution", _raise)
    assert upgrade._installed_revision() is None


@pytest.mark.parametrize(
    "requested_revision",
    [None, "", 123],
    ids=["missing", "empty", "non_str"],
)
def test_installed_revision_returns_none_for_invalid_requested_revision(
    monkeypatch: pytest.MonkeyPatch,
    requested_revision: object,
) -> None:
    """A vcs_info block with a missing/empty/non-str requested_revision -> None.

    Args:
        requested_revision: The malformed ``requested_revision`` value under
            test -- ``None`` means the key is omitted entirely (missing),
            versus an explicit empty string or a non-str value.
    """
    vcs_info: dict[str, object] = {"vcs": "git", "commit_id": "abc123"}
    if requested_revision is not None:
        vcs_info["requested_revision"] = requested_revision
    content = json.dumps(
        {"url": "https://github.com/misnaej/forge.git", "vcs_info": vcs_info}
    )
    monkeypatch.setattr(
        upgrade.metadata, "distribution", lambda _name: _StubDistribution(content)
    )
    assert upgrade._installed_revision() is None


def test_apply_aborts_when_auth_none_and_non_interactive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """auth=none + non-interactive → abort 2 before pip runs.

    SCENARIO: CI-style run where no git auth is usable and there is no TTY
        to prompt for credentials.
    MOCK SETUP: ``repo_root`` → sandbox; ``git_auth_mode`` → ``"none"`` and
        ``is_non_interactive`` → ``True`` to arm the abort guard;
        ``subprocess.run`` raises if reached, asserting pip is never run.
    EXPECTED BEHAVIOR: rc 2 with a "no usable git auth detected" log, no
        credential prompt against ``/dev/null``, no hung subprocess.
    """
    pp = tmp_path / "pyproject.toml"
    pp.write_text(_BASE_PYPROJECT)
    monkeypatch.setattr(upgrade, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(upgrade, "git_auth_mode", lambda: "none")
    monkeypatch.setattr(upgrade, "is_non_interactive", lambda: True)
    # Pip subprocess MUST NOT be invoked — would hang the test on a real call.
    pip_calls: list[list[str]] = []

    def _fake_run(cmd: list[str], **_kw: object) -> object:
        pip_calls.append(cmd)
        msg = "subprocess should not have been called"
        raise AssertionError(msg)

    monkeypatch.setattr(upgrade.subprocess, "run", _fake_run)

    argv = ["forge-upgrade", "--apply", "--channel", "main"]
    with patch.object(upgrade.sys, "argv", argv), caplog.at_level("ERROR"):
        rc = upgrade.main()

    assert rc == 2
    assert pip_calls == []  # never reached the pip step
    assert any("no usable git auth detected" in r.getMessage() for r in caplog.records)


def test_apply_passes_ssh_auth_to_pip_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """auth=ssh in --apply → pip subprocess receives the ssh URL.

    SCENARIO: ``--apply`` in an environment whose only usable git auth is
        ssh; the pip install URL form must follow.
    MOCK SETUP: ``repo_root`` → sandbox; ``_stub_run_context`` pins
        ``git_auth_mode`` → ``"ssh"`` past the CI gate; ``subprocess.run``
        captures the pip argv; ``_bootstrap_run`` stubbed to a no-op.
    EXPECTED BEHAVIOR: the captured pin spec carries a
        ``git+ssh://git@github.com/`` URL.
    """
    pp = tmp_path / "pyproject.toml"
    pp.write_text(_BASE_PYPROJECT)
    monkeypatch.setattr(upgrade, "repo_root", lambda: tmp_path)
    _stub_run_context(monkeypatch, auth_mode="ssh")

    pip_calls: list[list[str]] = []

    def _fake_run(cmd: list[str], **_kw: object) -> FakeProc:
        pip_calls.append(cmd)
        return FakeProc(returncode=0)

    monkeypatch.setattr(upgrade.subprocess, "run", _fake_run)
    monkeypatch.setattr(upgrade, "_bootstrap_run", lambda: 0)

    argv = ["forge-upgrade", "--apply", "--channel", "main"]
    with patch.object(upgrade.sys, "argv", argv):
        upgrade.main()

    pip_argv = pip_calls[0]
    pin_spec = next(arg for arg in pip_argv if "forge-scripts @" in arg)
    assert "git+ssh://git@github.com/" in pin_spec


def test_apply_pip_timeout_default_ci(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-interactive --apply defaults pip timeout to ``_DEFAULT_PIP_TIMEOUT_CI``.

    SCENARIO: ``--apply`` with no ``--pip-timeout`` flag in a
        non-interactive (CI) run.
    MOCK SETUP: ``repo_root`` → sandbox; ``git_auth_mode`` → ``"ssh"`` and
        ``is_non_interactive`` → ``True`` to select the CI default;
        ``subprocess.run`` captures the ``timeout`` kwarg;
        ``_bootstrap_run`` stubbed to a no-op.
    EXPECTED BEHAVIOR: the subprocess receives
        ``timeout == _DEFAULT_PIP_TIMEOUT_CI``.
    """
    pp = tmp_path / "pyproject.toml"
    pp.write_text(_BASE_PYPROJECT)
    monkeypatch.setattr(upgrade, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(upgrade, "git_auth_mode", lambda: "ssh")
    monkeypatch.setattr(upgrade, "is_non_interactive", lambda: True)

    captured_timeout: dict[str, object] = {}

    def _fake_run(_cmd: list[str], **kw: object) -> FakeProc:
        captured_timeout["timeout"] = kw.get("timeout")
        return FakeProc(returncode=0)

    monkeypatch.setattr(upgrade.subprocess, "run", _fake_run)
    monkeypatch.setattr(upgrade, "_bootstrap_run", lambda: 0)

    argv = ["forge-upgrade", "--apply", "--channel", "main"]
    with patch.object(upgrade.sys, "argv", argv):
        upgrade.main()

    assert captured_timeout["timeout"] == upgrade._DEFAULT_PIP_TIMEOUT_CI


def test_apply_pip_timeout_explicit_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--pip-timeout 42`` reaches the subprocess timeout kwarg.

    SCENARIO: ``--apply`` with an explicit ``--pip-timeout 42`` overriding
        the CI default.
    MOCK SETUP: ``repo_root`` → sandbox; ``_stub_run_context`` pins ssh auth
        past the CI gate; ``subprocess.run`` captures the ``timeout`` kwarg;
        ``_bootstrap_run`` stubbed to a no-op.
    EXPECTED BEHAVIOR: the subprocess receives ``timeout == 42``.
    """
    pp = tmp_path / "pyproject.toml"
    pp.write_text(_BASE_PYPROJECT)
    monkeypatch.setattr(upgrade, "repo_root", lambda: tmp_path)
    _stub_run_context(monkeypatch, auth_mode="ssh")

    captured_timeout: dict[str, object] = {}

    def _fake_run(_cmd: list[str], **kw: object) -> FakeProc:
        captured_timeout["timeout"] = kw.get("timeout")
        return FakeProc(returncode=0)

    monkeypatch.setattr(upgrade.subprocess, "run", _fake_run)
    monkeypatch.setattr(upgrade, "_bootstrap_run", lambda: 0)

    argv = ["forge-upgrade", "--apply", "--channel", "main", "--pip-timeout", "42"]
    with patch.object(upgrade.sys, "argv", argv):
        upgrade.main()

    assert captured_timeout["timeout"] == 42


def test_apply_returns_124_on_pip_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pip subprocess timeout → exit 124 (matches GNU ``timeout(1)``).

    SCENARIO: ``--apply --pip-timeout 1`` where the pip install exceeds its
        deadline.
    MOCK SETUP: ``repo_root`` → sandbox; ``_stub_run_context`` pins ssh auth
        past the CI gate; ``subprocess.run`` raises ``TimeoutExpired``;
        ``_bootstrap_run`` records whether it was reached.
    EXPECTED BEHAVIOR: rc 124 and bootstrap skipped (pip failure short-
        circuits the flow).
    """
    pp = tmp_path / "pyproject.toml"
    pp.write_text(_BASE_PYPROJECT)
    monkeypatch.setattr(upgrade, "repo_root", lambda: tmp_path)
    _stub_run_context(monkeypatch, auth_mode="ssh")

    def _fake_run(_cmd: list[str], **_kw: object) -> object:
        raise upgrade.subprocess.TimeoutExpired(cmd="pip", timeout=1)

    bootstrap_called: dict[str, bool] = {"yes": False}

    def _fake_bootstrap() -> int:
        bootstrap_called["yes"] = True
        return 0

    monkeypatch.setattr(upgrade.subprocess, "run", _fake_run)
    monkeypatch.setattr(upgrade, "_bootstrap_run", _fake_bootstrap)

    argv = ["forge-upgrade", "--apply", "--channel", "main", "--pip-timeout", "1"]
    with patch.object(upgrade.sys, "argv", argv):
        rc = upgrade.main()

    assert rc == 124
    assert bootstrap_called["yes"] is False  # bootstrap skipped on pip failure


_CHANGELOG_SAMPLE = """# Changelog

## v2.0.0 — 2026-01-01

### ⚠️ Upgrade notes
- Breaking: do X before upgrading.

### Features
- a thing

## v1.9.0 — 2025-12-01

### Features
- additive only, no action

## v1.8.0 — 2025-11-01

### ⚠️ Upgrade notes
- Action: do Y.
"""


def test_consumer_upgrade_notes_extracts_lanes_skipping_versions_without() -> None:
    """Only versions with an `⚠️ Upgrade notes` lane are surfaced, newest-first."""
    notes = upgrade._consumer_upgrade_notes(_CHANGELOG_SAMPLE)
    assert notes is not None
    assert "v2.0.0:" in notes
    assert "do X before upgrading" in notes
    assert "v1.8.0:" in notes
    assert "do Y" in notes
    assert "v1.9.0" not in notes  # additive release, no lane → skipped


def test_consumer_upgrade_notes_respects_max_versions() -> None:
    """`max_versions` caps how many note-bearing versions are surfaced."""
    notes = upgrade._consumer_upgrade_notes(_CHANGELOG_SAMPLE, max_versions=1)
    assert notes is not None
    assert "v2.0.0:" in notes
    assert "v1.8.0:" not in notes


def test_consumer_upgrade_notes_none_when_no_lanes() -> None:
    """A changelog with no upgrade-notes lane yields None (nothing to surface)."""
    text = "# Changelog\n\n## v1.0.0 — x\n\n### Features\n- y\n"
    assert upgrade._consumer_upgrade_notes(text) is None


def test_read_changelog_returns_packaged_text() -> None:
    """The changelog ships as package data and reads back as the real file."""
    text = upgrade._read_changelog()
    assert text is not None
    assert text.startswith("# Changelog")


# ---------------------------------------------------------------------------
# Pin discovery / regex — pyproject.toml, requirements*.txt, environment.yml
# ---------------------------------------------------------------------------


def test_pin_regex_for_pyproject_returns_quoted_regex(tmp_path: Path) -> None:
    """``pyproject.toml`` routes to the quote-delimited regex."""
    assert upgrade._pin_regex_for(tmp_path / "pyproject.toml") is upgrade._PIN_RE


def test_pin_regex_for_requirements_returns_bare_regex(tmp_path: Path) -> None:
    """Any other pin-carrying file routes to the bare (unquoted) regex."""
    regex = upgrade._pin_regex_for(tmp_path / "requirements.txt")
    assert regex is upgrade._PIN_BARE_RE


def test_find_pin_in_requirements_txt(tmp_path: Path) -> None:
    """A bare pin in requirements.txt is found when no pyproject.toml exists."""
    (tmp_path / "requirements.txt").write_text(
        "forge-scripts @ git+https://github.com/misnaej/forge.git@v1.0.0\n",
    )
    pin = upgrade.find_pin(tmp_path)
    assert pin is not None
    assert pin.path.name == "requirements.txt"
    assert pin.ref == "v1.0.0"


def test_find_pin_in_environment_yml(tmp_path: Path) -> None:
    """A `pip:` list entry in environment.yml is found."""
    (tmp_path / "environment.yml").write_text(
        "name: example\n"
        "dependencies:\n"
        "  - python=3.11\n"
        "  - pip\n"
        "  - pip:\n"
        "    - forge-scripts @ git+https://github.com/misnaej/forge.git@dev\n",
    )
    pin = upgrade.find_pin(tmp_path)
    assert pin is not None
    assert pin.path.name == "environment.yml"
    assert pin.ref == "dev"


def test_find_pin_prefers_pyproject_over_requirements(tmp_path: Path) -> None:
    """pyproject.toml wins over requirements.txt when both carry a pin."""
    (tmp_path / "pyproject.toml").write_text(_BASE_PYPROJECT)
    (tmp_path / "requirements.txt").write_text(
        "forge-scripts @ git+https://github.com/misnaej/forge.git@dev\n",
    )
    pin = upgrade.find_pin(tmp_path)
    assert pin is not None
    assert pin.path.name == "pyproject.toml"
    assert pin.ref == "v1.2.0"


def test_find_pin_requirements_sorted_glob_deterministic(tmp_path: Path) -> None:
    """requirements*.txt candidates are searched in sorted (deterministic) order."""
    (tmp_path / "requirements-a.txt").write_text("pytest>=8.0\n")
    (tmp_path / "requirements-b.txt").write_text(
        "forge-scripts @ git+https://github.com/misnaej/forge.git@dev\n",
    )
    pin = upgrade.find_pin(tmp_path)
    assert pin is not None
    assert pin.path.name == "requirements-b.txt"


def test_find_pin_bare_requirements_no_pin_returns_none(tmp_path: Path) -> None:
    """requirements.txt without a forge-scripts pin → None."""
    (tmp_path / "requirements.txt").write_text("pytest>=8.0\n")
    assert upgrade.find_pin(tmp_path) is None


# ---------------------------------------------------------------------------
# _PIN_BARE_RE — SSH-format last-@ split regression coverage
# ---------------------------------------------------------------------------


def test_find_pin_bare_ssh_format_splits_on_last_at(tmp_path: Path) -> None:
    """A bare (requirements.txt) SSH-format pin splits on the LAST ``@``.

    Regression for #77 applied to the bare (unquoted) regex path: an
    SSH URL carries three ``@`` characters (user, host, ref separators),
    so the url/ref boundary must anchor on the last one.
    """
    (tmp_path / "requirements.txt").write_text(
        "forge-scripts @ git+ssh://git@github.com/misnaej/forge.git@dev\n",
    )
    pin = upgrade.find_pin(tmp_path)
    assert pin is not None
    assert pin.url == "ssh://git@github.com/misnaej/forge.git"
    assert pin.ref == "dev"


def test_rewrite_pin_bare_ssh_preserves_host_on_rewrite(tmp_path: Path) -> None:
    """Rewriting a bare SSH pin keeps the full URL; only the ref changes."""
    (tmp_path / "requirements.txt").write_text(
        "forge-scripts @ git+ssh://git@github.com/misnaej/forge.git@dev\n",
    )
    pin = upgrade.find_pin(tmp_path)
    assert pin is not None
    new_text = upgrade._rewrite_pin(pin, "v2.0.0")
    assert "git+ssh://git@github.com/misnaej/forge.git@v2.0.0" in new_text
    assert "git+ssh://git@v2.0.0" not in new_text


@pytest.mark.parametrize("terminator", [" # comment", ",", "", "  "])
def test_pin_bare_re_end_of_token_lookahead_stops_at_whitespace(
    tmp_path: Path,
    terminator: str,
) -> None:
    """Ref lookahead stops at whitespace/comma/EOL without swallowing trailing text.

    Args:
        terminator: Text appended immediately after the ref on the pin line.
    """
    (tmp_path / "requirements.txt").write_text(
        f"forge-scripts @ git+https://github.com/misnaej/forge.git@dev{terminator}\n",
    )
    pin = upgrade.find_pin(tmp_path)
    assert pin is not None
    assert pin.ref == "dev"


def test_pin_bare_re_no_match_without_ref_separator(tmp_path: Path) -> None:
    """A pin with no ``@ref`` at all is not recognised as a pin."""
    (tmp_path / "requirements.txt").write_text(
        "forge-scripts @ git+https://github.com/misnaej/forge.git\n",
    )
    assert upgrade.find_pin(tmp_path) is None


def test_rewrite_pin_bare_no_quote_suffix(tmp_path: Path) -> None:
    """Rewriting a bare (requirements.txt) pin adds no trailing quote character."""
    (tmp_path / "requirements.txt").write_text(
        "forge-scripts @ git+https://github.com/misnaej/forge.git@dev\n",
    )
    pin = upgrade.find_pin(tmp_path)
    assert pin is not None
    new_text = upgrade._rewrite_pin(pin, "v2.0.0")
    line = next(line for line in new_text.splitlines() if "forge-scripts @" in line)
    assert '"' not in line
    assert "'" not in line


# ---------------------------------------------------------------------------
# _write_atomic
# ---------------------------------------------------------------------------


def test_write_atomic_works_on_non_pyproject_file(tmp_path: Path) -> None:
    """`_write_atomic` replaces any target file's contents, not just pyproject.toml."""
    target = tmp_path / "requirements.txt"
    target.write_text("old content\n")
    upgrade._write_atomic(target, "new content\n")
    assert target.read_text() == "new content\n"


def test_write_atomic_cleans_up_tempfile_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crash mid-write leaves no `*.tmp` sibling and the original untouched.

    SCENARIO: `os.fdopen` raises after `mkstemp` has already created the
        sibling tempfile.
    MOCK SETUP: `upgrade.os.fdopen` monkeypatched to raise `OSError`.
    EXPECTED BEHAVIOR: the tempfile is unlinked, the original file's
        content is untouched, and the `OSError` propagates.
    """
    target = tmp_path / "requirements.txt"
    target.write_text("original\n")

    def _raise_fdopen(*_a: object, **_kw: object) -> object:
        msg = "disk full"
        raise OSError(msg)

    monkeypatch.setattr(upgrade.os, "fdopen", _raise_fdopen)

    with pytest.raises(OSError, match="disk full"):
        upgrade._write_atomic(target, "new content\n")

    assert target.read_text() == "original\n"
    assert list(tmp_path.glob("*.tmp")) == []


# ---------------------------------------------------------------------------
# Action-item helpers — _recent_action_items / _pending_action_count
# ---------------------------------------------------------------------------


_ACTIONS_CHANGELOG = """# Changelog

## v4.0.0

**Action:** action four.

## v3.0.0

**Action:** action three a.

**Action:** action three b.

## v2.0.0

**Action:** action two.

## v1.0.0

**Action:** action one.
"""


def test_recent_action_items_dedups_versions_respects_max() -> None:
    """`max_versions` caps distinct versions but keeps every item of a kept version."""
    items = upgrade._recent_action_items(_ACTIONS_CHANGELOG, max_versions=2)
    versions = {version for version, _action in items}
    assert versions == {"v4.0.0", "v3.0.0"}
    assert ("v3.0.0", "action three a.") in items
    assert ("v3.0.0", "action three b.") in items
    assert len(items) == 3  # v4.0.0 x1 + v3.0.0 x2


def test_recent_action_items_empty_when_no_markers() -> None:
    """No `**Action:**` markers anywhere → empty list."""
    text = "# Changelog\n\n## v1.0.0\n\n### Features\n- a thing\n"
    assert upgrade._recent_action_items(text) == []


def test_pending_action_count_counts_versions_newer_than_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only markers in versions strictly newer than the installed one count."""
    text = (
        "# Changelog\n\n"
        "## v1.8.0\n\n**Action:** a.\n\n"
        "## v1.6.0\n\n**Action:** b.\n\n"
        "## v1.4.0\n\n**Action:** c.\n"
    )
    monkeypatch.setattr(upgrade.metadata, "version", lambda _name: "1.5.0")
    assert upgrade._pending_action_count(text) == 2


def test_pending_action_count_zero_on_package_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Installed forge-scripts version unavailable → 0, not a crash."""

    def _raise(_name: str) -> str:
        raise upgrade.metadata.PackageNotFoundError

    monkeypatch.setattr(upgrade.metadata, "version", _raise)
    text = "## v99.0.0\n\n**Action:** x.\n"
    assert upgrade._pending_action_count(text) == 0


def test_pending_action_count_zero_on_unparseable_installed_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unparseable installed-version string degrades to 0 rather than crashing."""
    monkeypatch.setattr(upgrade.metadata, "version", lambda _name: "not-a-version")
    text = "## v99.0.0\n\n**Action:** x.\n"
    assert upgrade._pending_action_count(text) == 0


# ---------------------------------------------------------------------------
# Output wiring — --check pending-action warning, _print_upgrade_notes
# ---------------------------------------------------------------------------


def test_check_mode_warns_on_pending_action_items(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`--check --channel main` warns when pending `**Action:**` items exist.

    SCENARIO: `--check` mode with a changelog carrying an `**Action:**`
        marker in a version newer than the (stubbed) installed forge.
    MOCK SETUP: `repo_root` → sandbox; `_read_changelog` monkeypatched to
        a fixed changelog text; `metadata.version` monkeypatched to an
        older installed version so the marker's version counts as pending.
    EXPECTED BEHAVIOR: caplog carries a "pending action item" warning.
    """
    monkeypatch.setattr(upgrade, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(
        upgrade,
        "_read_changelog",
        lambda: "## v9.0.0\n\n**Action:** upgrade config.\n",
    )
    monkeypatch.setattr(upgrade.metadata, "version", lambda _name: "1.0.0")

    argv = ["forge-upgrade", "--check", "--channel", "main"]
    with patch.object(upgrade.sys, "argv", argv), caplog.at_level("INFO"):
        rc = upgrade.main()
    assert rc == 0
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "pending action item" in msgs


def test_check_mode_silent_when_no_pending_actions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`--check` stays silent on Action items the installed forge already covers.

    SCENARIO: `--check` mode where the only Action marker belongs to a
        version the (stubbed) installed forge already covers.
    MOCK SETUP: `repo_root` → sandbox; `_read_changelog` monkeypatched;
        `metadata.version` monkeypatched to a version at/after the
        marker's version.
    EXPECTED BEHAVIOR: no "pending action item" warning in caplog.
    """
    monkeypatch.setattr(upgrade, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(
        upgrade,
        "_read_changelog",
        lambda: "## v1.0.0\n\n**Action:** old news.\n",
    )
    monkeypatch.setattr(upgrade.metadata, "version", lambda _name: "2.0.0")

    argv = ["forge-upgrade", "--check", "--channel", "main"]
    with patch.object(upgrade.sys, "argv", argv), caplog.at_level("INFO"):
        rc = upgrade.main()
    assert rc == 0
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "pending action item" not in msgs


_ACTION_AND_NOTES_CHANGELOG = """# Changelog

## v2.0.0 — 2026-01-01

### ⚠️ Upgrade notes
- Breaking: do X before upgrading.

**Action:** do Y.
"""


def test_print_upgrade_notes_includes_action_required_section(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`_print_upgrade_notes` prints an "Action required" section when markers exist."""
    monkeypatch.setattr(upgrade, "_read_changelog", lambda: _ACTION_AND_NOTES_CHANGELOG)
    with caplog.at_level("INFO"):
        upgrade._print_upgrade_notes()
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "Action required" in msgs
    assert "do Y" in msgs


def test_print_upgrade_notes_omits_action_section_when_none(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`_print_upgrade_notes` prints notes, skipping "Action required" markers."""
    text = "# Changelog\n\n## v1.0.0 — x\n\n### ⚠️ Upgrade notes\n- Breaking: do X.\n"
    monkeypatch.setattr(upgrade, "_read_changelog", lambda: text)
    with caplog.at_level("INFO"):
        upgrade._print_upgrade_notes()
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "do X" in msgs
    assert "Action required" not in msgs
