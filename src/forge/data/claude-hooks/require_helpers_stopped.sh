#!/usr/bin/env bash
# Refuse an agent's hand-back while a helper it launched is still running.
#
# An agent that delegates (Task / Agent) and hands back before its helper
# finishes returns a report about work still in progress — a "done" whose
# edits keep landing, a test run with no result, a "placeholder". The
# template rule (agents/_TEMPLATE.md "Delegating agents wait for their
# helpers") is the contract; this SubagentStop hook is the mechanical half.
#
# Evidence is the agent-timing ledger (code_health/agent_timing.jsonl):
# log_agent_timing.sh records, on each Agent/Task launch, the launcher's
# agent_id and the launched helper's id (`spawned_agent_id`), and a
# SubagentStop row when any agent stops. A helper the stopping agent
# launched with no SubagentStop row yet is still running.
#
#   no helper still running                     → allow
#   helper running, first stop of this agent    → block once, naming it
#   same agent stops again                      → allow (never loops)
#
# Fails open (allows) without jq, an agent_id, a repo root, a readable
# ledger, the shared block-once lib, or with FORGE_NO_AGENT_TIMING=1
# (no ledger is written then, so helpers are invisible — a silent guard
# drop by that switch's design). Covers subagents only: the main session
# stops through Stop, not SubagentStop, and background shell jobs are not
# attributed to an agent in any payload.
set -uo pipefail
[ "${FORGE_NO_AGENT_TIMING:-0}" = "1" ] && exit 0
command -v jq >/dev/null 2>&1 || exit 0
INPUT=$(cat)
{
    IFS= read -r AGENT_TYPE
    IFS= read -r AGENT_ID
    IFS= read -r SESSION_ID
    IFS= read -r CWD
} <<< "$(jq -r '.agent_type // "", .agent_id // "", .session_id // "", .cwd // "."' <<< "$INPUT" 2>/dev/null)"
[ -n "$AGENT_ID" ] || exit 0
ROOT=${CLAUDE_PROJECT_DIR:-}
[ -n "$ROOT" ] && [ -e "$ROOT/.git" ] || ROOT=$(git -C "${CWD:-.}" rev-parse --show-toplevel 2>/dev/null) || exit 0
ONCE_LIB="$(dirname "$0")/once_mark.sh"
[ -r "$ONCE_LIB" ] || exit 0
# shellcheck source=once_mark.sh
source "$ONCE_LIB"
once_mark_paths "$ROOT"
[ -r "$LEDGER" ] || exit 0
once_mark_seen handback_block "$AGENT_ID" && exit 0

# Helpers this agent launched that have no SubagentStop row yet, as
# "id (type)" lines. One jq pass over the ledger; malformed lines skipped.
RUNNING=$(jq -rRn --arg a "$AGENT_ID" '
    [inputs | (try fromjson catch null) | select(. != null)] as $rows
    | ($rows | map(select(.event == "PostToolUse" and .agent_id == $a
                          and (.spawned_agent_id // null) != null)
                   | .spawned_agent_id) | unique) as $spawned
    | ($rows | map(select(.event == "SubagentStop") | .agent_id) | unique) as $stopped
    | ($rows | map(select(.event == "SubagentStart") | {(.agent_id): (.agent_type // "?")})
             | add // {}) as $types
    | $spawned - $stopped
    | .[] | "\(.) (\($types[.] // "?"))"
' < "$LEDGER" 2>/dev/null) || exit 0
[ -n "$RUNNING" ] || exit 0

# Mark before blocking; an unwritable mark allows the stop (once_mark.sh).
once_mark_write handback_block "$SESSION_ID" "$AGENT_ID" "$AGENT_TYPE" || exit 0

REASON="Helper(s) you launched are still running: $(printf '%s' "$RUNNING" | paste -sd ',' - | sed 's/,/, /g'). Wait for each result before handing back — your report is the caller's verdict, and one written while a helper still works describes work in progress (agents/_TEMPLATE.md \"Delegating agents wait for their helpers\")."
jq -cn --arg r "$REASON" '{decision: "block", reason: $r}'
exit 0
