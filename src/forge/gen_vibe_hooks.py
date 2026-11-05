"""Generate .vibe/hooks.toml from .claude-plugin/plugin.json.

This module provides the generator that reads Claude Code's plugin.json
hooks block and emits an equivalent .vibe/hooks.toml configuration.

Key invariants (per VIBE-HANDOFF.md Section 4, Deliverable 3):

1. strict = true on every blocking entry. Vibe defaults it false, which
   turns a hook failure into a warning. Non-strict, a corrupted install
   disarms every guard and reports nothing.

2. match = "re:bash|process_start" on every Bash guard. Vibe routes
   agent-started background commands through process.start; a bare
   "bash" matcher misses them, so a backgrounded `git push --force` walks
   past every guard.

3. An unmapped matcher or event is an error, never a skip.

4. Entries with no Vibe equivalent are reported, never silently dropped.

5. The generator runs at install time, NOT at runtime. Runtime reads the
   generated file only.

Usage:
    python -m forge.gen_vibe_hooks [--check]
"""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

logger = logging.getLogger(__name__)


# Mapping from Claude Code event names to Vibe event names
# Per VIBE-HANDOFF.md Section 2:
# - PreToolUse -> pre_tool
# - PostToolUse -> post_tool
# - SubagentStop -> post_agent
# - SessionStart -> none (Vibe doesn't have this)
# - PreCompact -> none (Vibe doesn't have this)
# - SubagentStart -> none (Vibe doesn't have this)
CLAUDE_TO_VIBE_EVENT: Mapping[str, str | None] = {
    "PreToolUse": "pre_tool",
    "PostToolUse": "post_tool",
    "SubagentStop": "post_agent",
    "SessionStart": None,  # No Vibe equivalent
    "PreCompact": None,  # No Vibe equivalent
    "SubagentStart": None,  # No Vibe equivalent
}

# Mapping from Claude Code matcher names to Vibe matcher names
# Per VIBE-HANDOFF.md Section 2:
# - Bash -> "re:bash|process_start"
# - Edit -> "edit"
# - Write -> "write_file"
# Regex matchers (like "^(forge:)?precommit-fixer$") are passed through as-is
# but note that Vibe's post_agent doesn't support matchers, so these
# agent-specific matchers need special handling.
CLAUDE_TO_VIBE_MATCHER: Mapping[str, str] = {
    "Bash": "re:bash|process_start",
    "Edit": "edit",
    "Write": "write_file",
}

# Hooks that are explicitly unenforced in Vibe (per Section 6)
UNENFORCED_CLAUDE_HOOKS: frozenset[str] = frozenset(
    {
        "block_fixer_recon.sh",
        "require_fixer_verdict.sh",
        "caveman_mode.sh",
        "load_continuation.sh",
        "keep_continuation_state.sh",
        "log_agent_timing.sh",
    }
)

# Events that are blocking in Vibe
# Per VIBE-HANDOFF.md Section 6: Vibe has no SessionStart, PreCompact, or
# SubagentStart, so those hooks cannot be enforced. Only pre_tool, post_tool,
# and post_agent can block.
BLOCKING_VIBE_EVENTS: frozenset[str] = frozenset(
    {
        "pre_tool",
        "post_tool",
        "post_agent",
    }
)


@dataclass(frozen=True)
class _EventContext:
    """Translation context shared by all matcher groups of one Claude event.

    Attributes:
        vibe_event: The translated Vibe event name.
        is_blocking: Whether the event can block in Vibe.
        plugin_root: Plugin root for expanding ${CLAUDE_PLUGIN_ROOT}.
        repo_root: Repository root for relativizing paths.
    """

    vibe_event: str
    is_blocking: bool
    plugin_root: Path
    repo_root: Path


def _load_plugin_json(plugin_dir: Path) -> dict:
    """Load and parse plugin.json from the .claude-plugin directory.

    Args:
        plugin_dir: Path to the .claude-plugin directory.

    Returns:
        Parsed plugin.json dict.

    Raises:
        FileNotFoundError: If plugin.json doesn't exist.
        json.JSONDecodeError: If plugin.json is malformed.
    """
    plugin_json_path = plugin_dir / "plugin.json"
    if not plugin_json_path.exists():
        msg = f"plugin.json not found at {plugin_json_path}"
        raise FileNotFoundError(msg)
    with plugin_json_path.open("rb") as f:
        return json.load(f)


