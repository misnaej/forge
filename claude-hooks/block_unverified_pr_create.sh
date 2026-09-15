#!/usr/bin/env bash
# PreToolUse(Bash): a pull request is published only through forge-pr-create.
#
# FOUNDATION §6 — verification precedes publication. The evidence is
# code_health/pr_wrapup.md, whose `verified-at:` header names the tree it
# verified, and the question this guard exists to answer is whether that
# evidence covers the branch about to be published.
#
# This guard used to answer that itself, by reading the `gh pr create`
# command and inferring the branch from its text. That cannot be made
# reliable, and review proved it: the command carries free-form --title
# and --body prose in which anything can look like a flag, a repeated
# flag resolves the opposite way in a linear scan than in a real parser,
# and this hook runs in the session's checkout, which need not be the one
# holding the branch. Two working steering proofs each published a branch
# with no wrap-up at all.
#
# So the inference is gone rather than improved. `forge-pr-create` runs in
# the checkout it publishes — the branch is where the process stands, not
# something parsed — and it verifies that checkout's wrap-up against that
# checkout's HEAD before creating anything. This guard is left with a
# question about the command's NAME, which needs no understanding of its
# arguments and cannot be steered by prose inside them.
#
# Same shape as block_raw_wrapup_post / block_raw_git / block_raw_ruff: the
# work moves behind a forge CLI and the raw form is blocked at the cause. A
# hook sees what an agent types, never what a forge CLI does inside itself,
# so forge-pr-create's own `gh` call is not intercepted here.
#
# Bypasses, unchanged in spirit:
#   - a human runs it directly:       ! gh pr create ...
#   - the USER asks to skip the gate: FORGE_SKIP_WRAPUP_GATE=1 prefixing
#     the command — only on explicit user request, never on the agent's
#     own judgment.
set -e
INPUT=$(cat)
COMMAND=$(echo "$INPUT" | jq -r '.tool_input.command // empty')
[ -n "$COMMAND" ] || exit 0

# Anchors + their rationale live in the shared lib (one home for the
# whole git-/gh-guard family).
ANCHOR_LIB="$(dirname "$0")/git_anchor.sh"
if [ ! -r "$ANCHOR_LIB" ]; then
    # Fail CLOSED: a missing/unreadable lib (corrupted plugin cache) must
    # block, not silently disarm the guard — only exit 2 blocks in the
    # PreToolUse contract.
    echo "BLOCKED: guard anchor lib missing at $ANCHOR_LIB — refusing the command rather than running unguarded." >&2
    exit 2
fi
source "$ANCHOR_LIB"

# `gh pr create` at start-of-string or after a shell separator — same
# matching convention as block_pr_merge.sh (a plain space ahead of `gh`
# is not a separator, letting text mentions through).
if ! echo "$COMMAND" | grep -qE "${GH_ANCHOR}pr[[:space:]]+create\b"; then
    exit 0
fi

# The embedded skip form must sit at command position, directly prefixing
# the create invocation — a free-text mention (e.g. in a --title/--body
# that discusses this hook) must NOT trip the bypass.
if echo "$COMMAND" | grep -qE '(^|[;&|(])[[:space:]]*FORGE_SKIP_WRAPUP_GATE=1[[:space:]]+gh[[:space:]]+pr[[:space:]]+create\b' \
    || [ "${FORGE_SKIP_WRAPUP_GATE:-}" = "1" ]; then
    exit 0
fi

echo "BLOCKED: publish with \`forge-pr-create --base <base> --title <title> --body-file <file>\` (add --draft for a draft), not a raw \`gh pr create\`." >&2
echo "It runs in the checkout it publishes, so it verifies that branch's own wrap-up against that branch's own HEAD. A raw create forces this guard to infer the branch from your command text, which review showed can be steered into approving an unverified branch." >&2
exit 2
