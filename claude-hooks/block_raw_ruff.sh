#!/usr/bin/env bash
# Block raw `ruff check` / `ruff format` invocations from Bash.
# FOUNDATION §2: agents use forge-precommit (which internally calls ruff
# via Python subprocess, not via the Bash tool — this hook doesn't see
# it). No forge agent currently needs to call raw ruff via Bash, so there
# is no bypass list.
set -e
INPUT=$(cat)
COMMAND=$(jq -r '.tool_input.command // empty' <<< "$INPUT")

# Anchor: ruff at start-of-string or after a shell separator. Quoted
# bodies (PR descriptions, commit messages mentioning "ruff") do not
# fire. verify-forge-ruff*, fix-forge-ruff*, and similar one-token
# wrappers don't match because ruff sits mid-string in those.
#
# Known accepted slip-through: `bash -c "ruff check ..."` — ruff sits
# after a quote, not a separator. Acceptable (matches
# block_install_deps.sh's xargs slip-through stance) — narrowing further
# re-introduces quoted-body false positives.
if echo "$COMMAND" | grep -qE '(^|[;&|]\s*)ruff\s+(check|format)'; then
    echo "BLOCKED: raw 'ruff' from Bash is forbidden (FOUNDATION §2). Use forge-precommit — it verifies and self-heals (ruff format + ruff check --fix --unsafe-fixes on failure). Agents delegate to the forge:precommit-fixer agent." >&2
    exit 2
fi
