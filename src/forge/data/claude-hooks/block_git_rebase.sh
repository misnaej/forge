#!/usr/bin/env bash
# Block every rebase of a published branch from Bash: `git rebase`,
# `git pull --rebase`, and the server-side form `gh pr update-branch
# --rebase` (with the GraphQL mutation behind it).
#
# Rebasing rewrites commits on a branch that is usually already published
# and discards the exact commits a reviewer saw. forge squash-merges every
# PR, so a feature branch's commit shape never reaches the base branch — a
# plain merge of the base branch (`forge-update-branch`) resolves "sync my
# branch" without rewriting anything.
#
# Locally a rebase also forces a force-push (itself blocked by
# block_force_push.sh), so the local route is covered twice over. The
# server-side form reaches the same effect with NO push at all: GitHub
# rewrites the PR branch itself, which neither the git stages nor
# block_force_push can see — the gh stages below are what cover it. The
# rewrite, not the transport, is what is forbidden.
#
# No agent bypass. A human who truly needs a
# rebase runs it themselves with `! git rebase …`.
set -e
INPUT=$(cat)
COMMAND=$(jq -r '.tool_input.command // empty' <<< "$INPUT")

_block() {
    echo "BLOCKED: '$1' is forbidden for agents. It rewrites branch history — on this machine, or on GitHub's side with no push at all; forge squash-merges PRs, so sync a branch by merging the base branch instead: \`forge-update-branch\`. If a human truly needs to rebase, run it yourself with: ! $COMMAND" >&2
    exit 2
}

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
# Commands are found on the command-positions view (git_anchor.sh): a
# quoted mention is not a rebase, a wrapped `bash -c "git rebase …"` is.
CMDPOS=$(command_positions "$COMMAND")
if echo "$CMDPOS" | grep -qE "${GIT_ANCHOR}rebase\b" \
    && ! guard_help_only "$CMDPOS" "${GIT_ANCHOR}rebase\b"; then
    _block "git rebase"
fi

# `git pull --rebase` / `-r` is a rebase too — it replays local commits onto
# the upstream tip, rewriting them exactly as `git rebase` would. The flag
# is read from the --words view, so a quoted `"--rebase"` still counts.
if echo "$CMDPOS" | grep -qE "${GIT_ANCHOR}pull\b" \
    && ! guard_help_only "$CMDPOS" "${GIT_ANCHOR}pull\b" \
    && command_positions --words "$COMMAND" | grep -qE -- '--rebase\b|(^|\s)-[a-zA-Z]*r\b'; then
    _block "git pull --rebase"
fi

# `gh pr update-branch --rebase` asks GitHub to rebase the PR branch onto its
# base, server-side. The gate is the verb at a real invocation position
# (`invoked`, which rules out a quoted mention and a `--help` request alike);
# the flag is then read from the --words view — as in the pull stage, so a
# quoted `"--rebase"` still counts — but bounded to this verb's own segment,
# so a `--rebase` belonging to a command after a separator cannot
# false-block. `--rebase=false` does false-block: fail closed, and gh
# accepts no value there anyway.
#
# The bound is `[^;&|]*`, NOT the family's usual `[^;&|)]*`: `)` would end
# the segment at the close of a command substitution, and
# `gh pr update-branch $(gh pr view -q .number) --rebase` — the natural way
# to say "the current PR" — would slip the gate with the flag sitting after
# that `)`. Cost of dropping it: `(gh pr update-branch 5) --rebase` blocks
# though the flag is outside the subshell. Fail closed on nonsense beats a
# hole on an idiom. A pipe INSIDE a substitution still ends the segment
# (`$(gh pr view -q .n | head -1) --rebase`); listed with the blind spots
# in git-safety.md.
#
# The plain merge form is deliberately NOT blocked: it rewrites nothing, it
# is a base sync, and FOUNDATION §6 lets an agent resolve a behind branch
# without asking. What it does cost — a merge commit no local pre-commit
# gate sees, and a PR head that moves with no push for warn_stale_wrapup to
# notice — is why forge does not USE it, not a reason to refuse it.
_UPDATE_BRANCH="pr[[:space:]]+${GH_OPTS}update-branch\b"
if invoked "${GH_ANCHOR}${_UPDATE_BRANCH}" \
    && command_positions --words --no-redir "$COMMAND" \
        | grep -oE -- "gh[[:space:]]+${GH_OPTS}${_UPDATE_BRANCH}[^;&|]*" \
        | grep -qE -- '--rebase\b'; then
    _block "gh pr update-branch --rebase"
fi

# The mutation gh itself calls for that flag, reached directly:
# `updatePullRequestBranch` with `updateMethod: REBASE`. Both tokens in
# either order, since REBASE often arrives as a separate `-f` variable for a
# `updateMethod: $m` query, and bounded to one command (`[^;&|]*`) so a
# REBASE after a separator cannot false-block. `updateMethod: MERGE` stays
# allowed, like the plain flag form above.
#
# Blind spots, shared with every api guard (block_pr_merge.sh): a query read
# from a file (`-F query=@q.graphql`), from stdin (`--input -`), from a shell
# variable or a command substitution, or behind a `gh alias` is not visible
# here. These stages are tripwires on the routes an agent would take, not a
# sandbox. REST needs no stage at all: its update-branch endpoint takes only
# `expected_head_sha` and cannot rebase.
if echo "$CMDPOS" | grep -qE "${GH_ANCHOR}api\b" \
    && command_positions --words --no-redir "$COMMAND" \
        | grep -qE "gh[[:space:]]+${GH_OPTS}api[^;&|]*graphql[^;&|]*(updatePullRequestBranch[^;&|]*REBASE|REBASE[^;&|]*updatePullRequestBranch)"; then
    _block "gh api graphql updatePullRequestBranch updateMethod: REBASE"
fi
