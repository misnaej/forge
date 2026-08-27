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

# Anchor + rationale live in the shared lib (one home for the whole
# git-guard family — issue #348).
ANCHOR_LIB="$(dirname "$0")/git_anchor.sh"
if [ ! -r "$ANCHOR_LIB" ]; then
    # Fail CLOSED: a missing/unreadable lib (corrupted plugin cache)
    # must block, not silently disarm the whole guard family — only
    # exit 2 is a block signal in the PreToolUse contract.
    echo "BLOCKED: git-guard anchor lib missing at $ANCHOR_LIB — refusing the command rather than running unguarded." >&2
    exit 2
fi
source "$ANCHOR_LIB"
if echo "$COMMAND" | grep -qE "${GIT_ANCHOR}(commit|push)\b"; then
    echo "BLOCKED: raw 'git commit' / 'git push' from Bash is forbidden by FOUNDATION §3 mandatory-delegation. Use the forge:git-commit-push agent — it runs pre-commit, signs the commit per the convention, and pushes with the right tracking flags." >&2
    exit 2
fi
