#!/usr/bin/env bash
# Block Claude/AI attribution anywhere user-supplied prose lands in
# permanent git/GitHub history. Covers:
#   - git commit (commit messages)
#   - gh pr create / edit / comment / review (PR body, comments, reviews)
#   - gh issue create / edit / comment (issue body / comments)
#   - gh release create / edit (release notes)
#   - gh api ... (catches direct REST POSTs to PR/issue/release endpoints)
set -e
INPUT=$(cat)
COMMAND=$(echo "$INPUT" | jq -r '.tool_input.command // empty')
if ! echo "$COMMAND" | grep -qE '^(git commit|gh (pr|issue|release) (create|edit|comment|review)|gh api )'; then
    exit 0
fi
# FORGE_ATTRIBUTION_PATTERNS_BEGIN — managed by `forge-gen-attribution-patterns`.
# Mirror of forge.pr_squash_comment.AI_ATTRIBUTION_PATTERNS (FOUNDATION §12,
# same mechanism as FORGE_COMMIT_TYPES). Only the phrase list is mirrored —
# the Python validator's bare-vendor-token backstop stays Python-only (its
# citable-path exemption has no shell equivalent; bare tokens here would
# re-block legitimate CLAUDE.md / .claude/ path mentions). The phrases carry
# the same over-block bias as the Python side: "generated with care" is
# blocked and a human rewords or uses the `!` escape hatch.
ATTRIBUTION_PATTERNS='co\-authored\-by:|🤖|generated\ with|generated\ by|assisted\ by\ ai|ai\-generated|with\ claude\ code|authored\ by\ claude'
# FORGE_ATTRIBUTION_PATTERNS_END
if echo "$COMMAND" | grep -qiE "$ATTRIBUTION_PATTERNS"; then
    echo "BLOCKED: Claude attribution is forbidden in commits and PRs. Remove any Co-Authored-By, 'Generated with Claude', or AI references." >&2
    exit 2
fi
