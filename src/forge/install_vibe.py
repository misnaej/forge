"""install-forge-vibe — install and configure Mistral Vibe integration.

This module provides the main installer that sets up Vibe integration for
forge, including:

1. Generating .vibe/hooks.toml from .claude-plugin/plugin.json
2. Generating .vibe/forge-roster.json (roster sidecar for dispatcher)
3. Publishing skills to .agents/skills/
4. Generating AGENTS.md (if not already generated)
5. Generating subagent TOML + prompt pairs from agents/*.md
6. Printing trust requirement and unenforced protections

Per VIBE-HANDOFF.md Section 4, Deliverable 5:
- Writes .vibe/hooks.toml and the roster sidecar
- Publishes skills
- Generates AGENTS.md
- Generates 12 subagent TOML + prompt pairs from agents/*.md
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
import json
import logging
import sys
from pathlib import Path

from forge.gen_vibe_agents import generate_vibe_agents
from forge.gen_vibe_hooks import verify_existing_hooks, write_vibe_hooks
from forge.git_utils import configure_cli_logging


configure_cli_logging()
logger = logging.getLogger(__name__)


# Unenforced protections per VIBE-HANDOFF.md Section 6
# These hooks have no Vibe equivalent and cannot be enforced
UNENFORCED_PROTECTIONS: list[tuple[str, str]] = [
    (
        "require_fixer_verdict.sh",
        "SubagentStop blocking gate - Vibe has no SubagentStop, "
        "post_agent fires after turn",
    ),
    ("caveman_mode.sh", "SessionStart context injection - Vibe has no SessionStart"),
    (
        "load_continuation.sh",
        "SessionStart context injection - Vibe has no SessionStart",
    ),
    ("keep_continuation_state.sh", "PreCompact state write - Vibe has no PreCompact"),
    (
        "log_agent_timing.sh",
        "SubagentStart half - Vibe has no SubagentStart, partial at best",
    ),
]

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
    import json as json_module

    warnings: list[str] = []

    with plugin_json_path.open("rb") as f:
        plugin_json = json_module.load(f)

    hooks = plugin_json.get("hooks", {})
    plugin_root = plugin_json_path.parent

    # In the forge repo itself, claude-hooks are at repo root, not in .claude-plugin
    # Check if this is a forge development checkout
    local_claude_hooks = repo_root / "claude-hooks"
    if local_claude_hooks.exists() and not (plugin_root / "claude-hooks").exists():
        # Use local claude-hooks path
        hooks_base = repo_root
    else:
        # Use plugin directory
        hooks_base = plugin_root

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

    toml_content = (
        f'agent_type = "subagent"\n'
        f'description = "{description}"\n'
        f'system_prompt_id = "{prompt_id}"\n'
    )

    return toml_content, prompt_id


def _generate_prompt_file(agent_file: Path) -> str:
    """Generate a prompt file from an agent.md file.

    Args:
        agent_file: Path to the agent.md file.

    Returns:
        Prompt content.
    """
    # For now, just use the content as-is
    # In a full implementation, we'd extract the prompt section
    return agent_file.read_text(encoding="utf-8")


def _publish_subagents(agents_dir: Path, repo_root: Path) -> tuple[int, list[str]]:
    """Generate subagent TOML + prompt pairs from agents/*.md.

    Args:
        agents_dir: Path to agents directory.
        repo_root: Repository root directory.

    Returns:
        Tuple of (count, warnings).
    """
    warnings: list[str] = []
    count = 0

    prompts_dir = repo_root / ".vibe" / "prompts"
    prompts_dir.mkdir(parents=True, exist_ok=True)
    agents_toml_dir = repo_root / ".vibe" / "agents"
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
        toml_path.write_text(toml_content, encoding="utf-8")

        # Clean up legacy .vibe/NAME.toml from older generator versions
        legacy_path = repo_root / ".vibe" / f"{agent_name}.toml"
        if legacy_path.exists():
            legacy_path.unlink()

        # Generate and write prompt
        prompt_content = _generate_prompt_file(agent_file)
        prompt_path = prompts_dir / f"{prompt_id}.md"
        prompt_path.write_text(prompt_content, encoding="utf-8")

        count += 1

    return count, warnings


def _warn(warnings: list[str]) -> None:
    """Write each warning to stderr.

    Args:
        warnings: Warning messages to print.
    """
    for warning in warnings:
        sys.stderr.write(f"Warning: {warning}\n")


def _install_agents_md(repo_root: Path) -> None:
    """Generate AGENTS.md from FOUNDATION.md + CLAUDE.md when absent.

    Args:
        repo_root: Repository root directory.
    """
    agents_md_path = repo_root / "AGENTS.md"
    if agents_md_path.exists():
        return

    foundation_path = repo_root / "FOUNDATION.md"
    claude_md_path = repo_root / "CLAUDE.md"
    if not (foundation_path.exists() and claude_md_path.exists()):
        return

    # Flatten FOUNDATION.md + CLAUDE.md, dropping the @FOUNDATION.md directive
    foundation_content = foundation_path.read_text(encoding="utf-8")
    claude_content = claude_md_path.read_text(encoding="utf-8")
    claude_no_directive = "".join(
        line
        for line in claude_content.splitlines(keepends=True)
        if line.strip() != "@FOUNDATION.md"
    )
    agents_content = f"{foundation_content}\n\n---\n\n{claude_no_directive}"
    agents_md_path.write_text(agents_content, encoding="utf-8")


def _print_trust_summary() -> None:
    """Print the trust requirement and unenforced protections to stderr."""
    sys.stderr.write("Trust requirement: This repository must be a trusted folder")
    sys.stderr.write("for Vibe to load .vibe/hooks.toml and AGENTS.md.\n")
    sys.stderr.write("\nUnenforced protections:\n")
    for hook, reason in UNENFORCED_PROTECTIONS:
        sys.stderr.write(f"  - {hook}: {reason}\n")


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
        verify_mode: If True, verify existing installation.

    Returns:
        Exit code (0 = success, 1 = error).
    """
    if repo_root is None:
        repo_root = Path.cwd()

    if verify_mode:
        return _verify_vibe_installation(repo_root)

    plugin_json_path = repo_root / ".claude-plugin" / "plugin.json"
    if not plugin_json_path.exists():
        return 1

    if check_mode:
        return 0

    # Step 1: Generate .vibe/hooks.toml
    if write_vibe_hooks(repo_root, check_mode=False) != 0:
        return 1

    # Step 2: Generate roster sidecar
    roster, roster_warnings = _generate_roster_sidecar(plugin_json_path, repo_root)
    _write_roster_sidecar(roster, repo_root)
    _warn(roster_warnings)

    # Step 3: Publish skills
    _skills_count, skills_warnings, _references = generate_vibe_agents(
        repo_root, check_mode=False
    )
    _warn(skills_warnings)

    # Step 4: Generate subagent TOMLs and prompts
    agents_dir = repo_root / "agents"
    if agents_dir.exists():
        _subagent_count, subagent_warnings = _publish_subagents(agents_dir, repo_root)
        _warn(subagent_warnings)

    # Step 5: Generate AGENTS.md (if needed)
    _install_agents_md(repo_root)

    # Step 6: Print trust requirement and unenforced protections
    _print_trust_summary()

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
            "generates subagent configs, and prints trust requirements."
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
        help="Verify existing Vibe installation.",
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
