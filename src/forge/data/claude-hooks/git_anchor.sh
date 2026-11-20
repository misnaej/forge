#!/usr/bin/env bash
# Shared git/gh command anchors and the guard watchdog for the guard hooks.
# The command-positions scanner (command_positions / guard_unredir) and the
# hook-payload extraction (parse_command_segment) live in
# lib/command_utils.sh, sourced below.
#
# NOT a hook — a sourced library (never registered in plugin.json).
# Consumers: every Claude Code guard that locates a command in the Bash
# command text — the git/gh family (block_force_push.sh,
# block_git_rebase.sh, block_raw_git.sh, block_git_destructive.sh,
# block_amend_pushed_commit.sh, block_protected_branches.sh,
# block_no_verify.sh, block_claude_attribution.sh, block_pr_merge.sh,
# block_unverified_pr_create.sh, warn_merge_conflicts.sh,
# warn_stale_wrapup.sh) and the other locating guards
# (block_install_deps.sh, block_raw_ruff.sh, block_branch_deletion.sh,
# block_continuation_delete.sh, block_raw_wrapup_post.sh,
# block_fixer_recon.sh, check_commit_format.sh, warn_pr_checks.sh,
# keep_squash_comment_last.sh), via:
#
#     source "$(dirname "$0")/git_anchor.sh"
#
# GIT_ANCHOR matches a real `git <subcommand>` invocation at line-start or
# after a shell separator (`;` `&` `|` `(` `)`). `)` is a separator because
# a `case` arm starts its command there — `case x in x) git push -f …` —
# and without it every locating guard missed what a case pattern
# introduced. The other `)` shapes are read the same way and that is a
# deliberate over-read, not a claim about the shell: `$(id) git push -f`
# runs git only when the expansion is empty, and `(cd x) git …` is a syntax
# error, but a word the hook cannot prove harmless is judged as a command.
# The cost is a `!` on shapes like `cp $(x) ruff`, where a guarded name is
# only an argument. It tolerates:
# - a leading run of `VAR=val` assignments, `command`/`env`/`exec`/
#   `builtin`/`sudo`/`doas`/`nohup`/`nice`/`time`/`timeout`/`setsid`/
#   `ionice`/`stdbuf`/`taskset`/`chrt`/`xargs` wrappers — with their
#   options, each optionally followed by one value word (`sudo -n`,
#   `sudo -u root`, `env -C /tmp`), and the one positional argument the
#   positional ones take (`timeout 5`, `timeout -k 3 5`) — the shell words
#   that start a command inside a compound (`then`, `do`, `else`, `elif`,
#   `{`, `!`), and redirection words (GUARD_REDIR, below), so
#   `GIT_DIR=x git ...`, `if …; then git …; fi` and `2>&1 git …` can't
#   slip the gate (rationale at _GUARD_PREFIX, below);
# - a directory path in front of the tool or wrapper name (GUARD_BINPATH,
#   below), so `/usr/bin/git …`, `./git …` and `/usr/bin/env git …` are
#   the same invocation as `git …`. A different one-word name that merely
#   contains the tool's name (`forge-git-x`, `my-gh`) is not;
# - a bounded run of git GLOBAL options between `git` and the subcommand
#   (`--no-pager`, `-c k=v`, `-C <dir>`, attached `-C<dir>`,
#   `--git-dir=<x>` and `--git-dir <x>`, likewise `--work-tree`,
#   `--namespace`, `--exec-path`, `--config-env`) and redirection words,
#   so `git --no-pager <verb>` and `git 2>&1 <verb>` can't slip it either.
#
# GUARD_BINPATH is that optional directory part: any run of non-separator,
# non-redirection characters ending in `/`. `<` and `>` are excluded so
# `>/tmp/git push -f` stays a redirection target, not a command. Accepted
# side effects: `FOO=a/git push` reads as a path-qualified git (`=` is a
# legal path character), and a local script named git or gh is treated as
# the real tool. The `/` is written `[/]` because block_protected_branches
# splices GIT_ANCHOR into a `s/…//` sed expression; it holds no backslash
# and never spells the letters `git`, for the same reasons GUARD_REDIR
# doesn't (below).
#
# The anchors are never run against the raw command text alone — a raw
# text cannot tell a command from a quoted mention of one. Guards run
# them against the views `command_positions` builds (below), which know
# where the shell would actually start a command.
#
# Known accepted residuals (documented, deliberately out of scope):
# `${IFS}` splicing, a substitution with a blank inside it as the command
# word (`$(which git) push`; an unquoted `$G` there is GIT_UNKNOWN_ANCHOR,
# below, which the destructive and force-push guards block; the other
# guards do not — its quoted form is listed further down), wrappers
# outside the prefix list (`flock`, `su -c`, `runuser`, `parallel`,
# `watch`, `unshare`, `nsenter`, `systemd-run`, `script -c`), an `env -S`
# payload attached to its option (`env -S'…'`, `--split-string='…'`),
# wrappers nested more than one level deep with escaped inner quotes,
# a non-heredoc script piped into a shell (`echo "…" | bash`; a heredoc
# whose line pipes into a shell IS covered), an interpreter running git
# from a script FILE (`python x.py`) or from a verb assembled at runtime
# (inline interpreter code that names git — `python -c "…git…"`, a heredoc
# script — is refused by block_raw_git.sh as a tripwire, not a
# boundary), git aliases (inline `git -c alias.x='!…' x` and configured
# ones), other git verbs that overwrite files or move refs
# (`read-tree -u`, `checkout-index -f`, `branch -f`),
# positional parameters whose values exist only at run time
# (`find … -exec sh -c '"$@"' _ {} +`), a command an ssh option runs on
# the local machine (`ssh -o ProxyCommand=…`, `LocalCommand`), an ssh
# command whose start unknowable words could move by more than two (a
# word holding an expansion where the host is read moves it up to two,
# an option letter outside OpenSSH's getopt string one), a positional
# reference past JOINCAP in a padded payload (substitution stops there,
# below), assembled text nested past the re-scan depth,
# a redirection between a valued git option and its value
# (`git -C 2>&1 <dir> push`: the anchor takes the redirection for the
# value, and `<dir>` then ends the option run), a command word hidden by escapes inside `$'…'`
# (`$'\x67it' push`), a quoted command word holding a variable or a blank
# (`"$G" push`, `'git push'` as one word), and two
# accepted false blocks: `x &>log git push`, where
# git is only an argument, reads as `&` followed by a redirected
# `git push`, and an `ssh` word that is only an argument
# (`echo ssh h git push -f`) has its following words read as the remote
# command — each costs a `!` — the guards are tripwires for the routes
# agents were seen to take, not a sandbox.
#
# The command-positions scanner and the payload extraction live in the
# shared lib; this file adds the git/gh anchors and the guard_begin
# watchdog on top. Fail CLOSED when the lib is missing — only exit 2 is a
# block signal in the PreToolUse contract.
CMD_LIB="$(dirname "$0")/lib/command_utils.sh"
if [ ! -r "$CMD_LIB" ]; then
    echo "BLOCKED: command-utils lib missing at $CMD_LIB — refusing the command rather than running unguarded." >&2
    exit 2
