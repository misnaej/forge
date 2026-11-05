"""Tests for ``forge.install_vibe`` — the install-forge-vibe CLI.

# MOCKING STRATEGY: no mocks — the ``consumer_repo`` fixture is a real tmp
# consumer checkout (plugin.json + plugin claude-hooks + skills + agents +
# FOUNDATION.md/CLAUDE.md); install/verify run for real against tmp_path.
"""

from __future__ import annotations

import json
import tomllib
from typing import TYPE_CHECKING

import pytest

from forge.install_vibe import install_vibe


if TYPE_CHECKING:
    from pathlib import Path


_HOOKS_DIR = "${CLAUDE_PLUGIN_ROOT}/claude-hooks"


@pytest.fixture
def consumer_repo(tmp_path: Path) -> Path:
    """A tmp consumer checkout shaped like a plugin install.

    Contains ``.claude-plugin/plugin.json`` with one Bash PreToolUse hook,
    the plugin's ``claude-hooks/block_demo.sh``, one skill, one agent md
    with frontmatter, and FOUNDATION.md + CLAUDE.md (the latter carrying
    an ``@FOUNDATION.md`` include directive).

    Returns:
        The project root path.
    """
    root = tmp_path / "consumer"
    plugin_dir = root / ".claude-plugin"
    hooks_dir = plugin_dir / "claude-hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "block_demo.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    plugin_json = {
        "name": "demo",
        "version": "0.0.0",
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [
                        {
                            "command": f"{_HOOKS_DIR}/block_demo.sh",
                            "timeout": 5,
                        }
                    ],
                }
            ]
        },
    }
    (plugin_dir / "plugin.json").write_text(json.dumps(plugin_json), encoding="utf-8")
    skill_dir = root / "skills" / "demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo\n---\n\nDemo skill.\n", encoding="utf-8"
    )
    agents_dir = root / "agents"
    agents_dir.mkdir()
    (agents_dir / "myagent.md").write_text(
        '---\nname: myagent\ndescription: "Demo agent"\n---\n\n# My agent\n',
        encoding="utf-8",
    )
    (root / "FOUNDATION.md").write_text("# Foundation\n\nRules.\n", encoding="utf-8")
    (root / "CLAUDE.md").write_text(
        "# Consumer rules\n\n@FOUNDATION.md\n\nMore rules.\n", encoding="utf-8"
    )
    return root


def _roster_commands(roster: dict) -> list[str]:
    """Flatten every guard command in a roster sidecar.

    Args:
        roster: Parsed roster dict.

    Returns:
        All guard command strings, in event/matcher order.
    """
    return [
        guard["command"]
        for event_data in roster["events"].values()
        for guards in event_data["matchers"].values()
        for guard in guards
    ]


def test_roster_sidecar_shape_project_root_and_expansion(consumer_repo: Path) -> None:
    """The roster sidecar carries project_root and fully expanded commands."""
    assert install_vibe(consumer_repo) == 0
    roster_path = consumer_repo / ".vibe" / "forge-roster.json"
    roster = json.loads(roster_path.read_text(encoding="utf-8"))
    assert roster["project_root"] == str(consumer_repo)
    assert roster["events"]["PreToolUse"]["matchers"]["Bash"]
    commands = _roster_commands(roster)
    assert commands
    for command in commands:
        assert "${CLAUDE_PLUGIN_ROOT}" not in command
        # Consumer layout: guards expand under the plugin dir, and the
        # expanded path really exists (the dispatcher runs it at runtime).
        assert command.startswith(str(consumer_repo / ".claude-plugin"))
        assert (
            consumer_repo / command.removeprefix(str(consumer_repo) + "/")
        ).is_file()


def test_subagent_toml_schema_and_prompt_pair(consumer_repo: Path) -> None:
    """Each agent md yields a valid subagent TOML plus its prompt file."""
    assert install_vibe(consumer_repo) == 0
    toml_path = consumer_repo / ".vibe" / "agents" / "myagent.toml"
    with toml_path.open("rb") as f:
        data = tomllib.load(f)
    assert data["agent_type"] == "subagent"
    assert data["system_prompt_id"] == "myagent"
    assert data["description"] == "Demo agent"
    prompt = consumer_repo / ".vibe" / "prompts" / "myagent.md"
    assert prompt.is_file()
    assert prompt.read_text(encoding="utf-8") == (
        consumer_repo / "agents" / "myagent.md"
    ).read_text(encoding="utf-8")


def test_check_mode_writes_nothing(consumer_repo: Path) -> None:
    """--check reports success without writing .vibe/, .agents/ or AGENTS.md."""
    assert install_vibe(consumer_repo, check_mode=True) == 0
    assert not (consumer_repo / ".vibe").exists()
    assert not (consumer_repo / ".agents").exists()
    assert not (consumer_repo / "AGENTS.md").exists()


def test_verify_flags_missing_artifacts(consumer_repo: Path) -> None:
    """--verify on a fresh checkout fails: every required artifact missing."""
    assert install_vibe(consumer_repo, verify_mode=True) == 1


def test_verify_passes_on_full_install(consumer_repo: Path) -> None:
    """After a real install, --verify exits 0 (artifacts + strict hooks)."""
    assert install_vibe(consumer_repo) == 0
    assert install_vibe(consumer_repo, verify_mode=True) == 0


def test_agents_md_generation_drops_foundation_directive(consumer_repo: Path) -> None:
    """Generated AGENTS.md flattens FOUNDATION.md + CLAUDE.md sans @-include."""
    assert install_vibe(consumer_repo) == 0
    agents_md = consumer_repo / "AGENTS.md"
    assert agents_md.is_file()
    content = agents_md.read_text(encoding="utf-8")
    assert "# Foundation" in content
    assert "# Consumer rules" in content
    assert "---" in content
    assert all(line.strip() != "@FOUNDATION.md" for line in content.splitlines())
    assert "More rules." in content
