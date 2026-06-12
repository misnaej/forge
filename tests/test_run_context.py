"""Unit tests for forge.run_context — runtime-context detection helpers."""

from __future__ import annotations

import io
import subprocess

import pytest

from forge import run_context as mod
from tests.conftest import FakeProc


# ---------------------------------------------------------------------------
# is_non_interactive
# ---------------------------------------------------------------------------


def _clear_ci_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip every recognised CI marker from the test process's env."""
    for name in mod._CI_MARKERS:
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("marker", mod._CI_MARKERS)
def test_is_non_interactive_when_ci_marker_set(
    marker: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each recognised CI env var marks the run as non-interactive.

    Args:
        marker: Name of a CI env var from ``_CI_MARKERS``.
    """
    _clear_ci_env(monkeypatch)
    monkeypatch.setenv(marker, "1")
    assert mod.is_non_interactive() is True


def test_is_non_interactive_when_stdin_not_tty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No CI markers + non-TTY stdin → non-interactive (the pytest default)."""
    _clear_ci_env(monkeypatch)
    # pytest captures stdin so isatty() is False by default
    assert mod.is_non_interactive() is True


def test_is_non_interactive_returns_false_when_tty_and_no_markers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fake TTY stdin + no CI markers → interactive."""
    _clear_ci_env(monkeypatch)
    monkeypatch.setattr(mod, "_stdin_is_tty", lambda: True)
    assert mod.is_non_interactive() is False


def test_is_non_interactive_ignores_empty_marker_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A CI marker set to the empty string does not flip the verdict."""
    _clear_ci_env(monkeypatch)
    monkeypatch.setenv("CI", "")
    monkeypatch.setattr(mod, "_stdin_is_tty", lambda: True)
    assert mod.is_non_interactive() is False


def test_stdin_is_tty_handles_closed_stdin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stdin whose isatty raises returns False (defensive)."""

    class _BrokenStdin:
        """Stub stdin whose isatty raises OSError."""

        def isatty(self) -> bool:
            """Raise to simulate a closed / broken stream.

            Raises:
                OSError: Always.
            """
            msg = "stdin closed"
            raise OSError(msg)

    monkeypatch.setattr("sys.stdin", _BrokenStdin())
    assert mod._stdin_is_tty() is False


# ---------------------------------------------------------------------------
# git_auth_mode
# ---------------------------------------------------------------------------


def _force_no_ssh(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``_ssh_agent_has_identity`` return False for the test."""
    monkeypatch.setattr(mod, "_ssh_agent_has_identity", lambda: False)


def _clear_token_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip every recognised HTTPS-token env var from the test process."""
    for name in mod._HTTPS_TOKEN_ENV:
        monkeypatch.delenv(name, raising=False)


def test_git_auth_mode_ssh_when_agent_has_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SSH wins when the agent reports a loaded key, even with token set."""
    monkeypatch.setattr(mod, "_ssh_agent_has_identity", lambda: True)
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    assert mod.git_auth_mode() == "ssh"


@pytest.mark.parametrize("token_var", mod._HTTPS_TOKEN_ENV)
def test_git_auth_mode_https_token(
    token_var: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No SSH identity + any recognised token env → https-token.

    Args:
        token_var: Name of an HTTPS-token env var from ``_HTTPS_TOKEN_ENV``.
    """
    _force_no_ssh(monkeypatch)
    _clear_token_env(monkeypatch)
    monkeypatch.setenv(token_var, "ghp_secret")
    assert mod.git_auth_mode() == "https-token"


def test_git_auth_mode_https_anonymous_when_tty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No SSH + no token + TTY → anonymous HTTPS (interactive prompt-able)."""
    _force_no_ssh(monkeypatch)
    _clear_token_env(monkeypatch)
    monkeypatch.setattr(mod, "_stdin_is_tty", lambda: True)
    assert mod.git_auth_mode() == "https-anonymous"


def test_git_auth_mode_none_when_no_signals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No SSH + no token + no TTY → ``none`` (caller must fail loud)."""
    _force_no_ssh(monkeypatch)
    _clear_token_env(monkeypatch)
    monkeypatch.setattr(mod, "_stdin_is_tty", lambda: False)
    assert mod.git_auth_mode() == "none"


def test_ssh_agent_has_identity_false_when_ssh_add_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing ``ssh-add`` binary → False (no agent to consult)."""
    monkeypatch.setattr(mod.shutil, "which", lambda _name: None)
    assert mod._ssh_agent_has_identity() is False


def test_ssh_agent_has_identity_true_on_zero_exit_with_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Zero exit + non-empty stdout from ``ssh-add -l`` → True."""
    monkeypatch.setattr(mod.shutil, "which", lambda _name: "/usr/bin/ssh-add")
    proc = FakeProc(returncode=0, stdout="256 SHA256:abc user@host (ED25519)\n")
    monkeypatch.setattr(mod.subprocess, "run", lambda *_a, **_kw: proc)
    assert mod._ssh_agent_has_identity() is True


def test_ssh_agent_has_identity_false_on_no_identities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exit 1 ('agent has no identities') → False."""
    monkeypatch.setattr(mod.shutil, "which", lambda _name: "/usr/bin/ssh-add")
    proc = FakeProc(returncode=1, stdout="The agent has no identities.\n")
    monkeypatch.setattr(mod.subprocess, "run", lambda *_a, **_kw: proc)
    assert mod._ssh_agent_has_identity() is False


@pytest.mark.parametrize(
    ("exc", "label"),
    [
        (subprocess.TimeoutExpired(cmd="ssh-add", timeout=2), "timeout"),
        (OSError("ssh-add failed"), "oserror"),
    ],
)
def test_ssh_agent_has_identity_false_on_subprocess_error(
    exc: BaseException,
    label: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Subprocess timeout or OSError → False (degrade gracefully).

    Args:
        exc: Exception instance the patched subprocess.run will raise.
        label: pytest-id label (unused at runtime; parametrize tag only).
    """
    del label
    monkeypatch.setattr(mod.shutil, "which", lambda _name: "/usr/bin/ssh-add")

    def _boom(*_args: object, **_kwargs: object) -> None:
        """Raise *exc* to simulate subprocess failure.

        Args:
            *_args: Ignored positional arguments from subprocess.run().
            **_kwargs: Ignored keyword arguments from subprocess.run().

        Raises:
            BaseException: The parametrized exception instance.
        """
        raise exc

    monkeypatch.setattr(mod.subprocess, "run", _boom)
    assert mod._ssh_agent_has_identity() is False


# ---------------------------------------------------------------------------
# progress_logger
# ---------------------------------------------------------------------------


def test_progress_logger_emits_start_and_done() -> None:
    """The context manager emits start + done banners around the body."""
    buf = io.StringIO()
    with mod.progress_logger("pip install", out=buf) as note:
        note("force-reinstall")
    output = buf.getvalue()
    assert "[pip install] start" in output
    assert "[pip install] force-reinstall" in output
    assert "[pip install] done (" in output


def test_progress_logger_prints_elapsed_seconds() -> None:
    """The done line ends with ``(Xs)`` and X parses as a non-negative float."""
    buf = io.StringIO()
    with mod.progress_logger("step", out=buf) as _note:
        pass
    done_line = next(line for line in buf.getvalue().splitlines() if line.endswith(")"))
    # "[step] done (0.0s)" — pull the number between '(' and 's)'.
    elapsed_str = done_line.rsplit("(", 1)[1].rstrip("s)")
    elapsed = float(elapsed_str)
    assert elapsed >= 0


def _raise_boom() -> None:
    """Raise a RuntimeError to exercise the finally clause.

    Raises:
        RuntimeError: Always.
    """
    msg = "boom"
    raise RuntimeError(msg)


def test_progress_logger_done_fires_on_exception() -> None:
    """A raised exception still produces a done banner (finally clause)."""
    buf = io.StringIO()
    with (
        pytest.raises(RuntimeError, match="boom"),
        mod.progress_logger("step", out=buf) as _note,
    ):
        _raise_boom()
    assert "[step] done (" in buf.getvalue()


def test_progress_logger_flushes_each_line() -> None:
    """Each emit calls flush on the underlying stream (CI visibility)."""

    class _CountingStream:
        """Stream that counts flush() invocations."""

        def __init__(self) -> None:
            """Initialise the buffer + flush counter."""
            self.buf: list[str] = []
            self.flushes = 0

        def write(self, text: str) -> int:
            """Append *text* to the buffer.

            Args:
                text: Text to record.

            Returns:
                Length of *text*.
            """
            self.buf.append(text)
            return len(text)

        def flush(self) -> None:
            """Increment the flush counter."""
            self.flushes += 1

    stream = _CountingStream()
    with mod.progress_logger("step", out=stream) as note:
        note("alpha")
        note("beta")
    # start + alpha + beta + done = 4 flushed lines.
    assert stream.flushes == 4
