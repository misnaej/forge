#!/usr/bin/env bash
# Block force push without explicit user approval
set -e
INPUT=$(cat)
COMMAND=$(echo "$INPUT" | jq -r '.tool_input.command // empty')
# Only check git push commands
if ! echo "$COMMAND" | grep -qE '^git push'; then
    exit 0
fi
if echo "$COMMAND" | grep -qE -- '--force|--force-with-lease'; then
    echo "BLOCKED: Force push is not allowed for agents. Suggest the user run the command themselves with: ! $COMMAND" >&2
    exit 2
fi