fi
source "$CMD_LIB"

# GUARD_REDIR is one or more redirection words: an optional fd number or
# `{name}`, the operator (`<<<` `<<` `<<-` `<>` `>>` `>|` `>&` `<&` `&>>`
# `&>` `<` `>`), optional blanks, then a target of non-separator
# characters or one level of `(…)`. The shell accepts one wherever a word
# of the command may stand — before the command name (`2>&1 git push`)
# and between a tool and its subcommand (`git 2>&1 push`) — so every
# anchor below reads past it in those places. Among a command's
# ARGUMENTS a guard instead reads a copy with redirections blanked
# (`command_positions --no-redir`, guard_unredir). It holds no `/` (a hook
# splices GIT_ANCHOR into a `s/…//` sed expression) and no backslash
# (guard_git_invocations hands the anchor to awk -v, which would eat it);
# a test pins both. It also never spells the letters `git` — hence `[0-9]`,
# not `[[:digit:]]` — because block_raw_ruff cuts its prefix out of
# GIT_ANCHOR at the first `git[[:space:]]+` and refuses every command when
# the cut prefix still contains `git`.
GUARD_REDIR='(([0-9]+|[{][[:alnum:]_]+[}])?(<<<|<<-?|<>|>>|>[|]|>&|<&|&>>|&>|<|>)[[:space:]]*([^[:space:];&|()<>]|[(][^()]*[)])+)+'
# GUARD_BINPATH: an optional directory path before a tool or wrapper name
# (rationale in the header above).
GUARD_BINPATH='([^[:space:];&|()<>]*[/])?'
# The command-prefix run (VAR=val, wrapper and compound words — each
# wrapper optionally path-qualified — wrapper options, redirections)
# and git's global-option run, shared by the
# anchors so a word learned once is learned everywhere.
#
# A wrapper option may take one following value word (`sudo -u root`,
# `env -C /tmp`, `nice -n 5`) — gh's own parsing rule (GH_OPTS, below),
# not a list of which options take one. The match is existential, so the
# option is also read without a value wherever that is what lets the
# command word be found: reading too much can only widen what a guard
# blocks. The accepted cost is a false block when a value-less wrapper
# option is followed by a program that itself runs the tool
# (`sudo -E somecmd git reset --hard` reads as `sudo -E somecmd` then git).
# The wrappers that take a positional argument before the command
# (`timeout 5`, `taskset 0x1`, `chrt 10`, `ionice 3`, `stdbuf …`) may also
# skip one plain word after their options (`timeout -k 3 5 git …`).
_GUARD_PREFIX='(([[:alnum:]_]+=[^[:space:]]+|'"${GUARD_BINPATH}"'('"${GUARD_WRAPPERS}"')|'"${GUARD_BINPATH}"'(timeout|ionice|stdbuf|taskset|chrt)([[:space:]]+-[^[:space:]]+([[:space:]]+[^[:space:]]+)?)*[[:space:]]+[^[:space:]]+|then|do|else|elif|[{!]|-[^[:space:]]+([[:space:]]+[^[:space:]]+)?|'"${GUARD_REDIR}"')[[:space:]]+)*'
_GUARD_GIT_OPTS='((-c|-C|--git-dir|--work-tree|--namespace|--exec-path|--config-env)[[:space:]]+[^[:space:]]+[[:space:]]+|--?[a-zA-Z][^[:space:]]*[[:space:]]+|'"${GUARD_REDIR}"'[[:space:]]+)*'
GIT_ANCHOR='(^|[;&|()])[[:space:]]*'"${_GUARD_PREFIX}${GUARD_BINPATH}"'git[[:space:]]+'"${_GUARD_GIT_OPTS}"
# GIT_UNKNOWN_ANCHOR is GIT_ANCHOR with the command word replaced by a
# word that starts with `$` and holds no `/` (spelled as the characters
# it may hold, since a bare `/` is barred here), or a whole `${…}`
# expansion, whose `/` belongs to the expansion (`${G:-/usr/bin/git}`,
# `${G//x/y}`) — `$G`, `${G}`, `${G:-git}`,
# `$1`, `$@` — or, in the command-positions view, a quoted span the view
# could not decode to a plain word (`"$G"` reads as `_` there), standing
# where git would (`$G reset --hard`, `sudo "$G" clean -fd`). A word
# with a `/` after its variable (`$HOME/bin/tool reset`) names a path and
# is not matched — but quoted (`"$HOME/bin/tool" reset`) it is the `_` of
# an undecodable span, and blocks like any quoted program word holding a
# `$` or a blank (`"a b" stash`): an accepted false block;
# neither is a command substitution (residual above). What such a word
# runs is unknowable to a hook, so a guard that matches it followed by a
# verb it blocks treats the invocation as unknowable and blocks. It is a
# separate constant, never part of _GUARD_PREFIX: block_raw_ruff cuts its
# prefix out of GIT_ANCHOR, and every other tool's anchor shares that
# prefix. `[$]`, not a backslash, for the same awk -v and sed reasons as
# GUARD_REDIR.
# shellcheck disable=SC2034  # consumed by sourcing hooks
GIT_UNKNOWN_ANCHOR='(^|[;&|()])[[:space:]]*'"${_GUARD_PREFIX}"'([$]([{][^}[:space:]]*[}]|[][[:alnum:]_{}:#%@*!?=+.,~^-]*)|_)[[:space:]]+'"${_GUARD_GIT_OPTS}"

