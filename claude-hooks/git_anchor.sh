#!/usr/bin/env bash
# Shared command anchors and command-position views for the guard hooks.
#
# NOT a hook — a sourced library (never registered in plugin.json).
# Consumers: every Claude Code guard that locates a command in the Bash
# command text — the git/gh family (block_force_push.sh,
# block_git_rebase.sh, block_raw_git.sh, block_git_destructive.sh,
# block_amend_pushed_commit.sh, block_protected_branches.sh,
# block_no_verify.sh, block_claude_attribution.sh, block_pr_merge.sh,
# block_unverified_pr_create.sh, warn_generated_conflicts.sh,
# warn_stale_wrapup.sh) and the other locating guards
# (block_install_deps.sh, block_raw_ruff.sh, block_branch_deletion.sh,
# block_continuation_delete.sh, block_raw_wrapup_post.sh,
# block_fixer_recon.sh, check_commit_format.sh, warn_pr_checks.sh,
# keep_squash_comment_last.sh), via:
#
#     source "$(dirname "$0")/git_anchor.sh"
#
# GIT_ANCHOR matches a real `git <subcommand>` invocation at line-start or
# after a shell separator (`;` `&` `|` `(`). It tolerates:
# - a leading run of `VAR=val` assignments and a `command`/`env`/`exec`/
#   `builtin`/`sudo` wrapper (incl. wrapper flag tokens like `sudo -n`),
#   so `GIT_DIR=x git ...` can't slip the gate;
# - a bounded run of git GLOBAL options between `git` and the subcommand
#   (`--no-pager`, `-c k=v`, `-C <dir>`, `--git-dir=<x>`), so
#   `git --no-pager <verb>` can't slip it either.
#
# SEG_ANCHOR is the same shape anchored to segment start, for hooks that
# split a compound command at separators and evaluate each segment.
#
# The anchors are never run against the raw command text alone — a raw
# text cannot tell a command from a quoted mention of one. Guards run
# them against the views `command_positions` builds (below), which know
# where the shell would actually start a command.
#
# Known accepted residuals (documented, deliberately out of scope):
# `${IFS}` splicing and variable indirection (`$GIT push`), `xargs git`,
# a backslash-newline continuation BETWEEN the words of an anchor,
# wrappers nested more than one level deep with escaped inner quotes,
# a script piped into a shell (`… | bash`), an interpreter running a
# subprocess (`python -c "subprocess.run(...)"`), space-separated
# arg-taking globals other than -c/-C (`--git-dir x`) and multi-arg
# wrapper flags (`sudo -u root`).
GIT_ANCHOR='(^|[;&|(])[[:space:]]*(([[:alnum:]_]+=[^[:space:]]+|command|env|exec|builtin|sudo|-[^[:space:]]+)[[:space:]]+)*git[[:space:]]+((-c|-C)[[:space:]]+[^[:space:]]+[[:space:]]+|--?[a-zA-Z][a-zA-Z-]*(=[^[:space:]]*)?[[:space:]]+)*'
# shellcheck disable=SC2034  # consumed by sourcing hooks
SEG_ANCHOR='^[[:space:]]*(([[:alnum:]_]+=[^[:space:]]+|command|env|exec|builtin|sudo|-[^[:space:]]+)[[:space:]]+)*git[[:space:]]+((-c|-C)[[:space:]]+[^[:space:]]+[[:space:]]+|--?[a-zA-Z][a-zA-Z-]*(=[^[:space:]]*)?[[:space:]]+)*'

# GH_ANCHOR is the same shape for `gh`: a real invocation at line-start
# (leading whitespace included — the hand-rolled `^gh` patterns this
# replaced were bypassed by a single leading space) or after a shell
# separator, tolerating the same VAR=val / wrapper-token prefix run.
# gh also takes global options before its subcommand, including the
# arg-bearing `-R` / `--repo` (`gh --repo o/r pr view 1` is ordinary
# syntax), so the same bounded global-option run GIT_ANCHOR allows
# follows `gh` here.
# shellcheck disable=SC2034  # consumed by sourcing hooks
GH_ANCHOR='(^|[;&|(])[[:space:]]*(([[:alnum:]_]+=[^[:space:]]+|command|env|exec|builtin|sudo|-[^[:space:]]+)[[:space:]]+)*gh[[:space:]]+((-R|--repo)[[:space:]]+[^[:space:]]+[[:space:]]+|--?[a-zA-Z][a-zA-Z-]*(=[^[:space:]]*)?[[:space:]]+)*'

