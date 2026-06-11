#!/usr/bin/env bash
# Block --no-verify flag on git commit
set -e
INPUT=$(cat)
COMMAND=$(echo "$INPUT" | jq -r '.tool_input.command // empty')
# Only check git commit/push commands, not arbitrary text containing the string
if ! echo "$COMMAND" | grep -qE '^git (commit|push)'; then
    exit 0
fi
if echo "$COMMAND" | grep -qE -- '--no-verify'; then
    echo "BLOCKED: --no-verify is forbidden. Fix the violations instead of bypassing pre-commit hooks. If absolutely required, the user can run: ! $COMMAND" >&2
    exit 2
fi
