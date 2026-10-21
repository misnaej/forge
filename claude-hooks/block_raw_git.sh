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
# `forge:git-commit-push`, only the commit/push rule is waived, and a
# branch-deleting push — or any `gh api` DELETE or `deleteRef`
# mutation, or a push or `gh api` call built at run time and so not
# judgeable — stays refused even for that agent (deleting remote
# branches is not its job; merged ones go to `forge-next-prep
# --prune-remote`). That refusal is a tripwire, not a boundary; known
# residuals: `git send-pack <remote> :ref` and a configured git alias
# (`git config alias.*`, then its use) — both owned by the guard-coverage
# work — and a gh run through a variable (`$GH api …`). Every other rule
# here — the fail-closed anchor-lib check, revert/cherry-pick, ref
# plumbing and inline interpreter git — applies to that agent like to
# everyone: each creates commits or moves refs with no pre-commit hook,
# which is exactly what that agent exists to prevent. Same scoped-bypass
# shape as block_protected_branches.sh.
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
guard_begin
# No pathname expansion: the per-invocation word splitting below
# (`toks=($inv)`) must never glob a word such as `*`.
set -f
# Commands are found on the command-positions view (git_anchor.sh): a
# quoted mention is not a commit, `bash -c "git commit …"` is. Asking a
# guarded verb for help runs nothing and stays allowed.
CMDPOS=$(command_positions "$COMMAND")
if [ "$IS_COMMIT_AGENT" != 1 ] && echo "$CMDPOS" | grep -qE "${GIT_ANCHOR}(commit|push)\b" \
    && ! guard_help_only "$CMDPOS" "${GIT_ANCHOR}(commit|push)\b"; then
    echo "BLOCKED: raw 'git commit' / 'git push' from Bash is forbidden by FOUNDATION §3 mandatory-delegation. Use the forge:git-commit-push agent — it runs pre-commit, signs the commit per the convention, and pushes with the right tracking flags." >&2
    exit 2
fi

# A branch-deleting push from the commit agent. Its waiver above covers
# committing and pushing work, not removing remote branches: a merged
# branch is pruned by `forge-next-prep --prune-remote` (lease-pinned to
# the merged tip), anything else is the user's call. Judged per
# invocation over the --words view with redirections blanked, reading
# only option tokens (a word starting with `-`) and refspecs: a long
# delete option by every prefix git accepts as unique (`--de` …
# `--delete`; `--d` is ambiguous with `--dry-run`), `--m` … `--mirror`
# and `--pru` … `--prune` (either can delete any remote ref; `--p` and
# `--pr` are ambiguous with `--porcelain` / `--progress` /
# `--push-option`), each also as `=value`; a short cluster holding `d`
# (`-d`, `-fd`); or a refspec with an empty source (`:feature`,
# `+:feature`). A branch name containing `d` is a positional, never an
# option.
#
# A push whose words hold a `$`, a backtick, a `{` (bash brace expansion:
# `origin {,:}x` expands to a delete) or a command substitution
# (`origin $x`, `origin $(cat f)`), one fed its words by `xargs`, and a
# push whose git is itself a variable (`$g push`) cannot be judged: the
# refspec only exists at run time, so those fail closed.
PUSH_LONG_DELETE_RE='^--(de|del|dele|delet|delete|m|mi|mir|mirr|mirro|mirror|pru|prun|prune)(=.*)?$'

# _push_tok_class <word> — classifies one push word read while options
# are still being parsed. Returns 0 when it deletes, 1 for any other
# option, 2 when it takes the next word as its value, 3 for `--` (end of
# options), 4 for a positional (a remote or a refspec).
_push_tok_class() {
    case "$1" in
        --) return 3 ;;
        --repo | --receive-pack | --exec | --push-option) return 2 ;;
    esac
    [[ $1 =~ $PUSH_LONG_DELETE_RE ]] && return 0
    case "$1" in
        --*) return 1 ;;
        -*d*) return 0 ;;
        -*o) return 2 ;;
        -*) return 1 ;;
    esac
    return 4
}

