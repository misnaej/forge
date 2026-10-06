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
# Also blocked, for every agent: ref plumbing (`update-ref`,
# `fast-import`) and inline interpreter code that names git (a tripwire,
# not a boundary — see the rule below).
# FOUNDATION §3 mandatory-delegation — use the forge:git-commit-push agent.
#
# Bypass: the forge:git-commit-push agent may run `git commit` / `git
# push` — that is its job. The PreToolUse payload includes `agent_type`
# (the `name:` frontmatter of the calling subagent, per
# code.claude.com/docs/en/hooks); when it matches `git-commit-push` or
# `forge:git-commit-push`, only the commit/push rule is waived. Every other
# rule here — the fail-closed anchor-lib check, revert/cherry-pick, ref
# plumbing and inline interpreter git — applies to that agent like to
# everyone: each creates commits or moves refs with no pre-commit hook,
# which is exactly what that agent exists to prevent. Same scoped-bypass shape as block_protected_branches.sh.
set -e
INPUT=$(cat)
COMMAND=$(jq -r '.tool_input.command // empty' <<< "$INPUT")
AGENT_TYPE=$(jq -r '.agent_type // empty' <<< "$INPUT")
IS_COMMIT_AGENT=0
if [ "$AGENT_TYPE" = "git-commit-push" ] || [ "$AGENT_TYPE" = "forge:git-commit-push" ]; then
    IS_COMMIT_AGENT=1
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
# Commands are found on the command-positions view (git_anchor.sh): a
# quoted mention is not a commit, `bash -c "git commit …"` is. Asking a
# guarded verb for help runs nothing and stays allowed.
CMDPOS=$(command_positions "$COMMAND")
if [ "$IS_COMMIT_AGENT" != 1 ] && echo "$CMDPOS" | grep -qE "${GIT_ANCHOR}(commit|push)\b" \
    && ! guard_help_only "$CMDPOS" "${GIT_ANCHOR}(commit|push)\b"; then
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
# unexempted invocation blocks the command. Invocations come from the
# --words view: only real invocations are judged (a quoted mention is not
# one), and a quoted `"--abort"` handed to a real invocation still counts
# as the flag it is.
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
    done <<< "$(command_positions --words "$COMMAND" | grep -oE "${SEQUENCER_RE}[^;&|)#]*" || true)"
    return 1
}
if echo "$CMDPOS" | grep -qE "$SEQUENCER_RE" \
    && ! guard_help_only "$CMDPOS" "$SEQUENCER_RE" && sequencer_blocked; then
    echo "BLOCKED: 'git revert' / 'git cherry-pick' create a commit through git's sequencer, which runs NO pre-commit hook — forbidden by FOUNDATION §3 for the same reason as raw 'git commit'. To undo a change, make a new commit through the normal flow. To leave a conflicted sequencer state, '--abort' / '--quit' / '--skip' stay allowed, as does '--no-commit'. If a human truly needs this, run it yourself with: ! $COMMAND" >&2
    exit 2
fi

# Ref plumbing: `update-ref` moves a branch to any commit (the last step
# of assembling a commit by hand from `write-tree` + `commit-tree`), and
# `fast-import` writes whole histories. Neither runs a hook, and neither
# is part of the commit agent's job, so its bypass does not cover them.
REFPLUMB_RE="${GIT_ANCHOR}(update-ref|fast-import)\b"
if echo "$CMDPOS" | grep -qE "$REFPLUMB_RE" && ! guard_help_only "$CMDPOS" "$REFPLUMB_RE"; then
    echo "BLOCKED: 'git update-ref' / 'git fast-import' write refs and history directly, past every hook — forbidden for agents (FOUNDATION §3). Commits go through the forge:git-commit-push agent; a probe that needs a repo with particular commits uses \`forge-scratch-repo build\`. If a human truly needs this, run it yourself with: ! $COMMAND" >&2
    exit 2
fi

