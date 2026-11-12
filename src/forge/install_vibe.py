"""install-forge-vibe — install and configure Mistral Vibe integration.

This module provides the main installer that sets up Vibe integration for
forge, including:

1. Generating .vibe/hooks.toml from .claude-plugin/plugin.json
2. Generating .vibe/forge-roster.json (roster sidecar for dispatcher)
3. Publishing skills to .agents/skills/
4. Generating AGENTS.md (if not already generated)
5. Generating subagent TOML + prompt pairs from agents/*.md
6. Printing trust requirement and unenforced protections

Installs the Vibe integration:
- Writes .vibe/hooks.toml and the roster sidecar
- Publishes skills
- Generates AGENTS.md
- Generates 13 subagent TOML + prompt pairs from agents/*.md
- Prints the trust requirement and every unenforced protection (Section 6)

Wire into install_bootstrap.STEPS behind an opt-in [tool.forge] gate.
Generated artifacts stay gitignored.

Usage:
    install-forge-vibe              # Full installation
    install-forge-vibe --check      # Dry-run, report what would be done
    install-forge-vibe --verify     # Verify existing installation
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
import tomllib
from pathlib import Path

from forge.agent_harness import (
    UNENFORCED_VIBE_HOOKS,
    get_harness_profile,
    resolve_hooks_base,
)
from forge.gen_vibe_agents import generate_vibe_agents
from forge.gen_vibe_hooks import verify_existing_hooks, write_vibe_hooks
from forge.git_utils import configure_cli_logging
from forge.instruction_files import flatten_agents_md


configure_cli_logging()
logger = logging.getLogger(__name__)


# Unenforced protections — single-sourced from the harness profile
UNENFORCED_PROTECTIONS: list[tuple[str, str]] = list(UNENFORCED_VIBE_HOOKS.items())

# Trust requirement
TRUST_REQUIREMENT = (
    "TRUST REQUIREMENT: .vibe/ and AGENTS.md only load for trusted folders. "
    "An untrusted checkout skips them silently — no guards, no rules, "
    "nothing on screen. "
    "Use forge-doctor to check trust status."
)


def _generate_roster_sidecar(
    plugin_json_path: Path, repo_root: Path
) -> tuple[dict, list[str]]:
    """Generate the roster sidecar from plugin.json.

    The roster sidecar is used by the dispatcher at runtime to avoid
    reading plugin.json (which has ${CLAUDE_PLUGIN_ROOT} placeholders).

    Args:
        plugin_json_path: Path to .claude-plugin/plugin.json.
        repo_root: Repository root directory.

    Returns:
        Tuple of (roster_dict, warnings).
    """
    warnings: list[str] = []

    with plugin_json_path.open("rb") as f:
        plugin_json = json.load(f)

    hooks = plugin_json.get("hooks", {})
    plugin_root = plugin_json_path.parent

    # In the forge repo itself, claude-hooks are at repo root, not in .claude-plugin
    # Check if this is a forge development checkout
    hooks_base = resolve_hooks_base(repo_root, plugin_root)

    # Build roster structure: {events: {claude_event: {matchers: {matcher: [hooks]}}}}
    roster: dict = {
        "project_root": str(repo_root),
        "events": {},
    }

    for event, event_config in hooks.items():
        roster["events"][event] = {"matchers": {}}

        for matcher_config in event_config:
            if not isinstance(matcher_config, dict):
                continue

            # Expand hooks with ${CLAUDE_PLUGIN_ROOT} placeholders
            expanded_hooks = []
            for hook in matcher_config.get("hooks", []):
                if not isinstance(hook, dict):
                    continue
                expanded_hook = dict(hook)
                if "command" in expanded_hook:
                    cmd = expanded_hook["command"]
                    # Replace ${CLAUDE_PLUGIN_ROOT} with the appropriate base
                    expanded_cmd = cmd.replace("${CLAUDE_PLUGIN_ROOT}", str(hooks_base))
                    expanded_hook["command"] = expanded_cmd
                expanded_hooks.append(expanded_hook)

            if "matcher" in matcher_config:
                matcher = matcher_config["matcher"]
                roster["events"][event]["matchers"][matcher] = expanded_hooks
            else:
                # No matcher specified - applies to all
                roster["events"][event]["matchers"][""] = expanded_hooks

    return roster, warnings


def _write_roster_sidecar(roster: dict, repo_root: Path) -> tuple[Path, int]:
    """Write the roster sidecar to .vibe/forge-roster.json.

    Args:
        roster: The roster dict to write.
        repo_root: Repository root directory.

    Returns:
        Tuple of (output_path, hook_count).
    """
    output_path = repo_root / ".vibe" / "forge-roster.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with output_path.open("w", encoding="utf-8") as f:
        json.dump(roster, f, indent=2)

    # Count hooks
    hook_count = 0
    for event_data in roster.get("events", {}).values():
        for matcher_hooks in event_data.get("matchers", {}).values():
            hook_count += len(matcher_hooks)

    return output_path, hook_count


def _extract_frontmatter_field(content: str, field: str) -> str:
    """Extract a field from YAML frontmatter in an agent markdown file.

    Args:
        content: The full markdown file content.
        field: The frontmatter field name to extract.

    Returns:
        The field value, or empty string if not found.
    """
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return ""
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if line.startswith(f"{field}:"):
            return line.split(":", 1)[1].strip().strip('"').strip("'")
    return ""


def _generate_subagent_toml(agent_file: Path) -> tuple[str, str]:
    """Generate a Vibe subagent TOML file from an agent.md file.

    Vibe discovers custom subagents at .vibe/agents/NAME.toml with:
    agent_type = "subagent", a description, and system_prompt_id pointing
    at .vibe/prompts/NAME.md.

    Args:
        agent_file: Path to the agent.md file.

    Returns:
        Tuple of (toml_content, prompt_id).
    """
    content = agent_file.read_text(encoding="utf-8")
    agent_name = agent_file.stem
    description = _extract_frontmatter_field(content, "description") or (
        f"Forge {agent_name} agent"
    )
    # Escape double quotes for TOML
    description = description.replace('"', '\\"')
    prompt_id = agent_name
    model_tier = _extract_frontmatter_field(content, "model")

    toml_content = (
        f'agent_type = "subagent"\n'
        f'description = "{description}"\n'
        f'system_prompt_id = "{prompt_id}"\n'
    )
    alias = (
        get_harness_profile("vibe").model_alias_map.get(model_tier)
        if model_tier
        else None
    )
    if alias:
        # Pin the tier to the forge-owned preset alias that
        # _write_model_presets installs into .vibe/config.toml — the alias
        # is guaranteed to resolve because we write it ourselves.
        toml_content += f'active_model = "{alias}"\n'
    elif model_tier:
        toml_content += (
            f"# forge model tier: {model_tier} "
            "(no forge preset; inherits session model)\n"
        )

    return toml_content, prompt_id


def _generate_prompt_file(agent_file: Path) -> str:
    """Generate a prompt file from an agent.md file.

    The agent's markdown is used verbatim. The terse-output rule is not
    appended here — it is required in every agent source by
    agents/_TEMPLATE.md and enforced by forge-audit-agents, so generated
    prompts carry it from the source (Vibe has no SessionStart/SubagentStart
    hook to enforce a voice on subagents).

    Args:
        agent_file: Path to the agent.md file.

    Returns:
        Prompt content.
    """
    return agent_file.read_text(encoding="utf-8")


def _publish_subagents(
    agents_dir: Path, repo_root: Path, *, check_mode: bool = False
) -> tuple[int, list[str]]:
    """Generate subagent TOML + prompt pairs from agents/*.md.

    Args:
        agents_dir: Path to agents directory.
        repo_root: Repository root directory.
        check_mode: When True, report only; write nothing.

    Returns:
        Tuple of (count, warnings).
    """
    warnings: list[str] = []
    count = 0

    prompts_dir = repo_root / ".vibe" / "prompts"
    agents_toml_dir = repo_root / ".vibe" / "agents"
    if not check_mode:
        prompts_dir.mkdir(parents=True, exist_ok=True)
        agents_toml_dir.mkdir(parents=True, exist_ok=True)

    for agent_file in sorted(agents_dir.glob("*.md")):
        # Skip template
        if agent_file.name == "_TEMPLATE.md":
            continue

        # Generate TOML
        toml_content, prompt_id = _generate_subagent_toml(agent_file)

        # Write TOML to .vibe/agents/NAME.toml (Vibe discovery location)
        agent_name = agent_file.stem
        toml_path = agents_toml_dir / f"{agent_name}.toml"
        if not check_mode:
            toml_path.write_text(toml_content, encoding="utf-8")

        # Clean up legacy .vibe/NAME.toml from older generator versions
        legacy_path = repo_root / ".vibe" / f"{agent_name}.toml"
        if legacy_path.exists() and not check_mode:
            legacy_path.unlink()

        # Generate and write prompt
        prompt_content = _generate_prompt_file(agent_file)
        prompt_path = prompts_dir / f"{prompt_id}.md"
        if not check_mode:
            prompt_path.write_text(prompt_content, encoding="utf-8")

        count += 1

    return count, warnings


def _write_if_changed(path: Path, content: str) -> bool:
    """Write content to path only when it differs; return True when written.

    Args:
        path: Target file.
        content: New content.

    Returns:
        True if the file was created or updated, False when already in sync.
    """
    if path.is_file() and path.read_text(encoding="utf-8") == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return True


_GITIGNORE_ENTRIES = (".vibe/", ".agents/", "AGENTS.md")


def _ensure_gitignored(repo_root: Path) -> list[str]:
    """Ensure the generated Vibe artifacts are gitignored in this repo.

    Args:
        repo_root: Repository root.

    Returns:
        Entries that were added to .gitignore (empty when already covered).
    """
    gitignore = repo_root / ".gitignore"
    existing = (
        gitignore.read_text(encoding="utf-8").splitlines()
        if gitignore.is_file()
        else []
    )
    missing = [e for e in _GITIGNORE_ENTRIES if e not in existing]
    if missing:
        with gitignore.open("a", encoding="utf-8") as f:
            if existing and existing[-1] != "":
                f.write("\n")
            f.write("# Generated by install-forge-vibe\n")
            for entry in missing:
                f.write(f"{entry}\n")
    return missing


def _warn(warnings: list[str]) -> None:
    """Write each warning to stderr.

    Args:
        warnings: Warning messages to print.
    """
    for warning in warnings:
        sys.stderr.write(f"Warning: {warning}\n")


def _install_agents_md(repo_root: Path) -> bool:
    """Regenerate AGENTS.md from FOUNDATION.md + CLAUDE.md with includes resolved.

    Always regenerates (idempotent via _write_if_changed) so a checkout,
    pull or merge that touched the sources never leaves a stale
    instruction surface. Returns None-safe: when either input is missing,
    AGENTS.md is left alone.

    Args:
        repo_root: Repository root directory.

    Returns:
        True if AGENTS.md was created or updated.
    """
    content = flatten_agents_md(repo_root)
    if content is None:
        return False
    return _write_if_changed(repo_root / "AGENTS.md", content)


def _print_trust_summary(*, restart_needed: bool) -> None:
    """Print the trust requirement and unenforced protections to stderr.

    Args:
        restart_needed: When True, also print the session-restart notice —
            Vibe pins hooks and agents at startup, so regenerated artifacts
            only take effect after a restart.
    """
    sys.stderr.write("Trust requirement: This repository must be a trusted folder")
    sys.stderr.write("for Vibe to load .vibe/hooks.toml and AGENTS.md.\n")
    sys.stderr.write("\nUnenforced protections:\n")
    for hook, reason in UNENFORCED_PROTECTIONS:
        sys.stderr.write(f"  - {hook}: {reason}\n")
    if restart_needed:
        sys.stderr.write(
            "\nNOTE: generated Vibe artifacts (.vibe/ hooks+agents, .agents/ skills, "
            "AGENTS.md) changed. Vibe pins hooks and agents at session start — "
            "restart the Vibe session (or /reload) to pick them up.\n"
        )


def _artifact_snapshot(repo_root: Path) -> dict[str, str]:
    """Map every generated Vibe artifact path to its content hash.

    Args:
        repo_root: Repository root.

    Returns:
        Dict of relative path -> sha256 hex for all existing artifacts.
    """
    snapshot: dict[str, str] = {}
    roots = [
        repo_root / ".vibe",
        repo_root / ".agents",
        repo_root / "AGENTS.md",
        repo_root / ".gitignore",
    ]
    for root in roots:
        if root.is_file():
            snapshot[str(root.relative_to(repo_root))] = hashlib.sha256(
                root.read_bytes()
            ).hexdigest()
        elif root.is_dir():
            for f in sorted(root.rglob("*")):
                if f.is_file():
                    snapshot[str(f.relative_to(repo_root))] = hashlib.sha256(
                        f.read_bytes()
                    ).hexdigest()
    return snapshot


_MODEL_PRESET_PROVIDER = "mistral"

# Marker delimiting the forge-owned block in .vibe/config.toml: everything
# from the marker line on is regenerated, everything before it is user-owned.
_MODEL_PRESET_MARKER = "# forge-owned model presets for subagent active_model pins"

_MODEL_PRESETS: tuple[tuple[str, str], ...] = (
    ("forge-haiku", "ministral-8b-latest"),
    ("forge-sonnet", "mistral-small-latest"),
    ("forge-opus", "mistral-large-latest"),
)


def _configured_aliases(text: str) -> set[str]:
    """Collect the aliases of every model entry in a config.toml fragment.

    Args:
        text: TOML text to parse.

    Returns:
        Aliases (falling back to name) of all [[models]] entries; empty when
        the text is unparseable or has no models.
    """
    try:
        models = tomllib.loads(text).get("models", [])
    except tomllib.TOMLDecodeError:
        return set()
    if isinstance(models, dict):
        return {str(alias) for alias in models}
    aliases: set[str] = set()
    for entry in models:
        if isinstance(entry, dict):
            alias = entry.get("alias", entry.get("name"))
            if isinstance(alias, str):
                aliases.add(alias)
    return aliases


def _write_model_presets(repo_root: Path) -> bool:
    """Write forge-owned [[models]] presets into .vibe/config.toml.

    Each preset carries name, provider and alias: Vibe counts a [[models]]
    entry as a whole model only when both name and provider are present, and
    silently drops the rest as orphan overrides — which then rejects every
    generated agent file pinning that alias at load. The forge-owned block
    (from the marker line on) is regenerated whole, so entries written by
    older forge versions without provider are repaired; content before the
    marker is user-owned and never touched, and a preset whose alias already
    appears there is skipped.

    Args:
        repo_root: Repository root.

    Returns:
        True if the file was created or changed.
    """
    config_path = repo_root / ".vibe" / "config.toml"
    existing = config_path.read_text(encoding="utf-8") if config_path.is_file() else ""
    head, marker_found, _old_block = existing.partition(_MODEL_PRESET_MARKER)
    configured = _configured_aliases(head)
    additions = "".join(
        f'\n[[models]]\nname = "{name}"\nprovider = "{_MODEL_PRESET_PROVIDER}"\n'
        f'alias = "{alias}"\n'
        for alias, name in _MODEL_PRESETS
        if alias not in configured
    )
    if marker_found:
        new_content = head if head.endswith("\n") or not head else head + "\n"
        if additions:
            new_content += _MODEL_PRESET_MARKER + "\n" + additions
    else:
        if not additions:
            return False
        new_content = (
            existing if existing.endswith("\n") or not existing else existing + "\n"
        )
        new_content += _MODEL_PRESET_MARKER + "\n" + additions
    if new_content == existing:
        return False
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(new_content, encoding="utf-8")
    return True


def install_vibe(
    repo_root: Path | None = None,
    *,
    check_mode: bool = False,
    verify_mode: bool = False,
) -> int:
    """Install Vibe integration for forge.

    Args:
        repo_root: Repository root directory.
        check_mode: If True, print what would be done without writing.
        verify_mode: If True, verify existing installation (exit 1 on drift).

    Returns:
        Exit code (0 = success, 1 = error or drift in verify mode).
    """
    if repo_root is None:
        repo_root = Path.cwd()

    if verify_mode:
        return _verify_vibe_installation(repo_root)

    plugin_json_path = repo_root / ".claude-plugin" / "plugin.json"
    if not plugin_json_path.exists():
        return 1

    before = _artifact_snapshot(repo_root)

    # Step 1: Generate .vibe/hooks.toml
    if write_vibe_hooks(repo_root, check_mode=check_mode) != 0:
        return 1

    # Step 2: Generate roster sidecar
    roster, roster_warnings = _generate_roster_sidecar(plugin_json_path, repo_root)
    if not check_mode:
        _write_roster_sidecar(roster, repo_root)
    _warn(roster_warnings)

    # Step 3: Publish skills
    _skills_count, skills_warnings, _references = generate_vibe_agents(
        repo_root, check_mode=check_mode
    )
    _warn(skills_warnings)

    # Step 4: Generate subagent TOMLs and prompts
    agents_dir = repo_root / "agents"
    if agents_dir.exists():
        _subagent_count, subagent_warnings = _publish_subagents(
            agents_dir, repo_root, check_mode=check_mode
        )
        _warn(subagent_warnings)

    # Step 5: Regenerate AGENTS.md (always; idempotent write)
    if not check_mode:
        _install_agents_md(repo_root)

    # Step 5b: keep the generated artifacts gitignored in consumer repos
    if not check_mode:
        _ensure_gitignored(repo_root)
        _write_model_presets(repo_root)

    # Step 6: Print trust requirement, unenforced protections, restart notice
    changed = _artifact_snapshot(repo_root) != before
    _print_trust_summary(restart_needed=changed and not check_mode)

    return 0


def _missing_artifact_issues(repo_root: Path) -> list[str]:
    """Check the required Vibe artifact paths exist and are non-empty.

    Args:
        repo_root: Repository root directory.

    Returns:
        Issue labels for every missing or empty artifact path.
    """
    issues: list[str] = []

    required_paths = [
        repo_root / ".vibe" / "hooks.toml",
        repo_root / ".vibe" / "forge-roster.json",
        repo_root / ".agents" / "skills",
        repo_root / ".vibe" / "prompts",
        repo_root / ".vibe" / "agents",
    ]
    issues.extend(f"Missing: {path}" for path in required_paths if not path.exists())

    # The skills and subagent directories must also hold content
    skills_dir = repo_root / ".agents" / "skills"
    if skills_dir.exists() and not any(skills_dir.iterdir()):
        issues.append(f"Empty: {skills_dir}")
    agents_toml_dir = repo_root / ".vibe" / "agents"
    if agents_toml_dir.exists() and not any(agents_toml_dir.glob("*.toml")):
        issues.append(f"Empty: {agents_toml_dir}")

    return issues


def _verify_vibe_installation(repo_root: Path | None = None) -> int:
    """Verify existing Vibe installation.

    Args:
        repo_root: Repository root directory.

    Returns:
        Exit code (0 = all good, 1 = issues found).
    """
    if repo_root is None:
        repo_root = Path.cwd()

    issues = _missing_artifact_issues(repo_root)

    # AGENTS.md content drift: present but stale vs the flattened sources
    agents_md = repo_root / "AGENTS.md"
    expected = flatten_agents_md(repo_root)
    if (
        agents_md.is_file()
        and expected is not None
        and agents_md.read_text(encoding="utf-8") != expected
    ):
        issues.append(
            "AGENTS.md is stale vs FOUNDATION.md + CLAUDE.md — run `install-forge-vibe`"
        )

    # Verify hooks.toml has strict=true on blocking entries
    if verify_existing_hooks(repo_root) != 0:
        issues.append("hooks.toml has blocking entries missing strict=true")

    if not issues:
        return 0
    for issue in issues:
        sys.stderr.write(f"Verification issue: {issue}\n")
    return 1


def main() -> int:
    """CLI entry point for install-forge-vibe.

    Returns:
        Exit code.
    """
    parser = argparse.ArgumentParser(
        prog="install-forge-vibe",
        description=(
            "Install Mistral Vibe integration for forge. Generates .vibe/hooks.toml, "
            ".vibe/forge-roster.json, publishes skills to .agents/skills/, "
            "generates subagent configs, regenerates AGENTS.md (recursive @-includes), "
            "ensures gitignore entries, writes forge-* [[models]] presets to "
            ".vibe/config.toml, and prints trust requirements plus a "
            "session-restart notice when generated artifacts changed."
        ),
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Dry-run: report what would be done without writing.",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help=(
            "Verify existing Vibe installation; exit 1 on missing or stale "
            "artifacts (including AGENTS.md drift vs FOUNDATION.md + CLAUDE.md)."
        ),
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help="Repository root directory (default: current directory).",
    )

    args = parser.parse_args()
    return install_vibe(args.repo_root, check_mode=args.check, verify_mode=args.verify)


if __name__ == "__main__":
    sys.exit(main())
