#!/usr/bin/env bash
# Block raw commit-creating and push invocations from Bash.
#
# `revert` and `cherry-pick` are here because they create commits through
# git's sequencer, which runs no pre-commit hook at all — so they were a
# way to land an unchecked commit on any branch, including the protected
# base, past every guard in this family. The forms that end the operation
# (`--abort` / `--quit` / `--skip`) or stage without committing
# (`--no-commit` / `-n`) create nothing and stay allowed: they are how an
# agent gets out of a conflicted sequencer state.
# FOUNDATION §3 mandatory-delegation — use the forge:git-commit-push agent.
#
# Bypass: the forge:git-commit-push agent itself must call these. The
# PreToolUse payload includes `agent_type` (the `name:` frontmatter of the
# calling subagent, per code.claude.com/docs/en/hooks). When that matches
# `git-commit-push` or `forge:git-commit-push`, allow the call.
set -e
INPUT=$(cat)
COMMAND=$(jq -r '.tool_input.command // empty' <<< "$INPUT")
AGENT_TYPE=$(jq -r '.agent_type // empty' <<< "$INPUT")

if [ "$AGENT_TYPE" = "git-commit-push" ] || [ "$AGENT_TYPE" = "forge:git-commit-push" ]; then
    # The one agent legitimately allowed to drive `git commit` / `git push`.
    exit 0
fi

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
    echo "BLOCKED: raw 'git commit' / 'git push' from Bash is forbidden by FOUNDATION §3 mandatory-delegation. Use the forge:git-commit-push agent — it runs pre-commit, signs the commit per the convention, and pushes with the right tracking flags." >&2
    exit 2
fi

# Commit-creating sequencer verbs. The exempt forms create no commit.
#
# Checked PER INVOCATION, never over the whole line: a single global
# match would let an exempt flag anywhere — `git revert HEAD; git
# cherry-pick --abort`, or even a shell comment `git revert HEAD
# #--abort` that bash never executes — exempt a real commit-creating
# invocation earlier in the same command. Each occurrence is extracted
# with its own argument list (terminated by `;`, `&`, `|`, `)`, or `#`
# so a comment cannot smuggle a flag in) and judged alone; one
# unexempted invocation blocks the command.
SEQUENCER_RE="${GIT_ANCHOR}(revert|cherry-pick)\b"
SEQUENCER_EXEMPT_RE="(--(abort|quit|skip|no-commit)|[[:space:]]-n)\b"
sequencer_blocked() {
    local invocation
    while IFS= read -r invocation; do
        [ -n "$invocation" ] || continue
        # Everything past a standalone `--` is a pathspec, not a flag —
        # `git revert HEAD -- --no-commit` passes a path, it does not
        # exempt the commit.
        invocation="${invocation%% -- *}"
        echo "$invocation" | grep -qE "$SEQUENCER_EXEMPT_RE" || return 0
    done <<< "$(echo "$COMMAND" | grep -oE "${SEQUENCER_RE}[^;&|)#]*" || true)"
    return 1
}
if echo "$COMMAND" | grep -qE "$SEQUENCER_RE" && sequencer_blocked; then
    echo "BLOCKED: 'git revert' / 'git cherry-pick' create a commit through git's sequencer, which runs NO pre-commit hook — forbidden by FOUNDATION §3 for the same reason as raw 'git commit'. To undo a change, make a new commit through the normal flow. To leave a conflicted sequencer state, '--abort' / '--quit' / '--skip' stay allowed, as does '--no-commit'. If a human truly needs this, run it yourself with: ! $COMMAND" >&2
    exit 2
fi
