#!/usr/bin/env bash
# Block raw `git commit` / `git push` invocations from Bash.
# FOUNDATION §3 mandatory-delegation — use the forge:git-commit-push agent.
#
# Bypass: the forge:git-commit-push agent itself must call these. The
# PreToolUse payload includes `agent_type` (the `name:` frontmatter of the
# calling subagent, per code.claude.com/docs/en/hooks). When that matches
# `git-commit-push` or `forge:git-commit-push`, allow the call.
set -e
INPUT=$(cat)
COMMAND=$(jq -r '.tool_input.command // empty' <<< "$INPUT")
AGENT_TYPE=$(jq -r '.agent_type // empty' <<< "$INPUT")

if [ "$AGENT_TYPE" = "git-commit-push" ] || [ "$AGENT_TYPE" = "forge:git-commit-push" ]; then
    # The one agent legitimately allowed to drive `git commit` / `git push`.
    exit 0
fi

# Anchor at start-of-string or after a shell separator so substrings
# inside quoted bodies (PR descriptions, commit messages) don't fire.
#
# Known accepted slip-through: `bash -c "git commit ..."` — git sits
# after a quote, not a separator. Acceptable (matches
# block_install_deps.sh's xargs slip-through stance).
if echo "$COMMAND" | grep -qE '(^|[;&|]\s*)git\s+(commit|push)\b'; then
    echo "BLOCKED: raw 'git commit' / 'git push' from Bash is forbidden by FOUNDATION §3 mandatory-delegation. Use the forge:git-commit-push agent — it runs pre-commit, signs the commit per the convention, and pushes with the right tracking flags." >&2
    exit 2
fi
