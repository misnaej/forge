#!/usr/bin/env bash
# Keep the git pre-commit gate from being skipped.
#
# Three ways an agent could commit with no forge gate running, and the
# guard for each:
#   1. `--no-verify` on a commit or push — refused, with every abbreviation
#      git accepts for it (`--no-veri`, `--no-verif`; from `--no-ve` up),
#      and `-n` on a commit.
#   2. Moving where git looks for hooks, or hiding a bypass in an alias.
#      git reads `core.hooksPath` to find the pre-commit hook; pointed
#      anywhere else, even for one command, the commit runs no gate and
#      succeeds as if it had passed. Refused: a `git config` write of that
#      key in any scope or file (set — an empty value too — `--add`,
#      `--replace-all`, `--unset`, `--unset-all`, the `set` / `unset`
#      subcommands), removing or renaming the `core` section, a one-off
#      `-c core.hooksPath=…` / `--config-env=…` on any git invocation, and
#      `GIT_CONFIG_KEY_<n>` / `GIT_CONFIG_PARAMETERS` assignments naming
#      the key (or holding a value only known at run time). The key is
#      matched case-insensitively, as git reads it. Reading it
#      (`git config --get core.hooksPath`, which `.githooks/install.sh`
#      does) stays allowed. An alias can carry the same bypass
#      (`alias.x '!git -c core.hooksPath=… commit'`, `alias.nv 'commit
#      -n'`), so a write of an `alias.*` key whose value starts with `!`,
#      or names commit / merge / pull, `-n`, `--no-ve…` or hooksPath, or
#      holds a value only known at run time, is refused in the same forms
#      (`-c alias.x=…`; `--config-env=alias.*` and `GIT_CONFIG_*` naming an
#      alias always, since their value is not in the command).
#   3. A hooks folder with nothing in it. On a commit, merge or pull, the
#      folder git will actually use (`git rev-parse --git-path hooks`, in
#      the payload's cwd) must hold an executable `pre-commit` — checked
#      only in a repo that tracks forge's `.githooks/pre-commit`, so a repo
#      deliberately on another hook manager is never refused. `git merge
#      --abort` / `--quit` create nothing and are not checked.
#
# No agent bypass — the commit agent's whole job is a gated commit. A
# human runs a refused form with `! …`; the real fix for 3 is
# `install-forge-githooks`.
#
# Accepted residuals (tripwire, not sandbox): a fake `git` earlier on
# PATH (FOUNDATION §2: never reach a blocked effect another way); a
# global or system config (`GIT_CONFIG_GLOBAL`, `HOME`, `XDG_CONFIG_HOME`)
# in a repo not yet bootstrapped, where there is no gate to protect; an
# `include.path` pointing at a file that sets the key; `git config -e`;
# a key held in a variable (`git config "$K" x`); editing `.git/config`
# directly; aliases defined before forge was installed; and an empty or
# stub executable pre-commit, which passes check 3 — it proves a hook
# exists, not that it is forge's. Check 3 judges the payload's cwd only,
# so a command that runs git somewhere else — `git -C <other repo>`,
# `--git-dir` / `GIT_DIR`, `--work-tree`, a `cd` earlier in the command —
# is judged against that cwd, not where git really runs. The accepted
# false block of the same rule: `git -C <wired repo> commit` run from a
# cwd whose own hooks folder is unwired is refused.
set -e
INPUT=$(cat)
COMMAND=$(jq -r '.tool_input.command // empty' <<< "$INPUT")
REPO_ROOT=$(jq -r '.cwd // empty' <<< "$INPUT")
# Trust only an absolute payload cwd (same validation as
# block_amend_pushed_commit.sh / block_protected_branches.sh).
if [ -n "$REPO_ROOT" ] && ! echo "$REPO_ROOT" | grep -qE '^/'; then
    REPO_ROOT="."
fi
# Anchors + their rationale live in the shared lib (one home for the
# whole git-/gh-guard family).
ANCHOR_LIB="$(dirname "$0")/git_anchor.sh"
if [ ! -r "$ANCHOR_LIB" ]; then
    # Fail CLOSED: a missing/unreadable lib (corrupted plugin cache) must
    # block, not silently disarm the guard — only exit 2 blocks in the
    # PreToolUse contract.
    echo "BLOCKED: guard anchor lib missing at $ANCHOR_LIB — refusing the command rather than running unguarded." >&2
    exit 2
fi
source "$ANCHOR_LIB"
guard_begin

_block_hooks_path() {
    echo "BLOCKED: this command changes or overrides core.hooksPath — where git looks for the pre-commit hook — or defines a git alias that could commit past it. Pointed anywhere else, a commit runs no forge gate and goes through as if it had passed. Hooks are wired by \`install-forge-githooks\`; reading the key (\`git config --get core.hooksPath\`) stays allowed. If a human truly needs this, run it yourself with: ! $COMMAND" >&2
    exit 2
}

