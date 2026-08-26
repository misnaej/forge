#!/usr/bin/env bash
# Block destructive git recovery verbs from Bash.
#
# The verbs guarded here share one failure mode: an agent that thinks its
# state is wrong reaches for a "recovery" command that destroys work —
# escalating a recoverable mistake into an unrecoverable one (#363's
# incident: a reset loop past upstream commits, then `git clean -fd`
# proposed against untracked files that had no git-side recovery at all).
# The sanctioned behavior on unexpected repo state is FOUNDATION §2's
# stop-and-report rule: an unwanted commit is trivially fixable; a
# destroyed working tree is not.
#
# Blocked, in all shell shapes the anchor covers:
# - `git reset` in EVERY form (soft/mixed/hard/merge/keep, any target).
#   Rewinds walk into published history on a synced branch, and --hard/
#   --merge discard uncommitted work. Agents unstage with
#   `git restore --staged <path>` instead (never touches the worktree).
# - `git clean` with -f/-d/-x/-X/--force. Deletes untracked files — the
#   only verb here with no recovery path. Dry runs (`git clean -n`) stay
#   allowed.
# - Literal discard-everything restores: `git checkout .`,
#   `git checkout -- .`, `git restore .`. Branch switching and
#   path-targeted restores (`git checkout --ours -- <path>`,
#   `git checkout <ref> -- <path>`, `git restore <path>`) stay allowed.
# - `git stash drop` / `git stash clear`. A dropped stash is
#   unreferenced; `stash push`/`pop`/`list` stay allowed.
#
# No agent bypass — not even forge:git-commit-push. A human who truly
# needs one of these runs it themselves with `! git …`.
set -e
INPUT=$(cat)
COMMAND=$(jq -r '.tool_input.command // empty' <<< "$INPUT")

_block() {
    echo "BLOCKED: '$1' is forbidden for agents. $2 If repository state is not what you expected, STOP and report it (FOUNDATION §2) — never undo, rewind, or clean. If a human truly needs this, run it yourself with: ! $COMMAND" >&2
    exit 2
}

# Anchor at line-start or after a shell separator so the words inside a
# quoted body (a commit message, PR description) never fire. The anchor also
# tolerates a leading run of `VAR=val` assignments and a `command`/`env`/
# `exec`/`builtin`/`sudo` wrapper, so `GIT_DIR=x git reset` can't slip the
# gate (shared verbatim with block_git_rebase.sh / block_force_push.sh).
GIT_ANCHOR='(^|[;&|(])[[:space:]]*(([[:alnum:]_]+=[^[:space:]]+|command|env|exec|builtin|sudo|-[^[:space:]]+)[[:space:]]+)*git[[:space:]]+'

# `git reset` — every form. The blanket ban subsumes the --hard/--merge
# block that previously lived in block_git_reset_hard.sh (retired).
if echo "$COMMAND" | grep -qE "${GIT_ANCHOR}reset\b"; then
    _block "git reset" "Rewinds un-commit history (published commits on a synced branch) and --hard/--merge discard uncommitted work; to unstage, use \`git restore --staged <path>\` instead."
fi

# `git clean` with any of -f/-d/-x/-X (clustered or separate) or --force.
# A dry run (`-n`/--dry-run) short-circuits — it only lists candidates and
# is the sanctioned way to REPORT untracked state — but the exemption is
# evaluated PER INVOCATION: the command is split at shell separators so
# `git clean -n; git clean -f` still blocks on the second segment.
SEG_ANCHOR='^[[:space:]]*(([[:alnum:]_]+=[^[:space:]]+|command|env|exec|builtin|sudo|-[^[:space:]]+)[[:space:]]+)*git[[:space:]]+'
while IFS= read -r seg; do
    if echo "$seg" | grep -qE "${SEG_ANCHOR}clean\b"; then
        # Flags are scanned only AFTER the `clean` token, so a wrapper's
        # own flag (`sudo -n git clean -f`) can't masquerade as dry-run.
        rest="${seg#*clean}"
        if echo "$rest" | grep -qE '(^|[[:space:]])(--dry-run\b|-[a-zA-Z]*n)'; then
            continue
        fi
        if echo "$rest" | grep -qE '(^|[[:space:]])(--force\b|-[a-zA-Z]*[fdxX])'; then
            _block "git clean" "It deletes untracked files permanently — no reflog, no index, no recovery; report the untracked paths instead (\`git clean -n\` to list them)."
        fi
    fi
done < <(printf '%s\n' "$COMMAND" | tr ';&|(' '\n')

# Literal discard-everything restores only. The `.` must be the whole
# pathspec (end of invocation or followed by a separator), so
# `git checkout ./subdir`, `git checkout <branch>`, `git restore <path>`,
# and `git checkout --ours -- <path>` all stay allowed.
if echo "$COMMAND" | grep -qE "${GIT_ANCHOR}checkout[[:space:]]+(--[[:space:]]+)?\.([[:space:]]|$|[;&|])"; then
    _block "git checkout ." "It discards every uncommitted modification in the tree; restore individual paths deliberately, or stop and report."
fi
if echo "$COMMAND" | grep -qE "${GIT_ANCHOR}restore[[:space:]]+(--[[:space:]]+)?\.([[:space:]]|$|[;&|])"; then
    _block "git restore ." "It discards every uncommitted modification in the tree; restore individual paths deliberately, or stop and report."
fi

# `git stash drop` / `clear` — a dropped stash is unreferenced and gone.
# FOUNDATION §2's stash dance ends with the stash either popped or left
# alone; deleting it is never the agent's call.
if echo "$COMMAND" | grep -qE "${GIT_ANCHOR}stash[[:space:]]+(drop|clear)\b"; then
    _block "git stash drop/clear" "A dropped stash is unreferenced and unrecoverable; leave the stash alone and report (\`git stash list\` to show it)."
fi
