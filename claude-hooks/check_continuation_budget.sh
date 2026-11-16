#!/usr/bin/env bash
# PostToolUse(Write|Edit): after a write to .plan/CONTINUATION.md, print the
# written section's budget check (FOUNDATION §10) so the agent sees the usage
# at the moment it is written. Advisory only — every path exits 0: a blocked
# write of the handoff note is worse than a fat note, so this hook reports,
# never gates. Registered in plugin.json as separate "Write"/"Edit" matcher
# groups (a combined "Write|Edit" matcher would be misread as an agent regex
# by gen_vibe_hooks and mapped to match every post_tool event); on Vibe they
# become post_tool write_file/edit via forge-gen-vibe-hooks.
set -uo pipefail

command -v jq >/dev/null 2>&1 || exit 0
INPUT=$(cat)
FILE_PATH=$(jq -r '.tool_input.file_path // empty' <<< "$INPUT")
case "$FILE_PATH" in
    .plan/CONTINUATION.md|*/.plan/CONTINUATION.md) ;;
    *) exit 0 ;;
esac

ROOT=${CLAUDE_PROJECT_DIR:-}
if [ -z "$ROOT" ] || [ ! -e "$ROOT/.git" ]; then
    CWD=$(jq -r '.cwd // "."' <<< "$INPUT")
    ROOT=$(git -C "$CWD" rev-parse --show-toplevel 2>/dev/null) || exit 0
fi
command -v forge-continuation >/dev/null 2>&1 || exit 0

(cd "$ROOT" && forge-continuation check) || true
exit 0