# GH_ANCHOR is the same shape for `gh`: a real invocation at line-start
# (leading whitespace included — the hand-rolled `^gh` patterns this
# replaced were bypassed by a single leading space) or after a shell
# separator, tolerating the same VAR=val / wrapper-token prefix run and
# the same optional directory path (`/usr/bin/gh`).
#
# GH_OPTS is the run of options gh accepts wherever a command word is
# expected: before the subcommand (`gh -R o/r pr view 1`,
# `gh -X DELETE api …` — a valued flag there takes the next word) and
# between a command group and its action (`gh pr -R o/r merge 9`,
# `gh pr --repo=o/r merge 9`). gh does not limit which options may sit
# there, so neither does this: any dash word, each optionally followed
# by one value word — gh's own parsing rule, not a list of flag names.
# GH_ANCHOR ends with it, and the guards that name an action after a
# command group (`pr[[:space:]]+${GH_OPTS}merge`) or match a second stage
# on another view (`gh[[:space:]]+${GH_OPTS}api`) use it in those places.
# shellcheck disable=SC2034  # consumed by sourcing hooks
GH_OPTS='(-[^[:space:]]+[[:space:]]+([^[:space:]]+[[:space:]]+)?|'"${GUARD_REDIR}"'[[:space:]]+)*'
# shellcheck disable=SC2034  # consumed by sourcing hooks
GH_ANCHOR='(^|[;&|()])[[:space:]]*'"${_GUARD_PREFIX}${GUARD_BINPATH}"'gh[[:space:]]+'"${GH_OPTS}"


