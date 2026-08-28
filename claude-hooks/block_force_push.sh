#!/usr/bin/env bash
# Block force push without explicit user approval.
#
# All four force vectors are caught, not just the long flags:
#   --force / --force-with-lease   long flags (incl. =value forms)
#   -f  /  -uf  /  -fu             a short-flag cluster containing `f`
#   origin +main / +src:dst        a `+`-prefixed (force) refspec token
# `git push` short flags never contain `f` except `--force`, and a leading
# `+` on a whitespace-delimited arg is only ever a force refspec, so these
# patterns don't over-block a benign push.
set -e
INPUT=$(cat)
COMMAND=$(echo "$INPUT" | jq -r '.tool_input.command // empty')
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
if ! echo "$COMMAND" | grep -qE "${GIT_ANCHOR}push\b"; then
    exit 0
fi
# Force-flag detection is scoped to the matched invocation (bounded at
# the next command separator), so a flag in another segment of a
# compound command never false-positives (issue #348 scoping fix).
if echo "$COMMAND" | grep -qE -- \
    "${GIT_ANCHOR}push\b[^;&|]*(--force|--force-with-lease|[[:space:]]-[a-zA-Z]*f[a-zA-Z]*\b|[[:space:]]\+[^[:space:]]+)"; then
    echo "BLOCKED: Force push is not allowed for agents. Suggest the user run the command themselves with: ! $COMMAND" >&2
    exit 2
fi
