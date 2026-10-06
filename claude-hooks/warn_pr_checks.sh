#!/usr/bin/env bash
# Warn to verify all checkers ran before creating PR
set -e
INPUT=$(cat)
COMMAND=$(echo "$INPUT" | jq -r '.tool_input.command // empty')
# Advisory only: without the shared lib there is nothing to locate with,
# and a reminder must never turn into a failure.
ANCHOR_LIB="$(dirname "$0")/git_anchor.sh"
[ -r "$ANCHOR_LIB" ] || exit 0
# shellcheck source=git_anchor.sh
source "$ANCHOR_LIB"
# Found on the shared command-positions view: a quoted mention creates
# nothing, and neither does a help request.
CMDPOS=$(command_positions "$COMMAND")
CREATE_RE="${GH_ANCHOR}pr[[:space:]]+create\b"
if echo "$CMDPOS" | grep -qE "$CREATE_RE" && ! guard_help_only "$CMDPOS" "$CREATE_RE"; then
    echo "REMINDER: Before creating a PR, verify these agents ran: design-checker, security-checker, precommit-fixer (mode: strict). Use pr-manager subagent for the full workflow."
fi
