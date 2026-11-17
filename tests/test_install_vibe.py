"""Tests for ``forge.install_vibe`` — the install-forge-vibe CLI.

# MOCKING STRATEGY: no mocks — the ``consumer_repo`` fixture is a real tmp
# consumer checkout (plugin.json + plugin claude-hooks + skills + agents +
# FOUNDATION.md/CLAUDE.md); install/verify run for real against tmp_path.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import pytest

from forge import hook_registry
from forge import install_vibe as install_vibe_module
from forge.audit.agents import REQUIRED_SECTIONS
from forge.install_vibe import _generate_roster_sidecar, install_vibe
from forge.instruction_files import generated_name, shipped_path


_HOOKS_DIR = "${CLAUDE_PLUGIN_ROOT}/claude-hooks"


@pytest.fixture
def consumer_repo(tmp_path: Path) -> Path:
    """A tmp consumer checkout with its own shipped sources.

    Carries one skill, one agent md with frontmatter, and FOUNDATION.md
    + CLAUDE.md (the latter with an ``@FOUNDATION.md`` include). It
    deliberately ships **no** ``claude-hooks/`` of its own, so
    :func:`forge.instruction_files.shipped_path` falls back to the
    packaged guards — the consumer case, where the scripts the wiring
    names come from the installed package. Tests needing different
    wiring inject it by replacing the renderer.

    Returns:
        The project root path.
    """
    root = tmp_path / "consumer"
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
        # Commands are absolute by the time they reach the sidecar, which
        # the dispatcher runs directly, so no placeholder may survive and
        # the named script must exist.
        assert "${CLAUDE_PLUGIN_ROOT}" not in command
        assert "${" not in command
        assert Path(command).is_file()


def test_roster_sidecar_merges_duplicate_matcher_hooks_and_warns(
    consumer_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two groups sharing a matcher keep both hooks and warn once.

    SCENARIO: wiring that declares the same matcher twice under one
    event, injected by replacing the renderer.
    EXPECTED BEHAVIOR: a roster keys hooks by matcher, so assignment used
    to drop the earlier group in silence; both survive and one warning
    names the event and matcher.
    """
    monkeypatch.setattr(
        install_vibe_module,
        "rendered_hooks",
        lambda command_for: {
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [
                        {"type": "command", "command": command_for("block_demo.sh")}
                    ],
                },
                {
                    "matcher": "Bash",
                    "hooks": [
                        {"type": "command", "command": command_for("block_other.sh")}
                    ],
                },
            ]
        },
    )

    roster, warnings = _generate_roster_sidecar(consumer_repo)

    commands = [
        guard["command"] for guard in roster["events"]["PreToolUse"]["matchers"]["Bash"]
    ]
    assert any(command.endswith("block_demo.sh") for command in commands)
    assert any(command.endswith("block_other.sh") for command in commands)
    repeated = [w for w in warnings if "PreToolUse" in w and "Bash" in w]
    assert len(repeated) == 1


def test_subagent_toml_schema_and_prompt_pair(consumer_repo: Path) -> None:
    """Each shipped agent yields a valid subagent TOML plus its prompt file.

    SCENARIO: a consumer repo with no sources of its own, so the agents
    come from the installed package — the consumer case, and now the only
    case, since a repo's own directory of the same name is deliberately
    ignored so a repo cannot substitute forge's definitions.
    EXPECTED BEHAVIOR: the TOML names the published (prefixed) agent and
    its matching prompt id, and the prompt file carries the definition.
    """
    assert install_vibe(consumer_repo) == 0
    published = generated_name("design-checker")
    toml_path = consumer_repo / ".vibe" / "agents" / f"{published}.toml"
    with toml_path.open("rb") as f:
        data = tomllib.load(f)
    assert data["agent_type"] == "subagent"
    assert data["system_prompt_id"] == published
    assert data["description"]
    prompt = consumer_repo / ".vibe" / "prompts" / f"{published}.md"
    assert prompt.is_file()
    body = prompt.read_text(encoding="utf-8")
    # The prompt is the packaged definition with its references rewritten,
    # so no `forge:`-marked reference survives for the harness to fail on.
    # Its own frontmatter `name:` is deliberately NOT rewritten: Vibe
    # resolves the agent through the TOML's `system_prompt_id` asserted
    # above, so the prompt's own field is inert text.
    assert "forge:precommit-fixer" not in body
    assert "forge-precommit-fixer" in body


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


def test_agents_md_install_then_verify_agree_on_mapped_content(
    consumer_repo: Path,
) -> None:
    """Verify accepts the AGENTS.md the installer just wrote after name mapping.

    Behavior: the writer and the drift check must share one composer; if
    verify compared against unmapped text it would flag a fresh install.
    """
    (consumer_repo / "FOUNDATION.md").write_text(
        "# Foundation\n\nSpawn forge:design-checker.\n"
        "<!-- forge:state:begin -->\nx\n<!-- forge:state:end -->\n",
        encoding="utf-8",
    )
    assert install_vibe(consumer_repo) == 0
    assert install_vibe(consumer_repo, verify_mode=True) == 0


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


