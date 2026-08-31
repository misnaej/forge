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

# Try heredoc pattern first: the subject is the first non-empty line
# after `<<'EOF'` (or unquoted `<<EOF`).
MSG=$(echo "$COMMAND" | awk "
    /<<'?EOF'?/ { in_heredoc=1; next }
    in_heredoc && /^[[:space:]]*EOF[[:space:]]*\$/ { exit }
    in_heredoc && /^[[:space:]]*\$/ { next }
    in_heredoc { gsub(/^[[:space:]]+/, \"\"); print; exit }
" 2>/dev/null || true)

# Fall back to -m "..." or -m '...' single-line extraction.
if [ -z "$MSG" ]; then
    MSG=$(echo "$COMMAND" | grep -oP '(?<=-m\s["\x27])[^"\x27]+' 2>/dev/null \
        || echo "$COMMAND" | sed -n 's/.*-m "\([^"]*\)".*/\1/p' 2>/dev/null \
        || true)
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
if echo "$COMMAND" | grep -qE 'git([[:space:]]+[^;&|[:space:]]+)*[[:space:]]+commit\b'; then
    IS_COMMIT=true
fi
HAS_WIP_ENV=false
HAS_WIP_MSG=false
if $IS_COMMIT && echo "$COMMAND" | grep -qE '(^|[;&|[:space:]])FORGE_WIP_SYNC=1([;&|[:space:]]|$)'; then
    HAS_WIP_ENV=true
fi
if ! $IS_COMMIT; then
    HAS_WIP_MSG=false
    MSG=""
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
