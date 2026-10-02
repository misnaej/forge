#!/usr/bin/env bash
# Refuse a precommit-fixer hand-back that the pre-commit verdict contradicts.
#
# The fixer's report is what the commit agent and the PR flow act on, and
# as prose it was wrong in both directions: "all checks passed" over failed
# or stale checks, and "semantics preserved" over a behavior change. The
# agent's contract (agents/precommit-fixer.md) makes the verdict a pasted
# `forge-precommit --verdict` output; this SubagentStop hook is the
# mechanical half: when the fixer stops, it runs the verdict itself.
#
#   verdict passes                                  → allow the stop
#   verdict fails, final message reports STUCK with
#   the failing verdict pasted ("verdict: FAIL")     → allow (honest hand-back)
#   verdict fails otherwise, first stop             → block once, the real
#                                                     verdict as the reason
#   same agent stops again                          → allow (never loops)
#
# The block-once mark is a `verdict_block` line in the agent-timing ledger
# (code_health/agent_timing.jsonl, keyed by agent_id), the ledger the
# fixer's full-run cap already uses. Fails open without an agent_id or a
# repo root — the stop cannot be keyed then.
set -e
INPUT=$(cat)
{
    IFS= read -r AGENT_TYPE
    IFS= read -r AGENT_ID
    IFS= read -r SESSION_ID
    IFS= read -r CWD
} <<< "$(jq -r '.agent_type // "", .agent_id // "", .session_id // "", .cwd // "."' <<< "$INPUT" 2>/dev/null)"

if [ "$AGENT_TYPE" != "precommit-fixer" ] && [ "$AGENT_TYPE" != "forge:precommit-fixer" ]; then
    exit 0
fi
[ -n "$AGENT_ID" ] || exit 0
ROOT=${CLAUDE_PROJECT_DIR:-}
[ -n "$ROOT" ] && [ -e "$ROOT/.git" ] || ROOT=$(git -C "${CWD:-.}" rev-parse --show-toplevel 2>/dev/null) || exit 0
LEDGER="$ROOT/code_health/agent_timing.jsonl"

# Already blocked once: let it through so a genuinely stuck agent can stop.
NEEDLE=$(jq -rn --arg a "$AGENT_ID" '"\"agent_id\":" + ($a | tojson)' 2>/dev/null) || NEEDLE=""
if [ -n "$NEEDLE" ] && [ -r "$LEDGER" ] \
    && grep -F '"event":"verdict_block"' "$LEDGER" 2>/dev/null | grep -qF "$NEEDLE"; then
    exit 0
fi

if command -v forge-precommit >/dev/null 2>&1; then
    set +e
    VERDICT=$(cd "$ROOT" && forge-precommit --verdict 2>&1)
    RC=$?
    set -e
else
    VERDICT="forge-precommit not found on PATH — forge-scripts is not installed. Install it (forge: ./dev/setup.sh; consumers: add forge-scripts to the dev environment), then re-run forge-precommit."
    RC=1
fi
[ "$RC" = 0 ] && exit 0

LAST=$(jq -r '.last_assistant_message // ""' <<< "$INPUT" 2>/dev/null) || LAST=""
case "$LAST" in
    *STUCK*"verdict: FAIL"*|*"verdict: FAIL"*STUCK*) exit 0 ;;
esac

mkdir -p "$ROOT/code_health" 2>/dev/null || true
LINE=$(jq -cn --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --arg s "$SESSION_ID" \
    --arg a "$AGENT_ID" --arg t "$AGENT_TYPE" \
    '{ts: $ts, event: "verdict_block", session_id: $s, agent_id: $a, agent_type: $t}' \
    2>/dev/null) && printf '%s\n' "$LINE" >> "$LEDGER" 2>/dev/null || true

REASON="The pre-commit verdict does not pass, so this hand-back cannot report success. Fix what it names and re-run forge-precommit, or hand back a STUCK block with this output pasted verbatim:
$VERDICT"
jq -cn --arg r "$REASON" '{decision: "block", reason: $r}'
exit 0
