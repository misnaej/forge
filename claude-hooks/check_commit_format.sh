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

if [ -n "$MSG" ]; then
    if ! echo "$MSG" | grep -qE "^(${CONVENTIONAL_TYPES})(\(.+\))?(!)?:"; then
        echo "WARNING: Commit message should follow conventional format: type(scope): description (types: ${CONVENTIONAL_TYPES})"
    fi
fi
