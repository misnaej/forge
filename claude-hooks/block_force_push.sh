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
# Only check git push commands. The anchor recognizes `git` at line-start or
# after a shell separator (`;` `&` `|`), and tolerates a leading run of
# `VAR=val` assignments and/or a `command`/`env`/`exec`/`builtin`/`sudo`
# wrapper — so `GIT_DIR=/tmp/x git push -f`, `  git push -f` (leading space),
# `command git push -f`, and `foo; git  push -f` all still hit the gate. The
# ${GIT_ANCHOR} idiom is shared verbatim with block_raw_git.sh /
# block_git_rebase.sh (all three had the narrower anchor).
GIT_ANCHOR='(^|[;&|])[[:space:]]*(([[:alnum:]_]+=[^[:space:]]+|command|env|exec|builtin|sudo)[[:space:]]+)*git[[:space:]]+'
if ! echo "$COMMAND" | grep -qE "${GIT_ANCHOR}push\b"; then
    exit 0
fi
if echo "$COMMAND" | grep -qE -- \
    '--force|--force-with-lease|(^|[[:space:]])-[a-zA-Z]*f|[[:space:]]\+[^[:space:]]+'; then
    echo "BLOCKED: Force push is not allowed for agents. Suggest the user run the command themselves with: ! $COMMAND" >&2
    exit 2
fi