# _commit_agent_push_verdict — sets PUSH_VERDICT to `dynamic` (a push
# that cannot be judged), `delete` (a branch-deleting push) or nothing.
# Sets a variable rather than printing: no subshell per call.
_commit_agent_push_verdict() {
    local inv pre args tok skip endopt rc
    local -a toks
    PUSH_VERDICT=""
    if echo "$CMDPOS" | grep -qE "${GIT_UNKNOWN_ANCHOR}push\b"; then
        PUSH_VERDICT=dynamic
        return 0
    fi
    while IFS= read -r inv; do
        [ -n "$inv" ] || continue
        pre="${inv%%	*}"
        args="${inv#*	}"
        if [[ $args == *[\$\`\{]* || $args == *@SUBST@* || $pre =~ (^|[[:space:]/])xargs([[:space:]]|$) ]]; then
            PUSH_VERDICT=dynamic
            return 0
        fi
        # `read -a`, not `toks=($args)`: no pathname expansion of a word
        # such as `:feat*`, whatever the shell options.
        read -ra toks <<< "$args"
        skip=0
        endopt=0
        for tok in "${toks[@]}"; do
            if [ "$skip" = 1 ]; then skip=0; continue; fi
            rc=4
            if [ "$endopt" = 0 ]; then
                if _push_tok_class "$tok"; then rc=0; else rc=$?; fi
            fi
            case "$rc" in
                0) PUSH_VERDICT=delete; return 0 ;;
                1) continue ;;
                2) skip=1; continue ;;
                3) endopt=1; continue ;;
            esac
            case "$tok" in
                :* | +:*) PUSH_VERDICT=delete; return 0 ;;
            esac
        done
    done <<< "$(guard_git_invocations "$(command_positions --words --no-redir "$COMMAND")" push || true)"
    return 0
}
# A `$g push` is judged even beside a help request: guard_help_only reads
# only the literal-git invocations.
if [ "$IS_COMMIT_AGENT" = 1 ] && {
    echo "$CMDPOS" | grep -qE "${GIT_UNKNOWN_ANCHOR}push\b" \
        || { echo "$CMDPOS" | grep -qE "${GIT_ANCHOR}push\b" && ! guard_help_only "$CMDPOS" "${GIT_ANCHOR}push\b"; }
}; then
    _commit_agent_push_verdict
    case "$PUSH_VERDICT" in
        dynamic)
            echo "BLOCKED: the forge:git-commit-push agent pushes only what a hook can read. This push takes its words from a variable, a command substitution, a backtick or xargs (or runs git through a variable), so whether it deletes a remote branch is unknowable until it runs — refused rather than guessed. Spell the remote and branch literally (\`git push -u origin <branch>\`). A branch whose PR merged is pruned by \`forge-next-prep --prune-remote\`; anything else is the user's decision — a human runs it with: ! $COMMAND" >&2
            exit 2
            ;;
        delete)
            echo "BLOCKED: the forge:git-commit-push agent commits and pushes work; it does not delete remote branches. A branch whose PR merged is pruned by \`forge-next-prep --prune-remote\` (lease-pinned to the merged tip). Anything else is the user's decision — a human runs it with: ! $COMMAND" >&2
            exit 2
            ;;
    esac
fi

# The same deletion through the GitHub API, for the commit agent. It has
# no legitimate DELETE at all, so any real `gh api` call whose method is
# DELETE is refused whatever the endpoint — which also covers a branch
# path spelled percent-encoded (`%68eads`) or with doubled slashes. Also
# refused: a call whose method or endpoint is built at run time (a
# variable, a substitution, a backtick, a bash brace expansion such as
# `{a,b}`; gh's own `{owner}` / `{repo}` placeholders are not one and
# stay allowed), and any call naming the GraphQL `deleteRef` mutation, in
# any case. Read per call from the --words view, so a quoted endpoint or
# method still counts. The method is `-X` / `--method`, as a separate
# word, attached (`-XDELETE`, `--method=delete`) or closing a short
# cluster (`-iX`); gh's flag parser accepts no abbreviated long option.
GH_API_VALUE_LONG_RE='^--(method|raw-field|field|header|jq|template|input|cache|preview|hostname)$'
GH_API_DYNAMIC_RE='[$`(]|@SUBST@|\{[^}]*(,|\.\.)[^}]*\}'

# _gh_api_call_verdict <segment> — 0 when the commit agent must not run
# this `gh api` call, 1 otherwise.
_gh_api_call_verdict() {
    local tok method="" endpoint="" seen_api=0 want=""
    local -a toks
    [[ ${1^^} == *DELETEREF* ]] && return 0
    read -ra toks <<< "$1"
    for tok in "${toks[@]}"; do
        if [ "$seen_api" = 0 ]; then
            [ "$tok" = api ] && seen_api=1
            continue
        fi
        if [ -n "$want" ]; then
            [ "$want" = method ] && method="$tok"
            want=""
            continue
        fi
        case "$tok" in
            --method=*) method="${tok#--method=}" ;;
            --*=*) ;;
            --*) [[ $tok =~ $GH_API_VALUE_LONG_RE ]] && want=value
                 [ "$tok" = --method ] && want=method ;;
            -*)
                # A short cluster: `i` takes no value, every other gh api
                # short flag takes one — the rest of the word, else the
                # next word.
                local cl="${tok#-}"
                while [ "${cl:0:1}" = i ]; do cl="${cl:1}"; done
                [ -n "$cl" ] || continue
                if [ "${cl:0:1}" = X ]; then
                    if [ -n "${cl:1}" ]; then method="${cl:1}"; else want=method; fi
                elif [ -z "${cl:1}" ]; then
                    want=value
                fi
                ;;
            *) [ -z "$endpoint" ] && endpoint="$tok" ;;
        esac
    done
    [ "$want" = method ] && return 0
    [[ ${method^^} == DELETE ]] && return 0
    [ -n "$method" ] && ! [[ $method =~ ^[A-Za-z]+$ ]] && return 0
    [[ $endpoint =~ $GH_API_DYNAMIC_RE ]] && return 0
    return 1
}

