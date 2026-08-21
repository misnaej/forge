#!/usr/bin/env bash
# Block agent edits inside the forge-managed forge-docs/ mirror.
#
# In consumer repos, forge-docs/ is a verbatim mirror of forge's shipped
# reference set (the pages FOUNDATION.md links to), refreshed by
# install-forge-claude-md on upgrades. Any local edit is overwritten on
# the next sync, so an agent write there is always wasted work — and a
# hand-tuned copy would silently diverge from the shipped rules. The
# folder's README carries the human-facing notice; this hook is the
# agent-facing enforcement (same pattern as block_protected_files).
#
# In forge's own repo the canonical files live in forge-docs/ too — but
# there they are the SOURCE, edited via normal PRs. The self test:
# forge's repo ships the package data as symlinks into forge-docs/, so
# src/forge/data/docs/ exists alongside. When that directory is present
# in the repo the hook stands down (forge-only escape; consumer repos
# never carry src/forge/data/).
set -e
INPUT=$(cat)
FILE_PATH=$(jq -r '.tool_input.file_path // empty' <<< "$INPUT")

# Segment-anchored matching AND root extraction: only the literal
# forge-docs/ path component counts, never a substring inside another
# directory name (my-forge-docs-notes/ stays editable).
MATCHED=0
case "$FILE_PATH" in
    forge-docs/*)
        ROOT=""; MATCHED=1 ;;
    */forge-docs/*)
        ROOT="${FILE_PATH%%/forge-docs/*}/"; MATCHED=1 ;;
esac
if [ "$MATCHED" = "1" ]; then
    if [ -d "${ROOT}src/forge/data/docs" ]; then
        exit 0  # forge's own repo: forge-docs/ is canonical source
    fi
    echo "BLOCKED: $FILE_PATH is inside forge-docs/, a forge-managed mirror refreshed by install-forge-claude-md — local edits are overwritten on the next sync. Change the source in the forge repository instead (see forge-docs/README.md)." >&2
    exit 2
fi