# Inline interpreter code that runs git. A guard reads the shell command,
# so git started from a `python3 -c` / `node -e` / heredoc script is
# invisible to every rule above — the route that would otherwise make
# commits past the commit guard. Blocked when an interpreter at a
# command position (bare, path-qualified, or behind a runner such as
# `env`, `uv run`, `pixi run`, `conda run`) receives inline code — `-c`,
# `-e`, `-E`, `-p`, `--eval`, `--print`, `-` or a heredoc / here-string —
# or a bare interpreter reads its script from a pipe, AND that code names
# `git` as a word (`.git`, `git_utils`, `github` do not count). No
# commit-agent bypass. This is a tripwire for the recorded route, not a
# boundary: a script file run as `python x.py`, or a verb assembled at
# runtime, is not seen (documented residuals, git_anchor.sh).
INTERP_PFX='(([[:alnum:]_]+=[^[:space:]]+|then|do|else|elif|[{!]|-[^[:space:]]+)[[:space:]]+)*'
INTERP_RUNNER='((command|env|exec|builtin|sudo|nice|nohup|time|timeout|stdbuf|xargs|uvx|npx|bunx|(uv|pixi|poetry|pipenv|pdm|hatch|rye|conda|mamba|micromamba)[[:space:]]+run)([[:space:]]+[^[:space:];&|()]+)*[[:space:]]+)?'
INTERP_NAME='([^[:space:];&|()]*/)?(python[0-9.]*|pypy[0-9.]*|node|nodejs|perl|ruby|bun|deno|php)'
INTERP_RE="(^|[;&|(])[[:space:]]*${INTERP_PFX}${INTERP_RUNNER}${INTERP_NAME}([[:space:]]|\$)"
INTERP_TOKEN_RE='^(.*/)?(python[0-9.]*|pypy[0-9.]*|node|nodejs|perl|ruby|bun|deno|php)$'
GIT_WORD_RE='(^|[^[:alnum:]_.-])git([^[:alnum:]_./-]|$)'

# _interp_code_source <invocation> — classifies what code the
# interpreter in one invocation runs, by walking ITS options left to
# right (the first interpreter-named word is the interpreter; anything
# before it is a runner). Prints `inline` (code on the command line:
# `-c`/`-e`/`-E`/`-p`/`--eval`/`--print`, also clustered like `-Ic`),
# `stdin` (`-`, a heredoc or here-string, or a bare interpreter fed by a
# pipe) or nothing (a script file or `-m module` — not inline code, so
# `python3 -m pytest -k git` is not this rule's business).
_interp_code_source() {
    local inv="$1" tok k=-1 i skip=0 piped=0
    local -a toks
    case "$inv" in [[:space:]]*'|'* | '|'*) piped=1 ;; esac
    # Drop the separator run the anchor matched (`|`, `;`, `(` …), which
    # may be glued to the first word.
    inv="${inv#"${inv%%[![:space:]\;\&\|\(]*}"}"
    read -ra toks <<< "$inv"
    for ((i = 0; i < ${#toks[@]}; i++)); do
        if [[ "${toks[$i]}" =~ $INTERP_TOKEN_RE ]]; then k=$i; break; fi
    done
    [ "$k" -ge 0 ] || return 0
    for ((i = k + 1; i < ${#toks[@]}; i++)); do
        tok="${toks[$i]}"
        if [ "$skip" = 1 ]; then skip=0; continue; fi
        case "$tok" in
            '<<'*) echo stdin; return 0 ;;
            -) echo stdin; return 0 ;;
            --) return 0 ;;
            -W | -X | -Q | -r | -I | -M | --require | --import | --loader | --experimental-loader) skip=1 ;;
            --eval | --eval=* | --print | --print=*) echo inline; return 0 ;;
            -m) return 0 ;;
            --*) ;;
            -*[ceEp]*) echo inline; return 0 ;;
            -*) ;;
            *) return 0 ;;
        esac
    done
    # Reached the end with no script and no inline code: the interpreter
    # reads its program from stdin — a pipe, or a heredoc on a later line.
    [ "$piped" = 1 ] && echo stdin
    return 0
}

_inline_interp_git() {
    local view inv src
    view=$(command_positions --words "$COMMAND")
    while IFS= read -r inv; do
        [ -n "$inv" ] || continue
        src=$(_interp_code_source "$inv")
        # Only the code the interpreter runs counts — `python -c 'print(1)'
        # && git status` runs git from the shell, not the interpreter. A
        # heredoc body is not on the invocation's line, so for one the raw
        # text from the first `<<` on is read; a program arriving on stdin
        # from a pipe is read from the whole raw command.
        case "$src" in
            inline) echo "$inv" | grep -qE "$GIT_WORD_RE" && return 0 ;;
            stdin)
                if echo "$inv" | grep -qE '<<'; then
                    printf '%s\n' "${COMMAND#*<<}" | grep -qE "$GIT_WORD_RE" && return 0
                else
                    printf '%s\n' "$COMMAND" | grep -qE "$GIT_WORD_RE" && return 0
                fi
                ;;
        esac
    done <<< "$(printf '%s\n' "$view" | grep -oE "${INTERP_RE}[^;&|]*" || true)"
    return 1
}
if echo "$CMDPOS" | grep -qE "$INTERP_RE" && _inline_interp_git; then
    echo "BLOCKED: inline interpreter code that runs git is invisible to the git guards, so it is refused for agents. If you need a repo to experiment in, use \`forge-scratch-repo snapshot\` / \`build\` and address it with \`git -C <path>\`; commits in the real checkout go through the forge:git-commit-push agent. If the operation you need is blocked, report it — never route around a guard (FOUNDATION §2). If a human truly needs this, run it yourself with: ! $COMMAND" >&2
    exit 2
fi
