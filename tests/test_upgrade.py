"""Tests for forge.upgrade."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from forge import upgrade


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
    pin = upgrade._find_pin(tmp_path)
    assert pin is not None
    assert pin.ref == "v1.2.0"
    assert pin.url == "https://github.com/misnaej/forge.git"
    assert pin.line_no == 7  # the forge-scripts line


def test_find_pin_returns_none_when_no_pyproject(tmp_path: Path) -> None:
    """No pyproject.toml → no pin to find."""
    assert upgrade._find_pin(tmp_path) is None


def test_find_pin_returns_none_when_no_pin_line(tmp_path: Path) -> None:
    """pyproject.toml without a forge-scripts pin → no pin."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\nversion = "0"\n',
    )
    assert upgrade._find_pin(tmp_path) is None


def test_find_pin_accepts_main_channel(tmp_path: Path) -> None:
    """A `@main` channel pin is recognised."""
    (tmp_path / "pyproject.toml").write_text(
        "[project.optional-dependencies]\n"
        'dev = ["forge-scripts @ git+https://github.com/misnaej/forge.git@main"]\n',
    )
    pin = upgrade._find_pin(tmp_path)
    assert pin is not None
    assert pin.ref == "main"


def test_rewrite_pin_changes_only_ref(tmp_path: Path) -> None:
    """Rewriting touches only the @ref portion of the pin line."""
    (tmp_path / "pyproject.toml").write_text(_BASE_PYPROJECT)
    pin = upgrade._find_pin(tmp_path)
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
    pin = upgrade._find_pin(tmp_path)
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
    pin = upgrade._find_pin(tmp_path)
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
    pin = upgrade._find_pin(tmp_path)
    assert pin is not None
    new_text = upgrade._rewrite_pin(pin, "v2.0.0")
    assert (
        "'forge-scripts @ git+https://github.com/misnaej/forge.git@v2.0.0'" in new_text
    )


def test_pip_command_uses_https_no_deps_force_reinstall() -> None:
    """The printed pip command matches the documented shape."""
    cmd = upgrade._pip_command("main")
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

    def _fake_bootstrap_main() -> int:
        captured["calls"] += 1
        return 0

    # Bootstrap is now imported at the top of the upgrade module as
    # ``_bootstrap_main`` — patch that symbol so the test substitutes
    # the right binding.
    monkeypatch.setattr(upgrade, "_bootstrap_main", _fake_bootstrap_main)
    argv = ["forge-upgrade", "--continue"]
    with patch.object(upgrade.sys, "argv", argv), caplog.at_level("INFO"):
        rc = upgrade.main()
    assert rc == 0
    assert captured["calls"] == 1
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "/plugin update forge@forge" in msgs


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
    """`--apply` rewrites pin, runs pip force-reinstall, then bootstrap."""
    pp = tmp_path / "pyproject.toml"
    pp.write_text(_BASE_PYPROJECT)
    monkeypatch.setattr(upgrade, "repo_root", lambda: tmp_path)
    _stub_run_context(monkeypatch)

    pip_calls: list[list[str]] = []

    class _Proc:
        """Mock subprocess result."""

        returncode = 0

    def _fake_run(cmd: list[str], **_kw: object) -> _Proc:
        pip_calls.append(cmd)
        return _Proc()

    bootstrap_calls: dict[str, int] = {"n": 0}

    def _fake_bootstrap() -> int:
        bootstrap_calls["n"] += 1
        return 0

    monkeypatch.setattr(upgrade.subprocess, "run", _fake_run)
    monkeypatch.setattr(upgrade, "_bootstrap_main", _fake_bootstrap)

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


