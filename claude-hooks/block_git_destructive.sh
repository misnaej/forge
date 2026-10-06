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
#   `git checkout -- .`, `git restore .`.
# - Path-targeted restores (`git checkout [<ref>] [--] <paths>`,
#   worktree-touching `git restore <paths>`) when a named path holds
#   uncommitted work — checked against live `git status`, failing closed
#   when the paths cannot be determined. Restoring clean paths, plain
#   branch switching and conflict resolution (`--ours`/`--theirs`) stay
#   allowed.
# - Forced switches: `git checkout -f` and `git switch -f` /
#   `--discard-changes`.
# - `git stash` in every form except `list` / `show`.
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

# Commands are found on the command-positions view (git_anchor.sh) — a
# quoted mention is not a reset, a wrapped `bash -c "git reset …"` is —
# and their flags and pathspecs are read from the --words view, where a
# quoted argument is still the argument git receives. `_guarded <verb-re>`
# is true when a real invocation exists that is not just asking for help.
CMDPOS=$(command_positions "$COMMAND")
WORDS=$(command_positions --words "$COMMAND")
_guarded() {
    echo "$CMDPOS" | grep -qE "${GIT_ANCHOR}$1" && ! guard_help_only "$CMDPOS" "${GIT_ANCHOR}$1"
}

# `git reset` — every form. The blanket ban subsumes the --hard/--merge
# block that previously lived in block_git_reset_hard.sh (retired).
if _guarded 'reset\b'; then
    _block "git reset" "Rewinds un-commit history (published commits on a synced branch) and --hard/--merge discard uncommitted work; to unstage, use \`git restore --staged <path>\` instead."
fi

# `git clean` with any of -f/-d/-x/-X (clustered or separate) or --force.
# A dry run (`-n`/--dry-run) short-circuits — it only lists candidates and
# is the sanctioned way to REPORT untracked state — but the exemption is
# evaluated PER INVOCATION: the command is split at shell separators so
# `git clean -n; git clean -f` still blocks on the second segment.
# Segments come from the --words view, so a separator inside a quoted
# argument never splits an invocation and a quoted mention never forms one.
_guarded 'clean\b' && while IFS= read -r seg; do
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
done < <(printf '%s\n' "$WORDS" | tr ';&|(' '\n')

# Discard-everything restores: the pathspec `.` (or `./`) as the whole
# target, with any run of flags tolerated in between so `git checkout -f .`
# / `git restore --quiet .` can't slip past literal-adjacency matching.
# `git checkout ./subdir`, `git checkout <branch>`, `git restore <path>`,
# and `git checkout --ours -- <path>` all stay allowed.
DOT_TAIL='([[:space:]]+--?[^[:space:]]+)*([[:space:]]+--)?[[:space:]]+\.(/)?([[:space:]]|$|[;&|])'
if _guarded 'checkout\b' && echo "$WORDS" | grep -qE "${GIT_ANCHOR}checkout${DOT_TAIL}"; then
    _block "git checkout ." "It discards every uncommitted modification in the tree; restore individual paths deliberately, or stop and report."
fi
if _guarded 'restore\b' && echo "$WORDS" | grep -qE "${GIT_ANCHOR}restore${DOT_TAIL}"; then
    # `git restore --staged .` only unstages (index-only, worktree
    # untouched) and is the sanctioned unstage-everything form — allowed
    # unless --worktree re-adds the destructive half.
    if ! echo "$WORDS" | grep -qE "${GIT_ANCHOR}restore[^;&|]*--staged\b" \
        || echo "$WORDS" | grep -qE "${GIT_ANCHOR}restore[^;&|]*(--worktree\b|(^|[[:space:]])-W\b)"; then
        _block "git restore ." "It discards every uncommitted modification in the tree; restore individual paths deliberately, or stop and report."
    fi
fi