# guard_begin — every blocking guard calls it right after sourcing this
# lib. A PreToolUse hook that runs past its timeout (5 s, plugin.json) is
# killed WITHOUT blocking — the command runs — so a guard must never be
# slow, and must fail closed when it is:
#   - a command longer than GUARD_MAX_CHARS is refused outright: a guard
#     reads the whole command, so its cost grows with the length, and the
#     command is not echoed back (it would flood the message);
#   - a watchdog stops the guard at GUARD_DEADLINE_S and blocks. Bash runs
#     a trap only once its foreground child exits, so the watchdog first
#     terminates the guard's child processes, then the guard itself. Its
#     output goes to /dev/null: a background job holding the hook's stdout
#     would make Claude Code wait for it after every command.
# The cap is sized for a typical machine: on a slow or loaded one, a
# crafted command near it can take the guard close to the deadline, and
# past it the watchdog blocks — a false block, never an allow.
GUARD_MAX_CHARS=131072
GUARD_DEADLINE_S=4
# _guard_kill_tree <pid> <spare pid> — signal (TERM, or GUARD_KILL_SIGNAL)
# every descendant of <pid>, deepest first, except <spare pid> and its
# descendants. Needs `pgrep`: without it no child is signalled, and the
# guard then stops only once its running child finishes on its own.
_guard_kill_tree() {
    local c
    for c in $(pgrep -P "$1" 2> /dev/null); do
        [ "$c" = "$2" ] && continue
        _guard_kill_tree "$c" "$2"
        kill "-${GUARD_KILL_SIGNAL:-TERM}" "$c" 2> /dev/null || true
    done
}
guard_begin() {
    local hook_pid=$$
    if [ "${#COMMAND}" -gt "$GUARD_MAX_CHARS" ]; then
        echo "BLOCKED: this command is ${#COMMAND} characters, over the $GUARD_MAX_CHARS the guards can judge within their timeout — and a guard that runs out of time lets a command through, so oversized commands are refused. Write large content with the Write tool, split the command, or run it yourself with \`!\`." >&2
        exit 2
    fi
    trap 'echo "BLOCKED: this guard could not finish judging the command within ${GUARD_DEADLINE_S}s, and a guard that runs out of time would let it through, so it is refused. Simplify or split the command, or run it yourself with \`!\`." >&2; exit 2' TERM
    (
        # The watchdog must reach its final kill whatever fails before it:
        # a guard's `set -e` would end it at the first failed `kill`.
        set +e
        # Told to stop (the guard finished in time), it ends its own sleep,
        # so a normal exit costs one signal and leaves nothing running.
        trap 'kill "$sleeper" 2> /dev/null; exit 0' TERM
        sleep "$GUARD_DEADLINE_S" &
        sleeper=$!
        wait "$sleeper"
        trap - TERM
        # The guard FIRST, its children second: killed first, a pipeline's
        # upstream stage would hand the downstream one an early EOF, the
        # guard would judge the truncated input as harmless and reach its
        # own `exit 0` before the TERM landed. Bash holds the trap until
        # the foreground child exits, which the tree kill then forces; a
        # child ignoring TERM gets KILL after a short grace.
        kill -TERM "$hook_pid" 2> /dev/null
        self="$(sh -c 'echo $PPID')"
        _guard_kill_tree "$hook_pid" "$self"
        sleep 0.5
        kill -0 "$hook_pid" 2> /dev/null && GUARD_KILL_SIGNAL=KILL _guard_kill_tree "$hook_pid" "$self"
    ) < /dev/null > /dev/null 2>&1 &
    GUARD_WATCHDOG_PID=$!
    # `|| true`: under a guard's `set -e`, a failing command in an EXIT trap
    # replaces the exit status — a watchdog already gone would turn a
    # block (2) into 1, which allows.
    trap 'kill -TERM "$GUARD_WATCHDOG_PID" 2> /dev/null || true' EXIT
}