# ---- Command positions ----------------------------------------------------
#
# `command_positions "$COMMAND"` prints the "command positions" copy of a
# Bash command: what the shell would actually run, with everything that is
# only DATA blanked. A guard finds commands here, so a commit message, a
# PR body, a grep pattern or a heredoc that merely mentions a guarded
# command never fires, while a command the shell runs is always seen:
#
# - single-quoted, double-quoted and `$'…'` spans become one `_` each;
#   comments are dropped; quoted-delimiter heredoc bodies are blanked;
# - `$(…)` and backticks stay visible wherever they execute — unquoted,
#   inside double quotes, inside an unquoted-delimiter heredoc body — and
#   are emitted as `(…)`, so the anchors' `(` separator sees their start;
# - the payload handed to `bash|sh|zsh|dash|ksh -c`, `eval`,
#   `ssh <host> "…"` or `bash <<<"…"` stays visible, re-scanned with its
#   own quoting, between two `;` marks — so each guard applies its
#   existing rule to the wrapped command exactly as if it were typed
#   directly; a heredoc fed to a shell (`bash <<EOF`, `ssh host <<EOF`)
#   is kept as commands for the same reason;
# - an escaped separator (`\;`) is a literal, so it is emitted as `_`.
#
# `command_positions --words "$COMMAND"` prints the same view with quoted
# spans kept as their words instead of blanked (quote characters dropped,
# separators inside them neutralised to `_`). Guards gate on the first
# view and read flags, refspecs, branch names and paths from this one: a
# quoted `"--no-verify"` or `"main"` is still the flag or branch the
# command receives, but a `;` inside a quoted message can no longer end
# the invocation early or start a fake one.
#
# The scanner is the quote state machine block_amend_pushed_commit.sh
# introduced, generalised: one left-to-right pass mirroring bash's own
# tokenizer (single quotes have no escapes; in double quotes a backslash
# escapes only `"` `\` `$` and backtick; `$'…'` honours backslashes; a
# LIVE `$` — never one produced by `\$`, and toggled by `$$` — opens
# ANSI-C quoting). Two independent regex passes cannot do this: whichever
# quote style strips first cross-pairs its delimiters embedded in the
# other style's spans. Byte mode (`LC_ALL=C`) keeps it locale-proof.
# Payload re-scans are capped at four levels; deeper payloads are emitted
# unscanned and visible (fail closed). If awk itself fails the raw text is
# returned — the guard then behaves as it did before the pre-pass existed.
_GUARD_CMDPOS_AWK='
function neut(t) { gsub(/[;&|()`<>\n!{}]/, "_", t); return t }
function tail(o) { return length(o) > 240 ? substr(o, length(o) - 239) : o }
function wrapper(o,   t) {
    t = tail(o)
    return (t ~ RE_SHELLC || t ~ RE_EVAL || t ~ RE_SSH || t ~ RE_HSTR)
}
function rescan(t,   r) {
    if (PD >= 4) return ";" t ";"
    PD++; r = scan(t, 1, 0); PD--
    return ";" r ";"
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
function scan(s, i, mode,    n, out, c, d, depth, dollar, nh, hdl, hq, ht, hx, k, j, w, q, line, sb, e, payload, ansi, dec, vis, kv, ch, h, cmp, dash, pc) {
    n = length(s); out = ""; depth = 0; dollar = 0; nh = 0
    while (i <= n) {
        c = substr(s, i, 1)
        if (c == "\\") {
            d = substr(s, i + 1, 1)
            if (d != "\n") out = out neut(d)
            dollar = 0; i += 2; continue
        }
        if (c == "\047") {
            ansi = dollar; dollar = 0
            payload = wrapper(out)
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
            if (payload) out = out rescan(dec)
            else if (KEEP) out = out neut(dec)
            else out = out "_"
            continue
        }
        if (c == "\"") {
            dollar = 0
            payload = wrapper(out)
            k = i + 1; dec = ""; vis = ""; kv = ""
            while (k <= n) {
                j = match(substr(s, k), /[\\"$`]/)
                if (j == 0) { ch = substr(s, k); dec = dec ch; kv = kv neut(ch); k = n + 1; break }
                if (j > 1) { ch = substr(s, k, j - 1); dec = dec ch; kv = kv neut(ch); k += j - 1 }
                ch = substr(s, k, 1)
                if (ch == "\\") {
                    d = substr(s, k + 1, 1)
                    if (d == "\n") { k += 2; continue }
                    if (index("\"\\$`", d)) { dec = dec d; kv = kv neut(d); k += 2; continue }
                    dec = dec ch; kv = kv ch; k++; continue
                }
                if (ch == "\"") break
                if ((ch == "$" && substr(s, k + 1, 1) == "(") || ch == "`") {
                    sb = (ch == "`") ? scan(s, k + 1, 2) : scan(s, k + 2, 1)
                    e = RPOS
                    dec = dec substr(s, k, e - k); vis = vis "(" sb ")"; kv = kv "(" sb ")"
                    k = e; continue
                }
                dec = dec ch; kv = kv ch; k++
            }
            i = k + 1
            if (payload) out = out rescan(dec)
            else if (KEEP) out = out kv
            else out = out "_" vis
            continue
        }
        if (c == "`") {
            if (mode == 2) { RPOS = i + 1; return out }
            sb = scan(s, i + 1, 2); out = out "(" sb ")"; i = RPOS; dollar = 0; continue
        }
        if (c == "(") { if (mode == 1) depth++; out = out c; i++; dollar = 0; continue }
        if (c == ")") {
            if (mode == 1) { if (depth == 0) { RPOS = i + 1; return out } depth-- }
            out = out c; i++; dollar = 0; continue
        }
        pc = (i == 1) ? "\n" : substr(s, i - 1, 1)
        if (c == "#" && index(" \t\n;&|()", pc)) {
            j = index(substr(s, i), "\n")
            i = (j == 0) ? n + 1 : i + j - 1
            continue
        }
        if (c == "<" && substr(s, i, 3) == "<<<") { out = out "<<<"; i += 3; dollar = 0; continue }
        if (c == "<" && substr(s, i + 1, 1) == "<") {
            k = i + 2; dash = 0
            if (substr(s, k, 1) == "-") { dash = 1; k++ }
            while (substr(s, k, 1) == " " || substr(s, k, 1) == "\t") k++
            w = ""; q = 0
            while (k <= n) {
                ch = substr(s, k, 1)
                if (index(" \t\n;&|()<>", ch)) break
                if (ch == "\047" || ch == "\"" || ch == "\\") { q = 1; k++; continue }
                w = w ch; k++
            }
            if (w != "") { nh++; hdl[nh] = w; hq[nh] = q; ht[nh] = dash; hx[nh] = (tail(out) ~ RE_FEED) }
            out = out "<<_"; i = k; dollar = 0; continue
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
        if (c != " " && c != "\t" && (pc == " " || pc == "\t") && tail(out) ~ RE_EVAL) out = out ";"
        out = out c; dollar = 0; i++
    }
    RPOS = n + 1
    return out
}
BEGIN {
    PD = 0
    B = "(^|[ \t\n;&|()!{}])"
    SHELLS = "([^ \t\n;&|()<>]*/)?(bash|sh|zsh|dash|ksh)"
    ARG = "[ \t]+[^ \t\n;&|()<>]+"
    RE_SHELLC = B SHELLS "(" ARG ")*[ \t]+-[A-Za-z]*c[A-Za-z]*[ \t]+$"
    RE_EVAL = B "eval[ \t]+$"
    RE_SSH = B "ssh(" ARG ")+[ \t]+$"
    RE_HSTR = B SHELLS "(" ARG ")*[ \t]*<<<[ \t]*$"
    RE_FEED = B "(" SHELLS "|ssh)(" ARG ")*[ \t]*$"
}
{ src = (NR == 1) ? $0 : src "\n" $0 }
END { printf "%s\n", scan(src, 1, 0) }
'

