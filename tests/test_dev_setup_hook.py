"""Tests for the dev/setup.sh auto-refresh git-hook extensions.

Exercises ``.githooks/post-merge.d/10-dev-setup.sh`` and its
post-checkout wrapper as real bash subprocesses, against a stubbed
``dev/setup.sh`` and a fake ``python3`` shim that pins the
non-interactive probe's outcome deterministically. The extension
*dispatch* mechanism these scripts run under (``run_hook_extensions`` —
sorted order, executable-bit gating, failure tolerance) is covered by
``tests/test_hook_helpers.py`` and is not re-tested here.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from tests.conftest import init_git_repo


_REPO_ROOT = Path(__file__).resolve().parents[1]
_POST_MERGE_HOOK = _REPO_ROOT / ".githooks" / "post-merge.d" / "10-dev-setup.sh"
_POST_CHECKOUT_HOOK = _REPO_ROOT / ".githooks" / "post-checkout.d" / "10-dev-setup.sh"


def _write_executable(path: Path, body: str) -> None:
    """Write *body* as an executable bash script at *path*.

    Args:
        path: Destination script path; parent directories are created.
        body: Script content (a shebang is prepended).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/usr/bin/env bash\n{body}\n", encoding="utf-8")
    path.chmod(0o755)


def _run_hook(
    hook: Path,
    repo: Path,
    *,
    fake_python3_exit: int | None,
    env_overrides: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a dev-setup hook script as a real bash subprocess.

    Args:
        hook: Absolute path to the hook script to execute.
        repo: The tmp git repo to run it in (its cwd).
        fake_python3_exit: Exit code for a ``python3`` shim placed first on
            PATH — simulates the non-interactive probe's outcome without
            needing a real forge install. ``None`` leaves the real
            ``python3`` on PATH.
        env_overrides: Extra environment variables to set (or clear, via
            ``""``... not supported — callers set explicit values only).

    Returns:
        The completed subprocess (stdout/stderr captured as text).
    """
    env = dict(os.environ)
    env.pop("FORGE_HOOK_NAME", None)
    env.pop("FORGE_NO_AUTO_SETUP", None)
    if fake_python3_exit is not None:
        bin_dir = repo / "fakebin"
        _write_executable(bin_dir / "python3", f"exit {fake_python3_exit}")
        env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    env.update(env_overrides or {})
    return subprocess.run(
        [str(hook)],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def tmp_repo(tmp_path: Path) -> Path:
    """A minimal git repo for the dev-setup hook scripts to run inside."""
    init_git_repo(tmp_path)
    return tmp_path


def test_forge_no_auto_setup_env_skips_before_running_stub(tmp_repo: Path) -> None:
    """FORGE_NO_AUTO_SETUP=1 exits 0 immediately, before the stub can run.

    MOCK SETUP: python3 shim would report "interactive" (exit 3) if
    reached — proving the env-var skip fires before the probe at all.
    """
    marker = tmp_repo / "ran"
    _write_executable(tmp_repo / "dev" / "setup.sh", f"touch '{marker}'")
    result = _run_hook(
        _POST_MERGE_HOOK,
        tmp_repo,
        fake_python3_exit=3,
        env_overrides={"FORGE_NO_AUTO_SETUP": "1"},
    )
    assert result.returncode == 0
    assert not marker.exists()


def test_missing_setup_script_exits_silently(tmp_repo: Path) -> None:
    """No dev/setup.sh at all → exit 0, no banner."""
    result = _run_hook(_POST_MERGE_HOOK, tmp_repo, fake_python3_exit=3)
    assert result.returncode == 0
    assert result.stderr == ""


def test_non_executable_setup_script_is_treated_as_absent(tmp_repo: Path) -> None:
    """A dev/setup.sh present but lacking the executable bit is skipped."""
    setup = tmp_repo / "dev" / "setup.sh"
    setup.parent.mkdir(parents=True)
    setup.write_text("#!/usr/bin/env bash\ntouch ran\n", encoding="utf-8")
    result = _run_hook(_POST_MERGE_HOOK, tmp_repo, fake_python3_exit=3)
    assert result.returncode == 0
    assert result.stderr == ""
    assert not (tmp_repo / "ran").exists()


def test_non_interactive_probe_skips_without_banner(tmp_repo: Path) -> None:
    """Probe exit 0 (non-interactive per forge.run_context) skips silently."""
    marker = tmp_repo / "ran"
    _write_executable(tmp_repo / "dev" / "setup.sh", f"touch '{marker}'")
    result = _run_hook(_POST_MERGE_HOOK, tmp_repo, fake_python3_exit=0)
    assert result.returncode == 0
    assert not marker.exists()
    assert "[forge]" not in result.stderr


def test_interactive_probe_runs_stub_with_banner(tmp_repo: Path) -> None:
    """Probe exit 3 (interactive) prints the refresh banner and runs the stub."""
    marker = tmp_repo / "ran"
    _write_executable(tmp_repo / "dev" / "setup.sh", f"touch '{marker}'")
    result = _run_hook(_POST_MERGE_HOOK, tmp_repo, fake_python3_exit=3)
    assert result.returncode == 0
    assert marker.is_file()
    assert "[forge] post-merge" in result.stderr


def test_unrecognised_probe_code_without_tty_falls_through_to_skip(
    tmp_repo: Path,
) -> None:
    """Probe exit 1 (forge not importable) with no tty on stdout also skips.

    Pins the documented fallthrough: any probe code other than 0/3 defers
    to a ``[ -t 1 ]`` check rather than a second copy of the CI markers,
    and a captured subprocess never has a tty on stdout — so this must
    skip exactly like the non-interactive case.
    """
    marker = tmp_repo / "ran"
    _write_executable(tmp_repo / "dev" / "setup.sh", f"touch '{marker}'")
    result = _run_hook(_POST_MERGE_HOOK, tmp_repo, fake_python3_exit=1)
    assert result.returncode == 0
    assert not marker.exists()
    assert "[forge]" not in result.stderr


def test_post_checkout_execs_post_merge_script_naming_itself(tmp_repo: Path) -> None:
    """post-checkout's wrapper runs the shared script, naming itself in the banner."""
    marker = tmp_repo / "ran"
    _write_executable(tmp_repo / "dev" / "setup.sh", f"touch '{marker}'")
    result = _run_hook(_POST_CHECKOUT_HOOK, tmp_repo, fake_python3_exit=3)
    assert result.returncode == 0
    assert marker.is_file()
    assert "[forge] post-checkout" in result.stderr
