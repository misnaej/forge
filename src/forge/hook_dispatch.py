"""Hook dispatcher CLI for agent harnesses.

This module provides the forge-hook-dispatch CLI that runs guard scripts
for a given harness and event, translating between harness-specific payload
formats and the guard's expected contract.

The dispatcher:
- Reads harness JSON from stdin (original bytes, not re-serialized)
- Runs the ordered guard set for the event
- Re-feeds each guard the original stdin bytes
- Stops at the first block
- Emits the harness's verdict via the profile
- Prints all dispatcher output to stderr (stdout carries exactly one JSON)

Fail-closed behavior:
    Every failure path that cannot express a deny must be an allow in Vibe.
    Therefore we wrap main() in a catch-all that emits deny-JSON on any
    internal exception.

Usage:
    forge-hook-dispatch --harness vibe --event pre_tool < roster.json

Where roster.json contains the guard configuration (generated at install
    time, NOT read from plugin.json at runtime).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

from forge.agent_harness import (
    get_harness_profile,
    translate_event,
    translate_tool_matcher,
)


logger = logging.getLogger(__name__)


# Claude Code guard exit code for block (exit 2 = deny)
BLOCK_EXIT_CODE = 2


class _DispatchDenyError(Exception):
    """Control-flow exception carrying a deny reason to the verdict emitter.

    Raised by the validation helpers; caught once in :func:`dispatch` and
    converted into the harness profile's deny/error verdict.
    """

    def __init__(self, reason: str) -> None:
        """Store the deny reason for the verdict emitter.

        Args:
            reason: Human-readable reason for the denial.
        """
        super().__init__(reason)
        self.reason = reason


def _deny_json(reason: str) -> str:
    """Create a Vibe-compatible deny JSON verdict.

    Args:
        reason: Human-readable reason for the denial.

    Returns:
        JSON string for stdout.
    """
    return json.dumps({"decision": "deny", "reason": reason}, ensure_ascii=False)


def _read_stdin_bytes() -> bytes:
    """Read all bytes from stdin.

    Returns:
        Raw bytes from stdin, or empty bytes if stdin is closed.
    """
    try:
        return sys.stdin.buffer.read()
    except (AttributeError, ValueError):
        # Fallback for text mode stdin
        return sys.stdin.read().encode("utf-8", errors="surrogateescape")


def _validate_payload(payload_bytes: bytes) -> dict | None:
    """Validate stdin contains parseable JSON.

    Args:
        payload_bytes: Raw bytes from stdin.

    Returns:
        Parsed JSON dict, or None if invalid/empty.

    Raises:
        ValueError: If JSON is malformed (will be caught and converted to deny).
    """
    if not payload_bytes:
        return None
    try:
        return json.loads(payload_bytes)
    except json.JSONDecodeError as e:
        msg = f"Malformed JSON on stdin: {e}"
        raise ValueError(msg) from e


def _get_roster_path(harness_name: str) -> Path:
    """Resolve the roster sidecar path for a harness.

    The roster is baked at generation time and read at runtime.
    For Vibe: .vibe/forge-roster.json
    For Claude: not used (Claude reads plugin.json directly).

    Args:
        harness_name: The harness identifier.

    Returns:
        Path to the roster sidecar file.
    """
    if harness_name == "vibe":
        return Path(".vibe/forge-roster.json")
    # For other harnesses, use a sensible default
    return Path(f".{harness_name}/forge-roster.json")


def _load_roster(roster_path: Path) -> dict:
    """Load the guard roster from the sidecar file.

    Args:
        roster_path: Path to the roster sidecar.

    Returns:
        Parsed JSON dict with guard configuration.

    Raises:
        FileNotFoundError: If roster file doesn't exist.
        json.JSONDecodeError: If roster file is malformed.
    """
    with roster_path.open("rb") as f:
        return json.load(f)


def _validate_guard_commands(roster: dict, roster_path: Path) -> None:
    """Ensure every roster guard command resolves under the project root.

    The roster sidecar is a trust anchor: a guard command outside the
    project root would let a tampered roster execute arbitrary paths.
    Commands are absolute paths baked at generation time; resolve and
    check containment.

    Args:
        roster: The loaded roster dict.
        roster_path: Path to the roster file (for error messages).

    Raises:
        _DispatchDenyError: If any command escapes the project root.
    """
    project_root = roster.get("project_root")
    if not project_root:
        msg = f"Roster {roster_path} has no project_root; refusing to run guards"
        raise _DispatchDenyError(msg)
    # Anchor to the roster file's own location, never to the roster's claim:
    # a tampered roster could set project_root=/ and every path would pass
    # containment. The roster lives at <root>/.vibe/forge-roster.json, so the
    # anchor is its parent's parent (or its parent outside a .vibe dir).
    # Deny on mismatch and on filesystem root.
    roster_abs = roster_path.resolve()
    anchor = (
        roster_abs.parent.parent
        if roster_abs.parent.name == ".vibe"
        else roster_abs.parent
    )
    claimed = Path(project_root).resolve()
    if claimed == Path(claimed.anchor):
        msg = f"Roster {roster_path} claims project_root at filesystem root; refusing"
        raise _DispatchDenyError(msg)
    if claimed != anchor:
        msg = (
            f"Roster {roster_path} claims project_root {claimed} but lives at "
            f"{roster_path}; refusing to run guards"
        )
        raise _DispatchDenyError(msg)
    root = anchor
    for event_data in roster.get("events", {}).values():
        for guards in event_data.get("matchers", {}).values():
            for guard in guards:
                cmd = guard.get("command", "")
                if not cmd:
                    continue
                resolved = Path(cmd).resolve()
                if root not in resolved.parents and resolved != root:
                    msg = (
                        f"Roster guard command escapes project root: {cmd} "
                        f"(roster {roster_path})"
                    )
                    raise _DispatchDenyError(msg)


def _resolve_guards(
    roster: dict,
    event: str,
    claude_matcher: str | None = None,
) -> list[dict]:
    """Resolve the ordered guard list for an event and optional matcher.

    Args:
        roster: The loaded roster dict.
        event: The Claude event name (e.g., "PreToolUse").
        claude_matcher: Optional Claude matcher ("Bash", "Edit", "Write")
            to select only that matcher's guards. None selects all.

    Returns:
        List of guard configs in order.
    """
    # Roster shape: an "events" map keyed by event name, whose values hold a
    # "matchers" map keyed by matcher name, each mapping to a list of guards.
    event_guards = roster.get("events", {}).get(event, {}).get("matchers", {})

    if claude_matcher is not None:
        return list(event_guards.get(claude_matcher, []))
    guards: list[dict] = []
    for matcher_guards in event_guards.values():
        guards.extend(matcher_guards)
    return guards


def _get_claude_project_dir(payload: dict, roster: dict) -> str:
    """Extract CLAUDE_PROJECT_DIR from payload or roster.

    Eight guards read
    CLAUDE_PROJECT_DIR, and Vibe does not set it. The dispatcher must
    export it for every guard.

    Precedence:
    1. payload's .cwd field
    2. roster's project_root field (baked at generation time)
    3. Current working directory

    Args:
        payload: The parsed stdin JSON.
        roster: The loaded roster dict.

    Returns:
        The project directory path to export as CLAUDE_PROJECT_DIR.
    """
    # Try payload.cwd first
    if "cwd" in payload:
        return payload["cwd"]

    # Try roster.project_root
    if "project_root" in roster:
        return roster["project_root"]

    # Fallback to current directory
    return str(Path.cwd())


def _run_guard(
    guard: dict,
    payload_bytes: bytes,
    project_dir: str,
    timeout: float,
) -> tuple[bool, str]:
    """Run a single guard script and return its verdict.

    Args:
        guard: Guard config dict with "command" and "timeout" keys.
        payload_bytes: Original stdin bytes to feed to the guard.
        project_dir: Value to set for CLAUDE_PROJECT_DIR.
        timeout: Timeout in seconds for the guard process.

    Returns:
        (is_blocked, reason) tuple. is_blocked=True means the guard
        voted to block.
    """
    command = guard["command"]

    # Check if guard script exists and is executable
    script_path = Path(command).resolve()
    if not script_path.exists():
        return (True, f"Guard script missing: {command}")
    if not os.access(script_path, os.X_OK):
        return (True, f"Guard script not executable: {command}")

    # Build environment with CLAUDE_PROJECT_DIR set
    env = os.environ.copy()
    env["CLAUDE_PROJECT_DIR"] = project_dir

    # Run the guard with the original bytes on stdin
    try:
        proc = subprocess.run(
            [command],
            input=payload_bytes,
            capture_output=True,
            timeout=timeout,
            env=env,
            text=False,
            check=False,
        )

        # Guard exit codes:
        # - 0 = pass (allow)
        # - 2 = block (deny)
        # - anything else = error, but per Section 5 Row 6: for blocking
        #   events, non-zero non-2 should also deny (stricter than Claude)
        if proc.returncode != 0:
            # Block or error: use stderr if available, otherwise generic
            reason = proc.stderr.decode("utf-8", errors="replace").strip()
            if not reason:
                if proc.returncode == BLOCK_EXIT_CODE:
                    reason = f"Guard blocked without reason: {command}"
                else:
                    reason = f"Guard exited {proc.returncode} without stderr: {command}"
            return (True, reason)
    except subprocess.TimeoutExpired:
        # Row 5: timeout -> deny
        return (True, f"Guard timeout after {timeout}s: {command}")
    except OSError as e:
        # Row 4: OSError -> deny
        return (True, f"Guard execution failed: {e}")
    else:
        # Exit 0 = pass
        return (False, "")


def _claude_matcher_for_payload(payload: dict, harness_name: str) -> str | None:
    """Map the payload's tool_name to a Claude matcher via the profile.

    Vibe payloads carry a qualified tool name (``file_system.bash``);
    the roster is keyed by Claude matchers ("Bash", "Edit", "Write").
    The harness profile's tool_matcher_map translates the Vibe matcher
    regex/glob to the Claude matcher.

    Args:
        payload: The parsed stdin JSON.
        harness_name: The harness identifier.

    Returns:
        The Claude matcher name, or None when the tool has no mapping.
    """
    profile = get_harness_profile(harness_name)
    tool_name = str(payload.get("tool_name", ""))
    short = tool_name.rsplit(".", 1)[-1]
    for vibe_matcher, claude_matcher in profile.tool_matcher_map.items():
        pattern = vibe_matcher.removeprefix("re:")
        if pattern == short or tool_name == vibe_matcher:
            return claude_matcher
    return None


def _parse_payload_or_deny(payload_bytes: bytes) -> dict:
    """Parse stdin bytes into a payload dict, denying on any bad input.

    Args:
        payload_bytes: Raw stdin bytes (original, not re-serialized).

    Returns:
        The parsed payload dict.

    Raises:
        _DispatchDenyError: When stdin is empty or the JSON is malformed.
    """
    if not payload_bytes:
        msg = "Empty stdin"
        raise _DispatchDenyError(msg)
    try:
        payload = _validate_payload(payload_bytes)
    except ValueError as e:
        # Row 7: malformed stdin -> deny
        raise _DispatchDenyError(str(e)) from e
    if payload is None:
        msg = "Empty stdin JSON"
        raise _DispatchDenyError(msg)
    return payload


def _load_roster_or_deny(
    harness_name: str, roster_path: Path | None
) -> tuple[dict, Path]:
    """Load the roster sidecar, denying when it is missing or malformed.

    Args:
        harness_name: The harness identifier, used for the default path.
        roster_path: Caller-supplied roster path, or None for the default.

    Returns:
        The parsed roster dict and the path it was read from.

    Raises:
        _DispatchDenyError: When the roster file is missing or unparseable.
    """
    if roster_path is None:
        roster_path = _get_roster_path(harness_name)
    try:
        roster = _load_roster(roster_path)
    except FileNotFoundError:
        # Row 3: roster missing -> deny, naming the path
        msg = f"Roster file missing: {roster_path}"
        raise _DispatchDenyError(msg) from None
    except json.JSONDecodeError as e:
        # Row 3: roster unparseable -> deny, naming the path
        msg = f"Roster file malformed: {roster_path} ({e})"
        raise _DispatchDenyError(msg) from e
    return roster, roster_path


def _require_tool_input(payload: dict, event: str, vibe_matcher: str | None) -> None:
    """Deny payloads whose tool_input shape cannot reach the guards safely.

    Row 7b: a pre_tool payload without the matched field (command for
    bash, file_path otherwise) would pass through to guards that fail
    open on empty input. Deny instead of passing through.

    Args:
        payload: The parsed stdin JSON.
        event: The event name from the harness (for error messages).
        vibe_matcher: The Vibe matcher from argv, when given.

    Raises:
        _DispatchDenyError: When tool_input or its matched field is missing.
    """
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        msg = f"Payload for event '{event}' has no tool_input object"
        raise _DispatchDenyError(msg)
    if vibe_matcher is not None and "bash" in vibe_matcher:
        command = tool_input.get("command")
        if not isinstance(command, str) or not command.strip():
            msg = "Payload tool_input.command is missing or empty"
            raise _DispatchDenyError(msg)
    else:
        file_path = tool_input.get("file_path")
        if not isinstance(file_path, str) or not file_path:
            msg = "Payload tool_input.file_path is missing or empty"
            raise _DispatchDenyError(msg)


def _resolve_claude_matcher(
    payload: dict, harness_name: str, vibe_matcher: str | None
) -> str | None:
    """Translate the Vibe matcher or the payload's tool_name to a Claude matcher.

    Args:
        payload: The parsed stdin JSON.
        harness_name: The harness identifier.
        vibe_matcher: The Vibe matcher from argv, when given; otherwise the
            payload's tool_name decides.

    Returns:
        The Claude matcher name, or None when the tool has no mapping.

    Raises:
        _DispatchDenyError: When an explicit Vibe matcher has no mapping.
    """
    if vibe_matcher is not None:
        claude_matcher = translate_tool_matcher(vibe_matcher, harness_name)
        if claude_matcher is None:
            msg = f"Unknown matcher '{vibe_matcher}' for harness '{harness_name}'"
            raise _DispatchDenyError(msg)
        return claude_matcher
    return _claude_matcher_for_payload(payload, harness_name)


def _prepare_guards(
    harness_name: str,
    event: str,
    payload_bytes: bytes,
    roster_path: Path | None,
    vibe_matcher: str | None = None,
) -> tuple[list[dict], str]:
    """Validate the payload and resolve the guard list for an event.

    Args:
        harness_name: The harness identifier ("vibe", "claude").
        event: The event name from the harness.
        payload_bytes: Raw stdin bytes (original, not re-serialized).
        roster_path: Path to the roster file, or None for the default.
        vibe_matcher: Optional Vibe matcher from argv (the hooks.toml
            entry's match). When given, selects only that matcher's
            guards; otherwise the payload's tool_name decides.

    Returns:
        (guards, project_dir) — the ordered guard list and the value to
        export as CLAUDE_PROJECT_DIR.

    Raises:
        _DispatchDenyError: When any input is invalid — empty or malformed
            stdin, missing or malformed roster, unknown event, or no
            guards configured for the event.
    """
    payload = _parse_payload_or_deny(payload_bytes)
    roster, roster_path = _load_roster_or_deny(harness_name, roster_path)

    # Trust anchor: every guard command must live under the project root
    _validate_guard_commands(roster, roster_path)

    # Translate event name to Claude event for guard matching
    # The roster is indexed by Claude event names
    claude_event = translate_event(event, harness_name)
    if claude_event is None:
        # Unknown event -> deny (Row 13)
        msg = f"Unknown event '{event}' for harness '{harness_name}'"
        raise _DispatchDenyError(msg)

    _require_tool_input(payload, event, vibe_matcher)
    claude_matcher = _resolve_claude_matcher(payload, harness_name, vibe_matcher)

    guards = _resolve_guards(roster, claude_event, claude_matcher)
    if not guards:
        # Row 3b: roster empty for a blocking event -> error
        msg = (
            f"No guards configured for event '{claude_event}' "
            f"matcher '{claude_matcher}'"
        )
        raise _DispatchDenyError(msg)

    return guards, _get_claude_project_dir(payload, roster)


def dispatch(
    harness_name: str,
    event: str,
    payload_bytes: bytes,
    roster_path: Path | None = None,
    vibe_matcher: str | None = None,
) -> tuple[int, str]:
    """Dispatch a hook event through the guard roster.

    This is the core dispatch logic, separated from CLI parsing for
    testability.

    Args:
        harness_name: The harness identifier ("vibe", "claude").
        event: The event name from the harness.
        payload_bytes: Raw stdin bytes (original, not re-serialized).
        roster_path: Optional path to roster file. If None, uses
            default location.
        vibe_matcher: Optional Vibe matcher selecting the guard subset.

    Returns:
        (exit_code, stdout) tuple. stdout contains exactly one JSON
        object for Vibe, or is empty for Claude pass.

    Raises:
        Any exception will be caught by the CLI wrapper and converted
        to a deny verdict (fail-closed).
    """
    profile = get_harness_profile(harness_name)

    try:
        guards, project_dir = _prepare_guards(
            harness_name, event, payload_bytes, roster_path, vibe_matcher
        )
    except _DispatchDenyError as deny:
        return profile.verdict_emitter.emit_error(deny.reason)

    # Run guards sequentially, short-circuit on first block
    for guard in guards:
        # Per-guard timeout; a present-but-null value means "no timeout",
        # which would hang the chain — treat as the default (Row 5)
        guard_timeout = guard.get("timeout") or 5.0

        is_blocked, reason = _run_guard(
            guard, payload_bytes, project_dir, guard_timeout
        )

        if is_blocked:
            return profile.verdict_emitter.emit_deny(reason)

    # All guards passed
    return profile.verdict_emitter.emit_pass()


def main() -> int:
    """CLI entry point for forge-hook-dispatch.

    Parses arguments, reads stdin, dispatches to guards, emits verdict.

    All dispatcher output goes to stderr. Stdout carries exactly one
    JSON object (for Vibe) or is empty (for Claude pass).

    Returns:
        Exit code (0 for pass/error with proper JSON, non-zero for
        internal failures that couldn't be expressed as JSON).
    """
    parser = argparse.ArgumentParser(
        prog="forge-hook-dispatch",
        description=(
            "Dispatch hook events to forge guard scripts. "
            "Reads harness JSON from stdin, runs ordered guards, "
            "emits verdict to stdout."
        ),
    )
    parser.add_argument(
        "--harness",
        required=True,
        choices=["claude", "vibe"],
        help="Agent harness name (claude or vibe).",
    )
    parser.add_argument(
        "--event",
        required=True,
        help="Event name (e.g., pre_tool, PreToolUse).",
    )
    parser.add_argument(
        "--roster",
        type=Path,
        default=None,
        help="Path to roster sidecar file (default: .vibe/forge-roster.json for vibe).",
    )
    parser.add_argument(
        "--matcher",
        default=None,
        help=(
            "Vibe tool matcher selecting the guard subset "
            '(e.g. "re:bash|process_start"); the hooks.toml entry\'s match. '
            "When omitted, the payload's tool_name decides."
        ),
    )

    args = parser.parse_args()

    try:
        # Read original bytes from stdin
        payload_bytes = _read_stdin_bytes()

        # Dispatch
        exit_code, stdout = dispatch(
            args.harness,
            args.event,
            payload_bytes,
            args.roster,
            args.matcher,
        )

        # Output verdict to stdout
        sys.stdout.write(stdout)
        sys.stdout.flush()
    except Exception as e:
        logger.exception("Dispatcher internal error")
        # Catch-all for fail-closed behavior (Row 2)
        # Emit deny-JSON on stderr for Vibe, or exit 2 for Claude
        try:
            profile = get_harness_profile(args.harness)
            exit_code, stdout = profile.verdict_emitter.emit_error(str(e))
            sys.stdout.write(stdout)
            sys.stdout.flush()
            sys.stderr.write(f"DISPATCHER ERROR: {e}\n")
            sys.stderr.flush()
        except Exception:
            logger.exception("Cannot load harness profile")
            # Even getting the profile failed - ultimate fallback
            sys.stderr.write(f"CRITICAL: Cannot load harness profile: {e}\n")
            sys.stderr.flush()
            # For Vibe: deny JSON
            sys.stdout.write(_deny_json("Internal dispatcher error"))
            sys.stdout.flush()
            return 0  # Vibe expects exit 0 with deny JSON
        else:
            return exit_code
    else:
        return exit_code


if __name__ == "__main__":
    sys.exit(main())