def test_subagent_toml_pins_active_model_from_tier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An agent with model: sonnet gets active_model = forge-sonnet.

    MOCK SETUP: `install_vibe.shipped_path` is redirected to this repo's
    own directories, so the test can supply one agent with a known tier.
    In production that function always reads the installed package and
    ignores a repo's directories, so a repo cannot substitute forge's
    definitions — the controlled input is the test's, not a behaviour.
    """
    root = tmp_path / "c"
    root.mkdir(parents=True)
    monkeypatch.setattr(
        install_vibe_module,
        "shipped_path",
        lambda rel: root / rel,
    )
    agents = root / "agents"
    agents.mkdir()
    (agents / "a.md").write_text(
        "---\nname: a\ndescription: d\nmodel: sonnet\n---\nbody\n"
    )
    assert install_vibe(root) == 0
    toml_text = (root / ".vibe" / "agents" / f"{generated_name('a')}.toml").read_text()
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
        published = root / ".vibe" / "agents" / f"{generated_name('prior-art')}.toml"
        assert published.is_file()
        assert not (root / ".vibe" / "agents" / "prior-art.toml").exists()
        assert (root / ".vibe" / "hooks.toml").is_file()
        assert (root / ".vibe" / "forge-roster.json").is_file()
        skills = list((root / ".agents" / "skills").iterdir())
        assert len(skills) == 19

    def test_generated_vibe_output_has_no_unresolvable_spawn_form(
        self, tmp_path: Path
    ) -> None:
        """No generated Vibe file names a shipped agent/skill as ``forge:<name>``.

        Behavior regression: Vibe has no plugin namespace, so a verbatim
        ``forge:<agent>`` spawn instruction cannot resolve. The name oracle
        is the packaged file stems, never ``shipped_plugin_names()`` — the
        roster is the code under test.
        """
        root = tmp_path / "bare"
        root.mkdir()
        (root / "pyproject.toml").write_text(
            '[project]\nname = "bare"\nversion = "0.1.0"\n', encoding="utf-8"
        )
        (root / "FOUNDATION.md").write_text(
            "# Foundation\n\nSpawn forge:design-checker.\n"
            "<!-- forge:state:begin -->\nbody\n<!-- forge:state:end -->\n"
            "Literal placeholder forge:<name> stays.\n",
            encoding="utf-8",
        )
        (root / "CLAUDE.md").write_text("# Rules\n\n@FOUNDATION.md\n", encoding="utf-8")
        agents_src = shipped_path("agents")
        skills_src = shipped_path("skills")
        names = {p.stem for p in agents_src.glob("*.md")} | {
            p.name for p in skills_src.iterdir() if p.is_dir()
        }
        reference = re.compile(r"forge:([\w-]+)")

        def shipped_refs(text: str) -> list[str]:
            return [m for m in reference.findall(text) if m in names]

        sources = [*agents_src.glob("*.md"), *skills_src.glob("*/SKILL.md")]
        # B: precondition — the oracle is non-empty and the sources really
        # carry shipped references, so A cannot pass vacuously.
        assert names
        assert any(shipped_refs(p.read_text(encoding="utf-8")) for p in sources)

        assert install_vibe(root) == 0
        generated = [
            *(root / ".vibe" / "prompts").glob("*.md"),
            *(root / ".agents" / "skills").glob("*/SKILL.md"),
            root / "AGENTS.md",
        ]
        assert generated
        for path in generated:
            text = path.read_text(encoding="utf-8")
            # A: nothing shipped keeps the unresolvable namespaced form.
            assert shipped_refs(text) == [], path
        # C: the bare prefix is never rewritten — only shipped names are.
        instructions = (root / "AGENTS.md").read_text(encoding="utf-8")
        assert "<!-- forge:state:begin -->" in instructions
        assert "<!-- forge:state:end -->" in instructions
        assert "forge:<name>" in instructions
        assert f"Spawn {generated_name('design-checker')}." in instructions

    def test_subagent_description_has_no_namespaced_reference(
        self, tmp_path: Path
    ) -> None:
        """The packaged test-writer subagent description names no ``forge:`` token.

        Behavior: a description is a routing trigger and may name the peer
        agent to reach for, so it must carry a name this harness resolves.
        """
        root = tmp_path / "bare"
        root.mkdir()
        (root / "pyproject.toml").write_text(
            '[project]\nname = "bare"\nversion = "0.1.0"\n', encoding="utf-8"
        )
        assert install_vibe(root) == 0
        toml_path = root / ".vibe" / "agents" / f"{generated_name('test-writer')}.toml"
        with toml_path.open("rb") as f:
            description = tomllib.load(f)["description"]
        assert "test-advisor" in description
        assert "forge:" not in description

    def test_bare_consumer_verify_passes_after_install(self, tmp_path: Path) -> None:
        """--verify is green on a bare consumer once install ran."""
        root = tmp_path / "bare"
        root.mkdir()
        (root / "pyproject.toml").write_text(
            '[project]\nname = "bare"\nversion = "0.1.0"\n', encoding="utf-8"
        )
        assert install_vibe(root) == 0
        assert install_vibe(root, verify_mode=True) == 0


def test_validate_shipped_scripts_passes_on_real_package() -> None:
    """The packaged wiring and scripts agree: every wired script ships."""
    needed = hook_registry.validate_shipped_scripts()
    assert hook_registry.script_names() <= needed


def test_validate_shipped_scripts_raises_on_missing_wired_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wired script absent from the shipped dir fails the validation loudly."""
    source = tmp_path / "hooks"
    source.mkdir()
    (source / "a.sh").write_text("echo a\n", encoding="utf-8")
    monkeypatch.setattr(
        hook_registry, "script_names", lambda: frozenset({"a.sh", "gone.sh"})
    )
    with pytest.raises(hook_registry.HookWiringError, match="gone\\.sh"):
        hook_registry.validate_shipped_scripts(source)


