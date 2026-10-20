#!/usr/bin/env bash
# Block shell deletion of .plan/CONTINUATION.md — the only file that carries
# state across a context clear (FOUNDATION §10). Edits via the Write/Edit
# tools are unaffected. Other files inside .plan/ (weekly summaries, goal
# files) are deliberately deletable — only CONTINUATION.md itself, or the
# .plan directory as a whole (which would take CONTINUATION.md with it),
# is protected.
set -e
INPUT=$(cat)
COMMAND=$(jq -r '.tool_input.command // empty' <<< "$INPUT")
# Anchors and the command-positions views live in the shared lib (one home
# for every guard that locates a command).
ANCHOR_LIB="$(dirname "$0")/git_anchor.sh"
if [ ! -r "$ANCHOR_LIB" ]; then
    # Fail CLOSED: a missing/unreadable lib (corrupted plugin cache) must
    # block, not silently disarm the guard — only exit 2 blocks in the
    # PreToolUse contract.
    echo "BLOCKED: guard anchor lib missing at $ANCHOR_LIB — refusing the command rather than running unguarded." >&2
    exit 2
fi
source "$ANCHOR_LIB"
# Fail CLOSED on a lib that predates a helper this hook calls.
if ! declare -F guard_begin command_positions guard_unredir guard_chunks > /dev/null; then
    echo "BLOCKED: the guard anchor lib at $ANCHOR_LIB lacks a helper this hook needs (older or damaged plugin copy) — refusing the command rather than running unguarded." >&2
    exit 2
fi
guard_begin

# Command-position anchor: the shared command-prefix run every guard uses
# (_GUARD_PREFIX in git_anchor.sh) in front of `rm` / `unlink`, so a word
# learned there is learned here — `VAR=val`, wrappers (`sudo`, `env`,
# `nice`, `time`, `nohup`, `timeout 5`, `xargs`, …) with their options
# and an option's value (`sudo -u root rm`, `xargs -0 rm`), compound
# keywords, and redirection words (`2>&1 rm …`). `xargs` matters most
# here: a piped `find .plan | xargs rm` carries its target upstream of the
# verb, and deletion is irreversible. `rm` or a wrapper launched by path
# (`/bin/rm`, `/usr/bin/sudo rm`) is still `rm` (GUARD_BINPATH). The
# delete is found on the shared command-positions view (git_anchor.sh), so
# `rm` quoted in prose (issue bodies, commit messages) never fires while
# `bash -c "rm …"`, `eval rm …` and `find … -exec rm …` do.
# Accepted slip-throughs — a guardrail against honest mistakes, not an
# adversarial boundary: the command-position limits of the whole family
# (indirect variables, interpreter one-liners), and deletes that do not
# run `rm` or `unlink` at all — `busybox rm`, `find … -delete`, `shred`,
# `truncate`, and emptying the file with a redirection (`: > …`).
RM_ANCHOR='(^|[;&|(])[[:space:]]*'"${_GUARD_PREFIX}${GUARD_BINPATH}"'(rm|unlink)([[:space:]]|$)'
if ! command_positions "$COMMAND" | grep -qE "${RM_ANCHOR}"; then
    exit 0
fi
# Target: CONTINUATION.md itself, or the whole .plan directory. The
# directory match requires the path to END at `.plan` — optionally with
# a trailing slash, glob (`.plan/*`, `.plan*`, `.plan?`, `.plan/{a,b}`),
# `/.` or `//` form, all of which expand to the directory's contents.
# A named sibling (`.plan/weekly_summary.md`) does not match, and a
# preceding-boundary class keeps `foo.plan` out. Ambiguity errs toward
# blocking: `.plan/.` also matches dotfile siblings (`.plan/.gitkeep`) —
# rare, and the safe direction for an irreversible delete.
# Two targets, two scopes, because they carry different risk.
CONTINUATION_RE="CONTINUATION\.md"
PLAN_DIR_RE="(^|[[:space:]\"'=(/\`])\.plan(/+([[:space:];&|)\"'\`*?{.]|$)|[[:space:];&|)\"'\`*?{]|$)"

# CONTINUATION.md is matched against the WHOLE command. It is the one
# file this hook exists for, and the two costs are not symmetric: a false
# positive costs a retry, a miss is unrecoverable. Narrowing this half to
# the verb's own chunk is what let `rm $(true).plan/CONTINUATION.md` and
# `f=.plan/CONTINUATION.md; rm $f` through — the separator landed between
# the verb and its target, and neither piece tripped both tests alone.
#
# The directory half IS matched per chunk, because that is the half that
# over-blocked: a whole-line match pairs any `rm` with any `.plan` token
# anywhere else in the line, so `rm -f .plan/note.md && ls -A .plan/`
# would be refused — a delete of a named sibling the header above calls
# deletable, stopped because a read-only `ls` supplied the shape.
#
# Chunks split on `;`, `&` and newline only (the charset passed to
# guard_chunks is `;&`; a newline always ends a chunk there). A splitter
# failure counts as a delete chunk, which blocks. `)` and `#` are deliberately
# NOT separators: a command substitution or trailing comment would then
# split a target away from its verb, which is the bypass described above.
# Nor is `|`: a pipeline is one unit of work, and `find .plan | xargs rm`
# carries its target upstream of the verb — splitting there drops the
# `find` half and reopens the xargs coverage the header calls
# load-bearing. For the same reason nothing is truncated at `--`: unlike
# git's pathspec separator, `rm --` is followed by the targets themselves.
# Chunks come from the --words view: a separator inside a quoted path
# never splits it, and a quoted `.plan` is still the target `rm` receives.
# Redirections are blanked first (guard_unredir), so the `&` of `2>&1`
# never cuts a verb away from the redirection that precedes it.
#
# Known gap, unchanged by this shaping and predating it: a *directory*
# delete built through command substitution (`rm -rf $(true).plan/`)
# matches neither form, since the literal text never spells a bare
# `.plan` terminator. Telling that apart needs shell parsing, which this
# hook family has declined.
plan_dir_in_delete_chunk() {
    local chunk chunks
    chunks=$(guard_chunks ';&' "$(guard_unredir "$(command_positions --words "$COMMAND")")") || return 0
    while IFS= read -r chunk; do
        [ -n "$chunk" ] || continue
        echo "$chunk" | grep -qE "$RM_ANCHOR" || continue
        echo "$chunk" | grep -qE "$PLAN_DIR_RE" && return 0
    done <<< "$chunks"
    return 1
}
if echo "$COMMAND" | grep -qE "$CONTINUATION_RE" || plan_dir_in_delete_chunk; then
    echo "BLOCKED: refusing to delete .plan/CONTINUATION.md — it is the only file that carries state across a context clear (FOUNDATION §10). Refresh its status panel with \`forge-continuation state\` and rewrite its written section in place instead of deleting it (see /next Phase 6)." >&2
    exit 2
fi
