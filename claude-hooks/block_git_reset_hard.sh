#!/usr/bin/env bash
# Block `git reset --hard` (and `git reset --merge`) from Bash.
#
# Both forms discard uncommitted work irrecoverably — the reflex "recovery"
# move when a merge or commit goes sideways, and exactly the wrong one for
# an agent holding a user's unstaged edits. The sanctioned dirty-tree base
# sync is the stash dance (`git stash -u` → `git merge origin/<base>` →
# `git stash pop`; on any failure leave the stash alone — FOUNDATION §2).
# Soft/mixed resets (`git reset`, `--soft`, `git reset HEAD~1`) keep the
# working tree and stay allowed.
#
# No agent bypass — not even forge:git-commit-push. A human who truly needs
# a hard reset runs it themselves with `! git reset --hard …`.
set -e
INPUT=$(cat)
COMMAND=$(jq -r '.tool_input.command // empty' <<< "$INPUT")

_block() {
    echo "BLOCKED: '$1' is forbidden for agents. It discards uncommitted work irrecoverably; to sync a dirty branch use the stash dance instead (\`git stash -u\`, \`git merge origin/<base>\`, \`git stash pop\` — on any failure leave the stash alone and report, per FOUNDATION §2). If a human truly needs it, run it yourself with: ! $COMMAND" >&2
    exit 2
}

# Anchor at line-start or after a shell separator so the words inside a
# quoted body (a commit message, PR description) never fire. The anchor also
# tolerates a leading run of `VAR=val` assignments and a `command`/`env`/
# `exec`/`builtin`/`sudo` wrapper, so `GIT_DIR=x git reset --hard` can't slip
# the gate (shared verbatim with block_git_rebase.sh / block_force_push.sh).
GIT_ANCHOR='(^|[;&|(])[[:space:]]*(([[:alnum:]_]+=[^[:space:]]+|command|env|exec|builtin|sudo|-[^[:space:]]+)[[:space:]]+)*git[[:space:]]+'
# The flag must appear inside the SAME `git reset` invocation — matching it
# anywhere in the command string would false-positive on e.g.
# `git commit -m "about --hard"; git reset HEAD~1`. `[^;&|]*` bounds the
# invocation at the next command separator.
if echo "$COMMAND" | grep -qE "${GIT_ANCHOR}reset[^;&|]*[[:space:]]--hard\b"; then
    _block "git reset --hard"
fi

# `git reset --merge` is the same destruction class: it discards unstaged
# changes to files that differ between HEAD and the target.
if echo "$COMMAND" | grep -qE "${GIT_ANCHOR}reset[^;&|]*[[:space:]]--merge\b"; then
    _block "git reset --merge"
fi
