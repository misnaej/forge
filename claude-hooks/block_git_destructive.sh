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
# - Branch resets that drop commits: `git checkout -B <name> [<start>]` /
#   `git switch -C <name> [<start>]` when <name> already exists and its
#   tip is not contained in <start> — checked against live refs, failing
#   closed when the hook cannot check. A new name or a fast-forward stays
#   allowed.
# - `git stash` in every form except `list` / `show`.
# - Any of those verbs (`reset`, `clean`, `checkout`, `restore`, `stash`,
#   `switch`) after a variable in the command-word slot (`$G reset`,
#   `sudo $G clean -fd`): the hook cannot tell what the variable runs, so
#   every form blocks, read-only ones too (`$G stash list`) — a false
#   block that costs only spelling `git` literally.
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
# Fail CLOSED on a lib that predates a helper this hook calls: an
# undefined function returns 127 inside an `if`, which reads as "not
# found" and would allow the command.
if ! declare -F guard_begin command_positions guard_git_invocations guard_help_only > /dev/null \
    || [ -z "${GIT_UNKNOWN_ANCHOR:-}" ] || [ -z "${GUARD_NEUTRAL:-}" ]; then
    echo "BLOCKED: the guard anchor lib at $ANCHOR_LIB lacks a helper this hook needs (older or damaged plugin copy) — refusing the command rather than running unguarded." >&2
    exit 2
fi
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

# A variable standing where git would (`$G reset --hard`, `sudo $G clean
# -fd`, GIT_UNKNOWN_ANCHOR in git_anchor.sh) followed by a verb this hook
# guards: what the word runs, and so whether the invocation destroys
# anything, is unknowable here, so it blocks. Every form of those verbs
# blocks this way — `$G stash list` and `$G checkout main` included — an
# accepted false block: spelling git as `git` costs nothing. So is a
# wrapper option whose value is a variable (`sudo -u $USER reset` reads
# `$USER` as the command word). The verb must end its word, so `$EDITOR checkout.txt` is not one.
GIT_UNKNOWN_VERBS='(reset|clean|checkout|restore|stash|switch)([[:space:];&|)]|$)'
if echo "$CMDPOS" | grep -qE "${GIT_UNKNOWN_ANCHOR}${GIT_UNKNOWN_VERBS}" \
    && ! guard_help_only "$CMDPOS" "${GIT_UNKNOWN_ANCHOR}${GIT_UNKNOWN_VERBS}"; then
    _block "<variable> reset|clean|checkout|restore|stash|switch" "A variable stands where the program name would be, so the hook cannot tell whether it runs git, or what that invocation would destroy. Spell the program name literally (\`git …\`) so the command can be judged."
