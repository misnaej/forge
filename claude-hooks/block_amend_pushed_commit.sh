#!/usr/bin/env bash
# Block `git commit --amend` when HEAD is already on a remote.
#
# Amending a pushed commit is the single-commit analogue of rebase (see
# block_git_rebase.sh): it rewrites published history, forcing a
# force-push that block_force_push.sh then refuses — leaving the branch
# diverged from origin exactly when the damage is already done. This
# hook blocks the CAUSE (the amend), not the symptom (the force-push).
# Amending an UNPUSHED commit stays allowed — nothing published is
# rewritten, and fixing up a local-only commit is normal work.
#
# No agent bypass — not even forge:git-commit-push (the one agent that
# runs `git commit`; its contract is "never amend — always a new
# commit"). A human who truly needs it runs: ! git commit --amend …
#
# Accepted residual: a commit pushed only to a remote whose tracking
# refs were never fetched locally reads as unpushed (refs/remotes is
# the detection source, updated by the agent's own `git push`).
set -e
INPUT=$(cat)
COMMAND=$(jq -r '.tool_input.command // empty' <<< "$INPUT")
REPO_ROOT=$(jq -r '.cwd // empty' <<< "$INPUT")
# Trust only an absolute payload cwd (same validation as
# block_protected_branches.sh / block_branch_deletion.sh).
if [ -n "$REPO_ROOT" ] && ! echo "$REPO_ROOT" | grep -qE '^/'; then
    REPO_ROOT="."
fi

_block() {
    echo "BLOCKED: '$1' rewrites a commit that already exists on a remote — the single-commit form of a rebase. It forces a force-push (itself blocked) and leaves the branch diverged from origin. Make a NEW commit instead (forge squash-merges PRs, so fixup commits never reach the base branch). If a human truly needs to amend, run it yourself with: ! $COMMAND" >&2
    exit 2
}

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

# Cheap text bail FIRST — this hook fires on every Bash call, so the two
# git subprocess queries below run only on a real `git commit … --amend`
# match. Quoted segments are stripped so a commit MESSAGE mentioning
# --amend never fires; `commit([[:space:]]|$)` excludes commit-tree /
# commit-graph; `--am(e(n(d)?)?)?` covers git's accepted unambiguous
# long-option abbreviations of --amend.
# The double-quote rule is escape-aware (`\"` inside a "…" span must not
# terminate it, or `-m "\""` desynchronizes the stripper and hides a live
# --amend). The single-quote rule stays naive ON PURPOSE: bash has no
# backslash-escaping inside single quotes, so an escape-aware pattern
# there would itself be wrong. Residual: a NON-git token like
# `./run.sh --amend` after `git commit …` in one compound command can
# false-positive; the block is conservative and a human runs it with `!`.
STRIPPED=$(printf '%s\n' "$COMMAND" | sed -E "s/'[^']*'//g"' ; s/"([^"\\]|\\.)*"//g')
if ! echo "$STRIPPED" | grep -qE "${GIT_ANCHOR}commit([[:space:]]|\$)"; then
    exit 0
fi
if ! echo "$STRIPPED" | grep -qE -- '(^|[[:space:]])--am(e(n(d)?)?)?([^[:alnum:]_-]|$)'; then
    # The flag tail accepts any non-flag character (not just whitespace),
    # so a trailing shell metachar — `(git commit --amend)` — can't slip
    # the gate, while longer flags like --amend-ish still don't match.
    exit 0
fi

# Live state, anchored to the payload's cwd (never the hook process's
# own — a wrong-cwd query would fail exactly like "no commits yet" and
# silently fail OPEN; same pattern as block_protected_branches.sh).
HEAD_SHA=$(git -C "${REPO_ROOT:-.}" rev-parse -q --verify HEAD 2>/dev/null || true)
if [ -z "$HEAD_SHA" ]; then
    # Not a git repo / no commits yet: nothing published to protect.
    exit 0
fi
PUSHED=$(git -C "${REPO_ROOT:-.}" for-each-ref --contains "$HEAD_SHA" --count=1 \
    --format='%(refname)' refs/remotes 2>/dev/null || true)
if [ -n "$PUSHED" ]; then
    _block "git commit --amend"
fi
exit 0
