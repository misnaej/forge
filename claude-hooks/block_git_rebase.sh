#!/usr/bin/env bash
# Block `git rebase` (and `git pull --rebase`) from Bash.
#
# Rebasing rewrites commits on a branch that is usually already published,
# which forces a force-push (itself blocked by block_force_push.sh) and
# discards the exact commits a reviewer saw. forge squash-merges every PR,
# so a feature branch's commit shape never reaches the base branch — a plain
# merge of the base branch (`git merge origin/<base>`) resolves "sync my
# branch" without rewriting anything.
#
# No agent bypass — not even forge:git-commit-push. A human who truly needs a
# rebase runs it themselves with `! git rebase …`.
set -e
INPUT=$(cat)
COMMAND=$(jq -r '.tool_input.command // empty' <<< "$INPUT")

_block() {
    echo "BLOCKED: '$1' is forbidden for agents. Rebasing rewrites published history and forces a force-push; forge squash-merges PRs, so sync a branch with a plain merge of the base branch instead (e.g. \`git merge origin/main\` / \`origin/dev\`). If a human truly needs to rebase, run it yourself with: ! $COMMAND" >&2
    exit 2
}

# Anchor at line-start or after a shell separator so the word "rebase" inside
# a quoted body (a commit message, PR description) never fires. The anchor also
# tolerates a leading run of `VAR=val` assignments and a `command`/`env`/
# `exec`/`builtin`/`sudo` wrapper, so `GIT_DIR=x git rebase` can't slip the
# gate (shared verbatim with block_force_push.sh / block_raw_git.sh).
GIT_ANCHOR='(^|[;&|])[[:space:]]*(([[:alnum:]_]+=[^[:space:]]+|command|env|exec|builtin|sudo)[[:space:]]+)*git[[:space:]]+'
if echo "$COMMAND" | grep -qE "${GIT_ANCHOR}rebase\b"; then
    _block "git rebase"
fi

# `git pull --rebase` / `-r` is a rebase too — it replays local commits onto
# the upstream tip, rewriting them exactly as `git rebase` would.
if echo "$COMMAND" | grep -qE "${GIT_ANCHOR}pull\b" \
    && echo "$COMMAND" | grep -qE -- '--rebase\b|(^|\s)-[a-zA-Z]*r\b'; then
    _block "git pull --rebase"
fi
