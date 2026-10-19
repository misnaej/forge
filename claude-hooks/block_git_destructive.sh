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
#   branch switching and conflict resolution (`--ours`/`--theirs`) on
#   paths actually in conflict stay allowed.
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
guard_begin

# Commands are found on the command-positions view (git_anchor.sh) — a
# quoted mention is not a reset, a wrapped `bash -c "git reset …"` is —
# and their flags and pathspecs are read from the --words view, where a
# quoted argument is still the argument git receives, with redirections
# blanked so `2>&1` cannot end an argument scan early. `_guarded <verb-re>`
# is true when a real invocation exists that is not just asking for help.
CMDPOS=$(command_positions "$COMMAND")
WORDS=$(command_positions --words --no-redir "$COMMAND")
# Per-invocation work below splits words with the shell's own word
# splitting (`toks=($x)`) rather than `read <<<` — a here-string costs a
# pipe or temp file each time, and per-invocation cost is what lets a long
# chain run a hook past its timeout. Pathname expansion is off so a word
# like `*` is never globbed by that splitting.
set -f
# Answers are cached per verb: each costs two matches of the full anchor
# over the whole command, which on a long command is the slowest thing
# this hook does, and several rules ask about the same verb.
GUARDED_CACHE=$'\n'
_guarded() {
    case "$GUARDED_CACHE" in
        *$'\n'"1 $1"$'\n'*) return 0 ;;
        *$'\n'"0 $1"$'\n'*) return 1 ;;
    esac
    if echo "$CMDPOS" | grep -qE "${GIT_ANCHOR}$1" && ! guard_help_only "$CMDPOS" "${GIT_ANCHOR}$1"; then
        GUARDED_CACHE+="1 $1"$'\n'
        return 0
    fi
    GUARDED_CACHE+="0 $1"$'\n'
    return 1
}

# `git reset` — every form. The blanket ban subsumes the --hard/--merge
# block that previously lived in block_git_reset_hard.sh (retired).
if _guarded 'reset\b'; then
    _block "git reset" "Rewinds un-commit history (published commits on a synced branch) and --hard/--merge discard uncommitted work; to unstage, use \`git restore --staged <path>\` instead."
fi

# `git clean` with any of -f/-d/-x/-X (clustered or separate) or --force.
# A dry run (`-n`/--dry-run) short-circuits — it only lists candidates and
# is the sanctioned way to REPORT untracked state — but the exemption is
# evaluated PER INVOCATION (guard_git_invocations), so
# `git clean -n; git clean -f` still blocks on the second invocation.
# Invocations come from the --words view, so a separator inside a quoted
# argument never ends an invocation and a quoted mention never forms one.
# Redirections are blanked first (guard_git_invocations), so the `&` of
# `git clean 2>&1 -fd` cannot end the invocation before its flags.
# Flags are read only from the arguments AFTER the matched `clean` verb,
# so neither a wrapper's own flag (`sudo -n git clean -f`) nor a `clean`
# spelled earlier in the prefix (`X=clean-n git clean -fd`) can
# masquerade as a dry run.
# Matched with bash's own regex engine: a process per invocation let a
# long chain of `git clean -n;` run this hook past its timeout.
CLEAN_DRY_RE='(^|[[:space:]])(--dry-run([^[:alnum:]_-]|$)|-[a-zA-Z]*n)'
CLEAN_FORCE_RE='(^|[[:space:]])(--force([^[:alnum:]_-]|$)|-[a-zA-Z]*[fdxX])'
_guarded 'clean\b' && while IFS=$'\t' read -r _ rest; do
    if [[ $rest =~ $CLEAN_DRY_RE ]]; then
        continue
    fi
    if [[ $rest =~ $CLEAN_FORCE_RE ]]; then
        _block "git clean" "It deletes untracked files permanently — no reflog, no index, no recovery; report the untracked paths instead (\`git clean -n\` to list them)."
    fi
