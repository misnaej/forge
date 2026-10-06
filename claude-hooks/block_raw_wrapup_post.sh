#!/usr/bin/env bash
# PreToolUse(Bash): the wrap-up comment is posted only through forge-pr-wrapup.
#
# FOUNDATION §6 — the wrap-up is the verification record; its length rule
# (report by exception) and its lifecycle (superseded wrap-ups collapse,
# the squash comment stays newest) are enforced by `forge-pr-wrapup post`.
# A raw `gh pr comment --body-file code_health/pr_wrapup.md` skips every
# one of those gates, so it is blocked at the cause — same family as
# block_raw_git / block_raw_ruff.
set -e
INPUT=$(cat)
COMMAND=$(jq -r '.tool_input.command // empty' <<< "$INPUT")
[ -n "$COMMAND" ] || exit 0
# Anchors and the command-positions views live in the shared lib (one home
# for every guard that locates a command).
ANCHOR_LIB="$(dirname "$0")/git_anchor.sh"
if [ ! -r "$ANCHOR_LIB" ]; then
    # Fail CLOSED: a missing/unreadable lib (corrupted plugin cache) must
    # block, not silently disarm the guard — only exit 2 blocks in the
    # PreToolUse contract.
    echo "BLOCKED: guard anchor lib missing at $ANCHOR_LIB — refusing the command rather than running unguarded." >&2
    exit 2
fi
source "$ANCHOR_LIB"

# No self-exemption for forge-pr-wrapup: its internal `gh pr comment` runs
# as a subprocess, never as the Bash tool's command, and the block regex
# needs a raw post the CLI's own invocation never contains. Exempting on a
# mention would let a chained or multi-line command smuggle a raw post through.

# The gh call is found on the shared command-positions view (a quoted
# mention posts nothing; `bash -c "gh pr comment …"` does); the posted
# path is read from the raw text, where a quoted path is still the file.
CMDPOS=$(command_positions "$COMMAND")
POST_RE="${GH_ANCHOR}(pr[[:space:]]+comment|api)\b"
if echo "$CMDPOS" | grep -qE "$POST_RE" && ! guard_help_only "$CMDPOS" "$POST_RE" \
    && echo "$COMMAND" | grep -qE '(--body-file|-F|--field|-f|--raw-field)[[:space:]=]+(body=@)?[^[:space:]]*code_health/pr_wrapup\.md\b'; then
    echo "BLOCKED: posting code_health/pr_wrapup.md with a raw gh command skips the wrap-up gates (FOUNDATION §6). Use \`forge-pr-wrapup post --pr <N> --body-file code_health/pr_wrapup.md\` — it validates report-by-exception, collapses superseded wrap-ups, and keeps the squash comment newest." >&2
    exit 2
fi