def _expand_claude_plugin_root(
    command: str, plugin_root: Path, relative_to: Path | None = None
) -> str:
    """Expand ${CLAUDE_PLUGIN_ROOT} in a command string.

    Args:
        command: The command string, potentially containing ${CLAUDE_PLUGIN_ROOT}.
        plugin_root: The path to the plugin root directory.
        relative_to: If provided, make the path relative to this directory.

    Returns:
        The command with ${CLAUDE_PLUGIN_ROOT} replaced. If relative_to is
        provided, the path is made relative; otherwise absolute.
    """
    expanded = command.replace("${CLAUDE_PLUGIN_ROOT}", str(plugin_root))

    if relative_to is not None:
        # Try to make it relative
        try:
            resolved = Path(expanded).resolve()
            rel_path = resolved.relative_to(relative_to.resolve())
            return str(rel_path)
        except (ValueError, OSError):
            # Can't make relative, keep absolute
            pass

    return expanded


def _get_claude_hooks(plugin_json: dict) -> dict:
    """Extract the hooks block from plugin.json.

    Args:
        plugin_json: Parsed plugin.json dict.

    Returns:
        The hooks dict from plugin.json.
    """
    return plugin_json.get("hooks", {})


def _get_hook_name(command: str) -> str:
    """Extract the hook script name from a command path.

    Args:
        command: Full command path.

    Returns:
        The script name (basename of the path).
    """
    return Path(command).name


def _is_unenforced_hook(command: str) -> bool:
    """Check if a hook is explicitly unenforced in Vibe.

    Args:
        command: Full command path.

    Returns:
        True if this hook cannot be enforced in Vibe.
    """
    return _get_hook_name(command) in UNENFORCED_CLAUDE_HOOKS


def _is_agent_matcher(matcher_name: str) -> bool:
    """Check whether a Claude matcher is an agent-name regex.

    Args:
        matcher_name: The Claude matcher string.

    Returns:
        True when the matcher is a regex over agent names rather than a
        plain tool name.
    """
    return matcher_name.startswith("^") or any(
        c in matcher_name for c in ["(", ")", "|", "$", "^"]
    )


def _iter_matcher_groups(event_config: list) -> Iterator[tuple[str | None, list]]:
    """Yield ``(matcher, hooks)`` per matcher config in an event config list.

    Args:
        event_config: The list of matcher configs for one Claude event.

    Yields:
        ``(matcher_name, hooks)`` — the item's "matcher" value, or None for
        matcher-less hook lists and bare hook entries.
    """
    for item in event_config:
        if not isinstance(item, dict):
            continue
        if "matcher" in item:
            yield item["matcher"], item.get("hooks", [])
        elif "hooks" in item:
            yield None, item["hooks"]
        elif "command" in item:
            yield None, [item]


def _unenforced_label(claude_event: str, matcher: str | None, hook_name: str) -> str:
    """Format the report label for a hook with no Vibe enforcement.

    Args:
        claude_event: The original Claude event name.
        matcher: The matcher name, or None for matcher-less groups.
        hook_name: The hook script basename.

    Returns:
        The label string.
    """
    if matcher:
        return f"{claude_event}[{matcher}]: {hook_name}"
    return f"{claude_event}: {hook_name}"


def _build_hook_entry(hook: dict, ctx: _EventContext, vibe_match: str | None) -> dict:
    """Build one Vibe hook entry from a Claude hook config.

    Args:
        hook: The Claude hook dict (must contain "command").
        ctx: The translation context for the event.
        vibe_match: The translated Vibe matcher, or None for no match.

    Returns:
        The Vibe hook entry dict.
    """
    entry: dict = {
        "name": _get_hook_name(hook["command"]),
        "type": ctx.vibe_event,
        "event": ctx.vibe_event,
        "command": _expand_claude_plugin_root(
            hook["command"], ctx.plugin_root, ctx.repo_root
        ),
        "timeout": hook.get("timeout", 5),
    }
    if vibe_match is not None:
        # For Bash matchers, Vibe needs the combined matcher
        entry["match"] = vibe_match
    if ctx.is_blocking:
        # Blocking hooks must have strict = true
        entry["strict"] = True
    return entry


