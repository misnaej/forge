#!/usr/bin/env bash
# Warn to verify all checkers ran before creating PR
set -e
INPUT=$(cat)
COMMAND=$(echo "$INPUT" | jq -r '.tool_input.command // empty')
if echo "$COMMAND" | grep -qE 'gh pr create'; then
    echo "REMINDER: Before creating a PR, verify these agents ran: design-checker, security-checker, precommit-fixer (mode: strict). Use pr-manager subagent for the full workflow."
fi
