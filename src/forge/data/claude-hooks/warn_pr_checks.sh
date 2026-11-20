#!/usr/bin/env bash
# Warn to verify all checkers ran before creating PR
set -e
INPUT=$(cat)
# Shared command-parsing lib (fail closed when missing — exit 2 is the
# only block signal in the PreToolUse contract).
CMD_LIB="$(dirname "$0")/lib/command_utils.sh"
if [ ! -r "$CMD_LIB" ]; then
    echo "BLOCKED: command-utils lib missing at $CMD_LIB — refusing the command rather than running unguarded." >&2
    exit 2
fi
source "$CMD_LIB"
COMMAND=$(parse_command_segment)
# Advisory only: without the shared lib there is nothing to locate with,
# and a reminder must never turn into a failure.
ANCHOR_LIB="$(dirname "$0")/git_anchor.sh"
[ -r "$ANCHOR_LIB" ] || exit 0
# shellcheck source=git_anchor.sh
source "$ANCHOR_LIB"
# Found on the shared command-positions view: a quoted mention creates
# nothing, and neither does a help request.
CMDPOS=$(command_positions "$COMMAND")
CREATE_RE="${GH_ANCHOR}pr[[:space:]]+${GH_OPTS}create\b"
if echo "$CMDPOS" | grep -qE "$CREATE_RE" && ! guard_help_only "$CMDPOS" "$CREATE_RE"; then
    echo "REMINDER: Before creating a PR, verify these agents ran: forge-design-checker, forge-security-checker, forge-precommit-fixer (mode: strict). Use forge-pr-manager subagent for the full workflow."
fi
