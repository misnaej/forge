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
# Anchors + their rationale live in the shared lib (one home for the
# whole git-/gh-guard family).
ANCHOR_LIB="$(dirname "$0")/git_anchor.sh"
if [ ! -r "$ANCHOR_LIB" ]; then
    # Fail CLOSED: a missing/unreadable lib (corrupted plugin cache) must
    # block, not silently disarm the guard — only exit 2 blocks in the
    # PreToolUse contract.
    echo "BLOCKED: guard anchor lib missing at $ANCHOR_LIB — refusing the command rather than running unguarded." >&2
    exit 2
fi
source "$ANCHOR_LIB"

# `gh pr merge` at a real invocation position (see GH_ANCHOR in
# git_anchor.sh for the exact shape). A plain space ahead of `gh` with
# no separator or wrapper token is NOT an invocation — that lets
# `echo gh pr merge` through, which is harmless (we want to block actual
# merges, not text mentions of the command). Found on the
# command-positions view (git_anchor.sh), so a quoted mention never fires
# and `bash -c "gh pr merge 1"` does; a help request merges nothing.
CMDPOS=$(command_positions "$COMMAND")
if echo "$CMDPOS" | grep -qE "${GH_ANCHOR}pr[[:space:]]+merge\b" \
    && ! guard_help_only "$CMDPOS" "${GH_ANCHOR}pr[[:space:]]+merge\b"; then
    echo "BLOCKED: agents must not merge PRs. Merging is the user's call. Have the user run: ! $COMMAND" >&2
    exit 2
fi

# Direct API merges that achieve the same effect: a real `gh api` call,
# whose (often quoted) endpoint is read from the --words view.
if echo "$CMDPOS" | grep -qE "${GH_ANCHOR}api\b" \
    && command_positions --words "$COMMAND" | grep -qE 'gh +api[^|]*pulls/[0-9]+/merge'; then
    echo "BLOCKED: agents must not merge PRs via the API. Have the user run: ! $COMMAND" >&2
    exit 2
fi

exit 0
