#!/usr/bin/env bash
# Keep the git pre-commit gate from being skipped.
#
# Three ways an agent could commit with no forge gate running, and the
# guard for each:
#   1. `--no-verify` / `-n` on a commit — refused.
#   2. Moving where git looks for hooks. git reads `core.hooksPath` to
#      find the pre-commit hook; pointed anywhere else, even for one
#      command, the commit runs no gate and succeeds as if it had passed.
#      Refused: a `git config` write of that key in any scope or file
#      (set, `--add`, `--replace-all`, `--unset`, `--unset-all`, the
#      `set` / `unset` subcommands), removing or renaming the `core`
#      section, a one-off `-c core.hooksPath=…` / `--config-env=…` on any
#      git invocation, and `GIT_CONFIG_KEY_<n>` / `GIT_CONFIG_PARAMETERS`
#      assignments naming the key (or holding a value only known at run
#      time). The key is matched case-insensitively, as git reads it.
#      Reading it (`git config --get core.hooksPath`, which
#      `.githooks/install.sh` does) stays allowed.
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
# directly; and an explicit `git -C <other repo>` target, which check 3
# does not resolve (it judges the payload's cwd).
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
    echo "BLOCKED: this command changes or overrides core.hooksPath — where git looks for the pre-commit hook. Pointed anywhere else, a commit runs no forge gate and goes through as if it had passed. Hooks are wired by \`install-forge-githooks\`; reading the key (\`git config --get core.hooksPath\`) stays allowed. If a human truly needs this, run it yourself with: ! $COMMAND" >&2
    exit 2
}

# Commands are found on the command-positions view (git_anchor.sh): a
# quoted mention is not a commit, `bash -c "git commit …"` is. Flags,
# keys and values are read from the --words view, where a quoted word is
# still the word git receives and a separator inside a quoted body cannot
# end the invocation early.
CMDPOS=$(command_positions "$COMMAND")
WORDS=$(command_positions --words --no-redir "$COMMAND")

# ---- 2. core.hooksPath changes and overrides ------------------------------
# Every form below names the key, the `core` section or a GIT_CONFIG_
# variable, so a command whose words hold none of them skips the work.
if echo "$WORDS" | grep -qiE 'hookspath|section|GIT_CONFIG_'; then
    # A one-off override on any git invocation: `-c core.hooksPath=…`
    # (without `=` git reads it as `true`) or `--config-env=core.hooksPath=…`.
    if echo "$WORDS" | grep -qiE "${GIT_ANCHOR}(-c[[:space:]]*|--config-env([=[:space:]])[[:space:]]*)core\.hookspath([^[:alnum:]_.-]|\$)"; then
        _block_hooks_path
    fi
    # The same override through the environment, assigned at a command
    # position (`GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.hooksPath … git`,
    # `export GIT_CONFIG_PARAMETERS=…`): the value names the key, or holds
    # a `$` / substitution whose value only exists at run time.
    if echo "$WORDS" | grep -qiE "(^|[;&|(])[[:space:]]*${_GUARD_PREFIX}((export|declare|typeset|readonly|local)[[:space:]]+([^[:space:];&|()]+[[:space:]]+)*)?GIT_CONFIG_(KEY_[0-9]+|PARAMETERS)=[^[:space:];&|()]*(core\.hookspath|[\$(])"; then
        _block_hooks_path
    fi
    # `git config` writes. A write flag or subcommand writes; a read flag
    # or a `get` / `list` subcommand before the key reads; otherwise a
    # value word after the key writes and a bare key reads. Removing or
    # renaming the `core` section drops the key with everything else.
    CONFIG_INVS=$(guard_git_invocations "$WORDS" config)
    if [ -n "$CONFIG_INVS" ] && printf '%s\n' "$CONFIG_INVS" | LC_ALL=C awk '
        {
            t = index($0, "\t"); args = tolower(substr($0, t + 1))
            n = split(args, T, /[ \t]+/)
            key = 0; sect = 0; core = 0; rd = 0; wr = 0; val = 0
            for (k = 1; k <= n; k++) {
                w = T[k]
                if (w == "") continue
                if (w == "core.hookspath") { if (!key) key = k; continue }
                if (w ~ /^--re(move|name)-section(=|$)/) { sect = 1; if (w ~ /=core$/) core = 1; continue }
                if (w ~ /^--(get|get-all|get-regexp|get-urlmatch|list)$/ || w == "-l") { rd = 1; continue }
                if (w ~ /^--(add|replace-all|unset|unset-all)$/) { wr = 1; continue }
                if (w ~ /^-/) continue
                if (!key && (w == "set" || w == "unset")) { wr = 1; continue }
                if (!key && (w == "get" || w == "list")) { rd = 1; continue }
                if (!key && (w == "remove-section" || w == "rename-section")) { sect = 1; continue }
                if (w == "core") core = 1
                if (key) val = 1
            }
            if ((sect && core) || (key && (wr || (!rd && val)))) { hit = 1; exit }
        }
        END { exit hit ? 0 : 1 }'; then
        _block_hooks_path
    fi
fi

# ---- 1. --no-verify --------------------------------------------------------
if echo "$CMDPOS" | grep -qE "${GIT_ANCHOR}(commit|push)\b" \
    && ! guard_help_only "$CMDPOS" "${GIT_ANCHOR}(commit|push)\b"; then
    # `-n` is `--no-verify` for a commit but `--dry-run` for a push, so
    # the short form counts only on the former. The long flag is matched in
    # the raw text — a quoted `"--no-verify"` is still the flag, and a quoted
    # body naming it reads as the flag (conservative). The short form is read
    # from the --words view, where a separator inside a quoted body no longer
    # ends the invocation early and hides a flag that follows it.
    if echo "$COMMAND" | grep -qE -- '--no-verify' \
        || echo "$WORDS" | grep -qE "${GIT_ANCHOR}commit\b[^;&|]*[[:space:]]-[a-zA-Z]*n[a-zA-Z]*([[:space:]]|[;&|)]|$)"; then
        echo "BLOCKED: --no-verify is forbidden. Fix the violations instead of bypassing pre-commit hooks. If absolutely required, the user can run: ! $COMMAND" >&2
        exit 2
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
    MERGE_INVS=$(guard_git_invocations "$WORDS" merge)
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
