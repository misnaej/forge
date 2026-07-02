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
# Only check git push commands. Anchor at start-of-string OR after a shell
# separator (and allow any whitespace between `git` and `push`) so a chained
# `foo; git push --force` or a doubled-space `git  push -f` can't slip past
# the gate — matching block_git_rebase.sh / block_raw_git.sh.
if ! echo "$COMMAND" | grep -qE '(^|[;&|]\s*)git\s+push\b'; then
    exit 0
fi
if echo "$COMMAND" | grep -qE -- \
    '--force|--force-with-lease|(^|[[:space:]])-[a-zA-Z]*f|[[:space:]]\+[^[:space:]]+'; then
    echo "BLOCKED: Force push is not allowed for agents. Suggest the user run the command themselves with: ! $COMMAND" >&2
    exit 2
fi
