#!/usr/bin/env bash
# Block agent-initiated PR merges.
#
# Merging a PR is high blast radius (puts code on the base branch, bypasses
# further review) and effectively irreversible without force-push to main.
# Agents should never make that call autonomously — the human is in the loop
# at the merge step.
#
# To merge a PR, the user runs the command themselves (the `!` prefix at the
# Claude Code prompt sends the command through the user's shell, bypassing
# agent hooks):
#     ! gh pr merge 9 --squash --delete-branch
set -e
INPUT=$(cat)
COMMAND=$(echo "$INPUT" | jq -r '.tool_input.command // empty')

# `gh pr merge` at start-of-string, or after a shell separator (`;`, `&&`,
# `||`, `|`). A plain space ahead of `gh` is NOT a separator — that lets
# `echo gh pr merge` through, which is harmless (we want to block actual
# merges, not text mentions of the command).
if echo "$COMMAND" | grep -qE '(^|[[:space:]]*[|;&]+[[:space:]]*)gh +pr +merge\b'; then
    echo "BLOCKED: agents must not merge PRs. Merging is the user's call. Have the user run: ! $COMMAND" >&2
    exit 2
fi

# Direct API merges that achieve the same effect.
if echo "$COMMAND" | grep -qE 'gh +api[^|]*pulls/[0-9]+/merge'; then
    echo "BLOCKED: agents must not merge PRs via the API. Have the user run: ! $COMMAND" >&2
    exit 2
fi

exit 0
