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

Fail-closed behavior (Section 5 of VIBE-HANDOFF.md):
    Every failure path that cannot express a deny must be an allow in Vibe.
    Therefore we wrap main() in a catch-all that emits deny-JSON on any
    internal exception.

Usage:
    forge-hook-dispatch --harness vibe --event pre_tool < roster.json

Where roster.json contains the guard configuration (generated at install
    time, NOT read from plugin.json at runtime).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


# Claude Code guard exit code for block (exit 2 = deny)
BLOCK_EXIT_CODE = 2


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


def _resolve_guards(
    roster: dict, event: str, tool_name: str | None = None
) -> list[dict]:
    """Resolve the ordered guard list for an event and optional tool matcher.

    Args:
        roster: The loaded roster dict.
        event: The event name (e.g., "pre_tool", "PreToolUse").
        tool_name: Optional tool matcher to filter guards.

    Returns:
        List of guard configs in order.
    """
    # Roster shape: an "events" map keyed by event name, whose values hold a
    # "matchers" map keyed by matcher name, each mapping to a list of guards.
    event_guards = roster.get("events", {}).get(event, {}).get("matchers", {})

    guards: list[dict] = []

    # If tool_name is specified, only get guards for that matcher
    if tool_name:
        if tool_name in event_guards:
            guards.extend(event_guards[tool_name])
    else:
        # Get all guards for all matchers in this event
        for matcher_guards in event_guards.values():
            guards.extend(matcher_guards)

    return guards


def _get_claude_project_dir(payload: dict, roster: dict) -> str:
    """Extract CLAUDE_PROJECT_DIR from payload or roster.

    Per Section 5 Row 10 of VIBE-HANDOFF.md: Eight guards read
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


def _translate_to_claude_payload(payload: dict, harness_name: str) -> dict:
    """Translate a harness payload to Claude Code format.

    Per Section 2 of VIBE-HANDOFF.md, we need to handle:
    - agent_name -> agent_type (for SubagentStop -> post_agent)
    - Other fields may need translation based on harness profile

    However, for Deliverable 2, we're re-feeding the original bytes.
    This function is for reference and potential future use.

    Args:
        payload: The parsed harness payload.
        harness_name: The harness identifier.

    Returns:
        Translated payload dict.
    """
    # For now, we re-feed original bytes, so this is just documentation
    # of what translation WOULD look like
    translated = dict(payload)

    if harness_name == "vibe":
        # Vibe uses agent_name, Claude uses agent_type
        if "agent_name" in translated:
            translated["agent_type"] = translated.pop("agent_name")
        # Vibe event names map to Claude names
        if "hook_event_name" in translated:
            event_map = {
                "pre_tool": "PreToolUse",
                "post_tool": "PostToolUse",
                "post_agent": "SubagentStop",
            }
            event = translated["hook_event_name"]
            if event in event_map:
                translated["hook_event_name"] = event_map[event]

    return translated


class _DispatchDenyError(Exception):
    """Internal control flow: dispatch must deny with a specific reason.

    Attributes:
        reason: Human-readable deny reason for the verdict emitter.
    """

    def __init__(self, reason: str) -> None:
        """Store the deny reason.

        Args:
            reason: Human-readable deny reason.
        """
        super().__init__(reason)
        self.reason = reason


def _prepare_guards(
    harness_name: str,
    event: str,
    payload_bytes: bytes,
    roster_path: Path | None,
) -> tuple[list[dict], str]:
    """Validate the payload and resolve the guard list for an event.

    Args:
        harness_name: The harness identifier ("vibe", "claude").
        event: The event name from the harness.
        payload_bytes: Raw stdin bytes (original, not re-serialized).
        roster_path: Path to the roster file, or None for the default.

    Returns:
        (guards, project_dir) — the ordered guard list and the value to
        export as CLAUDE_PROJECT_DIR.

    Raises:
        _DispatchDenyError: When any input is invalid — empty or malformed
            stdin, missing or malformed roster, unknown event, or no
            guards configured for the event.
    """
    from forge.agent_harness import translate_event

    if not payload_bytes:
        msg = "Empty stdin"
        raise _DispatchDenyError(msg)

    # Parse for project dir extraction (but re-feed original bytes to guards)
    try:
        payload = _validate_payload(payload_bytes)
    except ValueError as e:
        # Row 7: malformed stdin -> deny
        raise _DispatchDenyError(str(e)) from e
    if payload is None:
        msg = "Empty stdin JSON"
        raise _DispatchDenyError(msg)

    # Resolve roster path
    if roster_path is None:
        roster_path = _get_roster_path(harness_name)

    # Load roster
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

    # Translate event name to Claude event for guard matching
    # The roster is indexed by Claude event names
    claude_event = translate_event(event, harness_name)
    if claude_event is None:
        # Unknown event -> deny (Row 13)
        msg = f"Unknown event '{event}' for harness '{harness_name}'"
        raise _DispatchDenyError(msg)

    # Resolve guards for this event
    guards = _resolve_guards(roster, claude_event)
    if not guards:
        # Row 3b: roster empty for a blocking event -> error
        msg = f"No guards configured for event '{claude_event}'"
        raise _DispatchDenyError(msg)

    return guards, _get_claude_project_dir(payload, roster)


def dispatch(
    harness_name: str,
    event: str,
    payload_bytes: bytes,
    roster_path: Path | None = None,
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

    Returns:
        (exit_code, stdout) tuple. stdout contains exactly one JSON
        object for Vibe, or is empty for Claude pass.

    Raises:
        Any exception will be caught by the CLI wrapper and converted
        to a deny verdict (fail-closed).
    """
    from forge.agent_harness import get_harness_profile

    profile = get_harness_profile(harness_name)

    try:
        guards, project_dir = _prepare_guards(
            harness_name, event, payload_bytes, roster_path
        )
    except _DispatchDenyError as deny:
        return profile.verdict_emitter.emit_error(deny.reason)

    # Run guards sequentially, short-circuit on first block
    for guard in guards:
        # Use per-guard timeout or default
        guard_timeout = guard.get("timeout", 5.0)

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
    import argparse

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
        )

        # Output verdict to stdout
        sys.stdout.write(stdout)
        sys.stdout.flush()
    except Exception as e:  # noqa: BLE001
        # Catch-all for fail-closed behavior (Row 2)
        # Emit deny-JSON on stderr for Vibe, or exit 2 for Claude
        from forge.agent_harness import get_harness_profile

        try:
            profile = get_harness_profile(args.harness)
            exit_code, stdout = profile.verdict_emitter.emit_error(str(e))
            sys.stdout.write(stdout)
            sys.stdout.flush()
            sys.stderr.write(f"DISPATCHER ERROR: {e}\n")
            sys.stderr.flush()
        except Exception:  # noqa: BLE001
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