# guard_git_invocations <--words view> <verb>
# Prints one line per `git … <verb>` invocation in the view: the text the
# anchor matched before <verb> (wrappers, VAR=val, git global options),
# a TAB, then the arguments after <verb> up to the end of that
# invocation. A command substitution among the arguments — `$(…)`, or the
# `(…)` the view renders for a quoted one or a backtick — is printed as
# the single marker `@SUBST@` (its value is unknowable to a hook), and a
# `)` that closes an enclosing subshell ends the invocation. Scanning
# resumes right after <verb>, so an invocation nested inside a
# substitution is reported on its own line as well. <verb> is a literal
# subcommand; `git checkout-index` is not a `checkout`.
guard_git_invocations() {
    # Read past redirections: blanked here, not by callers, so every view
    # a caller compares (words against positions) is blanked the same way.
    guard_unredir "$1" | LC_ALL=C awk -v ANCHOR="$GIT_ANCHOR" -v VERB="$2" '
    {
        line = $0
        while (match(line, ANCHOR VERB)) {
            pre = substr(line, RSTART, RLENGTH - length(VERB))
            rest = substr(line, RSTART + RLENGTH)
            line = rest
            if (substr(rest, 1, 1) ~ /[[:alnum:]_-]/) continue
            out = ""; depth = 0; n = length(rest)
            for (i = 1; i <= n; i++) {
                c = substr(rest, i, 1)
                if (c == "$" && substr(rest, i + 1, 1) == "(") continue
                if (c == "(") { if (depth == 0) out = out "@SUBST@"; depth++; continue }
                if (c == ")") { if (depth == 0) break; depth--; continue }
                if (depth > 0) continue
                if (c == ";" || c == "&" || c == "|") break
                out = out c
            }
            sub(/^[ \t(;&|]+/, "", pre)
            printf "%s\t%s\n", pre, out
        }
    }'
}

# guard_help_only <command-positions view> <verb regex>
# True when <verb regex> matches at least one invocation in the view and
# EVERY such invocation asks for help: a `--help` / `-h` token in its own
# segment (up to the next `;` `&` `|` `)` or newline — a redirection is
# blanked first, so the `&` of `2>&1` is not a segment end), outside quotes
# (quoted spans are already blanked in the view; the scanner deliberately
# keeps a quoted `-h` / `--help` blank even though it decodes other quoted
# plain words, so `git commit "-h"` is not a help request here — the
# exemption never widens), before any standalone
# `--` (after it, `-h` is a path or refspec), and not directly after
# another dash-option (`-m -h` hands `-h` to `-m` as its value, and the
# command runs). `… ; echo --help` therefore exempts nothing. Help is
# the only central exemption — dry-run forms stay per-hook.
#
# Every invocation is judged in one awk pass. A process per invocation let
# a long chain of harmless invocations run a guard past its hook timeout —
# and a timed-out hook allows the command.
guard_help_only() {
    local out
    out=$(guard_unredir "$1" | grep -oE -- "${2}[^;&|)]*" | LC_ALL=C awk '
{
    found = 1
    s = $0
    i = index(s, " -- ")
    if (i) s = substr(s, 1, i - 1)
    n = split(s, T, /[ \t]+/)
    ok = 0; prev = ""
    for (k = 1; k <= n; k++) {
        t = T[k]
        if (t == "") continue
        if ((t == "--help" || t == "-h") && substr(prev, 1, 1) != "-") ok = 1
        prev = t
    }
    if (!ok) { print "no"; exit }
}
END { if (!found) print "none" }') || return 1
    [ -z "$out" ]
}

# invoked <regex>
# True when the hook's command-positions view holds a real invocation
# matching <regex> that is not only a help request (guard_help_only). It
# reads the caller's $CMDPOS at call time, so a hook sets CMDPOS (its
# `command_positions` view, redirections blanked or not as it needs)
# before the first call; an empty CMDPOS matches nothing.
invoked() {
    echo "$CMDPOS" | grep -qE "$1" && ! guard_help_only "$CMDPOS" "$1"
}

# guard_chunks <charset> <text>
# Prints <text> with every character of <charset> turned into a newline,
# so each chunk between two separators is one line. A newline already in
# <text> always ends a chunk too; <charset> does not list it. Which
# characters separate is a safety decision each hook makes for itself —
# splitting at `|` is right for one guard and drops the upstream half of
# `find .plan | xargs rm` for another — so the charset is always the
# caller's own literal, never a shared default.
#
# <charset> may hold only the shell separator characters `;&|(){}!<>`: a
# literal list, never a `tr` range, class or escape, so what a call site
# spells is exactly what splits. Anything else is refused with status 2
# and no output. Callers capture the output first and treat a non-zero
# status as "cannot judge" (block): read through a pipe or `<<<"$(…)"`,
# the status is lost and an empty result would allow.
guard_chunks() {
    local set="$1" nls="" i
    case "$set" in
        "" | *[!';&|(){}!<>']*)
            echo "guard_chunks: invalid separator charset '$set'" >&2
            return 2
            ;;
    esac
    for ((i = 0; i < ${#set}; i++)); do nls+=$'\n'; done
    printf '%s\n' "$2" | LC_ALL=C tr "$set" "$nls"
}


# guard_gh_api_calls <command>
# One row per real `gh ... api` invocation: METHOD<TAB>ENDPOINT<TAB>FIELDS.
# METHOD is the uppercased -X/--method value in every spelling (-X PATCH,
# -XPATCH, -X=PATCH, --method PATCH, --method=PATCH, lowercase, and X hidden
# in a short cluster), whether the flag sits before or after `api`; empty
# for a plain GET. ENDPOINT joins the positionals after `api` (usually one),
# space-joined — never URL-decoded, so %6Dain stays visible and a
# substituted endpoint keeps its text. FIELDS joins the values of
# -f/-F/--field/--raw-field/--input on either side of `api`. Columns, not a
# verdict: consumers match differently (DELETE of a protected branch vs
# any write to git/refs). Segmentation is the same words view the hooks
# use, with separators inside parentheses neutralised so a $(…) cannot end
# a call early. A segmentation failure exits non-zero with no rows, so a
# caller that captures the output and treats non-zero as "cannot judge"
# fails closed.
guard_gh_api_calls() {
    command_positions --words --no-redir "$1" | sed -E 's/(&&|\|\||;|\|)/\n/g' \
        | LC_ALL=C awk -v GHRE="gh[[:space:]]+${GH_OPTS}api" '
function scan(lo, hi,    i, t, name, val, hasval, uname, body, ub, j, c, r, found) {
    for (i = lo; i <= hi; i++) {
        t = T[i]
        if (t == "") continue
        if (pendmeth) { method = t; pendmeth = 0; continue }
        if (pendfield) { fields = fields (fields ? " " : "") t; pendfield = 0; continue }
        if (skip) { skip = 0; continue }
        if (!endopt && t == "--") { endopt = 1; continue }
        if (!endopt && substr(t, 1, 2) == "--") {
            name = t; val = ""; hasval = 0
            if (index(t, "=")) { name = substr(t, 1, index(t, "=") - 1); val = substr(t, index(t, "=") + 1); hasval = 1 }
            uname = toupper(name)
            if (uname == "--METHOD") { if (hasval) method = val; else pendmeth = 1; continue }
            if (uname == "--FIELD" || uname == "--RAW-FIELD" || uname == "--INPUT") {
                if (hasval) fields = fields (fields ? " " : "") val; else pendfield = 1
                continue
            }
            if (uname == "--HEADER" || uname == "--JQ" || uname == "--TEMPLATE") { if (!hasval) skip = 1; continue }
            continue
        }
        if (!endopt && substr(t, 1, 1) == "-") {
            body = substr(t, 2)
            if (body == "") continue
            ub = toupper(body)
            found = 0
            for (j = 1; j <= length(ub); j++) {
                c = substr(ub, j, 1)
                if (c == "X") {
                    r = substr(body, j + 1); sub(/^=/, "", r)
                    if (r != "") method = r; else pendmeth = 1
                    found = 1; break
                }
                if (c == "F") {
                    r = substr(body, j + 1); sub(/^=/, "", r)
                    if (r != "") fields = fields (fields ? " " : "") r; else pendfield = 1
                    found = 1; break
                }
            }
            if (found) continue
            if (substr(ub, length(ub), 1) ~ /[XHfqto]/) skip = 1
            continue
        }
        if (inapi) endpoint = endpoint (endpoint ? " " : "") t
    }
}
{
    line = $0; out = ""; depth = 0
    for (i = 1; i <= length(line); i++) {
        c = substr(line, i, 1)
        if (c == "(") depth++
        else if (c == ")" && depth > 0) depth--
        if (depth > 0 && (c == ";" || c == "&" || c == "|")) c = "_"
        out = out c
    }
    if (!match(out, GHRE)) next
    full = substr(out, RSTART)
    n = split(full, T, /[[:space:]]+/)
    apiidx = 0
    for (i = 1; i <= n; i++) if (tolower(T[i]) == "api") { apiidx = i; break }
    if (!apiidx) next
    method = ""; endpoint = ""; fields = ""; endopt = 0; skip = 0
    pendmeth = 0; pendfield = 0; inapi = 0
    scan(2, apiidx - 1)
    inapi = 1
    scan(apiidx + 1, n)
    printf "%s\t%s\t%s\n", toupper(method), endpoint, fields
}'
}