_commit_agent_gh_deletes() {
    local seg
    while IFS= read -r seg; do
        [ -n "$seg" ] || continue
        _gh_api_call_verdict "$seg" && return 0
    done <<< "$(command_positions --words --no-redir "$COMMAND" | grep -oE "gh[[:space:]]+${GH_OPTS}api[^;&|]*" || true)"
    return 1
}
if [ "$IS_COMMIT_AGENT" = 1 ] && echo "$CMDPOS" | grep -qE "${GH_ANCHOR}api\b" && _commit_agent_gh_deletes; then
    echo "BLOCKED: the forge:git-commit-push agent does not delete remote branches, through git or through the GitHub API — so it runs no \`gh api\` DELETE and no \`deleteRef\` mutation at all, and no \`gh api\` call whose method or endpoint comes from a variable, a substitution or a brace expansion (a hook cannot tell what that call does). A branch whose PR merged is pruned by \`forge-next-prep --prune-remote\` (lease-pinned to the merged tip). Anything else is the user's decision — a human runs it with: ! $COMMAND" >&2
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
        [[ $invocation =~ $SEQUENCER_EXEMPT_RE ]] || return 0
    done <<< "$(command_positions --words --no-redir "$COMMAND" | grep -oE "${SEQUENCER_RE}[^;&|)#]*" || true)"
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
# `-e`, `-E`, `-p`, `--eval`, `--print`, `deno eval`, `-` or a heredoc /
# here-string —
# or a bare interpreter reads its script from a pipe, AND that code names
# `git` as a word (`.git`, `git_utils`, `github` do not count). No
# commit-agent bypass. This is a tripwire for the recorded route, not a
# boundary: a script file run as `python x.py`, or a verb assembled at
# runtime, is not seen (documented residuals, git_anchor.sh).
# A redirection word (GUARD_REDIR, git_anchor.sh) may stand before the
# interpreter or the runner and among the runner's arguments
# (`>x python -c …`, `env 2>&1 python -c …`), and the runner may be
# path-qualified like the interpreter (`/usr/bin/env python3 -c …`;
# GUARD_BINPATH, git_anchor.sh).
INTERP_PFX='(([[:alnum:]_]+=[^[:space:]]+|then|do|else|elif|[{!]|-[^[:space:]]+|'"${GUARD_REDIR}"')[[:space:]]+)*'
INTERP_RUNNER='('"${GUARD_BINPATH}"'(command|env|exec|builtin|sudo|nice|nohup|time|timeout|stdbuf|xargs|uvx|npx|bunx|(uv|pixi|poetry|pipenv|pdm|hatch|rye|conda|mamba|micromamba)[[:space:]]+run)([[:space:]]+([^[:space:];&|()]+|'"${GUARD_REDIR}"'))*[[:space:]]+)?'
INTERP_NAME='([^[:space:];&|()]*/)?(python[0-9.]*|pypy[0-9.]*|node|nodejs|perl|ruby|bun|deno|php)'
INTERP_RE="(^|[;&|(])[[:space:]]*${INTERP_PFX}${INTERP_RUNNER}${INTERP_NAME}([[:space:]]|\$)"
INTERP_TOKEN_RE='^(.*/)?(python[0-9.]*|pypy[0-9.]*|node|nodejs|perl|ruby|bun|deno|php)$'
GIT_WORD_RE='(^|[^[:alnum:]_.-])git([^[:alnum:]_./-]|$)'

