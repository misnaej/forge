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
# Only check git commit/push commands, not arbitrary text containing the
# string. Commands are found on the command-positions view (git_anchor.sh):
# a quoted mention is not a commit, `bash -c "git commit …"` is.
CMDPOS=$(command_positions "$COMMAND")
if ! echo "$CMDPOS" | grep -qE "${GIT_ANCHOR}(commit|push)\b" \
    || guard_help_only "$CMDPOS" "${GIT_ANCHOR}(commit|push)\b"; then
    exit 0
fi
# `-n` is `--no-verify` for a commit but `--dry-run` for a push, so
# the short form counts only on the former. The long flag is matched in
# the raw text — a quoted `"--no-verify"` is still the flag, and a quoted
# body naming it reads as the flag (conservative). The short form is read
# from the --words view, where a separator inside a quoted body no longer
# ends the invocation early and hides a flag that follows it.
if echo "$COMMAND" | grep -qE -- '--no-verify' \
    || command_positions --words "$COMMAND" | grep -qE "${GIT_ANCHOR}commit\b[^;&|]*[[:space:]]-[a-zA-Z]*n[a-zA-Z]*([[:space:]]|[;&|)]|$)"; then
    echo "BLOCKED: --no-verify is forbidden. Fix the violations instead of bypassing pre-commit hooks. If absolutely required, the user can run: ! $COMMAND" >&2
    exit 2
fi
