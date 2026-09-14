#!/usr/bin/env bash
# Block `gh pr create` until the wrap-up is authored at the current HEAD.
#
# FOUNDATION §6 "PR finalization": verification precedes publication, and
# the wrap-up + squash message are AUTHORED before the PR exists — only
# their posting needs a PR. The evidence is code_health/pr_wrapup.md whose
# first lines carry `verified-at: <sha>`; this hook refuses PR creation
# when that file is missing or names a different commit, so a tree can't
# be published with verification (or its record) stale or skipped.
#
# The /pr skill's Step 3.92 writes the file. Two sanctioned bypasses:
#   - a human runs it directly:      ! gh pr create ...
#   - the USER asks to skip the gate: the agent prefixes the command with
#     FORGE_SKIP_WRAPUP_GATE=1 — only on an explicit user request, never
#     on the agent's own judgment.
#
# A wrap-up whose header declares `wrapup-mode: light` (the /pr
# light-code short form) triggers a THIRD check on top of the HEAD
# match: the hook re-runs forge-pr-plan against the PR's base and blocks
# unless the classifier itself says light-code — fail closed on a
# missing classifier, unresolvable base, or any disagreement. The light
# escape is earned at publish time, never taken on agent say-so.
set -e
INPUT=$(cat)
COMMAND=$(echo "$INPUT" | jq -r '.tool_input.command // empty')
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

# Resolve the checkout holding the branch being PUBLISHED, which is not
# necessarily the one this session sits in. A `cd` inside the command
# takes effect only after this hook runs, so the hook's own cwd is the
# session's — and judging that tree both blocks legitimate worktree
# publication AND passes a branch whose wrap-up was never written,
# whenever the session checkout happens to hold a valid one of its own.
#
# The branch name is read from the create invocation's own argument
# span, isolated the way block_protected_branches.sh isolates each push,
# so an agent-authored --title/--body cannot reach the extraction. The
# light path below refuses command text outright for its base ref; this
# is a deliberate, bounded relaxation of that stance, not an oversight:
# the value here must additionally match a live worktree, so a steered
# one can only ever select a checkout that exists and already holds a
# wrap-up naming its own HEAD — strictly weaker than the fail-open it
# replaces.
#
# All three spellings must parse — `--head x`, `--head=x` and the
# attached short form `-Hx` that pflag accepts. A form that fails to
# parse yields an empty branch, which falls through to session-checkout
# behaviour: silently the very hole this resolver closes, reopened for
# one syntax. Tests cover each form for that reason.
CREATE_SPAN=$(echo "$COMMAND" | grep -oE "${GH_ANCHOR}pr[[:space:]]+create\b[^;&|)]*" | head -1)
HEAD_TOKENS=$(printf '%s' "$CREATE_SPAN" \
    | grep -oE '(--head([[:space:]]+|=)|-H[[:space:]]*)[^[:space:]]+' \
    | sed -E 's/^(--head([[:space:]]+|=)|-H[[:space:]]*)//' | sort -u)
HEAD_COUNT=$(printf '%s' "$HEAD_TOKENS" | grep -c . || true)

# Ambiguity is refused, never resolved by picking one. This scan is
# quote-blind, while `gh` parses properly and takes the LAST repeat of a
# flag — so where the two readings differ, taking any single match means
# checking a branch `gh` will not publish. Two `--head` flags, or a
# `--head`-shaped token inside `--title` prose alongside a real flag,
# both produce disagreeing tokens here and both now block.
if [ "$HEAD_COUNT" -gt 1 ]; then
    echo "BLOCKED: the create command carries more than one --head value ($(printf '%s' "$HEAD_TOKENS" | tr '\n' ' ')). Which branch publishes cannot be determined from the command text, so the wrap-up cannot be checked against it. Issue one unambiguous --head." >&2
    exit 2
fi
PUBLISHED_BRANCH=$HEAD_TOKENS

REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null || echo ".")
SESSION_BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || true)

