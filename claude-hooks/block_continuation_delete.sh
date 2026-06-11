#!/usr/bin/env bash
# Block shell deletion of .plan/CONTINUATION.md — the only file that carries
# state across a context clear (FOUNDATION §10). Edits via the Write/Edit
# tools are unaffected; only rm/unlink of the file (or its .plan/ dir) is
# blocked.
set -e
INPUT=$(cat)
COMMAND=$(echo "$INPUT" | jq -r '.tool_input.command // empty')
# Only inspect removal commands, not arbitrary text mentioning the file.
if ! echo "$COMMAND" | grep -qE '\b(rm|unlink)\b'; then
    exit 0
fi
# Match the file directly, or removal of its parent .plan/ directory
# (rm -rf .plan would take CONTINUATION.md with it).
if echo "$COMMAND" | grep -qE 'CONTINUATION\.md|\.plan($|[^[:alnum:]_-])'; then
    echo "BLOCKED: refusing to delete .plan/CONTINUATION.md — it is the only file that carries state across a context clear (FOUNDATION §10). Rewrite its sections in place instead of deleting it (see /next Phase 6)." >&2
    exit 2
fi
