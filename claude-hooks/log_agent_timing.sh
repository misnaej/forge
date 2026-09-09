#!/usr/bin/env bash
# SubagentStart / SubagentStop / PostToolUse: append one JSON line per
# event to code_health/agent_timing.jsonl — the agent-timing ledger
# `forge-agent-profile` reads.
#
# Forge times pre-commit steps, tests, and wrapped subprocesses, but
# nothing timed the agents themselves: how long forge:design-checker
# takes, whether forge:precommit-fixer honours its three-run cap, or
# whether a subagent is re-running the same command. The hook payloads
# are the documented, stable source for that: agent_id / agent_type on
# the subagent events, tool_name / duration_ms on PostToolUse. The
# ledger is append-only and per-workspace (code_health/ is gitignored);
# the CLI does the pairing and the statistics — this hook only records.
#
# Registered without a matcher on purpose: loop detection needs every
# tool call, not just Bash. Cost is one jq call and one append per event.
# `FORGE_NO_AGENT_TIMING=1` switches the hook off (no config surface —
# same shape as FORGE_NO_AUTO_SETUP).
#
# Never blocks, never prints: deliberately no `set -e`; every probe may
# legitimately fail (no git repo, no jq) and none are errors.
set -uo pipefail

[ "${FORGE_NO_AGENT_TIMING:-0}" = "1" ] && exit 0
command -v jq >/dev/null 2>&1 || exit 0

INPUT=$(cat)
CWD=$(printf '%s' "$INPUT" | jq -r '.cwd // empty' 2>/dev/null)
ROOT=$(git -C "${CWD:-.}" rev-parse --show-toplevel 2>/dev/null) || exit 0

LINE=$(printf '%s' "$INPUT" | jq -c '{
    ts: (now | todate),
    ts_ms: (now * 1000 | floor),
    event: .hook_event_name,
    session_id,
    agent_id,
    agent_type,
    transcript_path,
    tool_name,
    tool_use_id,
    duration_ms
}' 2>/dev/null) || exit 0
[ -n "$LINE" ] || exit 0

mkdir -p "$ROOT/code_health" 2>/dev/null || exit 0
printf '%s\n' "$LINE" >> "$ROOT/code_health/agent_timing.jsonl" 2>/dev/null
exit 0