if [ -n "$PUBLISHED_BRANCH" ] && [ "$PUBLISHED_BRANCH" != "$SESSION_BRANCH" ]; then
    # Find the checkout holding it. Skip `prunable` entries (a worktree
    # whose gitdir is gone) and detached ones (no branch line at all).
    # Decide per RECORD, not per line: `git worktree list --porcelain`
    # emits `prunable` AFTER `branch`, so testing it while reading the
    # branch line always sees the previous record's value — the check
    # would never fire. Each `worktree` line therefore judges the record
    # that just ended, and END judges the last one. A prunable entry
    # (gitdir gone) and a detached one (no branch line) both fail the
    # match and are skipped.
    TARGET_ROOT=$(git worktree list --porcelain 2>/dev/null | awk -v want="refs/heads/$PUBLISHED_BRANCH" '
        function emit() { if (br == want && !pru) { print path; return 1 } return 0 }
        /^worktree /  { if (emit()) exit; path = substr($0, 10); br = ""; pru = 0; next }
        /^branch /    { br = substr($0, 8); next }
        /^prunable/   { pru = 1; next }
        END           { emit() }
    ')
    if [ -z "$TARGET_ROOT" ]; then
        # Never fall back to the session checkout: that fallback IS the
        # reported fail-open, publishing an unverified branch because
        # some other branch's wrap-up happened to be valid.
        echo "BLOCKED: --head names '$PUBLISHED_BRANCH', which no live worktree holds, so its wrap-up cannot be checked. Publish from a checkout of that branch, or drop --head to publish the current one." >&2
        exit 2
    fi
    REPO_ROOT="$TARGET_ROOT"
fi

WRAPUP="$REPO_ROOT/code_health/pr_wrapup.md"
if [ ! -f "$WRAPUP" ]; then
    echo "BLOCKED: no authored wrap-up at $WRAPUP. FOUNDATION §6: author the wrap-up (\`/pr\` Step 3.92) before creating the PR." >&2
    exit 2
fi

HEAD_SHA=$(git -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null || true)
if [ -z "$HEAD_SHA" ]; then
    exit 0  # not a git repo — nothing to verify against
fi

# shellcheck source=wrapup_anchor.sh
source "$(dirname "$0")/wrapup_anchor.sh"
if ! wrapup_names_head "$WRAPUP" "$HEAD_SHA"; then
    echo "BLOCKED: $WRAPUP does not name current HEAD (${HEAD_SHA:0:7}) in a verified-at: line — the wrap-up was authored for a different tree. Re-run /pr Step 3.92." >&2
    exit 2
fi

# EMERGENCY wrap-up (`wrapup-mode: emergency`): the one-shot
# deferred-verification bypass. The earn is the armed forge-emergency
# sentinel — consumed here, so exactly one PR publishes this way per
# `forge-emergency start` (which filed the public ledger issue first).
# The verified-at HEAD match above still applied: traceability is never
# deferred, only verification. Fail CLOSED: missing CLI or no armed
# sentinel (never started, expired, already spent) blocks.
if grep -qE '^wrapup-mode:[[:space:]]*emergency[[:space:]]*$' "$WRAPUP"; then
    if ! command -v forge-emergency >/dev/null 2>&1; then
        echo "BLOCKED: wrap-up declares wrapup-mode: emergency but forge-emergency is not on PATH — install forge-scripts or author the full wrap-up." >&2
        exit 2
    fi
    if (cd "$REPO_ROOT" && forge-emergency consume >&2); then
        echo "NOTE: EMERGENCY bypass consumed — this PR publishes without verification; retro-verification is owed on the ledger issue." >&2
        exit 0
    fi
    echo "BLOCKED: wrapup-mode: emergency but no armed bypass (not started, expired, or already spent). A human arms one with \`forge-emergency start --reason ...\` — agents only on explicit user instruction." >&2
    exit 2
fi

# LIGHT wrap-up (`wrapup-mode: light` ANYWHERE in the file — a scan
# window would be dodgeable by formatting): the short-form wrap-up skips
# the reporter round, so the escape must be EARNED at publish time,
# never taken on agent say-so. The hook re-runs the deterministic
# classifier (forge-pr-plan) and blocks unless it agrees the diff is
# light-code. Fail CLOSED throughout: a missing classifier, missing
# branch config, or ANY classifier/parse error yields an empty MODE and
# blocks. The base ref comes ONLY from [tool.forge] config — never from
# the command text, which carries agent-authored --title/--body strings
# a naive extraction could be steered by. Staleness note: the diff is
# classified against the locally cached origin/<base>; a stale
# remote-tracking ref shifts the classified diff, an accepted TOCTOU
# residual (the reviewer and CI judge the true merge result).
if grep -qE '^wrapup-mode:[[:space:]]*light[[:space:]]*$' "$WRAPUP"; then
    if ! command -v forge-pr-plan >/dev/null 2>&1; then
        echo "BLOCKED: wrap-up declares wrapup-mode: light but forge-pr-plan is not on PATH to verify it — install forge-scripts or author the full wrap-up." >&2
        exit 2
    fi
    # [tool.forge] config only (base_branch) — same python3 heredoc
    # pattern as block_protected_branches.
    # pyproject.toml is itself high-blast-radius, so a light-eligible PR
    # cannot have poisoned it.
    BASE=$(python3 - "$REPO_ROOT" 2>/dev/null <<'PY'
import sys, tomllib, pathlib
try:
    cfg = tomllib.loads((pathlib.Path(sys.argv[1]) / "pyproject.toml").read_text())
    forge = cfg.get("tool", {}).get("forge", {})
    print(forge.get("base_branch") or "")
except Exception:
    print("")
PY
    )
    if [ -z "$BASE" ]; then
        echo "BLOCKED: wrapup-mode: light but no [tool.forge] base_branch config resolves the base ref — author the full wrap-up." >&2
        exit 2
    fi
    # Guarded assignment: under `set -e` a bare failing pipeline would
    # kill the hook at exit 1 — NOT a block signal — before the mode
    # check ran, silently failing OPEN. The `if !` wrapper absorbs any
    # classifier/parse failure into an empty MODE, which blocks below.
    if ! MODE=$(cd "$REPO_ROOT" && forge-pr-plan --base "origin/$BASE" 2>/dev/null \
        | python3 -c 'import json,sys; print(json.load(sys.stdin).get("mode",""))' 2>/dev/null); then
        MODE=""
    fi
    if [ "$MODE" != "light-code" ]; then
        echo "BLOCKED: wrap-up declares wrapup-mode: light but forge-pr-plan classifies this diff as '${MODE:-unclassifiable}' against origin/$BASE — the light escape is not earned. Author the full wrap-up (/pr Step 3.92)." >&2
        exit 2
    fi
fi

exit 0
