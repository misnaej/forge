#!/usr/bin/env bash
# Block --no-verify flag on git commit
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
# Only check git commit/push commands, not arbitrary text containing the string
if ! echo "$COMMAND" | grep -qE "${GIT_ANCHOR}(commit|push)\b"; then
    exit 0
fi
# `-n` is `--no-verify` for a commit but `--dry-run` for a push, so
# the short form counts only on the former. Residual shared with the
# family: the flag is matched anywhere in the segment, so a quoted
# body naming it reads as the flag — and a quoted body containing a
# separator hides a flag that follows it.
if echo "$COMMAND" | grep -qE -- '--no-verify' \
    || echo "$COMMAND" | grep -qE "${GIT_ANCHOR}commit\b[^;&|]*[[:space:]]-[a-zA-Z]*n[a-zA-Z]*([[:space:]]|[;&|)]|$)"; then
    echo "BLOCKED: --no-verify is forbidden. Fix the violations instead of bypassing pre-commit hooks. If absolutely required, the user can run: ! $COMMAND" >&2
    exit 2
fi