# `git stash` — every form except the read-only `list` / `show`. A stash
# moves work out of a checkout that other sessions may be editing, a
# `pop` can conflict or land on the wrong branch, `drop`/`clear` delete,
# and `-u`/`-a` run `git clean` internally. No sanctioned procedure uses
# stash: FOUNDATION §2's sync ladder secures dirty work with a checkpoint
# commit, and "does this failure predate my change?" is answered in a
# forge-scratch-repo copy. The subcommand is the first non-flag word
# after `stash`; a bare `git stash` is a push.
_stash_blocked() {
    local tail tok sub
    local -a toks
    while IFS=$'\t' read -r _ tail; do
        sub=""
        read -ra toks <<< "$tail"
        for tok in "${toks[@]}"; do
            case "$tok" in -*) ;; *) sub="$tok"; break ;; esac
        done
        case "$sub" in list | show) ;; *) return 0 ;; esac
    done <<< "$(guard_git_invocations "$WORDS" stash)"
    return 1
}
if _guarded 'stash\b' && _stash_blocked; then
    _block "git stash" "Stash moves uncommitted work out of a checkout other sessions may share, and pop/drop/-u can lose it. Only \`git stash list\` / \`git stash show\` are allowed. To secure dirty work use FOUNDATION §2's sync ladder (wip-sync checkpoint commit); to check whether a failure predates your change use \`git show <base>:<path>\` or a \`forge-scratch-repo snapshot --ref <base>\` copy."
fi

