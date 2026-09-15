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

# Command-position anchor, adapted from the family idiom in
# block_raw_git.sh / block_force_push.sh / block_git_rebase.sh with ONE
# deliberate addition: `xargs` in the wrapper list. The git hooks accept
# the xargs slip-through (block_install_deps.sh stance); here a piped
# `find .plan | xargs rm` was blocked before this anchor existed and
# deletion is irreversible, so dropping that coverage would be a silent
# safety regression. The anchor keeps `rm`/`unlink` quoted inside prose
# (issue bodies, commit messages) from firing. Accepted slip-throughs:
# the same command-position-anchor limitations as the rest of the family
# (shell keywords, eval/trap/nohup/time wrappers, backtick substitution,
# indirect variables, interpreter one-liners) — a guardrail against
# honest mistakes, not an adversarial boundary.
# The wrapper group also tolerates flag tokens (`xargs -0 rm`,
# `sudo -n rm`) — without it, any flag breaks the wrapper chain and the
# verb escapes the anchor.
RM_ANCHOR='(^|[;&|(])[[:space:]]*(([[:alnum:]_]+=[^[:space:]]+|command|env|exec|builtin|sudo|xargs|-[^[:space:]]+)[[:space:]]+)*(rm|unlink)([[:space:]]|$)'
if ! echo "$COMMAND" | grep -qE "${RM_ANCHOR}"; then
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
# Chunks split on `;`, `&` and newline only. `)` and `#` are deliberately
# NOT separators: a command substitution or trailing comment would then
# split a target away from its verb, which is the bypass described above.
# Nor is `|`: a pipeline is one unit of work, and `find .plan | xargs rm`
# carries its target upstream of the verb — splitting there drops the
# `find` half and reopens the xargs coverage the header calls
# load-bearing. For the same reason nothing is truncated at `--`: unlike
# git's pathspec separator, `rm --` is followed by the targets themselves.
#
# Known gap, unchanged by this shaping and predating it: a *directory*
# delete built through command substitution (`rm -rf $(true).plan/`)
# matches neither form, since the literal text never spells a bare
# `.plan` terminator. Telling that apart needs shell parsing, which this
# hook family has declined.
plan_dir_in_delete_chunk() {
    local chunk
    while IFS= read -r chunk; do
        [ -n "$chunk" ] || continue
        echo "$chunk" | grep -qE "$RM_ANCHOR" || continue
        echo "$chunk" | grep -qE "$PLAN_DIR_RE" && return 0
    done <<< "$(echo "$COMMAND" | tr ';&\n' '\n\n\n')"
    return 1
}
if echo "$COMMAND" | grep -qE "$CONTINUATION_RE" || plan_dir_in_delete_chunk; then
    echo "BLOCKED: refusing to delete .plan/CONTINUATION.md — it is the only file that carries state across a context clear (FOUNDATION §10). Rewrite its sections in place instead of deleting it (see /next Phase 6)." >&2
    exit 2
fi
