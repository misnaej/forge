#!/usr/bin/env bash
# Shared block-once mark for the SubagentStop gates.
#
# NOT a hook — a sourced library (never registered in plugin.json).
# Consumers: require_fixer_verdict.sh, require_helpers_stopped.sh, via:
#
#     source "$(dirname "$0")/once_mark.sh"
#
# A stop gate refuses an agent's hand-back at most once per agent: the
# first refusal writes a mark to the agent-timing ledger, and a later stop
# by the same agent finds it and passes, so a genuinely stuck agent can
# always stop. The mark must be on disk BEFORE the gate blocks — a block
# whose mark cannot be written would recur on every stop and never end, so
# a failed write makes the gate allow the stop instead (fail open, never
# loop). That ordering is the invariant this file exists to keep in one
# place.

# once_mark_paths ROOT — set CH_DIR and LEDGER for ROOT.
# FORGE_CODE_HEALTH_DIR when set (absolute, or relative to ROOT), else
# ROOT/code_health — the same rule as forge.git_utils.code_health_dir.
once_mark_paths() {
    CH_DIR=${FORGE_CODE_HEALTH_DIR:-code_health}
    case "$CH_DIR" in /*) ;; *) CH_DIR="$1/$CH_DIR" ;; esac
    LEDGER="$CH_DIR/agent_timing.jsonl"
}

# once_mark_seen EVENT AGENT_ID — 0 when LEDGER already holds an EVENT
# mark for AGENT_ID.
once_mark_seen() {
    local needle
    needle=$(jq -rn --arg a "$2" '"\"agent_id\":" + ($a | tojson)' 2>/dev/null) || return 1
    [ -n "$needle" ] && [ -r "$LEDGER" ] \
        && grep -F "\"event\":\"$1\"" "$LEDGER" 2>/dev/null | grep -qF "$needle"
}

# once_mark_write EVENT SESSION_ID AGENT_ID AGENT_TYPE — append the mark;
# non-zero when it could not be written (the caller must then allow).
once_mark_write() {
    local line
    mkdir -p "$CH_DIR" 2>/dev/null || return 1
    line=$(jq -cn --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --arg e "$1" \
        --arg s "$2" --arg a "$3" --arg t "$4" \
        '{ts: $ts, event: $e, session_id: $s, agent_id: $a, agent_type: $t}' \
        2>/dev/null) || return 1
    printf '%s\n' "$line" >> "$LEDGER" 2>/dev/null
}
