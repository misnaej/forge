#!/usr/bin/env bash
# Block raw `git commit` / `git push` invocations from Bash.
# FOUNDATION §3 — commits and pushes go through `forge-commit`, which
# enforces the commit guards in its own code (FOUNDATION §7: hooks see only
# the command an agent types, never a CLI's internal git calls). No agent
# bypass: every agent, subagent or not, commits through the CLI.
set -e
INPUT=$(cat)
COMMAND=$(jq -r '.tool_input.command // empty' <<< "$INPUT")

# Anchor + rationale live in the shared lib (one home for the whole
# git-guard family — issue #348).
ANCHOR_LIB="$(dirname "$0")/git_anchor.sh"
if [ ! -r "$ANCHOR_LIB" ]; then
    # Fail CLOSED: a missing/unreadable lib (corrupted plugin cache)
    # must block, not silently disarm the whole guard family — only
    # exit 2 is a block signal in the PreToolUse contract.
    echo "BLOCKED: guard anchor lib missing at $ANCHOR_LIB — refusing the command rather than running unguarded." >&2
    exit 2
fi
source "$ANCHOR_LIB"
if echo "$COMMAND" | grep -qE "${GIT_ANCHOR}(commit|push)\b"; then
    echo "BLOCKED: raw 'git commit' / 'git push' from Bash is forbidden by FOUNDATION §3. Use forge-commit: 'forge-commit -m \"<type>: <subject>\" (--all | <paths>)' commits and pushes with the guards checked; 'forge-commit --push-only' pushes existing commits; 'forge-commit --wip-sync -m \"wip-sync: <what>\"' makes a checkpoint commit." >&2
    exit 2
fi
