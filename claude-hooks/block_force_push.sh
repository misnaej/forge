#!/usr/bin/env bash
# Block force push without explicit user approval.
#
# All four force vectors are caught, not just the long flags:
#   --force / --force-with-lease   long flags (incl. =value forms)
#   -f  /  -uf  /  -fu             a short-flag cluster containing `f`
#   origin +main / +src:dst        a `+`-prefixed (force) refspec token
# `git push` short flags never contain `f` except `--force`, and a leading
# `+` on a whitespace-delimited arg is only ever a force refspec, so these
# patterns don't over-block a benign push. A push through a variable in
# the command-word slot (`$G push -f`) is judged the same way: the hook
# cannot tell whether the variable runs git, so force flags there block.
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
    echo "BLOCKED: guard anchor lib missing at $ANCHOR_LIB — refusing the command rather than running unguarded." >&2
    exit 2
fi
source "$ANCHOR_LIB"
guard_begin
# Find the push on the command-positions view (quoted mentions are not
# pushes; `bash -c "git push -f"` is), then read its flags from the
# --words view, where a quoted `"--force"` is still the flag git receives.
#
# A variable in the command-word slot (`$G push -f`, GIT_UNKNOWN_ANCHOR
# in git_anchor.sh) may be git, so a push there is judged the same way —
# blocked when the force flags are present, allowed otherwise. The
# accepted false block is a non-git program held in a variable and given
# a `push` verb and a force-shaped flag.
CMDPOS=$(command_positions "$COMMAND")
if [ -z "${GIT_UNKNOWN_ANCHOR:-}" ]; then
    echo "BLOCKED: the guard anchor lib at $ANCHOR_LIB lacks a pattern this hook needs (older or damaged plugin copy) — refusing the command rather than running unguarded." >&2
    exit 2
fi
PUSH_ANCHOR="(${GIT_ANCHOR}|${GIT_UNKNOWN_ANCHOR})push\b"
if ! echo "$CMDPOS" | grep -qE "${PUSH_ANCHOR}"; then
    exit 0
fi
if guard_help_only "$CMDPOS" "${PUSH_ANCHOR}"; then
    exit 0
fi
WORDS=$(command_positions --words --no-redir "$COMMAND")
# Force-flag detection is scoped to the matched invocation (bounded at
# the next command separator), so a flag in another segment of a
# compound command never false-positives.
# Redirections are blanked (--no-redir), so the `&` of `2>&1` is not
# taken for that separator.
if echo "$WORDS" | grep -qE -- \
    "${PUSH_ANCHOR}[^;&|]*(--force|--force-with-lease|[[:space:]]-[a-zA-Z]*f[a-zA-Z]*\b|[[:space:]]\+[^[:space:]]+)"; then
    echo "BLOCKED: Force push is not allowed for agents. Suggest the user run the command themselves with: ! $COMMAND" >&2
    exit 2
fi
