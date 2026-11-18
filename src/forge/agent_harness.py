"""Frozen harness profiles for agent runtime adapters.

This module provides pure-data profiles that describe how to translate
between different agent harnesses (Claude Code, Mistral Vibe) and forge's
internal guard contract.

Why this adapter exists (per FOUNDATION §7 and §16):
    Rewriting 31 shell guard scripts to a harness-neutral protocol would
    regress 9723 lines of contract tests to buy nothing. The interface
    (Claude Code's hook JSON schema and exit-2 contract) is outside our
    control for shipped guards. §7 permits layering when the interface is
    outside your control; §16 names the shipped-plugin case. Therefore we
    layer: this adapter maps harness-specific payloads to the guard's
    expected format and translates verdicts back to harness-native JSON.

This is NOT the place to detect which harness is running. The harness is
named explicitly by the caller (--harness vibe). Never sniffed from the
environment: a silent misdetection emits the wrong verdict format, which
reads as "allow". Do not add a detect() helper "for convenience" later.

An unknown harness name MUST hard-fail (FOUNDATION §2: fail loudly).
It must NOT default to 'claude'.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Final

from forge.instruction_files import AGENT_NAME_PREFIX, shipped_path
from forge.plugin_roster import shipped_plugin_names


if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping
    from pathlib import Path


class HarnessName(Enum):
    """Enumeration of supported agent harnesses."""

    CLAUDE = "claude"
    VIBE = "vibe"


@dataclass(frozen=True)
class VerdictEmitter:
    """Pure function emitter for harness-specific verdicts.

    Attributes:
        emit_deny: Callable that returns (exit_code, stdout) for a deny.
        emit_pass: Callable that returns (exit_code, stdout) for a pass.
        emit_error: Callable that returns (exit_code, stdout) for an error.
    """

    emit_deny: Callable[[str], tuple[int, str]]
    emit_pass: Callable[[], tuple[int, str]]
    emit_error: Callable[[str], tuple[int, str]]


# The namespace the canonical agent and skill content is authored in.
# Claude Code's plugin resolves agents under it; generators rewrite it into
# the target harness's own namespace.
CANONICAL_AGENT_NAMESPACE: Final = "forge:"

# A namespaced reference: the canonical prefix plus the whole name token.
# The hyphen is in the class because every shipped name carries one; `\w`
# alone stopped at it and read `forge:design` for `forge:design-checker`.
# A match followed by a colon is a managed-block marker, not a reference:
# see `match_is_marker`, which every caller deciding that uses.
PLUGIN_REFERENCE_RE: Final = re.compile(
    rf"{re.escape(CANONICAL_AGENT_NAMESPACE)}([\w-]+)"
)


def match_is_marker(match: re.Match[str]) -> bool:
    """Return whether a :data:`PLUGIN_REFERENCE_RE` match is a block marker.

    A managed-block marker carries a third segment — `forge:c4:start`,
    `forge:state:begin`, `forge:config:end`, `forge:badges:start` — while a
    reference ends at the name: `forge:design-checker`. Only the marker is
    followed by a colon, so that one character tells them apart — the colon
    is the definition, not a list of marker names to keep current.

    This is the single definition of the distinction: the mapper uses it to
    leave markers alone, and the reference-leak checks use it so a surviving
    marker is not reported as an unmapped reference. Without it, a marker
    whose token happens to be a shipped name is rewritten into the harness
    namespace and the tool that reads the marker no longer recognises it.

    Known limit: a reference written with a trailing colon as punctuation
    ("run `forge:commit`: then …") reads as a marker and is left alone.
    Nothing in the repo does that.

    Args:
        match: A match produced by :data:`PLUGIN_REFERENCE_RE`.

    Returns:
        True when the matched token is followed by a colon.
    """
    return match.string[match.end() : match.end() + 1] == ":"


@dataclass(frozen=True)
class HarnessProfile:
    """Frozen profile for a single agent harness.

    Attributes:
        name: The harness identifier (e.g., "vibe", "claude").
        event_map: Mapping from Vibe event names to Claude Code event names.
            Empty for Claude (identity mapping). For Vibe: maps Vibe events
            to the equivalent Claude Code events that guards understand.
        tool_matcher_map: Mapping from Vibe tool matcher names to Claude Code
            matcher names. For example, Vibe's "re:bash|process_start" maps
            to Claude's "Bash" for Bash guards.
        payload_field_renames: Mapping from Vibe payload field names to
            Claude Code payload field names. For example, Vibe's "agent_name"
            maps to Claude's "agent_type".
        verdict_emitter: The emitter for this harness's verdict format.
        blocking_events: Set of event names that are blocking (must have
            strict=true in Vibe, exit 2 in Claude).
        model_alias_map: Mapping from forge model tier names (haiku, sonnet,
            opus, inherit) to harness model aliases. Claude tiers are
            identity; Vibe maps to the forge-owned [[models]] presets that
            install-forge-vibe writes. "inherit"/unknown tiers map to None
            (emit nothing — the subagent inherits the session model).
        agent_namespace: Prefix this harness requires to address a shipped
            forge agent. The canonical content is written in Claude Code's
            plugin namespace, so Claude is identity and a harness that
            discovers agents in-repo takes the bare name. Generators map
            through this rather than hardcoding either vocabulary.
    """

    name: str
    event_map: Mapping[str, str]
    tool_matcher_map: Mapping[str, str]
    payload_field_renames: Mapping[str, str]
    verdict_emitter: VerdictEmitter
    blocking_events: frozenset[str]
    model_alias_map: Mapping[str, str | None]
    agent_namespace: str


def _emit_claude_deny(_reason: str) -> tuple[int, str]:
    """Emit a Claude Code deny verdict.

    Claude Code: block = exit 2 + stderr message.

    Args:
        _reason: Human-readable reason for the denial (unused in Claude Code).

    Returns:
        (exit_code, stdout) tuple. stdout is empty for Claude.
    """
    return (2, "")


def _emit_claude_pass() -> tuple[int, str]:
    """Emit a Claude Code pass verdict.

    Claude Code: pass = exit 0, silent.

    Returns:
        (exit_code, stdout) tuple. Both empty/zero.
    """
    return (0, "")


def _emit_claude_error(reason: str) -> tuple[int, str]:
    """Emit a Claude Code error verdict.

    Internal errors should also block in Claude Code.

    Args:
        reason: Human-readable error description.

    Returns:
        (exit_code, stdout) tuple.
    """
    return (2, reason)


def _emit_vibe_deny(reason: str) -> tuple[int, str]:
    """Emit a Vibe deny verdict.

    Vibe: block = exit 0 + stdout JSON {"decision":"deny","reason":"..."}.
    Must also set strict=true in the hook registration so non-zero exit
    blocks too, but that's the generator's concern (§4 deliverable 3).

    Args:
        reason: Human-readable reason for the denial.

    Returns:
        (exit_code, stdout) tuple with JSON verdict on stdout.
    """
    verdict = {"decision": "deny", "reason": reason}
    return (0, json.dumps(verdict, ensure_ascii=False))


def _emit_vibe_pass() -> tuple[int, str]:
    """Emit a Vibe pass verdict.

    Vibe: pass = exit 0, empty stdout.

    Returns:
        (exit_code, stdout) tuple.
    """
    return (0, "")


def _emit_vibe_error(reason: str) -> tuple[int, str]:
    """Emit a Vibe error verdict.

    Internal errors in the dispatcher must deny, not allow. Per Section 5
    — every failure path the dispatcher cannot express
    as a deny-JSON is an allow. So we emit deny-JSON on any internal
    exception.

    Args:
        reason: Human-readable error description.

    Returns:
        (exit_code, stdout) tuple with JSON deny verdict.
    """
    # Error = deny for fail-closed behavior
    verdict = {"decision": "deny", "reason": f"Internal error: {reason}"}
    return (0, json.dumps(verdict, ensure_ascii=False))


# Claude Code profile: identity mapping, exit-code based
CLAUDE_PROFILE = HarnessProfile(
    name=HarnessName.CLAUDE.value,
    event_map={},  # Identity: Claude event names match guard expectations
    tool_matcher_map={
        "Bash": "Bash",
        "Edit": "Edit",
        "Write": "Write",
    },
    payload_field_renames={},  # Identity: field names match
    model_alias_map={
        "haiku": "haiku",
        "sonnet": "sonnet",
        "opus": "opus",
        "inherit": None,
    },
    verdict_emitter=VerdictEmitter(
        emit_deny=_emit_claude_deny,
        emit_pass=_emit_claude_pass,
        emit_error=_emit_claude_error,
    ),
    blocking_events=frozenset(
        {
            "PreToolUse",
            "SessionStart",
            "PreCompact",
            "SubagentStart",
            "SubagentStop",
            "PostToolUse",
        }
    ),
    # Generated agents share a directory with the repo's own, so forge's
    # carry a prefix: it keeps the two sets apart on sight and removes the
    # name collision by construction rather than by convention. The
    # prefix is part of the file name, so a reference and the file it
    # names stay in step.
    agent_namespace=AGENT_NAME_PREFIX,
)

# Vibe profile: maps Vibe concepts to Claude Code equivalents
# Harness facts:
# - pre-tool -> PreToolUse
# - post-tool -> PostToolUse
# - subagent done -> post_agent (and .agent_type -> .agent_name)
# - SessionStart -> session_start on current Vibe (non-blocking; shipped
#   as an extra entry in vibe-extra-hooks.toml, not via this map); older
#   Vibe falls back to the post_agent vibe_session_start.sh extra
# - PreCompact -> none (doesn't exist in Vibe)
# - SubagentStart -> none (doesn't exist in Vibe)
# - Bash matcher -> "re:bash|process_start"
# - Edit matcher -> "edit"
# - Write matcher -> "write_file"
VIBE_PROFILE = HarnessProfile(
    name=HarnessName.VIBE.value,
    event_map={
        "pre_tool": "PreToolUse",
        "post_tool": "PostToolUse",
        "post_agent": "SubagentStop",
        # Vibe has no blocking equivalents for these Claude events:
        # SessionStart (session_start extra is non-blocking), PreCompact,
        # SubagentStart
    },
    tool_matcher_map={
        "re:bash|process_start": "Bash",
        "bash": "Bash",
        "edit": "Edit",
        "write_file": "Write",
    },
    payload_field_renames={
        "agent_name": "agent_type",
    },
    model_alias_map={
        "haiku": "forge-haiku",
        "sonnet": "forge-sonnet",
        "opus": "forge-opus",
        "inherit": None,
    },
    verdict_emitter=VerdictEmitter(
        emit_deny=_emit_vibe_deny,
        emit_pass=_emit_vibe_pass,
        emit_error=_emit_vibe_error,
    ),
    # post_agent cannot block in Vibe (deny only injects a retry message,
    # and strict is forbidden on it).
    blocking_events=frozenset({"pre_tool", "post_tool"}),
    # The same prefix as Claude's, though Vibe's generated directory is
    # forge's alone: one name per agent across every harness means a doc
    # read directly, rather than generated, can name it without knowing
    # which harness the reader is on.
    agent_namespace=AGENT_NAME_PREFIX,
)

# Registry of all known harness profiles
_HARNESS_REGISTRY: dict[str, HarnessProfile] = {
    HarnessName.CLAUDE.value: CLAUDE_PROFILE,
    HarnessName.VIBE.value: VIBE_PROFILE,
}


def get_harness_profile(name: str) -> HarnessProfile:
    """Retrieve a harness profile by name.

    Args:
        name: The harness identifier (e.g., "vibe", "claude").

    Returns:
        The HarnessProfile for the given harness.

    Raises:
        ValueError: If the harness name is unknown. Must NOT default to
            'claude' — silent misdetection emits wrong verdict format.
    """
    if name not in _HARNESS_REGISTRY:
        known = ", ".join(sorted(_HARNESS_REGISTRY.keys()))
        msg = f"Unknown harness name '{name}'. Known harnesses: {known}"
        raise ValueError(msg)
    return _HARNESS_REGISTRY[name]


def get_all_harness_names() -> list[str]:
    """Return the list of all known harness names.

    Returns:
        Sorted list of harness identifiers.
    """
    return sorted(_HARNESS_REGISTRY.keys())


def emit_block(reason: str, harness_name: str) -> tuple[int, str]:
    """Emit a block verdict for the specified harness.

    Pure function wrapper around the harness profile's emitter.

    Args:
        reason: Human-readable reason for the block.
        harness_name: The harness identifier.

    Returns:
        (exit_code, stdout) tuple appropriate for the harness.

    Raises:
        ValueError: If harness_name is unknown.
    """
    profile = get_harness_profile(harness_name)
    return profile.verdict_emitter.emit_deny(reason)


def emit_pass(harness_name: str) -> tuple[int, str]:
    """Emit a pass verdict for the specified harness.

    Pure function wrapper around the harness profile's emitter.

    Args:
        harness_name: The harness identifier.

    Returns:
        (exit_code, stdout) tuple appropriate for the harness.

    Raises:
        ValueError: If harness_name is unknown.
    """
    profile = get_harness_profile(harness_name)
    return profile.verdict_emitter.emit_pass()


def emit_error(reason: str, harness_name: str) -> tuple[int, str]:
    """Emit an error verdict for the specified harness.

    Pure function wrapper around the harness profile's emitter.

    Args:
        reason: Human-readable error description.
        harness_name: The harness identifier.

    Returns:
        (exit_code, stdout) tuple appropriate for the harness.

    Raises:
        ValueError: If harness_name is unknown.
    """
    profile = get_harness_profile(harness_name)
    return profile.verdict_emitter.emit_error(reason)


def translate_event(vibe_event: str, harness_name: str) -> str | None:
    """Translate a Vibe event name to the corresponding Claude event name.

    For Claude harness, returns the event unchanged (identity).
    For Vibe harness, maps to the equivalent Claude event.
    Returns None if the event has no equivalent in the target harness.

    Args:
        vibe_event: The Vibe event name (e.g., "pre_tool").
        harness_name: The target harness identifier.

    Returns:
        The translated event name, or None if no mapping exists.

    Raises:
        ValueError: If harness_name is unknown.
    """
    profile = get_harness_profile(harness_name)
    # Reverse lookup: if harness is Vibe, we want to go FROM Vibe TO Claude
    # So we look in the event_map
    if vibe_event in profile.event_map:
        return profile.event_map[vibe_event]
    # If not in map and harness is Claude, it's identity
    if harness_name == HarnessName.CLAUDE.value:
        return vibe_event
    return None


def translate_tool_matcher(harness_matcher: str, harness_name: str) -> str | None:
    """Translate a harness-specific tool matcher to Claude matcher.

    Args:
        harness_matcher: The matcher string from the harness (e.g.,
            "re:bash|process_start" for Vibe).
        harness_name: The harness identifier.

    Returns:
        The corresponding Claude matcher, or None if no mapping exists.

    Raises:
        ValueError: If harness_name is unknown.
    """
    profile = get_harness_profile(harness_name)
    if harness_matcher in profile.tool_matcher_map:
        return profile.tool_matcher_map[harness_matcher]
    return None


def translate_payload_field(field_name: str, harness_name: str) -> str | None:
    """Translate a payload field name from harness format to Claude format.

    Args:
        field_name: The field name from the harness payload.
        harness_name: The harness identifier.

    Returns:
        The corresponding Claude field name, or the original if no mapping.

    Raises:
        ValueError: If harness_name is unknown.
    """
    profile = get_harness_profile(harness_name)
    if field_name in profile.payload_field_renames:
        return profile.payload_field_renames[field_name]
    return field_name


# Claude hooks with no blocking Vibe equivalent (SessionStart's Vibe
# session_start is non-blocking and carries only the plugin-version line;
# PreCompact / SubagentStart / SubagentStop events Vibe does not implement,
# and post_agent fires after a turn so it cannot block like SubagentStop).
# Single source for the generator (which skips them) and the installer
# (which prints them as unenforced protections).
UNENFORCED_VIBE_HOOKS: Mapping[str, str] = {
    "require_fixer_verdict.sh": (
        "SubagentStop blocking gate - Vibe has no SubagentStop; "
        "post_agent fires after the turn and cannot block"
    ),
    "caveman_mode.sh": (
        "SessionStart context injection - no blocking Vibe equivalent "
        "(session_start is non-blocking and prints only the plugin version)"
    ),
    "load_continuation.sh": (
        "SessionStart context injection - no blocking Vibe equivalent; "
        "replaced by post_agent vibe_session_start.sh"
    ),
    "keep_continuation_state.sh": (
        "PreCompact state write - Vibe has no PreCompact; replaced by "
        "post_agent vibe_continuation_state.sh (every turn)"
    ),
    "log_agent_timing.sh": (
        "SubagentStart half - Vibe has no SubagentStart; partial at best"
    ),
    "require_helpers_stopped.sh": (
        "SubagentStop blocking gate - Vibe has no SubagentStop; "
        "post_agent fires after the turn and cannot block"
    ),
}


def resolve_hooks_base() -> Path:
    """Return the directory guard scripts live in for this checkout.

    In forge's own repo ``claude-hooks/`` sits at the repo root; a consumer
    gets it from the installed package. Shared by every converter so each
    resolves a script to the same file.

    Returns:
        The packaged guard directory.
    """
    return shipped_path("claude-hooks")


def map_plugin_references(
    text: str, *, harness: str, names: Iterable[str] | None = None
) -> str:
    """Rewrite canonical agent and skill references into *harness*'s namespace.

    A reference is rewritten only when the **whole** token after the prefix
    is a name forge ships. Matching the prefix by substring would strip it
    from any token a shipped name merely begins — ``forge:fixture`` reads
    as the skill ``fix``, and ``forge:precommit-managed`` as ``pr`` — so
    the token is taken whole and tested for membership. A placeholder
    (``forge:<name>``) and a name forge does not ship therefore survive
    untouched.

    A managed-block marker survives too: :func:`match_is_marker` excludes
    anything carrying a third segment, so a marker is safe even when its
    token is a shipped name. Rewriting one yields a marker the tool that
    reads it no longer recognises.

    Args:
        text: Canonical content, authored in ``CANONICAL_AGENT_NAMESPACE``.
        harness: Target harness identifier.
        names: Names to map; defaults to the shipped roster.

    Returns:
        The content with each shipped reference in the target harness's
        namespace. Unchanged when the target shares the canonical
        namespace.

    Raises:
        ValueError: If *harness* is not a known harness.
    """
    namespace = get_harness_profile(harness).agent_namespace
    if namespace == CANONICAL_AGENT_NAMESPACE:
        return text
    known = frozenset(shipped_plugin_names() if names is None else names)

    def _rewrite(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in known or match_is_marker(match):
            return match.group(0)
        return f"{namespace}{name}"

    return PLUGIN_REFERENCE_RE.sub(_rewrite, text)