# alias_unsafe(v): an alias value (the --words view's spelling, NEUT for a
# blank inside a quoted word) that could run a commit past the gate. A
# leading NEUT is a neutralised `!` (or a quoted leading blank — refused
# alike). One copy, shared by the two awk passes below.
_ALIAS_UNSAFE_AWK='
function alias_unsafe(v,   l) {
    if (v ~ /[$(]/) return 1
    if (substr(v, 1, 1) == "!" || substr(v, 1, 1) == NEUT) return 1
    l = tolower(v); gsub(NEUT, " ", l)
    if (l ~ /(^|[^a-z0-9_-])(commit|merge|pull)([^a-z0-9_-]|$)/) return 1
    if (l ~ /(^|[ \t])-[a-z]*n[a-z]*([ \t]|$)/) return 1
    if (l ~ /--no-ve/ || l ~ /hookspath/) return 1
    return 0
}'

# Commands are found on the command-positions view (git_anchor.sh): a
# quoted mention is not a commit, `bash -c "git commit …"` is. Flags,
# keys and values are read from the --words view, where a quoted word is
# still the word git receives and a separator inside a quoted body cannot
# end the invocation early.
CMDPOS=$(command_positions "$COMMAND")
WORDS=$(command_positions --words --no-redir "$COMMAND")
# The words view split at every separator, one simple command per line:
# reading invocations from it costs one linear pass however many there
# are (guard_git_invocations re-matches from each invocation's end, which
# is quadratic on one long line).
SEGMENTS=$(guard_chunks ';&|' "$WORDS")

# ---- 1. --no-verify --------------------------------------------------------
if echo "$CMDPOS" | grep -qE "${GIT_ANCHOR}(commit|push)\b" \
    && ! guard_help_only "$CMDPOS" "${GIT_ANCHOR}(commit|push)\b"; then
    # `-n` is `--no-verify` for a commit but `--dry-run` for a push, so
    # the short form counts only on the former. The long flag, and every
    # abbreviation git accepts for it, is matched in the raw text — a
    # quoted `"--no-verify"` is still the flag, and a quoted body naming it
    # reads as the flag (conservative). `--no-ve` and `--no-ver` are
    # ambiguous with `--no-verbose`, which git refuses rather than guess, so
    # refusing them costs nothing. The short form is read from the --words
    # view, where a separator inside a quoted body no longer ends the
    # invocation early and hides a flag that follows it.
    if echo "$COMMAND" | grep -qE -- '--no-verify|--no-ve(r(i(f)?)?)?([^[:alnum:]_-]|$)' \
        || echo "$WORDS" | grep -qE "${GIT_ANCHOR}commit\b[^;&|]*[[:space:]]-[a-zA-Z]*n[a-zA-Z]*([[:space:]]|[;&|)]|$)"; then
        echo "BLOCKED: --no-verify is forbidden. Fix the violations instead of bypassing pre-commit hooks. If absolutely required, the user can run: ! $COMMAND" >&2
        exit 2
    fi
fi

# ---- 2. core.hooksPath changes and overrides, and bypass aliases -----------
# Every form below names the key, the `core` section, an alias or a
# GIT_CONFIG_ variable, so a command whose words hold none of them skips
# the work.
if echo "$WORDS" | grep -qiE 'hookspath|section|alias|GIT_CONFIG_'; then
    # A one-off override on any git invocation: `-c core.hooksPath=…`
    # (without `=` git reads it as `true`) or `--config-env=core.hooksPath=…`.
    if echo "$WORDS" | grep -qiE "${GIT_ANCHOR}(-c[[:space:]]*|--config-env([=[:space:]])[[:space:]]*)core\.hookspath([^[:alnum:]_.-]|\$)"; then
        _block_hooks_path
    fi
    # A one-off alias: `--config-env` takes its value from the environment,
    # out of view; `-c alias.x=<value>` is judged by its value.
    if echo "$WORDS" | grep -qiE -- "(^|[[:space:]])--config-env([=[:space:]])[[:space:]]*alias\."; then
        _block_hooks_path
    fi
    if echo "$WORDS" | grep -oiE -- "(^|[[:space:]])-c[[:space:]]+alias\.[^[:space:]]*" \
        | LC_ALL=C awk -v NEUT="$GUARD_NEUTRAL" "${_ALIAS_UNSAFE_AWK}"'
        {
            sub(/^[ \t]*-c[ \t]+/, "")
            e = index($0, "=")
            if (e && alias_unsafe(substr($0, e + 1))) { hit = 1; exit }
        }
        END { exit hit ? 0 : 1 }'; then
        _block_hooks_path
    fi
    # The same overrides through the environment, assigned at a command
    # position (`GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.hooksPath … git`,
    # `export GIT_CONFIG_PARAMETERS=…`): the value names the key or an
    # alias, or holds a `$` / substitution whose value only exists at run
    # time.
    if echo "$WORDS" | grep -qiE "(^|[;&|(])[[:space:]]*${_GUARD_PREFIX}((export|declare|typeset|readonly|local)[[:space:]]+([^[:space:];&|()]+[[:space:]]+)*)?GIT_CONFIG_(KEY_[0-9]+|PARAMETERS)=[^[:space:];&|()]*(core\.hookspath|alias\.|[\$(])"; then
        _block_hooks_path
    fi
    # `git config` writes, one awk pass over every invocation. A write flag
    # or subcommand writes; a read flag or a `get` / `list` subcommand
    # before the key reads; otherwise any word after the key — git takes
    # them all as positionals, a dash word included — writes and a bare key
    # reads. Removing or renaming the `core` section drops the key with
    # everything else. An alias key is judged by its value (alias_unsafe).
    CONFIG_INVS=$(guard_git_invocations "$SEGMENTS" config)
    if [ -n "$CONFIG_INVS" ] && printf '%s\n' "$CONFIG_INVS" | LC_ALL=C awk -v NEUT="$GUARD_NEUTRAL" "${_ALIAS_UNSAFE_AWK}"'
        {
            t = index($0, "\t"); args = substr($0, t + 1)
            n = split(args, T, /[ \t]+/)
            key = 0; ak = 0; sect = 0; core = 0; rd = 0; wr = 0; val = 0; av = ""
            for (k = 1; k <= n; k++) {
                w = tolower(T[k])
                if (w == "") continue
                if (key || ak) {
                    val = 1
                    if (ak) av = (av == "") ? T[k] : av " " T[k]
                    continue
                }
                if (w == "core.hookspath") { key = k; continue }
                if (w ~ /^alias\./) { ak = k; continue }
                if (w ~ /^--re(move|name)-section(=|$)/) { sect = 1; if (w ~ /=core$/) core = 1; continue }
                if (w ~ /^--(get|get-all|get-regexp|get-urlmatch|list)$/ || w == "-l") { rd = 1; continue }
                if (w ~ /^--(add|replace-all|unset|unset-all)$/) { wr = 1; continue }
                if (w ~ /^-/) continue
                if (w == "set" || w == "unset") { wr = 1; continue }
                if (w == "get" || w == "list") { rd = 1; continue }
                if (w == "remove-section" || w == "rename-section") { sect = 1; continue }
                if (w == "core") core = 1
            }
            if ((sect && core) || (key && (wr || (!rd && val)))) { hit = 1; exit }
            if (ak && val && (wr || !rd) && alias_unsafe(av)) { hit = 1; exit }
        }
        END { exit hit ? 0 : 1 }'; then
        _block_hooks_path
    fi
    # An empty value (`git config core.hooksPath ""`) is a write too, but
    # an empty quoted word leaves nothing in the --words view, so it is read
    # from the raw text.
    if [ -n "$CONFIG_INVS" ] && echo "$COMMAND" | grep -qiE "core\.hookspath['\"]?[[:space:]]+([\$]?(''|\"\"))+([[:space:];&|)]|\$)"; then
        _block_hooks_path
    fi
fi

# ---- 3. the resolved hooks folder holds a runnable pre-commit --------------
# The verb must end there: `commit-tree`, `merge-base` and `merge-tree`
# create no gated commit.
VERB_END='([[:space:]]|[;&|)]|$)'
GATE=0
if invoked "${GIT_ANCHOR}(commit|pull)${VERB_END}"; then
    GATE=1
elif invoked "${GIT_ANCHOR}merge${VERB_END}"; then
    # Only `--abort` / `--quit` everywhere exempts; an invocation the
    # words view cannot pair is judged, never skipped.
    MERGE_INVS=$(guard_git_invocations "$SEGMENTS" merge)
    if [ -z "$MERGE_INVS" ] \
        || printf '%s\n' "$MERGE_INVS" | grep -qvE '[[:space:]]--(abort|quit)([[:space:]]|$)'; then
        GATE=1
    fi
fi
if [ "$GATE" = 1 ]; then
    ROOT="${REPO_ROOT:-.}"
    TOP=$(git -C "$ROOT" rev-parse --show-toplevel 2> /dev/null || true)
    # Outside a work tree, or in a repo that does not track forge's hook,
    # there is no forge gate to protect.
    if [ -n "$TOP" ] && [ -n "$(git -C "$TOP" ls-files -- .githooks/pre-commit 2> /dev/null || true)" ]; then
        HOOKS=$(git -C "$ROOT" rev-parse --git-path hooks 2> /dev/null || true)
        # --git-path answers relative to the directory git ran in.
        case "$HOOKS" in
            "") ;;
            /*) ;;
            *) HOOKS="$ROOT/$HOOKS" ;;
        esac
        if [ -n "$HOOKS" ] && { [ ! -f "$HOOKS/pre-commit" ] || [ ! -x "$HOOKS/pre-commit" ]; }; then
            echo "BLOCKED: git would run this with no pre-commit gate — the hooks folder it resolves here ($HOOKS) has no executable pre-commit, though this repo tracks forge's .githooks/pre-commit. Run \`install-forge-githooks\` to wire the hooks, then retry. \`forge-doctor\` shows the resolved folder. If a human truly needs this, run it yourself with: ! $COMMAND" >&2
            exit 2
        fi
    fi
fi
exit 0