fi

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
# An index-only `git restore --staged` (no `--worktree`) overwrites no
# working copy and is allowed wherever it runs, a leading `cd` included.
# When the hook cannot tell what a path names or where the command runs — a glob, `$`, `~`, pathspec magic, a
# quoted or escaped blank or separator in a path (GUARD_NEUTRAL, below),
# an earlier `cd`, `env -C`/`sudo -D`,
# `--git-dir`/`--work-tree`/`--namespace` or a `GIT_*` relocation —
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
#
# A word holding GUARD_NEUTRAL (git_anchor.sh) is unknowable wherever its
# value is handed to git — any argument of a restore, a branch-reset name
# or start point, a `-C` directory: the --words view puts that byte where
# a quoted or escaped blank or separator stood, so the word is one word as
# git receives it, but not the text git receives (`'a;b'` reads `a␟b`,
# which names no file). has_neutral() is that one test.
#
# _INVOCATION_AWK — the part every invocation parser here shares, prepended
# to each program: option abbreviations, the directory an invocation runs
# in, and the two-view reader. Each program reads two files: the
# command-positions view first, then the --words view; they must yield
# the same invocations, each with the same word count. Both views keep a
# quoted word as one word, so a difference means the views were not built
# from the same reading of the command, and the hook fails closed.
# The directory comes from ENVIRON["CWD"] (the payload cwd) and each
# `-C`; CHDIR (any `cd` / `pushd` / `popd` in the command) makes it
# unknowable. A user's `-c` options never reach a probe: probes run plain
# `git -C <dir>`, and a `-c` naming a worktree setting is unknowable.
_INVOCATION_AWK='
function abbrev(t, long, min) { return length(t) >= min && substr(long, 1, length(t)) == t }
# The word holds GUARD_NEUTRAL (NEUT, passed with -v): its value is unknowable.
function has_neutral(t) { return index(t, NEUT) > 0 }
# The directory one invocation runs in, or "" when it cannot be known.
# Words before the `git` word belong to wrappers: an `env` or `sudo` that
# changes directory or root there (`env -C <d>` / `--chdir`, `sudo -D <d>`
# / `--chdir` / `-R` / `--chroot`, in any cluster or abbreviation) makes
# the directory unknowable, and their other flags are not git options —
# `sudo -C3` closes descriptors, it names no directory. A repository-
# relocating variable or `--namespace` anywhere before the verb is
# unknowable too.
function inv_dir(pre,   n, P, i, t, o, dir, next1, nextc, g, w) {
    if (CHDIR) return ""
    dir = ENVIRON["CWD"]; next1 = 0; nextc = 0
    n = split(pre, P, /[ \t]+/)
    g = 0; w = ""
    for (i = 1; i <= n; i++) {
        t = P[i]
        if (t == "") continue
        if (t ~ /^(--git-dir|--work-tree|--config-env|--namespace|GIT_DIR=|GIT_WORK_TREE=|GIT_INDEX_FILE=|GIT_CONFIG_|GIT_NAMESPACE=|GIT_COMMON_DIR=|GIT_OBJECT_DIRECTORY=|GIT_ALTERNATE_OBJECT_DIRECTORIES=|GIT_REPLACE_REF_BASE=)/) return ""
        if (g) continue
        if (t ~ /(^|\/)git$/ && t !~ /^[A-Za-z_][A-Za-z0-9_]*=/) { g = i; continue }
        if (t ~ /(^|\/)(env|sudo)$/ && t !~ /^[A-Za-z_][A-Za-z0-9_]*=/) { w = t; sub(/.*\//, "", w); continue }
        if (t !~ /^-/) continue
        o = t; sub(/=.*/, "", o)
        if (w == "env" && (t ~ /^-[^-]*C/ || (o ~ /^--/ && abbrev(o, "--chdir", 3)))) return ""
        if (w == "sudo" && (t ~ /^-[^-]*[DR]/ || (o ~ /^--/ && (abbrev(o, "--chdir", 3) || abbrev(o, "--chroot", 3))))) return ""
    }
    # No `git` word: the words are not the shape the anchor matched.
    if (!g) return ""
    for (i = g + 1; i <= n; i++) {
        t = P[i]
        if (t == "") continue
        if (next1) {
            if (t ~ /[$]|@SUBST@/ || t ~ /^~/ || has_neutral(t)) return ""
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
            if (t ~ /[$]|@SUBST@/ || t ~ /^~/ || has_neutral(t)) return ""
            dir = (t ~ /^\//) ? t : dir "/" t
            continue
        }
    }
    return dir
}
# One --words invocation line, paired by line number with its
# command-positions line: sets PRE (the text before the verb), NW and
# W[1..NW] (the arguments, empty entries skipped by callers). Returns 0
# when the two views count different words.
function inv_read(line, fnr,   k, p, np, Q, cw, cq, i) {
    k = index(line, "\t")
    PRE = substr(line, 1, k - 1)
    p = POSN[fnr]; p = substr(p, index(p, "\t") + 1)
    NW = split(substr(line, k + 1), W, /[ \t]+/); np = split(p, Q, /[ \t]+/)
    cw = 0; for (i = 1; i <= NW; i++) if (W[i] != "") cw++
    cq = 0; for (i = 1; i <= np; i++) if (Q[i] != "") cq++
    return cw == cq
}
FILENAME == ARGV[1] { if ($0 != "") NPOS++; POSN[FNR] = $0; next }
'

# Pass 1 of the restore rules described above.
RESTORE_PARSE_AWK="${_INVOCATION_AWK}"'
function emit_dir(d) { if (!(d in SEEN)) { SEEN[d] = 1; print "D\t" d } }
function unknown() { print "UNKNOWN"; bad = 1; exit }
{
    line = $0
    if (line == "") next
    NWORDS++
    if (!inv_read(line, FNR)) unknown()
    dir = inv_dir(PRE)
    dd = 0; conflict = 0; branchop = 0; staged = 0; worktree = 0; src = 0
    nb = 0; na = 0
    split("", B); split("", A)
    for (i = 1; i <= NW; i++) {
        t = W[i]
        if (t == "") continue
        if (has_neutral(t)) unknown()
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
    # `git restore --staged` without `--worktree` writes only the index,
    # never a working copy, so its directory never matters either: the
    # unstage this hook recommends must pass behind a `cd` too. This
    # relies on the loop above consuming every value-taking option
    # (`--source`, `-s`, `--pa…`): a new git option with a value must be
    # handled there, or its value could be misread as `--staged`.
    if (VERB == "restore" && staged && !worktree) next
    if (dir == "") unknown()
    if (VERB == "restore") {
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
function meta(p) { return p ~ /@SUBST@|[$*?[{\\]/ || p ~ /^[~:\/]/ || index(p, NEUT) > 0 }
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
# invocations with the same number of words (_INVOCATION_AWK), and a path
# holding GUARD_NEUTRAL is unknowable — fail closed.
_restore_blocked() {
    local verb="$1" records verdict
    records=$(CWD="$PAYLOAD_CWD" LC_ALL=C awk -v VERB="$verb" -v CHDIR="$CMDPOS_CHDIR" -v NEUT="$GUARD_NEUTRAL" \
        "$RESTORE_PARSE_AWK" \
        <(guard_git_invocations "$CMDPOS" "$verb") \
        <(guard_git_invocations "$WORDS" "$verb")) || return 0
    case "$records" in *UNKNOWN*) return 0 ;; esac
    [ -n "$records" ] || return 1
    verdict=$({ _restore_state "$records"; printf '%s\n' "$records" | grep -E $'^(J|C)\t'; } \
        | LC_ALL=C awk -v NEUT="$GUARD_NEUTRAL" "$RESTORE_JUDGE_AWK") || return 0
    [ "$verdict" = BLOCK ]
}

# Behind a `cd` the hook cannot see where the command runs, so even a
# conflicted path's `--ours`/`--theirs` is refused; say how to make it
# checkable instead of leaving the reader to retry the same form.
RESTORE_CD_HINT=""
if [ "$CMDPOS_CHDIR" = 1 ]; then
    RESTORE_CD_HINT=" A leading \`cd\` hides where it runs: drop it and use \`git -C <dir>\`."
fi
if _guarded 'checkout\b' && _restore_blocked checkout; then
    _block "git checkout <paths>" "A path it would overwrite holds uncommitted work (a modified or staged file, or an untracked file in the way) — possibly another session's — or the hook cannot tell which paths it names.${RESTORE_CD_HINT} Report the state instead; experiments belong in a \`forge-scratch-repo\` copy."
fi
if _guarded 'restore\b' && _restore_blocked restore; then
    _block "git restore <paths>" "A path it would overwrite holds uncommitted work (a modified or staged file, or an untracked file in the way) — possibly another session's — or the hook cannot tell which paths it names.${RESTORE_CD_HINT} To unstage only, use \`git restore --staged <path>\`; otherwise report the state."
fi

# Branch resets: `git checkout -B <name> [<start>]` and `git switch -C |
# --force-create <name> [<start>]` move an EXISTING branch <name> to
# <start> (default HEAD). When <name>'s tip is not contained in <start>,
# its commits drop off the branch — a rewind like `git reset`. So they
# are blocked exactly then; a new name, or a move that only fast-forwards
# <name>, loses nothing and stays allowed. When the hook cannot check —
# the directory is unknowable (as for the restores above), a name or
# start point is a variable, substitution or glob, the start point does
# not resolve to one commit (missing, or an ambiguous name), or a probe
# fails — it blocks: it allows only what it can show loses nothing.
#
# BRANCH_RESET_PARSE_AWK prints one `K<TAB>dir<TAB>name<TAB>start` record
# per distinct reset (at most BR_MAX; more is unknowable, so no count of
# invocations runs the probes past the hook timeout), or UNKNOWN with its
# cause. Long options are read as git reads them, any prefix included, so
# an abbreviated value-taking option (`--confl merge`) never leaves its
# value to be read as the start point; a long option outside git's lists
# for the verb makes a reset unknowable. A short
# option cluster holding B (checkout) / C (switch) is read as that option,
# its value attached or the next word; read wrongly (`-tB`, where `-t`
# takes B as its value) it can only over-block. The first word before
# `--` is the start point; with none, both HEAD and the first word after
# `--` are probed, whichever git reads it as.
BR_MAX=8
BRANCH_RESET_PARSE_AWK="${_INVOCATION_AWK}"'
# UNKNOWN carries the cause, for the refusal: dir (where it runs), opaque
# (a name or start point), option (an option it cannot classify), views
# (the two views disagree), many (more than MAX resets).
function unknown(why) { print "UNKNOWN\t" why; bad = 1; exit }
# A word whose value as git receives it is unknowable here: a variable or
# command substitution, a glob or backslash bash may still expand, a
# leading `~`, a brace expansion, or a quoted or escaped blank or
# separator (has_neutral).
function opaque(t) { return t ~ /[$*?[\\]|@SUBST@/ || t ~ /^~/ || t ~ /[{][^}]*(,|[.][.])[^}]*[}]/ || has_neutral(t) }
# What a long option (without its `=value`) is to git, which accepts any
# prefix of a long option: "reset" (`--force-create`, its value the
# name), "val" (takes the next word unless `=` is given), "none", or "?"
# for one this hook does not know, which it cannot check past. An exact
# no-value name wins over a longer option it prefixes (`--force` beside
# `--force-create`), as in git; a prefix git calls ambiguous is read as
# the value-taking or resetting option — git refuses the command anyway,
# so that reading can only over-block.
function long_kind(o,   n, L, i) {
    if (o ~ /^--no-./) return "none"
    n = split(NOVAL, L, " ")
    for (i = 1; i <= n; i++) if (o == L[i]) return "none"
    if (VERB == "switch" && abbrev(o, "--force-create", 3)) return "reset"
    n = split(VALOPT, L, " ")
    for (i = 1; i <= n; i++) if (abbrev(o, L[i], 3)) return "val"
    n = split(NOVAL, L, " ")
    for (i = 1; i <= n; i++) if (abbrev(o, L[i], 3)) return "none"
    return "?"
}
function record(dir, name, start) {
    if (opaque(start)) unknown("opaque")
    if ((dir, name, start) in SEEN) return
    SEEN[dir, name, start] = 1
    if (++NREC > MAX) unknown("many")
    print "K\t" dir "\t" name "\t" start
}
BEGIN {
    # The long options of `git checkout -h` / `git switch -h`: those that
    # take a separate value, and those that take none (or only `=value`).
    if (VERB == "switch") {
        VALOPT = "--create --orphan --conflict"
        NOVAL = "--guess --discard-changes --quiet --recurse-submodules --progress --merge --detach --track --force --overwrite-ignore --ignore-other-worktrees"
    } else {
        VALOPT = "--orphan --conflict --pathspec-from-file"
        NOVAL = "--guess --overlay --quiet --recurse-submodules --progress --merge --detach --track --force --overwrite-ignore --ignore-other-worktrees --ours --theirs --patch --ignore-skip-worktree-bits --pathspec-file-nul"
    }
}
{
    if ($0 == "") next
    NWORDS++
    if (!inv_read($0, FNR)) unknown("views")
    # FLAG resets a branch; VAL takes a value and creates one (git refuses
    # it beside FLAG, so its value only has to be skipped).
    FLAG = (VERB == "switch") ? "C" : "B"; VAL = (VERB == "switch") ? "c" : "b"
    name = ""; have = 0; want = 0; skip = 0; dd = 0; np = 0; after = ""; unk = 0
    split("", P)
    for (i = 1; i <= NW; i++) {
        t = W[i]
        if (t == "") continue
        if (want) { name = t; have = 1; want = 0; continue }
        if (skip) { skip = 0; continue }
        if (dd) { if (after == "") after = t; continue }
        if (t == "--") { dd = 1; continue }
        if (t ~ /^--/) {
            k = index(t, "=")
            o = k ? substr(t, 1, k - 1) : t
            kind = long_kind(o)
            if (kind == "reset") {
                if (k) { name = substr(t, k + 1); have = 1 } else want = 1
            } else if (kind == "val") {
                if (!k) skip = 1
            } else if (kind == "?") unk = 1
            continue
        }
        if (t ~ /^-./) {
            for (j = 2; j <= length(t); j++) {
                c = substr(t, j, 1)
                if (c == FLAG) {
                    if (j < length(t)) { name = substr(t, j + 1); have = 1 } else want = 1
                    break
                }
                if (c == VAL) { if (j == length(t)) skip = 1; break }
            }
            continue
        }
        P[++np] = t
    }
    if (!have) next
    # An unknown option may take the word read here as the start point.
    if (unk) unknown("option")
    if (opaque(name)) unknown("opaque")
    dir = inv_dir(PRE)
    if (dir == "") unknown("dir")
    if (np) record(dir, name, P[1])
    else {
        record(dir, name, "HEAD")
        if (after != "") record(dir, name, after)
    }
}
END { if (!bad && NWORDS != NPOS) print "UNKNOWN\tviews" }
'

# _branch_reset_blocked <verb> <flag> — true when a <verb> branch reset
# would drop commits or cannot be checked. Sets BR_LABEL (the command) and
# BR_WHY (the reason) for the refusal.
_branch_reset_blocked() {
    local verb="$1" flag="$2" new records kind dir name start tip base rc
    new=$([ "$verb" = switch ] && echo -c || echo -b)
    BR_LABEL="git $verb $flag"
    records=$(CWD="$PAYLOAD_CWD" LC_ALL=C awk -v VERB="$verb" -v CHDIR="$CMDPOS_CHDIR" -v MAX="$BR_MAX" -v NEUT="$GUARD_NEUTRAL" \
        "$BRANCH_RESET_PARSE_AWK" \
        <(guard_git_invocations "$CMDPOS" "$verb") \
        <(guard_git_invocations "$WORDS" "$verb")) || records=UNKNOWN
    local cannot="The hook blocks a branch reset unless it can show the reset drops no commits, and here it cannot check:"
    local instead="Name the directory and start point literally (\`git -C <path> $verb $flag <name> <start>\`, no \`cd\` or variables), or start a branch under a new name with \`git $verb $new <new-name> <start>\`."
    case "$records" in
        *UNKNOWN*)
            # The parse names the cause on its UNKNOWN line; the refusal
            # names that one, not every cause there is.
            case $'\n'"$records" in
                *$'\n'UNKNOWN$'\t'dir*) BR_WHY="$cannot the directory it runs in cannot be known (an earlier \`cd\`/\`pushd\`, \`env -C\` or \`sudo -D\`, \`--git-dir\`/\`--work-tree\`/\`--namespace\`, a \`GIT_*\` relocation variable, or a \`-C\` value holding a variable)." ;;
                *$'\n'UNKNOWN$'\t'opaque*) BR_WHY="$cannot the branch name or start point is not literal (a variable or substitution, a glob, a backslash, a leading \`~\`, a brace expansion, or a quoted or escaped blank or separator)." ;;
                *$'\n'UNKNOWN$'\t'option*) BR_WHY="$cannot it holds a long option the hook does not know for \`git $verb\`, which may take the word read as the start point." ;;
                *$'\n'UNKNOWN$'\t'views*) BR_WHY="$cannot its words could not be read consistently, so they may not be the ones git receives." ;;
                *$'\n'UNKNOWN$'\t'many*) BR_WHY="$cannot the command holds more than $BR_MAX branch resets." ;;
                *) BR_WHY="$cannot the command could not be read." ;;
            esac
            BR_WHY="$BR_WHY $instead"
            return 0
            ;;
    esac
    while IFS=$'\t' read -r kind dir name start; do
        [ "$kind" = K ] || continue
        BR_LABEL="git $verb $flag $name $start"
        rc=0
        tip=$(git -C "$dir" rev-parse --verify --quiet "refs/heads/$name^{commit}" 2> /dev/null) || rc=$?
        # Status 1 with no output: no such branch, so a new one is created
        # and nothing is lost. Any other failure is a probe error.
        if [ "$rc" = 1 ] && [ -z "$tip" ]; then continue; fi
        if [ "$rc" != 0 ]; then
            BR_WHY="$cannot git could not look up the branch '$name'. $instead"
            return 0
        fi
        rc=0
        base=$(git -C "$dir" rev-parse --verify "$start^{commit}" 2>&1) || rc=$?
        # Exactly one object name and nothing else: an ambiguity warning on
        # stderr fails this too.
        if [ "$rc" != 0 ] || ! [[ $base =~ $BR_OID_RE ]]; then
            BR_WHY="$cannot the start point '$start' does not resolve to exactly one commit (it is missing, or the name is ambiguous). Fix the start point: name it as a full ref (\`refs/heads/<branch>\`, \`refs/tags/<tag>\`) or a commit SHA, and run the command again."
            return 0
        fi
        rc=0
        git -C "$dir" merge-base --is-ancestor "$tip" "$base" > /dev/null 2>&1 || rc=$?
        [ "$rc" = 0 ] && continue
        if [ "$rc" = 1 ]; then
            BR_WHY="Branch '$name' already exists and has commits that '$start' does not contain; resetting it there drops them off the branch, a rewind like \`git reset\`. To start a branch at '$start', give it a new name (\`git $verb $new <new-name> $start\`); to keep the old branch but free its name, rename it first with \`git branch -m $name <backup-name>\`."
        else
            BR_WHY="$cannot git could not compare '$name' with '$start'. $instead"
        fi
        return 0
    done <<< "$records"
    return 1
}
# A cheap first look on the --words view, so a plain checkout or switch
# skips the pass: any word that could be the option the parser reads as
# a reset (a superset of it — `git -C <dir>` matches too).
BR_OID_RE='^[0-9a-f]{40}([0-9a-f]{24})?$'
BR_CHECKOUT_RE='(^|[[:space:]])-[^-[:space:]]*B'
BR_SWITCH_RE='(^|[[:space:]])(-[^-[:space:]]*C|--force-)'
if _guarded 'checkout\b' && [[ $WORDS =~ $BR_CHECKOUT_RE ]] && _branch_reset_blocked checkout -B; then
    _block "$BR_LABEL" "$BR_WHY"
fi
if _guarded 'switch\b' && [[ $WORDS =~ $BR_SWITCH_RE ]] && _branch_reset_blocked switch -C; then
    _block "$BR_LABEL" "$BR_WHY"
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