done <<< "$(guard_git_invocations "$WORDS" clean)"

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
        toks=($tail)
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
# on any other path those flags overwrite it like a plain restore.
# When the hook cannot tell what a path names or where the command runs — a glob, `$`, `~`, pathspec magic, a
# quoted path with spaces, an earlier `cd`, `--git-dir`/`--work-tree` —
# it blocks: a false block costs a `!`, a wrong allow costs someone's
# work.
PAYLOAD_CWD=$(jq -r '.cwd // empty' <<< "$INPUT")
case "$PAYLOAD_CWD" in /*) ;; *) PAYLOAD_CWD="." ;; esac
CMDPOS_CHDIR=0
# Any `cd`/`pushd`/`popd` word outside quotes counts — behind `then`,
# `{`, `!`, `builtin`, `command` … as much as at line start. Over-matching
# only blocks a restore that would otherwise be checked (fail closed).
if echo "$CMDPOS" | grep -qE '(^|[[:space:];&|(!{])(cd|pushd|popd)([[:space:];&|)]|$)'; then
    CMDPOS_CHDIR=1
fi

# Live state, read ONCE per directory however many invocations name it
# (bash 3.2 has no associative arrays, so parallel indexed arrays keyed by
# position in LS_DIRS). For a directory: LS_IN (1 inside a work tree, 0
# not, u unknown) and LS_PREFIX (the directory relative to the repo root,
# as `git rev-parse --show-prefix` prints it). The entries `git status
# --porcelain -z --untracked-files=all -- <paths>` lists for the paths the
# command names there are kept raw in LS_RAW, with LS_MERGING, and
# LS_STATUS_AWK turns them into the judge's H/A/N path lists. Status is
# asked about the named paths only, never the whole tree — a tree of many
# untracked files made a whole-tree status slow enough to time the hook
# out — and once per directory rather than once per invocation, which let
# a long chain do the same (a timed-out hook allows the command).
LS_DIRS=() LS_IN=() LS_PREFIX=() LS_RAW=() LS_MERGING=()
LS_MAX=32
# _live_state <dir> [<pathspec>...] — sets LS_I to the cache slot for <dir>,
# reading the state of the named pathspecs on the first call for <dir>.
_live_state() {
    local dir="$1" i out inside prefix mh merging=0
    for ((i = 0; i < ${#LS_DIRS[@]}; i++)); do
        if [ "${LS_DIRS[$i]}" = "$dir" ]; then LS_I=$i; return 0; fi
    done
    LS_I=${#LS_DIRS[@]}
    LS_DIRS[LS_I]="$dir"
    LS_PREFIX[LS_I]=""
    # Each new directory costs two git processes; a command naming more
    # than LS_MAX of them (`git -C d1 …; git -C d2 …; …`) is judged
    # unknowable instead, so no count of directories can run this hook
    # past its timeout.
    if [ "$LS_I" -ge "$LS_MAX" ]; then LS_IN[LS_I]=u; return 0; fi
    if ! out=$(git -C "$dir" rev-parse --is-inside-work-tree --show-prefix --git-path MERGE_HEAD 2> /dev/null); then
        # Not a repository (or not readable as one): git would refuse the
        # restore, as before.
        LS_IN[LS_I]=0
        return 0
    fi
    inside="${out%%$'\n'*}"; out="${out#*$'\n'}"
    prefix="${out%%$'\n'*}"; mh="${out#*$'\n'}"
    if [ "$inside" != true ]; then LS_IN[LS_I]=0; return 0; fi
    LS_PREFIX[LS_I]="$prefix"
    case "$mh" in /*) ;; *) mh="$dir/$mh" ;; esac
    [ -e "$mh" ] && merging=1
    shift
    if [ "$#" -eq 0 ]; then LS_IN[LS_I]=1; return 0; fi
    # Records are NUL-separated; NUL cannot live in a shell variable, so it
    # becomes \001, and a \001 already in a path becomes \002. A path
    # holding a tab, a newline or such a byte cannot be matched reliably in
    # this form, so the directory is then unknowable (fail closed) rather
    # than a path that silently never matches.
    if ! out=$(set -o pipefail; git -C "$dir" status --porcelain -z --untracked-files=all -- "$@" 2> /dev/null | tr '\0\001' '\001\002'); then
        LS_IN[LS_I]=u
        return 0
    fi
    case "$out" in *$'\t'* | *$'\n'* | *$'\002'*) LS_IN[LS_I]=u; return 0 ;; esac
    LS_IN[LS_I]=1
    # Kept raw; _restore_state turns it into the H/A/N lists in one awk
    # pass (a shell loop per entry was slow on a directory of many files).
    LS_RAW[LS_I]="$out"
    LS_MERGING[LS_I]="$merging"
}
# LS_STATUS_AWK — reads one directory's raw status (\001-separated records;
# a rename / copy record's second field is its other path) and prints
# `H|A|N<TAB><dir><TAB><path>` lines: H for an entry a restore would
# destroy (changed in the worktree, untracked, or staged outside a merge),
# A for every entry, N for every entry that is not a merge conflict.
LS_STATUS_AWK='
BEGIN { RS = "\001"; d = ENVIRON["D"]; merging = ENVIRON["M"] }
$0 == "" || $0 == "\n" { next }
{
    if (pending != "") { xy = pending; path = $0; pending = "" }
    else {
        xy = substr($0, 1, 2); path = substr($0, 4)
        if (xy ~ /^[RC]/) pending = xy
    }
    print "A\t" d "\t" path
    if (substr(xy, 2, 1) != " " || merging == "0") print "H\t" d "\t" path
    if (xy !~ /^(DD|AU|UD|UA|DU|AA|UU)$/) print "N\t" d "\t" path
}'

# The restore rules run in awk, in two passes around one bash step, so
# that no per-invocation work happens in the shell: per-invocation shell
# work — processes, or even just interpreted bash — let a long chain of
# harmless invocations run this hook past its timeout, and a timed-out
# hook allows the command.
#
#   1. RESTORE_PARSE_AWK reads every invocation once: it checks the word
#      counts of the two views, finds the directory the invocation runs in
#      (payload cwd, then each `-C`), applies the checkout / restore
#      argument rules, and prints one record per invocation still to judge
#      (`J` paths, or `C` a checkout whose first word may be a ref), the
#      directories (`D`) and candidate refs (`R`) those need — or `UNKNOWN`
#      when an invocation cannot be read, which blocks.
#   2. _restore_state (bash) reads each directory's live state once
#      (_live_state, at most LS_MAX directories) and resolves all of its
#      candidate refs in ONE `git cat-file --batch-check`.
#   3. RESTORE_JUDGE_AWK judges every record against that state.
#
# Unchanged rules, in the order the parse applies them —
# checkout: `--` ends the options; `-m`/`--conflict=…` and any accepted
# abbreviation of `--ours`/`--theirs`/`--merge`/`--conflict` mark conflict
# resolution; `--pa…` (patch) and a short cluster holding `p` are
# unknowable; `-b`/`-B`/`--orphan` create a branch and restore nothing.
# Without `--`, `git checkout <ref> [paths]` restores only the paths after
# a ref — so the first word matters only when reading it as a ref or as a
# path gives different answers, and only then is git asked.
# restore: as checkout, plus `--staged` (4) / `--worktree` (3) / `-S` /
# `-W`, `--source[=]`/`-s <tree>`, `--ignore-unmerged`; `--staged`
# without `--worktree` only unstages and is allowed.
# A path is unknowable — blocked — when it holds a glob, `$`, `~`, `{`, a
# backslash, pathspec magic (`:`) or a command substitution, or climbs out
# of the repository; conflict-only paths are exempt under a conflict flag.
RESTORE_PARSE_AWK='
function abbrev(t, long, min) { return length(t) >= min && substr(long, 1, length(t)) == t }
function emit_dir(d) { if (!(d in SEEN)) { SEEN[d] = 1; print "D\t" d } }
function unknown() { print "UNKNOWN"; bad = 1; exit }
# The directory one invocation runs in, or "" when it cannot be known.
function inv_dir(pre,   n, P, i, t, dir, next1, nextc) {
    if (CHDIR) return ""
    dir = ENVIRON["CWD"]; next1 = 0; nextc = 0
    n = split(pre, P, /[ \t]+/)
    for (i = 1; i <= n; i++) {
        t = P[i]
        if (t == "") continue
        if (next1) {
            if (t ~ /[$]|@SUBST@/ || t ~ /^~/) return ""
            dir = (t ~ /^\//) ? t : dir "/" t
            next1 = 0; continue
        }
        if (nextc) {
            if (tolower(t) ~ /worktree/) return ""
            nextc = 0; continue
        }
        if (t == "-C") { next1 = 1; continue }
        if (t == "-c") { nextc = 1; continue }
        if (t ~ /^-C./) {
            t = substr(t, 3)
            if (t ~ /[$]|@SUBST@/ || t ~ /^~/) return ""
            dir = (t ~ /^\//) ? t : dir "/" t
            continue
        }
        if (t ~ /^(--git-dir|--work-tree|--config-env|GIT_DIR=|GIT_WORK_TREE=|GIT_INDEX_FILE=|GIT_CONFIG_)/) return ""
    }
    return dir
}
# First file: the command-positions view; second: the --words view. They
# must yield the same invocations (END), each with the same word count.
FILENAME == ARGV[1] { if ($0 != "") NPOS++; POSN[FNR] = $0; next }
{
    line = $0
    if (line == "") next
    NWORDS++
    k = index(line, "\t")
    pre = substr(line, 1, k - 1); tail = substr(line, k + 1)
    p = POSN[FNR]; p = substr(p, index(p, "\t") + 1)
    nw = split(tail, W, /[ \t]+/); np = split(p, Q, /[ \t]+/)
    cw = 0; for (i = 1; i <= nw; i++) if (W[i] != "") cw++
    cq = 0; for (i = 1; i <= np; i++) if (Q[i] != "") cq++
    if (cw != cq) unknown()
    dir = inv_dir(pre)
    dd = 0; conflict = 0; branchop = 0; staged = 0; worktree = 0; src = 0
    nb = 0; na = 0
    split("", B); split("", A)
    for (i = 1; i <= nw; i++) {
        t = W[i]
        if (t == "") continue
        if (dd) { A[++na] = t; continue }
        if (VERB == "restore" && src) { src = 0; continue }
        if (t == "--") { dd = 1; continue }
        if (t == "-m" || t ~ /^--conflict=/) { conflict = 1; continue }
        if (t ~ /^--pa/) unknown()
        if (VERB == "checkout") {
            if (t == "-b" || t == "-B" || t == "--orphan") { branchop = 1; continue }
            if (t ~ /^--/) {
                if (abbrev(t, "--ours", 4) || abbrev(t, "--theirs", 4) || abbrev(t, "--merge", 4) || abbrev(t, "--conflict", 4)) conflict = 1
                continue
            }
            if (t ~ /^-.*p/) unknown()
            if (t ~ /^-/) continue
            B[++nb] = t
        } else {
            if (t ~ /^--source=/) continue
            if (t ~ /^--/) {
                if (abbrev(t, "--staged", 4)) staged = 1
                else if (abbrev(t, "--worktree", 3)) worktree = 1
                else if (abbrev(t, "--source", 4)) src = 1
                else if (abbrev(t, "--ours", 4) || abbrev(t, "--theirs", 4) || abbrev(t, "--merge", 4) || abbrev(t, "--conflict", 4) || abbrev(t, "--ignore-unmerged", 4)) conflict = 1
                continue
            }
            if (t ~ /^-s./) continue
            if (t ~ /^-.*p/) unknown()
            if (t ~ /^-/) {
                if (t ~ /S/) staged = 1
                if (t ~ /W/) worktree = 1
                if (t == "-s") src = 1
                continue
            }
            A[++na] = t
        }
    }
    # A checkout that creates a branch (`-b` / `-B` / `--orphan`) with no
    # `--` restores no path: git refuses to "update paths and switch to
    # branch at the same time", so its directory never matters. Deciding
    # that first lets `cd x && git checkout -q -b feat` through; every
    # other form whose directory is unknowable still fails closed.
    if (VERB == "checkout" && branchop && !dd) next
    if (dir == "") unknown()
    if (VERB == "restore") {
        if (staged && !worktree) next
        out = ""; for (i = 1; i <= na; i++) out = out "\t" A[i]
        emit_dir(dir); print "J\t" dir "\t" conflict out
        next
    }
    if (dd) {
        out = ""; for (i = 1; i <= na; i++) out = out "\t" A[i]
        emit_dir(dir); print "J\t" dir "\t" conflict out
        next
    }
    if (branchop || nb == 0) next
    if (B[1] ~ /[$]|@SUBST@/) unknown()
    out = ""; for (i = 2; i <= nb; i++) out = out "\t" B[i]
    emit_dir(dir)
    if (!((dir SUBSEP B[1]) in RSEEN)) { RSEEN[dir, B[1]] = 1; print "R\t" dir "\t" B[1] }
    print "C\t" dir "\t" conflict "\t" B[1] out
}
END { if (!bad && NWORDS != NPOS) print "UNKNOWN" }
'

RESTORE_JUDGE_AWK='
# State lines first (S in/prefix, H/A/N paths, F ref verdicts), then the
# records from the parse.
# An absolute pathspec is not resolved against the repository here, so it
# is unknowable too — git accepts one inside the work tree.
function meta(p) { return p ~ /@SUBST@|[$*?[{\\]/ || p ~ /^[~:\/]/ }
# The repo-relative path a pathspec names ("" for the whole repo); sets
# RESOLVED_OK to 0 when it climbs out of the repository.
function resolve(prefix, p,   n, S, i, out) {
    RESOLVED_OK = 1; out = ""
    n = split(prefix p, S, "/")
    for (i = 1; i <= n; i++) {
        if (S[i] == "" || S[i] == ".") continue
        if (S[i] == "..") {
            if (out == "") { RESOLVED_OK = 0; return "" }
            if (index(out, "/")) sub(/\/[^\/]*$/, "", out); else out = ""
            continue
        }
        out = (out == "") ? S[i] : out "/" S[i]
    }
    return out
}
# Does a path list (kind H, A or N) of dir hold an entry spec covers?
function covers(kind, dir, spec) {
    if (spec == "") return (CNT[kind, dir] > 0)
    return ((kind, dir, spec) in EXACT) || ((kind, dir, spec) in UNDER)
}
function add(kind, dir, path,   d) {
    EXACT[kind, dir, path] = 1; CNT[kind, dir]++
    d = path
    while (sub(/\/[^\/]*$/, "", d)) UNDER[kind, dir, d] = 1
}
function hold_work(dir, n, P,   i, s) {
    if (n == 0) return 0
    for (i = 1; i <= n; i++) if (meta(P[i])) return 1
    if (IN[dir] == "0") return 0
    if (IN[dir] != "1") return 1
    for (i = 1; i <= n; i++) {
        s = resolve(PREFIX[dir], P[i])
        if (!RESOLVED_OK) return 1
        if (covers("H", dir, s)) return 1
    }
    return 0
}
function only_conflicts(dir, n, P,   i, s, any) {
    if (n == 0) return 0
    for (i = 1; i <= n; i++) if (meta(P[i])) return 0
    if (IN[dir] != "1") return 0
    any = 0
    for (i = 1; i <= n; i++) {
        s = resolve(PREFIX[dir], P[i])
        if (!RESOLVED_OK) return 0
        if (covers("N", dir, s)) return 0
        if (covers("A", dir, s)) any = 1
    }
    return any
}
function judged(dir, conflict, n, P) {
    if (conflict && only_conflicts(dir, n, P)) return 0
    return hold_work(dir, n, P)
}
BEGIN { FS = "\t" }
$1 == "X" { print "BLOCK"; exit }
$1 == "S" { IN[$2] = $3; PREFIX[$2] = $4; next }
$1 == "H" || $1 == "A" || $1 == "N" { add($1, $2, $3); next }
$1 == "F" { ISREF[$2, $3] = $4; next }
$1 == "J" {
    n = 0; for (i = 4; i <= NF; i++) P[++n] = $i
    if (judged($2, $3, n, P)) { print "BLOCK"; exit }
    next
}
$1 == "C" {
    n = 0; for (i = 5; i <= NF; i++) P[++n] = $i
    as_ref = judged($2, $3, n, P)
    m = 0; Q[++m] = $4; for (i = 5; i <= NF; i++) Q[++m] = $i
    as_path = judged($2, $3, m, Q)
    if (as_ref == as_path) { if (as_ref) { print "BLOCK"; exit } next }
    # The readings disagree: git decides whether the first word is a ref
    # (a lookup that failed reads as "not a ref", as before).
    r = (($2, $4) in ISREF && ISREF[$2, $4] == "1") ? as_ref : as_path
    if (r) { print "BLOCK"; exit }
    next
}
'

# _restore_state <records> — prints the judge's state lines for every
# directory and ref the parse records name. Directory values reach awk
# through the environment, never `-v`, which would rewrite a backslash in
# them and judge a directory other than the one the command runs in.
_restore_state() {
    local records="$1" dir words out
    local -a dirs=()
    # More directories than the state cache holds is unknowable, which
    # blocks; awk stops at the cap so no count reaches a shell loop.
    while IFS= read -r dir; do
        if [ "$dir" = $'\001X' ]; then
            printf 'X\n'
            return 0
        fi
        dirs+=("$dir")
    done < <(printf '%s\n' "$records" | LC_ALL=C awk -F'\t' -v MAX="$LS_MAX" \
        '$1 == "D" { if (++n > MAX) { print "\001X"; exit } print $2 }')
    local -a specs
    for dir in "${dirs[@]}"; do
        # The pathspecs this directory's invocations name (J paths; C first
        # word and paths) — status is asked about these only. Unknowable
        # ones are left out: the judge blocks them without asking git.
        # Word splitting is safe here: pathname expansion is off (set -f)
        # and a pathspec holds no blank.
        specs=($(printf '%s\n' "$records" | D="$dir" LC_ALL=C awk -F'\t' '
            ($1 == "J" || $1 == "C") && $2 == ENVIRON["D"] {
                for (i = 4; i <= NF; i++)
                    if ($i != "" && $i !~ /@SUBST@|[$*?[{\\]/ && $i !~ /^[~:\/]/ && !($i in seen)) { seen[$i] = 1; print $i }
            }'))
        _live_state "$dir" "${specs[@]}"
        printf 'S\t%s\t%s\t%s\n' "$dir" "${LS_IN[LS_I]}" "${LS_PREFIX[LS_I]}"
        [ "${LS_IN[LS_I]}" = 1 ] || continue
        printf '%s' "${LS_RAW[LS_I]}" | D="$dir" M="${LS_MERGING[LS_I]}" LC_ALL=C awk "$LS_STATUS_AWK"
        # Every candidate ref of this directory in ONE git process; a
        # `^{commit}` that does not resolve prints `… missing`.
        words=$(printf '%s\n' "$records" | D="$dir" LC_ALL=C awk -F'\t' '$1 == "R" && $2 == ENVIRON["D"] { print $3 }')
        [ -n "$words" ] || continue
        out=$(printf '%s\n' "$words" | LC_ALL=C awk '{ print $0 "^{commit}" }' \
            | git -C "$dir" cat-file --batch-check 2> /dev/null) || out=""
        paste -d '\t' <(printf '%s\n' "$words") <(printf '%s\n' "$out") \
            | D="$dir" LC_ALL=C awk -F'\t' '{ print "F\t" ENVIRON["D"] "\t" $1 "\t" (($2 ~ / commit /) ? 1 : 0) }'
    done
}

# _restore_blocked <verb> — true when any <verb> invocation holds work, or
# when one cannot be read. The --words view supplies the arguments; the
# command-positions view (one `_` per quoted span) must yield the same
# invocations with the same number of words, or a quoted path held spaces
# and the words are not the paths git receives — fail closed.
_restore_blocked() {
    local verb="$1" records verdict
    records=$(CWD="$PAYLOAD_CWD" LC_ALL=C awk -v VERB="$verb" -v CHDIR="$CMDPOS_CHDIR" \
        "$RESTORE_PARSE_AWK" \
        <(guard_git_invocations "$CMDPOS" "$verb") \
        <(guard_git_invocations "$WORDS" "$verb")) || return 0
    case "$records" in *UNKNOWN*) return 0 ;; esac
    [ -n "$records" ] || return 1
    verdict=$({ _restore_state "$records"; printf '%s\n' "$records" | grep -E $'^(J|C)\t'; } \
        | LC_ALL=C awk "$RESTORE_JUDGE_AWK") || return 0
    [ "$verdict" = BLOCK ]
}

if _guarded 'checkout\b' && _restore_blocked checkout; then
    _block "git checkout <paths>" "A path it would overwrite holds uncommitted work (a modified or staged file, or an untracked file in the way) — possibly another session's — or the hook cannot tell which paths it names. Report the state instead; experiments belong in a \`forge-scratch-repo\` copy."
fi
if _guarded 'restore\b' && _restore_blocked restore; then
    _block "git restore <paths>" "A path it would overwrite holds uncommitted work (a modified or staged file, or an untracked file in the way) — possibly another session's — or the hook cannot tell which paths it names. To unstage only, use \`git restore --staged <path>\`; otherwise report the state."
fi

# Forced branch switches discard local modifications instead of refusing:
# `git checkout -f|--force` (any form) and `git switch -f|--force|
# --discard-changes`. One awk pass over every invocation.
_forced_switch() {
    local hit
    hit=$(guard_git_invocations "$WORDS" "$1" | LC_ALL=C awk '
function abbrev(t, long, min) { return length(t) >= min && substr(long, 1, length(t)) == t }
{
    n = split(substr($0, index($0, "\t") + 1), T, /[ \t]+/)
    for (i = 1; i <= n; i++) {
        t = T[i]
        if (t == "") continue
        if (t == "--") break
        if (t ~ /^--/) { if (abbrev(t, "--force", 3) || abbrev(t, "--discard-changes", 4)) { print "y"; exit } continue }
        if (t ~ /^-.*f/) { print "y"; exit }
    }
}') || return 0
    [ -n "$hit" ]
}
if { _guarded 'checkout\b' && _forced_switch checkout; } || { _guarded 'switch\b' && _forced_switch switch; }; then
    _block "git checkout/switch --force" "A forced switch throws away uncommitted modifications instead of refusing; switch without --force, or stop and report the local changes."
fi