# _interp_code_source <invocation> — classifies what code the
# interpreter in one invocation runs, by walking ITS options left to
# right (the first interpreter-named word is the interpreter; anything
# before it is a runner). Prints `inline` (code on the command line:
# `-c`/`-e`/`-E`/`-p`/`--eval`/`--print`, also clustered like `-Ic`),
# `evalarg` (`deno eval`: inline code the scanner moved off the line),
# `stdin` (`-`, a heredoc or here-string, or a bare interpreter fed by a
# pipe) or nothing (a script file or `-m module` — not inline code, so
# `python3 -m pytest -k git` is not this rule's business).
#
# Sets SRC rather than printing it, and splits words with the shell's own
# word splitting (pathname expansion is off for the hook): it runs once
# per interpreter invocation, and a process per invocation let a long
# chain run this hook past its timeout — which allows the command.
_interp_code_source() {
    local inv="$1" tok k=-1 i skip=0 piped=0 interp
    local -a toks
    SRC=""
    case "$inv" in [[:space:]]*'|'* | '|'*) piped=1 ;; esac
    # Drop the separator run the anchor matched (`|`, `;`, `(` …), which
    # may be glued to the first word.
    inv="${inv#"${inv%%[![:space:]\;\&\|\(]*}"}"
    toks=($inv)
    for ((i = 0; i < ${#toks[@]}; i++)); do
        if [[ "${toks[$i]}" =~ $INTERP_TOKEN_RE ]]; then k=$i; break; fi
    done
    [ "$k" -ge 0 ] || return 0
    interp="${toks[$k]##*/}"
    for ((i = k + 1; i < ${#toks[@]}; i++)); do
        tok="${toks[$i]}"
        if [ "$skip" = 1 ]; then skip=0; continue; fi
        case "$tok" in
            '<<'*) SRC=stdin; return 0 ;;
            -) SRC=stdin; return 0 ;;
            --) return 0 ;;
            -W | -X | -Q | --require | --import | --loader | --experimental-loader | --preload) skip=1 ;;
            -d) case "$interp" in php*) skip=1 ;; esac ;;
            # These letters mean different things per interpreter, so the
            # interpreter decides: perl and ruby `-I dir` take a value
            # (perl's detached `-M` is skipped the same way, which can only
            # over-block); ruby, node and bun `-r lib` take one; php `-r`
            # IS the inline code; python's `-I` (isolated mode) and deno's
            # `-r` (reload) take none, so treating them as valued would
            # hide the inline code that follows.
            -I | -M) case "$interp" in perl* | ruby*) skip=1 ;; esac ;;
            -r)
                case "$interp" in
                    php*) SRC=inline; return 0 ;;
                    ruby* | node* | bun*) skip=1 ;;
                esac
                ;;
            --eval | --eval=* | --print | --print=*) SRC=inline; return 0 ;;
            -m) return 0 ;;
            --*) ;;
            -*[ceEp]*) SRC=inline; return 0 ;;
            -*) ;;
            # `deno eval <code>` runs the code it is given: its first
            # positional word is a subcommand, and `eval` is the inline one.
            # The scanner reads the code after any `eval` word as a shell
            # payload of its own, so it is not on this invocation's line
            # (SRC=evalarg: _inline_interp_git reads the raw text instead).
            eval) case "$interp" in deno*) SRC=evalarg ;; esac; return 0 ;;
            *) return 0 ;;
        esac
    done
    # Reached the end with no script and no inline code: the interpreter
    # reads its program from stdin — a pipe, or a heredoc on a later line.
    [ "$piped" = 1 ] && SRC=stdin
    return 0
}

_inline_interp_git() {
    local view inv
    # Redirections are blanked (--no-redir), so neither `python 2>&1 -c …`
    # nor `python <in -c …` ends the invocation or reads as a script file
    # before the `-c`; a heredoc or here-string operator is not a
    # redirection to blank and still marks a program on stdin.
    view=$(command_positions --words --no-redir "$COMMAND")
    while IFS= read -r inv; do
        [ -n "$inv" ] || continue
        _interp_code_source "$inv"
        # Only the code the interpreter runs counts — `python -c 'print(1)'
        # && git status` runs git from the shell, not the interpreter. A
        # heredoc body is not on the invocation's line, so for one the raw
        # text from the first `<<` on is read; a program arriving on stdin
        # from a pipe is read from the whole raw command. Both are matched
        # with bash's own regex engine — no process per invocation.
        case "$SRC" in
            inline) [[ $inv =~ $GIT_WORD_RE ]] && return 0 ;;
            evalarg) [[ ${COMMAND#*eval} =~ $GIT_WORD_RE ]] && return 0 ;;
            stdin)
                if [[ $inv == *'<<'* ]]; then
                    [[ ${COMMAND#*<<} =~ $GIT_WORD_RE ]] && return 0
                else
                    [[ $COMMAND =~ $GIT_WORD_RE ]] && return 0
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
