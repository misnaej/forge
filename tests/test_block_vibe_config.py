"""Black-box tests for claude-hooks/block_vibe_config.sh.

# MOCKING STRATEGY: the guard is a standalone bash + jq script. Each test
# synthesizes a JSON hook payload, runs the script via subprocess with
# cwd=tmp_path, and asserts the exit code (0 allow / 2 block) plus the
# BLOCKED marker on stderr. No forge Python is imported; no mocks — the
# script itself is the unit under test.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest


GUARD = Path(__file__).parent.parent / "claude-hooks" / "block_vibe_config.sh"


def _run_guard(payload: dict, cwd: Path) -> subprocess.CompletedProcess[bytes]:
    """Run the guard script with a JSON payload on stdin.

    Args:
        payload: The hook payload dict.
        cwd: Working directory for the subprocess (tmp_path).

    Returns:
        The completed process.
    """
    return subprocess.run(
        ["bash", str(GUARD)],
        input=json.dumps(payload).encode(),
        capture_output=True,
        cwd=cwd,
        check=False,
    )


def _bash_payload(command: str) -> dict:
    """Build a pre_tool bash payload.

    Args:
        command: The shell command string.

    Returns:
        Payload dict.
    """
    return {
        "hook_event_name": "pre_tool",
        "cwd": ".",
        "tool_name": "file_system.bash",
        "tool_input": {"command": command},
    }


def _edit_payload(file_path: str, *, edits: bool = False) -> dict:
    """Build a pre_tool edit payload.

    Args:
        file_path: The file being edited.
        edits: When True, nest the path in an edits[] array instead.

    Returns:
        Payload dict.
    """
    tool_input: dict = (
        {"edits": [{"file_path": file_path}]} if edits else {"file_path": file_path}
    )
    return {
        "hook_event_name": "pre_tool",
        "cwd": ".",
        "tool_name": "edit",
        "tool_input": tool_input,
    }


BLOCKED_COMMANDS = [
    pytest.param("rm -f .vibe/hooks.toml", id="rm-relative"),
    pytest.param("rm -rf .vibe", id="rm-dir"),
    pytest.param("rm -rf .vibe/", id="rm-dir-slash"),
    pytest.param("mv .vibe /tmp", id="mv-dir"),
    pytest.param("cp /tmp/evil .vibe/hooks.toml", id="cp-over"),
    pytest.param("echo x > .vibe/hooks.toml", id="redirect-over"),
    pytest.param("echo x >> .vibe/forge-roster.json", id="redirect-append"),
    pytest.param("sed -i s/x/y/ .vibe/forge-roster.json", id="sed-in-place"),
    pytest.param("python3 -c \"open('.vibe/hooks.toml','w')\"", id="python-writer"),
    pytest.param('F=.vibe/hooks.toml; rm -f "$F"', id="variable-indirection"),
    pytest.param("rm .agents/skills/pr/SKILL.md", id="rm-agents"),
    pytest.param("echo hi > AGENTS.md", id="redirect-agents-md"),
    pytest.param("rm -rf .vibe; echo done", id="rm-dir-then-echo"),
    pytest.param('rm -rf ".vibe"', id="rm-quoted-dir"),
    pytest.param("echo $(rm -rf .vibe)", id="command-substitution"),
    pytest.param(
        "python3 -c \"import shutil; shutil.rmtree('.vibe')\"", id="python-rmtree"
    ),
    pytest.param('D=.vibe; rm -rf "$D"', id="dir-variable-indirection"),
    pytest.param("S=sed; $S -i s/a/b/ .vibe/hooks.toml", id="verb-indirection"),
    pytest.param("unlink .vibe/hooks.toml", id="unlink"),
    pytest.param("dd of=.vibe/hooks.toml if=/dev/zero", id="dd-overwrite"),
    pytest.param("find .vibe -delete", id="find-delete"),
    pytest.param(
        "python3 -c \"open('.vibe'+'/forge-roster.json','w')\"",
        id="python-constructed-path",
    ),
]


@pytest.mark.parametrize("command", BLOCKED_COMMANDS)
def test_bash_bypass_forms_blocked(command: str, tmp_path: Path) -> None:
    """SCENARIO: agent tries a destructive shell form against generated config.

    EXPECTED BEHAVIOR: exit 2 with BLOCKED on stderr.

    Args:
        command: Bash command fed to the guard.
    """
    result = _run_guard(_bash_payload(command), tmp_path)
    assert result.returncode == 2, result.stderr.decode()
    assert b"BLOCKED" in result.stderr


def test_absolute_path_blocked(tmp_path: Path) -> None:
    """SCENARIO: absolute path into .vibe/. EXPECTED BEHAVIOR: exit 2."""
    cmd = f"rm -f {tmp_path}/.vibe/hooks.toml"
    result = _run_guard(_bash_payload(cmd), tmp_path)
    assert result.returncode == 2


def test_edit_protected_file_blocked(tmp_path: Path) -> None:
    """SCENARIO: Edit tool on .vibe/hooks.toml. EXPECTED BEHAVIOR: exit 2."""
    result = _run_guard(_edit_payload(".vibe/hooks.toml"), tmp_path)
    assert result.returncode == 2


def test_edit_symlink_to_protected_blocked(tmp_path: Path) -> None:
    """SCENARIO: Edit via a symlink pointing at .vibe/hooks.toml.

    EXPECTED BEHAVIOR: realpath resolution blocks, exit 2.
    """
    (tmp_path / ".vibe").mkdir()
    (tmp_path / ".vibe" / "hooks.toml").write_text("x")
    (tmp_path / "link").symlink_to(tmp_path / ".vibe" / "hooks.toml")
    result = _run_guard(_edit_payload("link"), tmp_path)
    assert result.returncode == 2


def test_edit_array_entry_blocked(tmp_path: Path) -> None:
    """SCENARIO: edits[] array carries the protected path.

    EXPECTED BEHAVIOR: exit 2.
    """
    result = _run_guard(_edit_payload(".vibe/forge-roster.json", edits=True), tmp_path)
    assert result.returncode == 2


def test_write_agents_md_blocked(tmp_path: Path) -> None:
    """SCENARIO: Write tool on repo-root AGENTS.md.

    EXPECTED BEHAVIOR: exit 2.
    """
    payload = {
        "hook_event_name": "pre_tool",
        "cwd": ".",
        "tool_name": "write_file",
        "tool_input": {"file_path": "AGENTS.md"},
    }
    result = _run_guard(payload, tmp_path)
    assert result.returncode == 2


ALLOWED_COMMANDS = [
    pytest.param("git status", id="git-status"),
    pytest.param("cat AGENTS.md", id="read-agents-md"),
    pytest.param("grep foo .vibe/hooks.toml", id="grep-guard-config"),
    pytest.param("forge-precommit", id="forge-precommit"),
    pytest.param("install-forge-vibe", id="regenerator-cli"),
    pytest.param("python3 -m pytest tests/ -q", id="pytest"),
]


@pytest.mark.parametrize("command", ALLOWED_COMMANDS)
def test_allowed_commands_pass(command: str, tmp_path: Path) -> None:
    """SCENARIO: ordinary read/navigation/regeneration commands.

    EXPECTED BEHAVIOR: exit 0, no BLOCKED marker.

    Args:
        command: Bash command fed to the guard.
    """
    result = _run_guard(_bash_payload(command), tmp_path)
    assert result.returncode == 0, result.stderr.decode()
    assert b"BLOCKED" not in result.stderr


def test_ordinary_edit_passes(tmp_path: Path) -> None:
    """SCENARIO: Edit on a normal source file. EXPECTED BEHAVIOR: exit 0."""
    result = _run_guard(_edit_payload("src/forge/doctor.py"), tmp_path)
    assert result.returncode == 0