def test_install_fails_closed_and_writes_nothing_when_guard_missing(
    consumer_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """install-forge-vibe exits 2 and writes nothing when a wired script is missing.

    SCENARIO: the packaged hooks dir is mirrored minus one wired script;
    the install must refuse before writing hooks.toml or the roster.
    EXPECTED BEHAVIOR: exit 2, stderr names the missing script and the
    reinstall command, and neither the hooks file nor the roster sidecar
    exists afterwards.
    """
    real = shipped_path(hook_registry.HOOKS_DIR)
    mirror = tmp_path / "mirror"
    mirror.mkdir()
    victim = "block_force_push.sh"
    assert victim in hook_registry.script_names()
    for p in real.glob("*.sh"):
        if p.name != victim:
            (mirror / p.name).write_text(
                p.read_text(encoding="utf-8"), encoding="utf-8"
            )
    monkeypatch.setattr(
        hook_registry,
        "shipped_path",
        lambda rel: mirror if rel == hook_registry.HOOKS_DIR else real.parent / rel,
    )
    assert install_vibe(consumer_repo) == 2
    err = capsys.readouterr().err
    assert victim in err
    assert "Reinstall forge-scripts" in err
    vibe_dir = consumer_repo / ".vibe"
    assert not (vibe_dir / "hooks.toml").exists()
    assert not (vibe_dir / "forge-roster.json").exists()


def _mirror_without(tmp_path: Path, victim: str) -> Path:
    """Copy the packaged hooks dir minus *victim*.

    Args:
        tmp_path: pytest tmp dir the mirror is created under.
        victim: Script file name to leave out of the mirror.

    Returns:
        The mirror directory path.
    """
    real = shipped_path(hook_registry.HOOKS_DIR)
    mirror = tmp_path / "mirror"
    mirror.mkdir()
    for p in real.glob("*.sh"):
        if p.name != victim:
            (mirror / p.name).write_text(
                p.read_text(encoding="utf-8"), encoding="utf-8"
            )
    return mirror


def _patched_source(monkeypatch: pytest.MonkeyPatch, mirror: Path) -> None:
    """Point hook_registry's shipped_path at *mirror* for the hooks dir.

    Args:
        monkeypatch: pytest monkeypatch fixture.
        mirror: Directory to serve as the packaged hooks dir.
    """
    real = shipped_path(hook_registry.HOOKS_DIR)
    monkeypatch.setattr(
        hook_registry,
        "shipped_path",
        lambda rel: mirror if rel == hook_registry.HOOKS_DIR else real.parent / rel,
    )


def test_validate_shipped_scripts_raises_on_missing_sourced_helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing sourced helper fails validation, not just a missing wired script.

    git_anchor.sh is sourced by most guards but wired nowhere; discovering
    helpers by on-disk glob would make its absence invisible. The
    reference must come from the wired scripts' text.
    """
    victim = "git_anchor.sh"
    real = shipped_path(hook_registry.HOOKS_DIR)
    wired_refs = hook_registry.required_scripts(real, hook_registry.script_names())
    assert victim in wired_refs  # wired scripts reference it in their text
    mirror = _mirror_without(tmp_path, victim)
    _patched_source(monkeypatch, mirror)
    with pytest.raises(hook_registry.HookWiringError, match="git_anchor\\.sh"):
        hook_registry.validate_shipped_scripts()


def test_validate_shipped_scripts_raises_on_missing_vibe_extra(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Vibe-only extra hooks are guard surface too: their scripts are validated."""
    victim = "vibe_session_start.sh"
    assert victim in hook_registry._vibe_extra_script_names()
    mirror = _mirror_without(tmp_path, victim)
    _patched_source(monkeypatch, mirror)
    with pytest.raises(hook_registry.HookWiringError, match="vibe_session_start\\.sh"):
        hook_registry.validate_shipped_scripts()
