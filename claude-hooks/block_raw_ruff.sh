#!/usr/bin/env bash
# Block raw `ruff check` / `ruff format` invocations from Bash.
# FOUNDATION §2: agents use forge-precommit (which internally calls ruff
# via Python subprocess, not via the Bash tool — this hook doesn't see
# it). No forge agent currently needs to call raw ruff via Bash, so there
# is no bypass list.
set -e
INPUT=$(cat)
COMMAND=$(jq -r '.tool_input.command // empty' <<< "$INPUT")
# Anchors and the command-positions views live in the shared lib (one home
# for every guard that locates a command).
ANCHOR_LIB="$(dirname "$0")/git_anchor.sh"
if [ ! -r "$ANCHOR_LIB" ]; then
    # Fail CLOSED: a missing/unreadable lib (corrupted plugin cache) must
    # block, not silently disarm the guard — only exit 2 blocks in the
    # PreToolUse contract.
    echo "BLOCKED: guard anchor lib missing at $ANCHOR_LIB — refusing the command rather than running unguarded." >&2
    exit 2
fi
source "$ANCHOR_LIB"

# Anchor: ruff at start-of-string or after a shell separator (incl. the
# `(` of a subshell or command substitution), matched on the shared
# command-positions view (git_anchor.sh): quoted bodies (PR descriptions,
# commit messages mentioning "ruff") are blanked there and never fire,
# while `bash -c "ruff check ..."` stays visible and does. fix-forge-ruff*
# and similar one-token wrappers don't match because ruff sits mid-string
# in those. A help request (`ruff check --help`) runs no check.
CMDPOS=$(command_positions "$COMMAND")
RUFF_RE='(^|[;&|(]\s*)ruff\s+(check|format)'
if echo "$CMDPOS" | grep -qE "$RUFF_RE" && ! guard_help_only "$CMDPOS" "$RUFF_RE"; then
    echo "BLOCKED: raw 'ruff' from Bash is forbidden (FOUNDATION §2). Use forge-precommit — it verifies and self-heals (ruff format + ruff check --fix --unsafe-fixes on failure). Agents delegate to the forge:precommit-fixer agent." >&2
    exit 2
fi