# command_positions [--words] <command>
# Prints the command-positions view of <command> (see above); with
# --words, quoted spans keep their words.
command_positions() {
    local keep=0 view
    if [ "$1" = "--words" ]; then
        keep=1
        shift
    fi
    view=$(printf '%s\n' "$1" | LC_ALL=C awk -v KEEP="$keep" "$_GUARD_CMDPOS_AWK" 2>/dev/null) || view="$1"
    printf '%s\n' "$view"
}

# guard_help_only <command-positions view> <verb regex>
# True when <verb regex> matches at least one invocation in the view and
# EVERY such invocation asks for help: a `--help` / `-h` token in its own
# segment (up to the next `;` `&` `|` `)` or newline), outside quotes
# (quoted spans are already blanked in the view), before any standalone
# `--` (after it, `-h` is a path or refspec), and not directly after
# another dash-option (`-m -h` hands `-h` to `-m` as its value, and the
# command runs). `… ; echo --help` therefore exempts nothing. Help is
# the only central exemption — dry-run forms stay per-hook.
guard_help_only() {
    local view="$1" verb_re="$2" inv tok prev ok found=0
    while IFS= read -r inv; do
        [ -n "$inv" ] || continue
        found=1
        inv="${inv%% -- *}"
        ok=0
        prev=""
        while IFS= read -r tok; do
            [ -n "$tok" ] || continue
            case "$tok" in
                --help | -h)
                    case "$prev" in -*) ;; *) ok=1 ;; esac
                    ;;
            esac
            prev="$tok"
        done <<< "$(printf '%s\n' "$inv" | tr -s ' \t' '\n\n')"
        [ "$ok" = 1 ] || return 1
    done <<< "$(printf '%s\n' "$view" | grep -oE -- "${verb_re}[^;&|)]*" || true)"
    [ "$found" = 1 ]
}