def _translate_matcher_group(
    claude_event: str,
    matcher: str | None,
    hooks: list,
    ctx: _EventContext,
) -> tuple[list[dict], list[str]]:
    """Translate one matcher group's hooks to Vibe entries.

    Args:
        claude_event: The original Claude event name.
        matcher: The matcher name, or None for matcher-less groups.
        hooks: The list of hook dicts in this group.
        ctx: The translation context for the event.

    Returns:
        (entries, unenforced) — the Vibe hook entries and the labels of
        hooks with no Vibe enforcement.

    Raises:
        ValueError: When a non-agent matcher has no Vibe mapping
            (invariant 3: an unmapped matcher is an error, never a skip).
    """
    vibe_match: str | None = None
    if matcher is not None and not _is_agent_matcher(matcher):
        vibe_match = CLAUDE_TO_VIBE_MATCHER.get(matcher)
        if vibe_match is None:
            msg = f"Unmapped Claude matcher '{matcher}' for event '{claude_event}'"
            raise ValueError(msg)
    # Agent matchers don't translate to Vibe's tool-based matchers; Vibe's
    # post_agent takes no matchers, so those entries carry no match key.

    entries: list[dict] = []
    unenforced: list[str] = []
    for hook in hooks:
        if not isinstance(hook, dict) or "command" not in hook:
            continue
        hook_name = _get_hook_name(hook["command"])
        if _is_unenforced_hook(hook["command"]):
            unenforced.append(_unenforced_label(claude_event, matcher, hook_name))
            continue
        entries.append(_build_hook_entry(hook, ctx, vibe_match))
    return entries, unenforced


def _translate_hooks_to_vibe(
    hooks: dict, plugin_root: Path, repo_root: Path
) -> tuple[list[dict], list[str]]:
    """Translate Claude Code hooks to Vibe hooks.toml entries.

    Args:
        hooks: The hooks dict from plugin.json.
        plugin_root: The plugin root directory for expanding paths.
        repo_root: The repository root directory.

    Returns:
        A tuple of (vibe_hooks, unenforced_hooks) where:
        - vibe_hooks: List of dicts representing Vibe hook entries
        - unenforced_hooks: List of hook names that couldn't be mapped

    Raises:
        ValueError: When a matcher has no Vibe mapping (invariant 3).
    """
    vibe_hooks: list[dict] = []
    unenforced_hooks: list[str] = []

    for claude_event, event_config in hooks.items():
        # Translate event name
        vibe_event = CLAUDE_TO_VIBE_EVENT.get(claude_event)

        if vibe_event is None:
            # No Vibe equivalent for this event (SessionStart, PreCompact,
            # SubagentStart). Report all hooks as unenforced (per invariant 4)
            unenforced_hooks.extend(
                f"{claude_event}: {_get_hook_name(hook['command'])}"
                for _matcher, group_hooks in _iter_matcher_groups(event_config)
                for hook in group_hooks
                if isinstance(hook, dict) and "command" in hook
            )
            continue

        # Check if this event can block in Vibe
        ctx = _EventContext(
            vibe_event=vibe_event,
            is_blocking=vibe_event in BLOCKING_VIBE_EVENTS,
            plugin_root=plugin_root,
            repo_root=repo_root,
        )

        # event_config is a list of matcher configs
        for matcher, group_hooks in _iter_matcher_groups(event_config):
            entries, unenforced = _translate_matcher_group(
                claude_event, matcher, group_hooks, ctx
            )
            vibe_hooks.extend(entries)
            unenforced_hooks.extend(unenforced)

    return vibe_hooks, unenforced_hooks


def _group_hooks_by_event_and_match(
    hooks: list[dict],
) -> dict[str, dict[str, list[dict]]]:
    """Group hook entries by event and match for TOML structure.

    Vibe's hooks.toml structure:
    [[hooks]]
    event = "pre_tool"
    match = "re:bash|process_start"
    command = "..."

    Args:
        hooks: List of hook entry dicts.

    Returns:
        Nested dict: {event: {match: [hook_dicts]}}
        Hooks without a match are keyed by empty string.
    """
    result: dict[str, dict[str, list[dict]]] = {}

    for hook in hooks:
        event = hook["event"]
        match = hook.get("match", "")

        if event not in result:
            result[event] = {}
        if match not in result[event]:
            result[event][match] = []

        result[event][match].append(hook)

    return result


def _generate_hooks_toml(grouped_hooks: dict[str, dict[str, list[dict]]]) -> str:
    """Generate the TOML content for .vibe/hooks.toml.

    Args:
        grouped_hooks: Hooks grouped by event and match.

    Returns:
        TOML string content.
    """
    lines: list[str] = []
    lines.append("# Generated by forge-gen-vibe-hooks from .claude-plugin/plugin.json")
    lines.append("# Do not edit manually - edits will be overwritten on next install")
    lines.append("")

    for event in sorted(grouped_hooks.keys()):
        match_groups = grouped_hooks[event]

        for match in sorted(match_groups.keys()):
            hooks = match_groups[match]

            for hook in hooks:
                lines.append("[[hooks]]")
                if "name" in hook:
                    lines.append(f'name = "{hook["name"]}"')
                if "type" in hook:
                    lines.append(f'type = "{hook["type"]}"')
                lines.append(f'event = "{event}"')

                if match:
                    lines.append(f'match = "{match}"')

                lines.append(f'command = "{hook["command"]}"')
                lines.append(f"timeout = {hook['timeout']}")

                # Add strict = true for blocking events
                if hook.get("strict", False):
                    lines.append("strict = true")

                lines.append("")

    return "\n".join(lines)


