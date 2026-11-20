#!/usr/bin/env bash
# Shared command-parsing lib for the guard hooks: the command-positions
# scanner (command_positions / guard_unredir and the _GUARD_*_AWK programs
# they run, moved verbatim from git_anchor.sh) plus the hook-payload
# extraction every guard needs (parse_or_block / parse_command_segment).
#
# NOT a hook — a sourced library (never registered in plugin.json).
# Consumers source it fail-closed:
#
#     CMD_LIB="$(dirname "$0")/lib/command_utils.sh"
#     if [ ! -r "$CMD_LIB" ]; then
#         echo "BLOCKED: command-utils lib missing at $CMD_LIB — refusing the command rather than running unguarded." >&2
#         exit 2
#     fi
#     source "$CMD_LIB"
#
# git_anchor.sh sources this lib too and adds the git/gh anchors and the
# guard_begin watchdog on top.

# GUARD_WRAPPERS is the one list of wrapper names: the command-positions
# scanner receives it too, to tell a run `env -S` from a mentioned one.
GUARD_WRAPPERS='command|env|exec|builtin|sudo|nohup|nice|time|timeout|setsid|ionice|stdbuf|taskset|chrt|doas|xargs'
# ---- Command positions ----------------------------------------------------
#
# `command_positions "$COMMAND"` prints the "command positions" copy of a
# Bash command: what the shell would actually run, with everything that is
# only DATA blanked. A guard finds commands here, so a commit message, a
# PR body, a grep pattern or a heredoc that merely mentions a guarded
# command never fires, while a command the shell runs is always seen:
#
# - single-quoted, double-quoted and `$'…'` spans become one `_` each,
#   except a word whose quoted parts decode to one plain word
#   (`'git' push`, `g"it" push`, `git 'push' -f`), which is emitted as that
#   word because the shell runs it as one (a quoted `-h` / `--help` stays
#   `_`); comments are dropped; quoted-delimiter heredoc bodies are blanked;
# - `$(…)` and backticks stay visible wherever they execute — unquoted,
#   inside double quotes, inside an unquoted-delimiter heredoc body — and
#   are emitted as `(…)`, so the anchors' `(` separator sees their start;
# - the payload handed to `bash|sh|zsh|dash|ksh -c`, `eval`,
#   `ssh <host> "…"` or `bash <<<"…"` stays visible, re-scanned with its
#   own quoting, between two `;` marks — so each guard applies its
#   existing rule to the wrapped command exactly as if it were typed
#   directly. A payload is a WHOLE shell word — adjacent quoted and
#   unquoted spans concatenate (`bash -c 'a'"; b"`). Once a shell `-c`
#   payload is found, EVERY later word of the same simple command is a
#   payload candidate too, quoted or not, so an option after `-c`
#   (`bash -c -x '…'`, `-o posix`, `--`) cannot shift the real payload out
#   of view; a later quoted argument of `eval` / `ssh` is re-scanned the
#   same way, each one separately. A heredoc fed to a shell (`bash <<EOF`,
#   `ssh host <<EOF`, or a heredoc line that pipes into one:
#   `cat <<EOF | sh`) is kept as commands for the same reason;
# - text the shell assembles from several words is ALSO read joined, as
#   the shell runs it, at the end of the simple command between two `;`
#   marks: the arguments of `eval` (`eval 'git' 'push -f'`), the words
#   after the host of an `ssh` (`ssh h git push -f`, its options read as
#   OpenSSH reads them), and a shell `-c` payload with its positional
#   parameters substituted from the words after it
#   (`bash -c 'git "$@"' _ push -f`). The word-by-word reading above stays
#   too, so joining only adds what a guard sees (details at
#   "Text the shell assembles", in the scanner);
# - redirections (`2>&1`, `>out`, `<in`, `&>log`, `>|f`, `{fd}>x`, with or
#   without a blank before the target) are not words of the command:
#   wrapper detection reads past them, so `bash 2>&1 -c '…'`,
#   `bash -c >out '…'`, `ssh h 2>&1 '…'` and `bash 2>/dev/null <<<'…'` are
#   wrappers like their unredirected forms, and a redirection's target is
#   never taken for a payload. The `&` / `|` inside `>&` `<&` `&>` `&>>`
#   `>|` is part of the operator, not a separator (`|&` and a lone `&`
#   still are). `ssh` is recognised by path too (`/usr/bin/ssh`). The
#   emitted text is unchanged — only detection reads the
#   redirection-stripped copy;
# - an escaped separator or blank (`\;`, `\ `) is a literal, so it is
#   emitted as `_` (a line continuation, backslash-newline, adds nothing,
#   inside a word or before one);
# - when unsure, the scanner keeps text VISIBLE (a false block costs a
#   `!`; a bypass costs the guard): `#` starts a comment only at an
#   unescaped word start outside `${…}` and never inside backticks, and
#   `$((…))` / `((…))` are copied through verbatim, so a `<<` shift there
#   never opens a heredoc that would blank the lines after it.
#
# `command_positions --words "$COMMAND"` prints the same view with quoted
# spans kept as their words instead of blanked (quote characters dropped;
# blanks and separators inside them, and escaped ones, neutralised to
# GUARD_NEUTRAL, below). Guards gate on the first view and read flags,
# refspecs, branch names and paths from this one: a quoted `"--no-verify"`
# or `"main"` is still the flag or branch the command receives, but a `;`
# inside a quoted message cannot end the invocation early or start a fake
# one, and every shell word is one word in both views.
#
# The scanner is a quote state machine: one left-to-right pass mirroring bash's own
# tokenizer (single quotes have no escapes; in double quotes a backslash
# escapes only `"` `\` `$` and backtick; `$'…'` honours backslashes; a
# LIVE `$` — never one produced by `\$`, and toggled by `$$` — opens
# ANSI-C quoting). Two independent regex passes cannot do this: whichever
# quote style strips first cross-pairs its delimiters embedded in the
# other style's spans. Byte mode (`LC_ALL=C`) keeps it locale-proof.
# Payload re-scans are capped at four levels; deeper payloads are emitted
# unscanned and visible (fail closed). If awk itself fails the raw text is
# returned — the guard then behaves as it did before the pre-pass existed.
#
# GUARD_NEUTRAL is the byte (US, \037) the --words view puts in place of
# every character inside a quoted or escaped word that would otherwise act
# as shell syntax or split the word: blanks, tabs, newlines, the separators
# `;` `&` `|` `(` `)` `<` `>`, `{` `}`, `!` and the backtick. It is not
# whitespace to bash, grep or awk, so a quoted word stays ONE word in that
# view (`-C 'a b'` is the single value `a␟b`, as git receives it), and it
# is no identifier character, so it never glues a word to its neighbour
# (`system(q(git push))` keeps `git` a word of its own). A guard that hands
# a word's value to git and finds this byte in it cannot know that value
# exactly, and treats it as unknowable (block_git_destructive.sh). The
# command-positions view never holds it: there a quoted span is `_`, and an
# escaped character that would be syntax — an escaped blank included —
# is `_` too.
# shellcheck disable=SC2034  # consumed by sourcing hooks
GUARD_NEUTRAL=$'\037'
# The redirection blanker, one copy: the scanner below includes it, and
# guard_unredir runs it alone.
_GUARD_UNREDIR_AWK='
# A run of n blanks (grown by doubling, so linear in total).
function blanks(n) { while (length(SP) < n) SP = SP SP; return substr(SP, 1, n) }
# Same-length copy of t with every redirection blanked — an optional fd
# number or {name} at a word start (after a blank, a newline or one of
# `;&|()`, or at the start of t), the operator (`<` `>` `>>` `>|` `<>`
# `>&` `<&` `&>` `&>>`), optional blanks, then the target (empty when t
# ends there). `<<`, `<<-`, `<<<` and the emitted `<<_` are left alone:
# they are heredoc / here-string openers, which the wrapper regexes read.
# Same length, so offsets into t stay valid for the chain-base move.
function unredir(t,   n, k, a, r, c, d, f, j, e, m) {
    n = length(t); a = 1; r = ""; k = 1
    while (k <= n) {
        c = substr(t, k, 1)
        if (c == "<" && substr(t, k + 1, 1) == "<") { while (substr(t, k, 1) == "<") k++; continue }
        if (!(c == "<" || c == ">" || (c == "&" && substr(t, k + 1, 1) == ">"))) { k++; continue }
        f = k; j = k - 1
        while (j >= a && substr(t, j, 1) ~ /[0-9]/) j--
        if (j < k - 1 && (j < 1 || index(" \t\n;&|()", substr(t, j, 1)))) f = j + 1
        else if (substr(t, k - 1, 1) == "}") {
            j = k - 2
            while (j >= a && substr(t, j, 1) ~ /[A-Za-z0-9_]/) j--
            if (j >= a && j < k - 2 && substr(t, j, 1) == "{" && (j == 1 || index(" \t\n;&|()", substr(t, j - 1, 1)))) f = j
        }
        d = substr(t, k + 1, 1); e = k + 1
        if (c == "&") e = (substr(t, k + 2, 1) == ">") ? k + 3 : k + 2
        else if (d != "" && index((c == ">") ? ">|&" : ">&", d)) e = k + 2
        while (e <= n && index(" \t", substr(t, e, 1))) e++
        # The target, as GUARD_REDIR reads it: plain characters and
        # unnested `(…)` groups, so `>$(mktemp)` is blanked whole rather
        # than leaving `(mktemp)` to read as a subshell before the command.
        # A group is a command bash runs (`>$(git clean -fd)`, `<(…)`), so
        # each one blanked is recorded in G[1..NG] when REC is set (only
        # guard_unredir sets it; calls from the scanner record nothing):
        # guard_unredir prints them back on lines of their own, where
        # guards still see them.
        while (e <= n) {
            if (substr(t, e, 1) == "(") {
                m = index(substr(t, e + 1), ")")
                if (m > 0 && index(substr(t, e + 1, m - 1), "(") == 0) {
                    if (REC) G[++NG] = substr(t, e, m + 1)
                    e += m + 1
                    continue
                }
                break
            }
            if (index(" \t\n;&|()<>", substr(t, e, 1))) break
            e++
        }
        r = r substr(t, a, f - a) blanks(e - f); a = e; k = e
    }
    return r substr(t, a)
}
'
_GUARD_CMDPOS_AWK="${_GUARD_UNREDIR_AWK}"'
# Neutralise every character of t that would act as shell syntax or split
# a word. The stand-in is NREP, set once per view in BEGIN: `_` in the
# command-positions view (where only escaped characters reach here), the
# GUARD_NEUTRAL byte in the --words view.
function neut(t) { gsub(/[;&|()`<>\n!{} \t]/, NREP, t); return t }
# Wrapper type of the command so far. t is the current wrapper chain — the
# output since the chain base cb (see scan), however long: a fixed-size
# window let padding (`bash -o posix -o posix … -c`) push the wrapper out
# of view.
function wtype(t) {
    if (t ~ RE_SHELLC || t ~ RE_HSTR) return 1
    if (t ~ RE_EVAL) return 2
    if (t ~ RE_SSH) return 3
    return 0
}
# The `&` or `|` at i belongs to a redirection operator (`>&` `<&` `&>`
# `&>>` `>|`), so it does not end the command; `|&` and a lone `&` do.
function redir_amp(s, i, c, pc) {
    if (c == "&" && substr(s, i + 1, 1) == ">") return 1
    if ((c == "&" && (pc == ">" || pc == "<")) || (c == "|" && pc == ">")) return substr(s, i - 2, 1) != "\\"
    return 0
}
# The word at i is part of a redirection, not an argument: it starts with
# an fd-numbered operator (`2>x`, `{fd}>x`) or is the target of the
# operator before it (`> x`, `<x`, `>& 2`) — a `<<<` here-string operand
# is not, it is the payload of `bash <<<`.
function redir_word(s, i,   k, ch) {
    if (substr(s, i, 40) ~ /^([0-9]+|[{][A-Za-z_][A-Za-z0-9_]*[}])(>|<[^<]|<$)/) return 1
    k = i - 1
    while (k >= 1 && index(" \t", substr(s, k, 1))) k--
    if (k < 1) return 0
    ch = substr(s, k, 1)
    if (ch == "&" || ch == "|") {
        k--
        if (!(substr(s, k, 1) == ">" || (ch == "&" && substr(s, k, 1) == "<"))) return 0
    } else if (ch != "<" && ch != ">") return 0
    if (substr(s, k - 1, 1) == "\\") return 0
    return substr(s, k - 2, 3) != "<<<"
}
function rescan(t,   r) {
    if (PD >= 4) return ";" t ";"
    PD++; r = scan(t, 1, 0); PD--
    return ";" r ";"
}
# ---- Text the shell assembles from several words --------------------------
# `eval` joins its arguments with blanks and parses the result; ssh joins
# the words after the host into the command the remote shell parses; and
# a shell `-c` payload reads the words after it as $0, $1, … So each is
# also read the way the shell runs it: joined (or substituted) into one
# text and re-scanned, in addition to the word-by-word reading.
#
# The cost stays linear in the command: a simple command is joined at most
# once per scan (the caller tracks how far a join already read), words are
# joined pairwise (joins), never by growing one string, and every joined
# or substituted text that is re-scanned is charged to BUDGET, one byte
# budget for the whole command across every depth. A text longer than
# JOINCAP, or one past the budget, is emitted unscanned with its quote
# characters dropped — visible to every guard (fail closed), not
# re-parsed.
#
# A redirection starts at i: an operator, or an fd number / {name} before
# one.
function redir_start(s, i,   c) {
    c = substr(s, i, 1)
    if (c == "<" || c == ">" || (c == "&" && substr(s, i + 1, 1) == ">")) return 1
    return substr(s, i, 40) ~ /^([0-9]+|[{][A-Za-z_][A-Za-z0-9_]*[}])[<>]/
}
# The position after the redirection at i: its fd part, its operator, the
# blanks after it and its target (one word, or one `(…)` group).
function redir_skip(s, i,   n, d, ch) {
    n = length(s)
    if (match(substr(s, i, 40), /^([0-9]+|[{][A-Za-z_][A-Za-z0-9_]*[}])/)) i += RLENGTH
    if (substr(s, i, 1) == "&") i++
    i++
    while (i <= n && index("<>&|-", substr(s, i, 1))) i++
    while (i <= n && index(" \t", substr(s, i, 1))) i++
    if (substr(s, i, 1) == "(") {
        d = 0
        while (i <= n) {
            ch = substr(s, i, 1); i++
            if (ch == "(") d++
            else if (ch == ")" && --d == 0) break
        }
        return i
    }
    pword(s, i)
    return (WPOS > i) ? WPOS : i
}
# The words of the simple command from i, each decoded as pword() reads
# it, redirections skipped, up to the first separator, newline or comment.
# Sets CN, CW[1..CN] (the words), CX[k] (1 when word k holds a `$` or a
# backtick, so its value after expansion is unknowable here), CR (how
# many words decode to text other than their own spelling: quoted or
# escaped) and CEND (the position where the simple command ends).
function cmd_words(s, i,   n, c, st, w) {
    n = length(s); CN = 0; CR = 0
    while (i <= n) {
        c = substr(s, i, 1)
        if (c == " " || c == "\t") { i++; continue }
        if (c == "\\" && substr(s, i + 1, 1) == "\n") { i += 2; continue }
        if (index(";&|\n()", c) && !redir_amp(s, i, c, substr(s, i - 1, 1))) break
        if (c == "#") break
        if (redir_start(s, i)) { i = redir_skip(s, i); continue }
        st = i; w = pword(s, i); i = WPOS
        if (i == st) { i++; continue }
        CW[++CN] = w; CX[CN] = (substr(s, st, i - st) ~ /[$`]/)
        if (substr(s, st, i - st) != w) CR++
    }
    CEND = i
}
# A[lo..hi] joined with sep, pairwise: the copying is n log n, where
# growing one string word by word is quadratic in some awks.
function joins(A, lo, hi, sep,   m) {
    if (lo > hi) return ""
    if (lo == hi) return A[lo]
    m = int((lo + hi) / 2)
    return joins(A, lo, m, sep) sep joins(A, m + 1, hi, sep)
}
# rescan(), for an assembled text: charged to BUDGET; past JOINCAP or the
# budget, emitted unscanned with quote characters dropped (see above).
function jrescan(t) {
    if (length(t) > JOINCAP || length(t) > BUDGET) { gsub(/["\047]/, "", t); return ";" t ";" }
    BUDGET -= length(t)
    return rescan(t)
}
# The words after `ssh`, L[1..n] (X[k] as CX), read as OpenSSH reads them
# — its getopt string SSH_GETOPT (BEGIN), verbatim from ssh.c: an option
# word is a dash and at least one letter; in a cluster, a letter that
# takes a value takes the rest of the word or, at its end, the next word;
# `--` ends the options. The run starts at k; returns the index of the
# first word that is no option (-1 when a value is missing), and sets
# SAMB (letters outside the string, each of which may take a value in a
# newer OpenSSH) and STERM (the run ended with `--`).
function ssh_optrun(L, n, k,   t, j, m, c) {
    SAMB = 0; STERM = 0
    while (k <= n) {
        t = L[k]
        if (t !~ /^-./) return k
        if (t == "--") { STERM = 1; return k + 1 }
        m = length(t)
        for (j = 2; j <= m; j++) {
            c = substr(t, j, 1)
            if (index(SSH_VAL, c)) { if (j == m && ++k > n) return -1; break }
            if (!index(SSH_FLAG, c)) SAMB++
        }
        k++
    }
    return k
}
# Where the remote command starts in L[1..n]: an option run, the host,
# then — unless `--` ended the first run — a second option run, because
# OpenSSH reads options again after the host. A word standing where the
# host is read that holds an expansion may expand to nothing, or to
# options taking the next word; that and each unknown letter may move the
# start later, never earlier. Sets SLO (the start when none does) and SHI
# (the latest start they allow); returns 0 when no command follows.
function ssh_start(L, X, n,   k, host, amb) {
    k = ssh_optrun(L, n, 1); amb = SAMB
    if (k < 0 || k > n) return 0
    host = k
    if (X[host]) amb += 2
    k = host + 1
    if (!STERM) { k = ssh_optrun(L, n, k); amb += SAMB }
    if (k < 0 || k > n) return 0
    SLO = k; SHI = k + amb
    return 1
}
# The ssh command from i (the word after `ssh`), joined and re-scanned at
# each start ssh_start() allows — at most three. Sets JEND to the end of
# the simple command.
function ssh_join(s, i,   L, X, n, k, hi, r) {
    cmd_words(s, i); JEND = CEND; n = CN
    for (k = 1; k <= n; k++) { L[k] = CW[k]; X[k] = CX[k] }
    if (!ssh_start(L, X, n)) return ""
    hi = SHI; if (hi > SLO + 2) hi = SLO + 2
    r = ""
    for (k = SLO; k <= hi && k <= n; k++) r = r jrescan(joins(L, k, n, " "))
    return r
}
# The eval arguments from i, joined and re-scanned. Sets JEND. When every
# word is spelled as it decodes, the joined text is the words themselves,
# which the word-by-word reading already scans after its `;` mark, so
# nothing is added: a chain of plain `eval eval …` costs no joins.
function eval_join(s, i,   L, n, k) {
    cmd_words(s, i); JEND = CEND; n = CN
    if (!CR) return ""
    for (k = 1; k <= n; k++) L[k] = CW[k]
    k = (n && L[1] == "--") ? 2 : 1
    return (k <= n) ? jrescan(joins(L, k, n, " ")) : ""
}
# v single-quoted for the shell; an embedded single quote is closed,
# escaped with a backslash and reopened.
function squote(v,   Q, n) {
    n = split(v, Q, "\047")
    return "\047" joins(Q, 1, n, "\047\\\047\047") "\047"
}
# The shell `-c` payload w with its positional parameters substituted from
# the words after it (from i: $0 first, then $1 …), re-scanned. A lone
# "$@" becomes every argument single-quoted, a lone "$*" or "$N" one
# single-quoted word; any other reference — unquoted, inside a longer
# quoted string, or `${…}` with an operator — the raw text: the arguments
# joined with blanks (`$@` `$*`, which never include $0) or the one
# argument (`$N`, and `$0` too: a payload of `$0 "$@"` before the words
# `git push -f` runs git). A slice `${@:N}` / `${@:N:L}` (and `${*:…}`)
# takes the arguments from $N, counting $0 as bash does: quoted, each
# single-quoted (`@`) or all as one word (`*`); unquoted, joined. A quoted
# `${@…}` / `${*…}` whose operator is not a plain-number slice (`${@: -3}`,
# `${@:$x}`) cannot be judged, so it reads as every argument. The
# first reference is always substituted — its text is at most the command
# itself, and it is the one that names what a lone `"$@"` payload runs; a
# later one only while the text stays within JOINCAP, after which the
# rest of the payload is kept as written and the whole is emitted
# unscanned (jrescan).
#
# pslice() sets PLO..PHI, the arguments slice body (`@:N` or `@:N:L`,
# braces dropped) names out of $0..$m.
function pslice(body, m,   B, n) {
    n = split(body, B, ":"); PLO = B[2] + 0; PHI = (n > 2) ? PLO + B[3] - 1 : m
    if (PHI > m) PHI = m
}
function pos_rescan(w, s, i,   P, m, k, n, c, d, j, e, body, v, A, na, tot, st, QP, raw, qall, qstar, num) {
    cmd_words(s, i); m = CN - 1
    for (k = 1; k <= CN; k++) { P[k - 1] = CW[k]; QP[k - 1] = squote(CW[k]) }
    raw = joins(P, 1, m, " "); qall = joins(QP, 1, m, " "); qstar = squote(raw)
    n = length(w); k = 1; st = 1; na = 0; tot = 0
    while (k <= n) {
        c = substr(w, k, 1)
        if (c == "\"" && match(substr(w, k, 64), /^"[$]([@*0-9]|[{]([@*]|[0-9]+)[}]|[{][@*][^}"]*[}])"/)) {
            e = k + RLENGTH; body = substr(w, k + 2, RLENGTH - 3); gsub(/[{}]/, "", body)
            if (body ~ /^[@*]:[0-9]+(:[0-9]+)?$/) { pslice(body, m); v = (body ~ /^@/) ? joins(QP, PLO, PHI, " ") : squote(joins(P, PLO, PHI, " ")) }
            else if (body ~ /^@/) v = qall
            else if (body ~ /^[*]/) v = qstar
            else v = (body + 0 <= m) ? QP[body + 0] : "\047\047"
        } else if (c == "$") {
            d = substr(w, k + 1, 1)
            if (d == "@" || d == "*") { v = raw; e = k + 2 }
            else if (d ~ /[0-9]/) { num = d + 0; v = (num <= m) ? P[num] : ""; e = k + 2 }
            else if (d == "{" && (j = index(substr(w, k + 2), "}")) > 0) {
                body = substr(w, k + 2, j - 1); e = k + 2 + j
                if (body ~ /^[@*]:[0-9]+(:[0-9]+)?$/) { pslice(body, m); v = joins(P, PLO, PHI, " ") }
                else if (body ~ /^[0-9]+$/) v = (body + 0 <= m) ? P[body + 0] : ""
                else if (body ~ /^#?([@*]|[0-9])/) v = raw
                else { k++; continue }
            } else { k++; continue }
        } else { k++; continue }
        if (na && tot + (k - st) + length(v) > JOINCAP) break
        A[++na] = substr(w, st, k - st); A[++na] = v; tot += (k - st) + length(v)
        k = e; st = e
    }
    A[++na] = substr(w, st)
    return jrescan(joins(A, 1, na, ""))
}
# One shell word at i, quotes removed — the payload of a wrapper such as
# `bash -c`. Its quote rules must match scan()s own handlers (single,
# double and $-quotes there) and litword() below; the quoting-parity test
# runs every case both ways so they cannot drift. Its scan() calls only
# find where a substitution ends — their output is not kept — so NJ turns
# the joins above off inside them.
function pword(s, i,   n, w, c, d, k, j, ch, e) {
    n = length(s); w = ""
    while (i <= n) {
        c = substr(s, i, 1)
        if (index(" \t\n;&|()<>", c)) break
        if (c == "\\") { d = substr(s, i + 1, 1); if (d != "\n") w = w d; i += 2; continue }
        if (c == "$" && substr(s, i + 1, 1) == "\047") {
            k = i + 2
            while (k <= n) {
                ch = substr(s, k, 1)
                if (ch == "\\") { w = w substr(s, k + 1, 1); k += 2; continue }
                if (ch == "\047") break
                w = w ch; k++
            }
            i = k + 1; continue
        }
        if (c == "\047") {
            j = index(substr(s, i + 1), "\047")
            if (j == 0) { w = w substr(s, i + 1); i = n + 1 } else { w = w substr(s, i + 1, j - 1); i = i + j + 1 }
            continue
        }
        if (c == "\"") {
            k = i + 1
            while (k <= n) {
                ch = substr(s, k, 1)
                if (ch == "\\") {
                    d = substr(s, k + 1, 1)
                    if (d == "\n") { k += 2; continue }
                    if (index("\"\\$`", d)) { w = w d; k += 2; continue }
                    w = w ch; k++; continue
                }
                if (ch == "\"") break
                if ((ch == "$" && substr(s, k + 1, 1) == "(") || ch == "`") {
                    NJ++; if (ch == "`") scan(s, k + 1, 2); else scan(s, k + 2, 1); NJ--
                    e = RPOS; w = w substr(s, k, e - k); k = e; continue
                }
                w = w ch; k++
            }
            i = k + 1; continue
        }
        if ((c == "$" && substr(s, i + 1, 1) == "(") || c == "`") {
            NJ++; if (c == "`") scan(s, i + 1, 2); else scan(s, i + 2, 1); NJ--
            e = RPOS; w = w substr(s, i, e - i); i = e; continue
        }
        w = w c; i++
    }
    WPOS = i
    return w
}
# A quoted word that the shell runs as one plain word: the word at i with
# its quotes removed (`'git'`, `g"it"`, `$'pr'`), or "" when it is not one
# — then scan()s handlers below blank it as before. Decoded only when at
# least one piece is quoted, every piece is literal (single-quoted text;
# double-quoted or $"…" text with no `$`, backtick or backslash; $'…'
# text with no backslash; unquoted characters; a backslash-escaped plain
# character; a backslash-newline line continuation, which adds nothing),
# every quote is closed, and the result is a run of
# [A-Za-z0-9_@%+=:,./-]. A quoted `-h` / `--help` stays blank, so the help
# exemption (guard_help_only) never widens. Its quote rules must match
# scan()s handlers and pword(). Reads one word: linear. Sets LPOS to the
# position after the word.
function litword(s, i,   n, w, q, c, d, k, j, body) {
    n = length(s); w = ""; q = 0
    while (i <= n) {
        c = substr(s, i, 1)
        if (index(" \t\n;&|()<>", c)) break
        if (c == "\\") {
            d = substr(s, i + 1, 1)
            # A line continuation joins the lines and adds nothing, so the
            # word goes on (`g\<newline>"it"`, or one that starts with it).
            if (d == "\n") { i += 2; continue }
            if (d !~ /^[A-Za-z0-9_@%+=:,.\/-]$/) return ""
            w = w d; i += 2; continue
        }
        k = 0
        if (c == "$") {
            d = substr(s, i + 1, 1)
            if (d != "\047" && d != "\"") return ""
            k = (d == "\047"); i++; c = d
        }
        if (c == "\047" || c == "\"") {
            j = index(substr(s, i + 1), c)
            if (j == 0) return ""
            body = substr(s, i + 1, j - 1)
            if ((k || c == "\"") && index(body, "\\")) return ""
            if (c == "\"" && body ~ /[$`]/) return ""
            w = w body; q = 1; i = i + j + 1; continue
        }
        if (c !~ /^[A-Za-z0-9_@%+=:,.\/-]$/) return ""
        w = w c; i++
    }
    if (!q || w !~ /^[A-Za-z0-9_@%+=:,.\/-]+$/ || w == "-h" || w == "--help") return ""
    LPOS = i
    return w
}
function hdline(s,   n, k, ch, out, sb) {
    n = length(s); k = 1; out = ""
    while (k <= n) {
        ch = substr(s, k, 1)
        if (ch == "\\") { k += 2; continue }
        if (ch == "$" && substr(s, k + 1, 1) == "(") {
            sb = scan(s, k + 2, 1); out = out "(" sb ")"; k = RPOS; continue
        }
        if (ch == "`") { sb = scan(s, k + 1, 2); out = out "(" sb ")"; k = RPOS; continue }
        k++
    }
    return out
}
# `$((…))` / `((…))`: arithmetic, emitted verbatim up to its balanced
# close. Its parentheses are not subshells, so they must not move the
# mode-1 depth count. Returns the text; the position after it is in APOS.
function arith(s, i,   n, k, d, ch) {
    n = length(s); d = 0
    for (k = i; k <= n; k++) {
        ch = substr(s, k, 1)
        if (ch == "(") d++
        else if (ch == ")") { d--; if (d == 0) break }
    }
    APOS = k + 1
    return substr(s, i, k - i + 1)
}
# A `#` opens a comment only where bash would: at a word start (after a
# blank or separator, not an escaped blank), outside `${…}` (where `#` is
# an operator) and outside backticks (mode 2, whose close ends the scan).
function is_comment(s, i, pc, bd, mode) {
    if (mode == 2 || bd != 0 || !index(" \t\n;&|()", pc)) return 0
    return !(index(" \t", pc) && substr(s, i - 2, 1) == "\\")
}
# Position of the newline ending the comment at i (or past the end).
function comment_end(s, i,   j) {
    j = index(substr(s, i), "\n")
    return (j == 0) ? length(s) + 1 : i + j - 1
}
# `<<word` / `<<-word` at i (the `<<<` here-string is handled before
# this). Sets HW (delimiter, quotes removed; "" when there is none), HQ
# (delimiter was quoted: body is literal), HD (`<<-`: leading tabs
# stripped), HX (the body is fed to a shell — `bash <<EOF`, read from
# *before*, the current wrapper chain the scanner emitted with its
# redirections blanked, or `<<EOF | sh`, read from the rest of the line)
# and HPOS (position after the delimiter).
# Two heredoc-to-shell patterns, two regexes: RE_FEED and RE_PIPESH.
function heredoc_open(s, i, before,   n, k, ch, j, rl) {
    n = length(s); k = i + 2; HD = 0
    if (substr(s, k, 1) == "-") { HD = 1; k++ }
    while (substr(s, k, 1) == " " || substr(s, k, 1) == "\t") k++
    HW = ""; HQ = 0; HX = 0
    while (k <= n) {
        ch = substr(s, k, 1)
        if (index(" \t\n;&|()<>", ch)) break
        if (ch == "\047" || ch == "\"" || ch == "\\") { HQ = 1; k++; continue }
        HW = HW ch; k++
    }
    if (HW != "") {
        j = index(substr(s, k), "\n")
        rl = (j == 0) ? substr(s, k) : substr(s, k, j - 1)
        HX = (before ~ RE_FEED || rl ~ RE_PIPESH)
    }
    HPOS = k
}
# Wrapper detection reads the whole current simple command, with no size
# cap, in linear time. Every wrapper regex ends in a shape the previous
# word alone reveals — a `-…c…` flag after a shell name, `<<<`, `eval`, or
# any word once `ssh` has been seen (only a quoted word can then be the
# payload) — so the full regex runs only when that word fits. It reads from
# cb, the chain base: past the last character no wrapper regex can span
# (a separator, `(`, `)`, or the `<` of a heredoc / here-string opener),
# which is also why a match can never start before it — except the `<<<`
# here-string, which is why the base moves only after the previous word
# has been checked. pws marks where the previous word began; hsh / hssh /
# hev record a shell name / `ssh` / `env` in the current chain.
#
# Two more previous words start something. `-exec`, `-execdir`, `-ok` and
# `-okdir` (options of find, but read wherever they appear: over-reading
# only widens what a guard sees) make the next word a command start,
# marked with a `;` like the word after `eval` (wt 4) — but unlike the
# arguments of `eval`, the words after it are arguments of that command, so a
# quoted one is not re-scanned (`-exec echo "git reset" ;` mentions git,
# it does not run it). `-S` (any short cluster ending in it) or
# `--split-string` after an `env` that is being run — the first word of
# the chain is env or a wrapper, so `echo env -S` only mentions it — makes
# a QUOTED next word a
# payload, re-scanned like a `bash -c` payload (wt 5) — env splits it into
# the command it runs; that word alone, since the words after it are its
# arguments. Only a word holding a blank is a payload: a quoted single
# word (`env -S "git" reset --hard`) is the program name, read as an
# ordinary word, so its arguments stay in view. An unquoted `env -S git …` needs no unwrapping: the shared
# prefix reads `-S` as a wrapper option. `sort -S 1G` and `sudo -S` have
# no `env` before them, so nothing changes there.
#
# Redirections are read past, never through: the previous word and the
# chain are checked on unredir() copies (same length, so cb / pws offsets
# hold), a word that is itself a redirection or its target (redir_word)
# is skipped — rt marks one in progress and moves pws past it when it
# ends, and lw / prr let the next word see the last real word behind it,
# in constant work per word, so padding with redirections stays linear.
# psh marks that a shell `-c` payload was found: every later word of the
# simple command is then re-scanned as a payload too (options may follow
# `-c`), until a separator clears it with pa.
#
# The joined readings (Text the shell assembles, above) are found at the
# same words — the first word after `eval` (ev), the first after `ssh`,
# and the first shell `-c` payload that names a positional parameter
# (pdone) — and are held in pj until the simple command ends, then
# emitted: written where they were found, they would cut the chain the
# word-by-word reading still has to see. je is where the last join
# stopped reading, so a word inside an already joined command (the second
# `eval` of `eval eval …`) starts no second join.
function scan(s, i, mode,    n, out, c, d, depth, dollar, nh, hdl, hq, ht, hx, k, j, w, line, sb, e, ansi, dec, vis, kv, ch, h, cmp, pc, bd, pa, wt, qs, cb, pws, pw, hsh, hssh, hev, psh, isr, rt, prr, lw, mpw, spw, st, lt, qb, ev, je, pj, pdone) {
    n = length(s); out = ""; depth = 0; dollar = 0; nh = 0; bd = 0; pa = 0; st = i
    cb = 0; pws = 0; hsh = 0; hssh = 0; hev = 0; psh = 0; rt = 0; prr = 0; lw = ""
    je = 0; pj = ""; pdone = 0
    while (i <= n) {
        c = substr(s, i, 1)
        pc = (i == 1) ? "\n" : substr(s, i - 1, 1)
        if (rt && index(" \t<>", c)) { pws = length(out); rt = 0 }
        if (index(";&|\n()", c) && !redir_amp(s, i, c, pc)) {
            if (pj != "") { out = out pj; pj = "" }
            pa = 0; psh = 0; rt = 0; prr = 0; lw = ""; pdone = 0
            cb = length(out); pws = cb + 1; hsh = 0; hssh = 0; hev = 0
        }
        if (!index(" \t\n;&|()<>", c) && index(" \t<", pc)) {
            qs = (c == "\047" || c == "\"" || (c == "$" && index("\047\"", substr(s, i + 1, 1))))
            isr = redir_word(s, i)
            pw = substr(out, pws + 1); sub(/[ \t]+$/, "", pw)
            mpw = unredir(pw); spw = mpw
            sub(/^[ \t]+/, "", spw); sub(/[ \t]+$/, "", spw)
            # A redirection word (2>&1) leaves spw empty; carry the last real
            # word so a -c or wrapper name behind it is still seen.
            if (prr && spw == "") spw = lw
            lw = spw; prr = isr
            wt = 0
            if (!isr && ((hsh && spw ~ /^-[A-Za-z]*c[A-Za-z]*$/) || spw ~ /<<<$/ || spw ~ RE_EVALW || (qs && hssh)))
                wt = wtype(unredir(substr(out, cb + 1)))
            ev = (wt == 2)
            if (!NJ && i >= je && (ev || spw ~ RE_SSHW)) {
                pj = pj (ev ? eval_join(s, i) : ssh_join(s, i)); je = JEND
            }
            if (!isr && wt == 0) {
                if (spw ~ RE_EXECW) wt = 4
                else if (qs && hev && spw ~ RE_ENVSW && pword(s, i) ~ /[ \t]/) wt = 5
            }
            # Order matters: the base moves past pw only after the check
            # above, or a `bash <<<` chain would be cut before it is read.
            if (match(mpw, /[\n;&|()<>][^\n;&|()<>]*$/)) { cb = pws + RSTART; hsh = 0; hssh = 0; hev = 0 }
            if (spw ~ RE_HEADW) hsh = 1
            if (spw ~ RE_SSHW) hssh = 1
            if (spw ~ RE_ENVW && unredir(substr(out, cb + 1)) ~ RE_ENVHEAD) hev = 1
            pws = length(out); rt = isr
            if (!isr && wt == 0) {
                if (psh && !(c == "#" && is_comment(s, i, pc, bd, mode))) wt = 1
                else if (pa && qs) wt = 2
            }
            if (wt != 4 && (wt == 1 || (wt && qs))) {
                w = pword(s, i); out = out rescan(w); i = WPOS
                if (wt == 1 && !pdone && !NJ && w ~ RE_POSREF) { pdone = 1; pj = pj pos_rescan(w, s, i) }
                cb = length(out); pws = cb; hsh = 0; hssh = 0; hev = 0; lw = ""; prr = 0
                if (wt == 1) psh = 1
                pa = (wt == 2 || wt == 3); dollar = 0; continue
            }
            if (wt == 2 || wt == 4) { out = out ";"; cb = length(out); pws = cb; hsh = 0; hssh = 0; hev = 0; lw = ""; prr = 0 }
        }
        # At an unescaped word start (an even run of backslashes before pc),
        # a quoted word that decodes to one plain word is emitted as that
        # word, so `'git' push -f` is the command the shell runs. The
        # --words view keeps quoted words already; there it only drops the
        # `$` of a `$'…'` / `$"…"` word, which the quote handlers keep.
        if (bd == 0 && !index(" \t\n;&|()<>", c) && (i == st || index(" \t\n;&|()<>`", pc))) {
            j = i - 2
            while (j >= 1 && substr(s, j, 1) == "\\") j--
            if ((i - 2 - j) % 2 == 0) {
                lt = litword(s, i)
                if (lt != "") { out = out lt; i = LPOS; dollar = 0; continue }
            }
        }
        if (c == "\\") {
            d = substr(s, i + 1, 1)
            if (d != "\n") out = out neut(d)
            dollar = 0; i += 2; continue
        }
        # Quote handlers: keep in step with pword(), which reads the same
        # quoting when it extracts a wrapper payload.
        if (c == "\047") {
            ansi = dollar; dollar = 0
            k = i + 1; dec = ""
            if (ansi) {
                while (k <= n) {
                    ch = substr(s, k, 1)
                    if (ch == "\\") { dec = dec substr(s, k + 1, 1); k += 2; continue }
                    if (ch == "\047") break
                    dec = dec ch; k++
                }
            } else {
                j = index(substr(s, k), "\047")
                if (j == 0) { dec = substr(s, k); k = n + 1 } else { dec = substr(s, k, j - 1); k = k + j - 1 }
            }
            i = k + 1
            if (KEEP) out = out neut(dec)
            else out = out "_"
            continue
        }
        if (c == "\"") {
            dollar = 0
            k = i + 1; vis = ""; kv = ""; qb = 0
            while (k <= n) {
                j = match(substr(s, k), /[\\"$`{}]/)
                if (j == 0) { kv = kv neut(substr(s, k)); k = n + 1; break }
                if (j > 1) { kv = kv neut(substr(s, k, j - 1)); k += j - 1 }
                ch = substr(s, k, 1)
                # A live `${…}` keeps its braces, so `"${G}" push` still reads
                # as a variable in the command-word slot (GIT_UNKNOWN_ANCHOR);
                # qb counts the braces open inside it. Any other brace is
                # neutralised like the rest of the span.
                if (ch == "$" && substr(s, k + 1, 1) == "{") { kv = kv "${"; qb++; k += 2; continue }
                if (ch == "{" || ch == "}") {
                    if (qb == 0) kv = kv NREP
                    else { kv = kv ch; qb += (ch == "{") ? 1 : -1 }
                    k++; continue
                }
                if (ch == "\\") {
                    d = substr(s, k + 1, 1)
                    if (d == "\n") { k += 2; continue }
                    if (index("\"\\$`", d)) { kv = kv neut(d); k += 2; continue }
                    kv = kv ch; k++; continue
                }
                if (ch == "\"") break
                if ((ch == "$" && substr(s, k + 1, 1) == "(") || ch == "`") {
                    sb = (ch == "`") ? scan(s, k + 1, 2) : scan(s, k + 2, 1)
                    vis = vis "(" sb ")"; kv = kv "(" sb ")"
                    k = RPOS; continue
                }
                kv = kv ch; k++
            }
            i = k + 1
            if (KEEP) out = out kv
            else out = out "_" vis
            continue
        }
        if (c == "`") {
            if (mode == 2) { RPOS = i + 1; return out pj }
            sb = scan(s, i + 1, 2); out = out "(" sb ")"; i = RPOS; dollar = 0; continue
        }
        if (c == "(" && substr(s, i + 1, 1) == "(" && (pc == "$" || index(" \t\n;&|(", pc))) {
            out = out arith(s, i); i = APOS; dollar = 0; continue
        }
        if (c == "(") { if (mode == 1) depth++; out = out c; i++; dollar = 0; continue }
        if (c == ")") {
            if (mode == 1) { if (depth == 0) { RPOS = i + 1; return out } depth-- }
            out = out c; i++; dollar = 0; continue
        }
        if (c == "{" && pc == "$") bd++
        if (c == "}" && bd > 0) bd--
        if (c == "#" && is_comment(s, i, pc, bd, mode)) { i = comment_end(s, i); continue }
        if (c == "<" && substr(s, i, 3) == "<<<") { out = out "<<<"; i += 3; dollar = 0; continue }
        if (c == "<" && substr(s, i + 1, 1) == "<") {
            heredoc_open(s, i, unredir(substr(out, cb + 1)))
            if (HW != "") { nh++; hdl[nh] = HW; hq[nh] = HQ; ht[nh] = HD; hx[nh] = HX }
            out = out "<<_"; i = HPOS; dollar = 0; continue
        }
        if (c == "\n") {
            out = out "\n"; i++; dollar = 0
            for (h = 1; h <= nh; h++) {
                while (i <= n) {
                    j = index(substr(s, i), "\n")
                    if (j == 0) { line = substr(s, i); i = n + 1 } else { line = substr(s, i, j - 1); i = i + j }
                    cmp = line
                    if (ht[h]) sub(/^\t+/, "", cmp)
                    if ((cmp "") == (hdl[h] "")) { out = out "\n"; break }
                    if (hx[h]) out = out scan(line, 1, 0) "\n"
                    else if (hq[h]) out = out "\n"
                    else out = out hdline(line) "\n"
                }
            }
            nh = 0
            continue
        }
        if (c == "$") { out = out c; dollar = !dollar; i++; continue }
        out = out c; dollar = 0; i++
    }
    RPOS = n + 1
    return out pj
}
BEGIN {
    PD = 0; SP = " "; NJ = 0
    NREP = KEEP ? NEUT : "_"
    # Assembled texts (Text the shell assembles, above): the most one may
    # hold and still be re-scanned, and the bytes all of them together may
    # re-scan.
    JOINCAP = 16384; BUDGET = 65536
    RE_POSREF = "[$]([@*0-9]|[{]#?([@*]|[0-9]))"
    # OpenSSH ssh.c getopt string, verbatim: a letter before a colon takes a
    # value. SSH_FLAG and SSH_VAL are its two letter sets.
    SSH_GETOPT = "1246ab:c:e:fgi:kl:m:no:p:qstvxAB:CD:E:F:GI:J:KL:MNO:P:Q:R:S:TVw:W:XYy"
    SSH_FLAG = ""; SSH_VAL = ""
    for (GK = 1; GK <= length(SSH_GETOPT); GK++) {
        GC = substr(SSH_GETOPT, GK, 1)
        if (GC == ":") continue
        if (substr(SSH_GETOPT, GK + 1, 1) == ":") SSH_VAL = SSH_VAL GC; else SSH_FLAG = SSH_FLAG GC
    }
    B = "(^|[ \t\n;&|()!{}])"
    SHELLS = "([^ \t\n;&|()<>]*/)?(bash|sh|zsh|dash|ksh)"
    SSHH = "([^ \t\n;&|()<>]*/)?ssh"
    ARG = "[ \t]+[^ \t\n;&|()<>]+"
    RE_SHELLC = B SHELLS "(" ARG ")*[ \t]+-[A-Za-z]*c[A-Za-z]*[ \t]+$"
    RE_EVAL = B "eval[ \t]+$"
    RE_SSH = B SSHH "(" ARG ")+[ \t]+$"
    RE_HSTR = B SHELLS "(" ARG ")*[ \t]*<<<[ \t]*$"
    RE_FEED = B "(" SHELLS "|" SSHH ")(" ARG ")*[ \t]*$"
    RE_PIPESH = "[|][ \t]*([^ \t\n;&|()<>]+[ \t]+)*(" SHELLS "|" SSHH ")([ \t;&|)]|$)"
    RE_HEADW = B SHELLS "$"
    RE_SSHW = B SSHH "$"
    RE_EVALW = B "eval$"
    RE_EXECW = "^-(exec|execdir|ok|okdir)$"
    RE_ENVW = B "([^ \t\n;&|()<>]*/)?env$"
    RE_ENVSW = "^(-[A-Za-z]*S|--split-string)$"
    # The first word of the chain (past assignments and compound keywords) is
    # env itself or a wrapper that runs a command, so `env` is being run.
    ASSIGN = "[A-Za-z_][A-Za-z0-9_]*=[^ \t]*[ \t]+"
    RE_ENVHEAD = "^[ \t\n;&|()]*((then|do|else|elif|[{!])[ \t]+)*(" ASSIGN ")*([^ \t\n;&|()<>]*/)?(" WRAPPERS ")([ \t]|$)"
}
{ src = (NR == 1) ? $0 : src "\n" $0 }
END { printf "%s\n", scan(src, 1, 0) }
'

# command_positions [--words] [--no-redir] <command>
# Prints the command-positions view of <command> (see above); with
# --words, quoted spans keep their words. With --no-redir, every
# redirection is blanked as guard_unredir does: a guard that reads a
# command's ARGUMENTS up to the next `;` `&` `|` matches this copy, so
# the `&` of `2>&1` or the `|` of `>|` cannot end the scan before a flag
# that follows. A command inside a blanked target (`>$(…)`, `<(…)`)
# follows as an extra line, so a --no-redir view can have more lines than
# the default one: read it as a set of segments, never pair its lines
# with another view's. Guards that look for a redirection target keep the
# default view.
command_positions() {
    local keep=0 noredir=0 view
    while :; do
        case "$1" in
            --words) keep=1 ;;
            --no-redir) noredir=1 ;;
            *) break ;;
        esac
        shift
    done
    view=$(printf '%s\n' "$1" | LC_ALL=C awk -v KEEP="$keep" -v NEUT="$GUARD_NEUTRAL" -v WRAPPERS="$GUARD_WRAPPERS" "$_GUARD_CMDPOS_AWK" 2>/dev/null) || view="$1"
    if [ "$noredir" = 1 ]; then
        view=$(guard_unredir "$view")
    fi
    printf '%s\n' "$view"
}

# guard_unredir <text>
# Prints <text> with every redirection blanked (unredir, above): same
# length, so a guard that splits a view at `;` `&` `|` can do it on this
# copy without the `&` of `2>&1` cutting a command in two. A target's
# `(…)` group holds a command bash runs, so each one blanked is printed
# again, redirections blanked, on a line of its own after the text: a
# guard reading these lines still sees `git clean -fd` in
# `echo >$(git clean -fd)`. If awk fails the text is returned unchanged.
guard_unredir() {
    local out
    out=$(printf '%s\n' "$1" | LC_ALL=C awk "${_GUARD_UNREDIR_AWK}"'
BEGIN { SP = " "; REC = 1 }
{ src = (NR == 1) ? $0 : src "\n" $0 }
END {
    NG = 0
    r = unredir(src)
    n = NG
    for (i = 1; i <= n; i++) H[i] = G[i]
    printf "%s\n", r
    for (i = 1; i <= n; i++) printf "%s\n", unredir(H[i])
}' 2>/dev/null) || out="$1"
    printf '%s\n' "$out"
}

# ---- Hook payload extraction ----------------------------------------------
#
# Every guard reads the Bash command out of the hook payload exactly once,
# through here — never a per-hook jq one-liner.

# parse_or_block <jq-filter> — print jq's answer for the payload in $INPUT,
# or deny (exit 2): an unparseable payload must never read as "no command".
parse_or_block() {
    jq -r "$1" <<< "$INPUT" 2>/dev/null || { echo "BLOCKED: unparseable hook payload" >&2; exit 2; }
}

# parse_command_segment — print the payload's .tool_input.command, empty when
# the tool call carries none. Denies (exit 2) on an unparseable payload.
parse_command_segment() {
    parse_or_block '.tool_input.command // empty'
}