# Restores that overwrite a path's working copy — `git checkout [<ref>]
# [--] <paths>` and `git restore` touching the worktree — are blocked
# when any named path holds work that exists nowhere else: a modified
# working copy, an untracked file the restore would overwrite, or staged
# changes outside a merge. Checked against live state (`git status` in
# the directory the command runs in), like block_amend_pushed_commit.sh:
# a restore of a clean path destroys nothing and stays allowed. Staged-
# only entries during a merge came from the merge and can be re-derived,
# so the documented release-recovery restore of `changelog.d/` keeps
# working. Conflict resolution (`--ours`/`--theirs`/`--merge`/`-m`/
# `--conflict`) stays allowed for paths that are actually in conflict —
# on any other path those flags overwrite it like a plain restore. When the hook cannot tell what a path
# names or where the command runs — a glob, `$`, `~`, pathspec magic, a
# quoted path with spaces, an earlier `cd`, `--git-dir`/`--work-tree` —
# it blocks: a false block costs a `!`, a wrong allow costs someone's
# work.
PAYLOAD_CWD=$(jq -r '.cwd // empty' <<< "$INPUT")
case "$PAYLOAD_CWD" in /*) ;; *) PAYLOAD_CWD="." ;; esac
CMDPOS_CHDIR=0
if echo "$CMDPOS" | grep -qE '(^|[;&|(])[[:space:]]*(cd|pushd|popd)([[:space:]]|$)'; then
    CMDPOS_CHDIR=1
fi

# _invocation_dir <anchor prefix> — prints the directory a git invocation
# runs in (payload cwd, then each `-C`); fails when it cannot be known.
_invocation_dir() {
    local dir="$PAYLOAD_CWD" tok next=0
    local -a toks
    [ "$CMDPOS_CHDIR" = 0 ] || return 1
    read -ra toks <<< "$1"
    for tok in "${toks[@]}"; do
        if [ "$next" = 1 ]; then
            case "$tok" in *'$'* | *'@SUBST@'* | '~'*) return 1 ;; esac
            case "$tok" in /*) dir="$tok" ;; *) dir="$dir/$tok" ;; esac
            next=0
            continue
        fi
        if [ "$next" = c ]; then
            # `-c core.worktree=…` relocates the work tree.
            case "$tok" in *[Ww]ork[Tt]ree*) return 1 ;; esac
            next=0
            continue
        fi
        case "$tok" in
            -C) next=1 ;;
            -c) next=c ;;
            -C?*) tok="${tok#-C}"
                case "$tok" in *'$'* | *'@SUBST@'* | '~'*) return 1 ;; esac
                case "$tok" in /*) dir="$tok" ;; *) dir="$dir/$tok" ;; esac ;;
            --git-dir* | --work-tree* | GIT_DIR=* | GIT_WORK_TREE=* | GIT_INDEX_FILE=*) return 1 ;;
        esac
    done
    printf '%s\n' "$dir"
}

# _paths_hold_work <dir> <path>... — true when restoring <path>s in <dir>
# could destroy work, or when that cannot be determined.
_paths_hold_work() {
    local dir="$1" p st merging=0 line y
    shift
    [ "$#" -gt 0 ] || return 1
    for p in "$@"; do
        case "$p" in
            *'@SUBST@'* | *'$'* | *'*'* | *'?'* | *'['* | *'{'* | '~'* | :* | *'\'*) return 0 ;;
        esac
    done
    git -C "$dir" rev-parse --is-inside-work-tree > /dev/null 2>&1 || return 1
    git -C "$dir" rev-parse -q --verify MERGE_HEAD > /dev/null 2>&1 && merging=1
    st=$(git -C "$dir" status --porcelain --untracked-files=all -- "$@" 2> /dev/null) || return 0
    while IFS= read -r line; do
        [ -n "$line" ] || continue
        y="${line:1:1}"
        if [ "$y" != " " ] || [ "$merging" = 0 ]; then
            return 0
        fi
    done <<< "$st"
    return 1
}

# _abbrev <token> <long option> <min length> — true when <token> is
# <long option> or an abbreviation git accepts for it (git takes any
# unambiguous prefix of a long option, so `--fo` is `--force`).
_abbrev() {
    [ "${#1}" -ge "$3" ] && [ "${2#"$1"}" != "$2" ]
}

# _only_conflicts <dir> <path>... — true when every named path is
# unmerged and none holds anything else. `--ours`/`--theirs`/`--merge`/
# `--conflict` are conflict-resolution flags, but on a path that is NOT
# in conflict they simply overwrite it from the index, discarding edits —
# so they are exempt only for paths that are actually conflicted.
_only_conflicts() {
    local dir="$1" st line p
    shift
    [ "$#" -gt 0 ] || return 1
    for p in "$@"; do
        case "$p" in
            *'@SUBST@'* | *'$'* | *'*'* | *'?'* | *'['* | *'{'* | '~'* | :* | *'\'*) return 1 ;;
        esac
    done
    st=$(git -C "$dir" status --porcelain --untracked-files=all -- "$@" 2> /dev/null) || return 1
    [ -n "$st" ] || return 1
    while IFS= read -r line; do
        [ -n "$line" ] || continue
        case "${line:0:2}" in DD | AU | UD | UA | DU | AA | UU) ;; *) return 1 ;; esac
    done <<< "$st"
    return 0
}

# _checkout_holds_work <dir> <args> — checkout argument rules (see above).
_checkout_holds_work() {
    local dir="$1" tok dd=0 branchop=0 conflict=0
    local -a before=() after=() paths=() toks
    read -ra toks <<< "$2"
    for tok in "${toks[@]}"; do
        if [ "$dd" = 1 ]; then after+=("$tok"); continue; fi
        case "$tok" in
            --) dd=1 ;;
            -m | --conflict=*) conflict=1 ;;
            --pa*) return 0 ;;
            -b | -B | --orphan) branchop=1 ;;
            --*)
                if _abbrev "$tok" --ours 4 || _abbrev "$tok" --theirs 4 \
                    || _abbrev "$tok" --merge 4 || _abbrev "$tok" --conflict 4; then
                    conflict=1
                fi
                ;;
            -*) ;;
            *) before+=("$tok") ;;
        esac
    done
    if [ "$dd" = 1 ]; then
        paths=("${after[@]}")
    elif [ "$branchop" = 0 ] && [ "${#before[@]}" -gt 0 ]; then
        case "${before[0]}" in *'@SUBST@'* | *'$'*) return 0 ;; esac
        if git -C "$dir" rev-parse -q --verify "${before[0]}^{commit}" > /dev/null 2>&1; then
            # `git checkout <branch>` alone carries local edits across (git
            # refuses to clobber them); only paths after the ref are restored.
            paths=("${before[@]:1}")
        else
            paths=("${before[@]}")
        fi
    else
        return 1
    fi
    if [ "$conflict" = 1 ] && _only_conflicts "$dir" "${paths[@]}"; then
        return 1
    fi
    _paths_hold_work "$dir" "${paths[@]}"
}

# _restore_holds_work <dir> <args> — restore argument rules (see above).
_restore_holds_work() {
    local dir="$1" tok dd=0 staged=0 worktree=0 src=0 conflict=0
    local -a paths=() toks
    read -ra toks <<< "$2"
    for tok in "${toks[@]}"; do
        if [ "$dd" = 1 ]; then paths+=("$tok"); continue; fi
        if [ "$src" = 1 ]; then src=0; continue; fi
        case "$tok" in
            --) dd=1 ;;
            -m | --conflict=*) conflict=1 ;;
            --pa*) return 0 ;;
            --source=*) ;;
            --*)
                if _abbrev "$tok" --staged 4; then staged=1
                elif _abbrev "$tok" --worktree 3; then worktree=1
                elif _abbrev "$tok" --source 4; then src=1
                elif _abbrev "$tok" --ours 4 || _abbrev "$tok" --theirs 4 \
                    || _abbrev "$tok" --merge 4 || _abbrev "$tok" --conflict 4 \
                    || _abbrev "$tok" --ignore-unmerged 4; then conflict=1
                fi
                ;;
            -*)
                case "$tok" in *S*) staged=1 ;; esac
                case "$tok" in *W*) worktree=1 ;; esac
                case "$tok" in -s) src=1 ;; esac
                ;;
            *) paths+=("$tok") ;;
        esac
    done
    if [ "$staged" = 1 ] && [ "$worktree" = 0 ]; then
        return 1
    fi
    if [ "$conflict" = 1 ] && _only_conflicts "$dir" "${paths[@]}"; then
        return 1
    fi
    _paths_hold_work "$dir" "${paths[@]}"
}

# _restore_blocked <verb> — true when any <verb> invocation holds work.
# The --words view supplies the arguments; the command-positions view
# (one `_` per quoted span) must yield the same invocations with the same
# number of words, or a quoted path held spaces and the words are not the
# paths git receives — fail closed.
_restore_blocked() {
    local verb="$1" pre tail dir i=0 n_words n_pos
    local -a word_lines=() pos_lines=()
    mapfile -t word_lines <<< "$(guard_git_invocations "$WORDS" "$verb")"
    mapfile -t pos_lines <<< "$(guard_git_invocations "$CMDPOS" "$verb")"
    [ "${#word_lines[@]}" = "${#pos_lines[@]}" ] || return 0
    for ((i = 0; i < ${#word_lines[@]}; i++)); do
        [ -n "${word_lines[$i]}" ] || continue
        IFS=$'\t' read -r pre tail <<< "${word_lines[$i]}"
        n_words=$(wc -w <<< "$tail")
        n_pos=$(wc -w <<< "${pos_lines[$i]#*$'\t'}")
        [ "$n_words" = "$n_pos" ] || return 0
        dir=$(_invocation_dir "$pre") || return 0
        if [ "$verb" = checkout ]; then
            _checkout_holds_work "$dir" "$tail" && return 0
        else
            _restore_holds_work "$dir" "$tail" && return 0
        fi
    done
    return 1
}

if _guarded 'checkout\b' && _restore_blocked checkout; then
    _block "git checkout <paths>" "A path it would overwrite holds uncommitted work (a modified or staged file, or an untracked file in the way) — possibly another session's — or the hook cannot tell which paths it names. Report the state instead; experiments belong in a \`forge-scratch-repo\` copy."
fi
if _guarded 'restore\b' && _restore_blocked restore; then
    _block "git restore <paths>" "A path it would overwrite holds uncommitted work (a modified or staged file, or an untracked file in the way) — possibly another session's — or the hook cannot tell which paths it names. To unstage only, use \`git restore --staged <path>\`; otherwise report the state."
fi

# Forced branch switches discard local modifications instead of refusing:
# `git checkout -f|--force` (any form) and `git switch -f|--force|
# --discard-changes`.
_forced_switch() {
    local verb="$1" tail tok
    local -a toks
    while IFS=$'\t' read -r _ tail; do
        read -ra toks <<< "$tail"
        for tok in "${toks[@]}"; do
            case "$tok" in
                --) break ;;
                --*)
                    if _abbrev "$tok" --force 4 || _abbrev "$tok" --discard-changes 4; then
                        return 0
                    fi
                    ;;
                -*f*) return 0 ;;
            esac
        done
    done <<< "$(guard_git_invocations "$WORDS" "$verb")"
    return 1
}
if { _guarded 'checkout\b' && _forced_switch checkout; } || { _guarded 'switch\b' && _forced_switch switch; }; then
    _block "git checkout/switch --force" "A forced switch throws away uncommitted modifications instead of refusing; switch without --force, or stop and report the local changes."
fi

