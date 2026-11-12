"""Tests for ``forge.install_vibe`` — the install-forge-vibe CLI.

# MOCKING STRATEGY: no mocks — the ``consumer_repo`` fixture is a real tmp
# consumer checkout (plugin.json + plugin claude-hooks + skills + agents +
# FOUNDATION.md/CLAUDE.md); install/verify run for real against tmp_path.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from forge.audit.agents import REQUIRED_SECTIONS
from forge.install_vibe import install_vibe


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


def test_model_presets_written_and_idempotent(tmp_path: Path) -> None:
    """Install writes forge-* [[models]] presets once; re-run adds nothing.

    Each preset must carry provider: Vibe drops a [[models]] entry lacking
    name or provider as an orphan override, which would reject every agent
    file pinning the alias.
    """
    root = tmp_path / "c"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text('{"hooks": {}}')
    assert install_vibe(root) == 0
    cfg = root / ".vibe" / "config.toml"
    data = tomllib.loads(cfg.read_text())
    presets = {m["alias"]: m for m in data["models"]}
    assert {"forge-haiku", "forge-sonnet", "forge-opus"} <= presets.keys()
    for alias in ("forge-haiku", "forge-sonnet", "forge-opus"):
        assert presets[alias]["provider"] == "mistral"
        assert presets[alias]["name"]
    first = cfg.read_text()
    assert install_vibe(root) == 0
    assert cfg.read_text() == first


def test_model_presets_repair_providerless_forge_block(tmp_path: Path) -> None:
    """A forge-owned preset block written without provider is regenerated whole.

    Older forge versions appended presets with only name+alias; Vibe drops
    those entries, so re-running the install must repair the marked block
    while leaving user content before the marker untouched.
    """
    root = tmp_path / "c"
    (root / ".vibe").mkdir(parents=True)
    (root / ".claude-plugin").mkdir()
    (root / ".claude-plugin" / "plugin.json").write_text('{"hooks": {}}')
    (root / ".vibe" / "config.toml").write_text(
        '[[models]]\nname = "my-own"\nprovider = "mistral"\nalias = "custom"\n'
        "# forge-owned model presets for subagent active_model pins\n"
        '\n[[models]]\nname = "mistral-small-latest"\nalias = "forge-sonnet"\n'
    )
    assert install_vibe(root) == 0
    data = tomllib.loads((root / ".vibe" / "config.toml").read_text())
    presets = {m["alias"]: m for m in data["models"]}
    assert presets["custom"] == {
        "name": "my-own",
        "provider": "mistral",
        "alias": "custom",
    }
    assert presets["forge-sonnet"]["provider"] == "mistral"
    assert {"forge-haiku", "forge-opus"} <= presets.keys()


def test_model_presets_never_clobber_user_alias(tmp_path: Path) -> None:
    """A pre-existing user model with the same alias is left untouched."""
    root = tmp_path / "c"
    (root / ".vibe").mkdir(parents=True)
    (root / ".claude-plugin").mkdir()
    (root / ".claude-plugin" / "plugin.json").write_text('{"hooks": {}}')
    (root / ".vibe" / "config.toml").write_text(
        '[[models]]\nname = "my-own"\nalias = "forge-haiku"\n'
    )
    assert install_vibe(root) == 0
    data = tomllib.loads((root / ".vibe" / "config.toml").read_text())
    haiku = [m for m in data["models"] if m["alias"] == "forge-haiku"]
    assert haiku == [{"name": "my-own", "alias": "forge-haiku"}]


def test_subagent_toml_pins_active_model_from_tier(tmp_path: Path) -> None:
    """An agent with model: sonnet gets active_model = forge-sonnet."""
    root = tmp_path / "c"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text('{"hooks": {}}')
    agents = root / "agents"
    agents.mkdir()
    (agents / "a.md").write_text(
        "---\nname: a\ndescription: d\nmodel: sonnet\n---\nbody\n"
    )
    assert install_vibe(root) == 0
    toml_text = (root / ".vibe" / "agents" / "a.toml").read_text()
    assert 'active_model = "forge-sonnet"' in toml_text


def test_agent_sources_carry_terse_block_and_audit_enforces_it() -> None:
    """Every shipped agent md carries the terse block; the audit requires it."""
    assert "Output style (always)" in REQUIRED_SECTIONS
    root = Path(__file__).parent.parent / "agents"
    for md in sorted(root.glob("*.md")):
        if md.name == "_TEMPLATE.md":
            continue
        assert "## Output style (always)" in md.read_text(encoding="utf-8"), md.name


class TestBareConsumerRepo:
    """install-forge-vibe in a consumer repo with no forge checkout files.

    A consumer checkout has no .claude-plugin/, agents/, skills/ or
    claude-hooks/ — the shipped copies inside the installed package must
    be used instead.
    """

    def test_bare_consumer_gets_shipped_agents_and_hooks(self, tmp_path: Path) -> None:
        """install_vibe succeeds from package data alone and writes all artifacts."""
        root = tmp_path / "bare"
        root.mkdir()
        (root / "pyproject.toml").write_text(
            '[project]\nname = "bare"\nversion = "0.1.0"\n', encoding="utf-8"
        )
        assert install_vibe(root) == 0
        tomls = sorted((root / ".vibe" / "agents").glob("*.toml"))
        prompts = sorted((root / ".vibe" / "prompts").glob("*.md"))
        assert len(tomls) == 14
        assert len(prompts) == 14
        assert (root / ".vibe" / "agents" / "prior-art.toml").is_file()
        assert (root / ".vibe" / "hooks.toml").is_file()
        assert (root / ".vibe" / "forge-roster.json").is_file()
        skills = list((root / ".agents" / "skills").iterdir())
        assert len(skills) == 19

    def test_bare_consumer_verify_passes_after_install(self, tmp_path: Path) -> None:
        """--verify is green on a bare consumer once install ran."""
        root = tmp_path / "bare"
        root.mkdir()
        (root / "pyproject.toml").write_text(
            '[project]\nname = "bare"\nversion = "0.1.0"\n', encoding="utf-8"
        )
        assert install_vibe(root) == 0
        assert install_vibe(root, verify_mode=True) == 0
