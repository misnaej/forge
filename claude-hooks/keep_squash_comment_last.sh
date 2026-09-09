#!/usr/bin/env bash
# PostToolUse(Bash): keep the squash-merge comment the PR's newest comment.
#
# FOUNDATION §6 — the human merging copies the squash body out of the PR
# conversation, so it belongs at the bottom, under whatever was posted
# last. That is machinery, not agent discipline: any command that
# comments on a PR triggers `forge-pr-squash-comment --pr N`, which
# re-posts the existing comment (quiet no-op when it is already newest).
#
# Deliberately no `set -e`: every probe here may legitimately fail (no
# match, no PR, no squash comment yet) and none of those are errors — a
# post-tool hook must never turn a working command into a failed one.
set -uo pipefail

INPUT=$(cat)
COMMAND=$(printf '%s' "$INPUT" | jq -r '.tool_input.command // empty')
[ -n "$COMMAND" ] || exit 0

# No self-exemption for forge-pr-squash-comment: the hook fires only on
# `gh pr comment|review` text, which the CLI's own invocation never
# contains. Exempting on a mention would let a chained or multi-line
# command post a comment and skip the re-post.

# The three surfaces that bury a comment: conversation comments, review
# submissions, and review-thread replies (REST `/pulls/<N>/comments`).
if ! printf '%s' "$COMMAND" |
    grep -qE 'gh +pr +(comment|review)|/pulls/[0-9]+/comments'; then
    exit 0
fi

command -v forge-pr-squash-comment >/dev/null 2>&1 || exit 0

PR=$(printf '%s' "$COMMAND" | grep -oE '/pulls/[0-9]+/comments' |
    grep -oE '[0-9]+' | head -1)
if [ -z "$PR" ]; then
    PR=$(printf '%s' "$COMMAND" | grep -oE 'gh +pr +(comment|review) +[0-9]+' |
        grep -oE '[0-9]+$' | head -1)
fi
if [ -z "$PR" ]; then
    # `gh pr comment` with no number targets the current branch's PR.
    PR=$(gh pr view --json number --jq .number 2>/dev/null)
fi
[ -n "$PR" ] || exit 0

# A PR whose squash comment has not been authored yet is the normal
# mid-review case: the CLI exits non-zero and the hook stays silent.
OUT=$(forge-pr-squash-comment --pr "$PR" 2>&1) || exit 0
case "$OUT" in
*"already the newest"*) exit 0 ;;
esac
echo "[forge] squash-merge comment re-posted as the newest comment on PR #$PR"