def test_apply_aborts_if_pip_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When pip fails, `--apply` reports failure and skips bootstrap."""
    pp = tmp_path / "pyproject.toml"
    pp.write_text(_BASE_PYPROJECT)
    monkeypatch.setattr(upgrade, "repo_root", lambda: tmp_path)
    _stub_run_context(monkeypatch)

    class _Proc:
        """Mock subprocess result."""

        returncode = 1

    bootstrap_calls: dict[str, int] = {"n": 0}

    def _fake_run(_cmd: list[str], **_kw: object) -> _Proc:
        return _Proc()

    def _fake_bootstrap() -> int:
        bootstrap_calls["n"] += 1
        return 0

    monkeypatch.setattr(upgrade.subprocess, "run", _fake_run)
    monkeypatch.setattr(upgrade, "_bootstrap_main", _fake_bootstrap)

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
    cmd = upgrade._pip_command("main", auth_mode="ssh")
    assert "git+ssh://git@github.com/" in cmd
    assert "git+https://" not in cmd


@pytest.mark.parametrize("mode", ["https-token", "https-anonymous", "none"])
def test_pip_command_non_ssh_modes_use_https_url(mode: str) -> None:
    """Every non-ssh auth_mode renders a plain ``git+https://...`` URL.

    Args:
        mode: Auth mode from the AuthMode Literal (excluding ``ssh``).
    """
    cmd = upgrade._pip_command("main", auth_mode=mode)
    assert "git+https://github.com/" in cmd
    assert "git+ssh://" not in cmd


def test_pip_command_default_auth_mode_is_https_anonymous() -> None:
    """Default ``auth_mode="https-anonymous"`` keeps the hint-display URL form."""
    cmd = upgrade._pip_command("main")
    assert "git+https://github.com/" in cmd


def test_apply_aborts_when_auth_none_and_non_interactive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """auth=none + non-interactive → abort 2 before pip runs.

    No credential prompt against ``/dev/null``, no hung subprocess.
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
    """auth=ssh in --apply → pip subprocess receives the ssh URL."""
    pp = tmp_path / "pyproject.toml"
    pp.write_text(_BASE_PYPROJECT)
    monkeypatch.setattr(upgrade, "repo_root", lambda: tmp_path)
    _stub_run_context(monkeypatch, auth_mode="ssh")

    pip_calls: list[list[str]] = []

    class _Proc:
        """Mock subprocess result."""

        returncode = 0

    def _fake_run(cmd: list[str], **_kw: object) -> _Proc:
        pip_calls.append(cmd)
        return _Proc()

    monkeypatch.setattr(upgrade.subprocess, "run", _fake_run)
    monkeypatch.setattr(upgrade, "_bootstrap_main", lambda: 0)

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
    """Non-interactive --apply defaults pip timeout to ``_DEFAULT_PIP_TIMEOUT_CI``."""
    pp = tmp_path / "pyproject.toml"
    pp.write_text(_BASE_PYPROJECT)
    monkeypatch.setattr(upgrade, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(upgrade, "git_auth_mode", lambda: "ssh")
    monkeypatch.setattr(upgrade, "is_non_interactive", lambda: True)

    captured_timeout: dict[str, object] = {}

    class _Proc:
        """Mock subprocess result."""

        returncode = 0

    def _fake_run(_cmd: list[str], **kw: object) -> _Proc:
        captured_timeout["timeout"] = kw.get("timeout")
        return _Proc()

    monkeypatch.setattr(upgrade.subprocess, "run", _fake_run)
    monkeypatch.setattr(upgrade, "_bootstrap_main", lambda: 0)

    argv = ["forge-upgrade", "--apply", "--channel", "main"]
    with patch.object(upgrade.sys, "argv", argv):
        upgrade.main()

    assert captured_timeout["timeout"] == upgrade._DEFAULT_PIP_TIMEOUT_CI


def test_apply_pip_timeout_explicit_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--pip-timeout 42`` reaches the subprocess timeout kwarg."""
    pp = tmp_path / "pyproject.toml"
    pp.write_text(_BASE_PYPROJECT)
    monkeypatch.setattr(upgrade, "repo_root", lambda: tmp_path)
    _stub_run_context(monkeypatch, auth_mode="ssh")

    captured_timeout: dict[str, object] = {}

    class _Proc:
        """Mock subprocess result."""

        returncode = 0

    def _fake_run(_cmd: list[str], **kw: object) -> _Proc:
        captured_timeout["timeout"] = kw.get("timeout")
        return _Proc()

    monkeypatch.setattr(upgrade.subprocess, "run", _fake_run)
    monkeypatch.setattr(upgrade, "_bootstrap_main", lambda: 0)

    argv = ["forge-upgrade", "--apply", "--channel", "main", "--pip-timeout", "42"]
    with patch.object(upgrade.sys, "argv", argv):
        upgrade.main()

    assert captured_timeout["timeout"] == 42


def test_apply_returns_124_on_pip_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pip subprocess timeout → exit 124 (matches GNU ``timeout(1)``)."""
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
    monkeypatch.setattr(upgrade, "_bootstrap_main", _fake_bootstrap)

    argv = ["forge-upgrade", "--apply", "--channel", "main", "--pip-timeout", "1"]
    with patch.object(upgrade.sys, "argv", argv):
        rc = upgrade.main()

    assert rc == 124
    assert bootstrap_called["yes"] is False  # bootstrap skipped on pip failure
