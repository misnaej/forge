#!/usr/bin/env bash
# Validate conventional commit format. Handles three -m message shapes:
#   1. -m "single-line message"
#   2. -m 'single-line message'
#   3. -m "$(cat <<'EOF'
#         subject line
#         ...
#         EOF
#         )"           ← multi-line heredoc pattern for commit messages
set -e
INPUT=$(cat)
COMMAND=$(echo "$INPUT" | jq -r '.tool_input.command // empty')
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
# Fail CLOSED on a lib that predates a helper this hook calls.
if ! declare -F command_positions guard_help_only > /dev/null; then
    echo "BLOCKED: the guard anchor lib at $ANCHOR_LIB lacks a helper this hook needs (older or damaged plugin copy) — refusing the command rather than running unguarded." >&2
    exit 2
fi

# Try heredoc pattern first: the subject is the first non-empty line
# after `<<'EOF'` (or unquoted `<<EOF`).
MSG=$(echo "$COMMAND" | awk "
    /<<'?EOF'?/ { in_heredoc=1; next }
    in_heredoc && /^[[:space:]]*EOF[[:space:]]*\$/ { exit }
    in_heredoc && /^[[:space:]]*\$/ { next }
    in_heredoc { gsub(/^[[:space:]]+/, \"\"); print; exit }
" 2>/dev/null || true)

# Fall back to -m "..." or -m '...' single-line extraction, with POSIX
# tools only: BSD grep (macOS) has no `-P`, and a lookbehind there emptied
# MSG for every single-quoted message, flipping the wip-sync pairing below
# both ways. Both expressions read the FIRST `-m` of a line — the
# subject, not a later body `-m`: MSG_PFX is any text holding no `-m`
# that opens a quoted message (a `-m` inside a path, `/a-m/x`, is passed
# over). The first expression takes a double-quoted message; `t` ends the
# line there, so only a line it did not match tries the single-quoted
# one. A sed failure is remembered: on a commit it blocks below rather
# than reading as "no message".
MSG_EXTRACT_FAILED=false
if [ -z "$MSG" ]; then
    MSG_PFX="([^-]|-[^m]|-m[[:space:]]*[^\"'[:space:]])*"
    MSG=$(printf '%s\n' "$COMMAND" | sed -E -n \
        -e "s/^${MSG_PFX}-m[[:space:]]*\"([^\"]*)\".*/\\2/p" \
        -e t \
        -e "s/^${MSG_PFX}-m[[:space:]]*'([^']*)'.*/\\2/p") || {
        MSG=""
        MSG_EXTRACT_FAILED=true
    }
fi

# FORGE_COMMIT_TYPES_BEGIN — managed by `forge-gen-commit-types`. The
# canonical type list lives in the forge package; run
# `forge-gen-commit-types` (shipped with forge-scripts) to regenerate
# the block below.
CONVENTIONAL_TYPES='feat|fix|refactor|test|docs|chore|perf|ci|build|style|revert'
# FORGE_COMMIT_TYPES_END

# wip-sync checkpoint pairing (FOUNDATION §2 sync ladder): the env
# marker FORGE_WIP_SYNC=1 defers the pre-commit gate, so it must be
# impossible to use it with a normal-looking message (silent gate skip)
# or to label a fully-gated commit as a checkpoint. Both directions
# block; a correctly paired checkpoint is exempt from the
# conventional-format warning.
# Scope the pairing rule to actual commit invocations — this hook fires
# on every Bash call, and a grep/forge-precommit command legitimately
# containing the literal FORGE_WIP_SYNC=1 must not be mistaken for a
# checkpoint commit.
IS_COMMIT=false
# Both the commit and the env marker are read from the shared
# command-positions view: a quoted mention of either is not one.
CMDPOS=$(command_positions "$COMMAND")
if echo "$CMDPOS" | grep -qE "${GIT_ANCHOR}commit\b" \
    && ! guard_help_only "$CMDPOS" "${GIT_ANCHOR}commit\b"; then
    IS_COMMIT=true
fi
HAS_WIP_ENV=false
HAS_WIP_MSG=false
if $IS_COMMIT && echo "$CMDPOS" | grep -qE '(^|[;&|[:space:]])FORGE_WIP_SYNC=1([;&|[:space:]]|$)'; then
    HAS_WIP_ENV=true
fi
if ! $IS_COMMIT; then
    HAS_WIP_MSG=false
    MSG=""
elif $MSG_EXTRACT_FAILED; then
    echo "BLOCKED: could not read the commit message from this command (the message extraction failed), so the wip-sync pairing check cannot run — refusing the commit rather than skipping the check." >&2
    exit 2
fi
if [ -n "$MSG" ] && echo "$MSG" | grep -qE '^wip-sync:'; then
    HAS_WIP_MSG=true
fi
if $HAS_WIP_ENV && ! $HAS_WIP_MSG; then
    echo "BLOCKED: FORGE_WIP_SYNC=1 requires a commit message starting 'wip-sync:' — the deferred gate must be visible in history (FOUNDATION §2 sync ladder)." >&2
    exit 2
fi
if $HAS_WIP_MSG && ! $HAS_WIP_ENV; then
    echo "BLOCKED: a 'wip-sync:' message without FORGE_WIP_SYNC=1 mislabels a fully-gated commit as a checkpoint — pair them or rename the commit." >&2
    exit 2
fi

if [ -n "$MSG" ] && ! $HAS_WIP_MSG; then
    if ! echo "$MSG" | grep -qE "^(${CONVENTIONAL_TYPES})(\(.+\))?(!)?:"; then
        echo "WARNING: Commit message should follow conventional format: type(scope): description (types: ${CONVENTIONAL_TYPES})"
    fi
fi
