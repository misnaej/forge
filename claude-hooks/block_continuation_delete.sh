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
# (issue bodies, commit messages) from firing. Known accepted
# slip-through: interpreter one-liners (`python -c "...unlink(...)"`) —
# same stance as block_raw_git.sh's `bash -c` note.
RM_ANCHOR='(^|[;&|(])[[:space:]]*(([[:alnum:]_]+=[^[:space:]]+|command|env|exec|builtin|sudo|xargs)[[:space:]]+)*(rm|unlink)([[:space:]]|$)'
if ! echo "$COMMAND" | grep -qE "${RM_ANCHOR}"; then
    exit 0
fi
# Target: CONTINUATION.md itself, or the whole .plan directory. The
# directory match requires the path to END at `.plan` (optional trailing
# slash) — `.plan/<sibling>` does not match, and a preceding-boundary
# class keeps `foo.plan` out.
if echo "$COMMAND" | grep -qE "CONTINUATION\.md|(^|[[:space:]\"'=(/])\.plan/?([[:space:];&|)\"'\`]|$)"; then
    echo "BLOCKED: refusing to delete .plan/CONTINUATION.md — it is the only file that carries state across a context clear (FOUNDATION §10). Rewrite its sections in place instead of deleting it (see /next Phase 6)." >&2
    exit 2
fi
