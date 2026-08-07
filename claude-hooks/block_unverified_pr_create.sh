#!/usr/bin/env bash
# Block `gh pr create` until the wrap-up is authored at the current HEAD.
#
# FOUNDATION §6 "PR finalization": verification precedes publication, and
# the wrap-up + squash message are AUTHORED before the PR exists — only
# their posting needs a PR. The evidence is code_health/pr_wrapup.md whose
# first lines carry `verified-at: <sha>`; this hook refuses PR creation
# when that file is missing or names a different commit, so a tree can't
# be published with verification (or its record) stale or skipped.
#
# The /pr skill's Step 3.92 writes the file. Two sanctioned bypasses:
#   - a human runs it directly:      ! gh pr create ...
#   - the USER asks to skip the gate: the agent prefixes the command with
#     FORGE_SKIP_WRAPUP_GATE=1 — only on an explicit user request, never
#     on the agent's own judgment.
set -e
INPUT=$(cat)
COMMAND=$(echo "$INPUT" | jq -r '.tool_input.command // empty')

# `gh pr create` at start-of-string or after a shell separator — same
# matching convention as block_pr_merge.sh (a plain space ahead of `gh`
# is not a separator, letting text mentions through).
if ! echo "$COMMAND" | grep -qE '(^|[[:space:]]*[|;&]+[[:space:]]*)gh +pr +create\b'; then
    exit 0
fi

if echo "$COMMAND" | grep -q 'FORGE_SKIP_WRAPUP_GATE=1' \
    || [ "${FORGE_SKIP_WRAPUP_GATE:-}" = "1" ]; then
    exit 0
fi

REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null || echo ".")
WRAPUP="$REPO_ROOT/code_health/pr_wrapup.md"
if [ ! -f "$WRAPUP" ]; then
    echo "BLOCKED: no authored wrap-up at $WRAPUP. FOUNDATION §6: author the wrap-up (\`/pr\` Step 3.92) before creating the PR." >&2
    exit 2
fi

HEAD_SHA=$(git rev-parse HEAD 2>/dev/null || true)
if [ -z "$HEAD_SHA" ]; then
    exit 0  # not a git repo — nothing to verify against
fi

if ! head -5 "$WRAPUP" | grep -qE "verified-at:.*(${HEAD_SHA}|${HEAD_SHA:0:7})"; then
    echo "BLOCKED: $WRAPUP does not name current HEAD (${HEAD_SHA:0:7}) in a verified-at: line — the wrap-up was authored for a different tree. Re-run /pr Step 3.92." >&2
    exit 2
fi

exit 0