def generate_vibe_hooks(
    repo_root: Path | None = None,
) -> tuple[str, list[str], list[str]]:
    """Generate .vibe/hooks.toml from .claude-plugin/plugin.json.

    Args:
        repo_root: Repository root directory. If None, uses current directory.

    Returns:
        Tuple of (toml_content, unenforced_hooks, warnings):
        - toml_content: The generated TOML string
        - unenforced_hooks: List of hook names that couldn't be mapped to Vibe
        - warnings: List of warning messages

    Raises:
        FileNotFoundError: If plugin.json doesn't exist.
        ValueError: If there are unmapped matchers.
    """
    if repo_root is None:
        repo_root = Path.cwd()

    plugin_dir = repo_root / ".claude-plugin"

    # In the forge repo itself, claude-hooks are at repo root, not in .claude-plugin
    local_claude_hooks = repo_root / "claude-hooks"
    if local_claude_hooks.exists() and not (plugin_dir / "claude-hooks").exists():
        # Override plugin_dir to use local claude-hooks
        # We'll still read plugin.json from .claude-plugin but use local hooks path
        hooks_base = repo_root
    else:
        hooks_base = plugin_dir

    plugin_json = _load_plugin_json(plugin_dir)
    hooks = _get_claude_hooks(plugin_json)

    # Translate to Vibe format
    vibe_hooks, unenforced_hooks = _translate_hooks_to_vibe(
        hooks, hooks_base, repo_root
    )

    # Group for TOML structure
    grouped = _group_hooks_by_event_and_match(vibe_hooks)

    # Generate TOML
    toml_content = _generate_hooks_toml(grouped)

    warnings: list[str] = []

    # Add warnings for unenforced hooks
    if unenforced_hooks:
        warnings.append(
            f"Unenforced hooks (no Vibe equivalent): {', '.join(unenforced_hooks)}"
        )

    return toml_content, unenforced_hooks, warnings


def write_vibe_hooks(
    repo_root: Path | None = None,
    *,
    check_mode: bool = False,
) -> int:
    """Generate and write .vibe/hooks.toml.

    Args:
        repo_root: Repository root directory.
        check_mode: If True, print what would be done without writing.

    Returns:
        Exit code (0 = success, 1 = error).
    """
    if repo_root is None:
        repo_root = Path.cwd()

    output_path = repo_root / ".vibe" / "hooks.toml"

    try:
        toml_content, _unenforced, warnings = generate_vibe_hooks(repo_root)
    except Exception:  # noqa: BLE001
        return 1
    else:
        if check_mode:
            return 0

        # Ensure .vibe directory exists
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # Write the file
        output_path.write_text(toml_content, encoding="utf-8")

        for warning in warnings:
            sys.stderr.write(f"Warning: {warning}\n")
        return 0


def verify_existing_hooks(repo_root: Path | None = None) -> int:
    """Verify an existing .vibe/hooks.toml has strict=true on blocking entries.

    This is a doctor check that catches hand edits and drift.

    Args:
        repo_root: Repository root directory.

    Returns:
        Exit code (0 = all good, 1 = violations found).
    """
    import tomllib

    if repo_root is None:
        repo_root = Path.cwd()

    hooks_path = repo_root / ".vibe" / "hooks.toml"

    if not hooks_path.exists():
        return 0

    violations: list[str] = []

    try:
        with hooks_path.open("rb") as f:
            hooks_toml = tomllib.load(f)

        for hook in hooks_toml.get("hooks", []):
            event = hook.get("event", "")
            is_blocking = event in BLOCKING_VIBE_EVENTS
            has_strict = hook.get("strict", False)

            if is_blocking and not has_strict:
                command = hook.get("command", "?")
                msg = f"Blocking event '{event}' missing strict=true: {command}"
                violations.append(msg)

        if violations:
            return 1
    except Exception:  # noqa: BLE001
        return 1
    else:
        return 0


def main() -> int:
    """CLI entry point for forge-gen-vibe-hooks.

    Returns:
        Exit code.
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="forge-gen-vibe-hooks",
        description="Generate .vibe/hooks.toml from .claude-plugin/plugin.json",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check mode: don't write, just report what would be done.",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Verify existing .vibe/hooks.toml has strict=true on blocking entries.",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help="Repository root directory (default: current directory).",
    )

    args = parser.parse_args()

    if args.verify:
        return verify_existing_hooks(args.repo_root)
    return write_vibe_hooks(args.repo_root, check_mode=args.check)


if __name__ == "__main__":
    sys.exit(main())
