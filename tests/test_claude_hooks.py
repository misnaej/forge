"""Module for testing forge-continuation hooks."""

import json
import os
import subprocess
from pathlib import Path

import pytest

from forge.instruction_files import generated_name, shipped_path


@pytest.fixture
def bin_with_fake_forge_continuation(tmp_path: Path) -> Path:
    """Fake bin dir with forge-continuation executable.

    Returns:
        The bin directory path.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True)
    shim = bin_dir / "forge-continuation"
    shim.write_text("#!/bin/sh\necho forge-continuation-marker\nexit 0")
    shim.chmod(0o755)

    """A fake bin dir containing an executable forge-continuation that echoes a marker.

    Returns:
        The bin directory path.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True)
    shim = bin_dir / "forge-continuation"
    shim.write_text("""#!/bin/sh
echo forge-continuation-marker
exit 0
""")
    shim.chmod(0o755)
    return bin_dir


def _run_agent_guard(
    script: str, agent_type: str, command: str, scoped_agent: str | None
) -> subprocess.CompletedProcess[str]:
    """Run a shipped guard script on a payload from *agent_type*.

    Args:
        script: Guard file name in the shipped hooks directory.
        agent_type: The `agent_type` the payload carries.
        command: The Bash command in the payload.
        scoped_agent: Value for `FORGE_HOOK_AGENT`, or ``None`` to leave it unset.

    Returns:
        The completed process, stdout and stderr as text.
    """
    env = {k: v for k, v in os.environ.items() if k != "FORGE_HOOK_AGENT"}
    if scoped_agent is not None:
        env["FORGE_HOOK_AGENT"] = scoped_agent
    payload = {
        "agent_type": agent_type,
        "agent_id": "",
        "session_id": "s1",
        "cwd": ".",
        "tool_input": {"command": command},
    }
    return subprocess.run(
        [str(shipped_path("claude-hooks") / script)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


_FIXER = generated_name("precommit-fixer")
_OTHER = generated_name("design-checker")


@pytest.mark.parametrize(
    ("script", "unset_exit"),
    [
        pytest.param("block_fixer_recon.sh", 0, id="block_fixer_recon"),
        pytest.param("require_fixer_verdict.sh", 0, id="require_fixer_verdict"),
    ],
)
def test_agent_scoped_guard_gate(script: str, unset_exit: int) -> None:
    """Behavior: scoped guards act on the named agent only and report a missing scope.

    Args:
        script: The guard under test.
        unset_exit: Exit code the guard gives when `FORGE_HOOK_AGENT` is unset.

    SCENARIO: payloads from a different agent with the scope set, and from
    the scoped agent with it unset.
    EXPECTED BEHAVIOR: another agent passes (exit 0); an unset scope names
    `FORGE_HOOK_AGENT` on stderr rather than passing silently.
    """
    other = _run_agent_guard(script, _OTHER, "git status", _FIXER)
    assert other.returncode == 0

    unset = _run_agent_guard(script, _FIXER, "git status", None)
    assert unset.returncode == unset_exit
    assert "FORGE_HOOK_AGENT" in unset.stderr


def test_fixer_recon_guard_blocks_off_allowlist_command_for_scoped_agent() -> None:
    """Behavior: the scoped agent's Bash outside the step CLIs exits 2."""
    result = _run_agent_guard("block_fixer_recon.sh", _FIXER, "git status", _FIXER)

    assert result.returncode == 2
    assert "BLOCKED" in result.stderr


def test_fixer_recon_guard_allows_api_digest_regen() -> None:
    """Behavior: the fixer agent may run forge-gen-api-digest."""
    result = _run_agent_guard(
        "block_fixer_recon.sh", _FIXER, "forge-gen-api-digest", _FIXER
    )
    assert result.returncode == 0
