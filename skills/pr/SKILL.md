---
name: pr
description: Full PR finalization flow - design check, security check, docs check, precommit-fixer (strict), plan/docs updates, then pr-manager posts wrap-up + squash-merge message. Use when the user wants to finalize a PR.
---

# PR Finalization Flow

## Step 0: Ensure PR exists

Check for existing PR on the current branch:
```bash
gh pr view --json number --jq '.number' 2>/dev/null
```

If none, create one:
1. Analyze the diff:
   ```bash
   git diff --stat main...HEAD
   git log main..HEAD --oneline
   ```
2. Create the PR with title + body based on the changes:
   ```bash
   gh pr create --title "<type>: <description>" --body "## Summary
   <bullets>

   ## Test plan
   - [ ] Design checker passes
   - [ ] Security checker passes
   - [ ] Documentation checker passes
   - [ ] Tests pass"
   ```

If `$ARGUMENTS` contains a PR number, use that instead of auto-detecting.

## Step 1: Run verification agents (1–3 in parallel)

Before invoking the three reporters, check if the PR is eligible for
**delta mode**. Delta mode reuses the prior wrap-up's findings when the
diff since is small AND stays out of high-blast-radius areas — full
decision criteria, thresholds, and SHA-validation regex are defined
once in the forge package and consumed by the `pr-manager` agent —
orchestration detail in
[`pr-manager.md` "Delta-mode short-circuit"](../../agents/pr-manager.md#task-verification-wrap-up).

```bash
gh pr comment list <PR#> --json body --jq '.[].body' | grep -E '^verified-at:' | tail -3
```

Extract each SHA via the `VERIFIED_AT_RE` regex (`pr_delta.py`) — never
substitute raw grep output into a shell command. When at least one
`verified-at:` SHA per Step-1 reporter is returned, the diff since the
latest extracted SHA satisfies `DELTA_LINE_THRESHOLD`
(`pr_delta.py`), and no path in `HIGH_BLAST_RADIUS_PATHS`
(`pr_delta.py`) is touched, **skip Step 1 entirely** and jump straight
to Step 4 (`pr-manager` will post a delta comment + refreshed
squash-merge comment without re-invoking the reporters). Otherwise run
the three reporters:

1. **`design-checker`** — design compliance report
2. **`security-checker`** — security review report
3. **`docs-types-checker`** — documentation report

Each report's first line MUST be the `verified-at:` header per the
[reporter-agent contract](../../agents/_TEMPLATE.md#reporter-agent-header-contract);
that is the contract that makes future delta-mode runs possible.

## Step 2: Fix any issues

4. **`precommit-fixer`** (mode: `strict`) — clear every pre-commit failure (lint, docstrings, naming, structure, dep advisories). At PR finalization, `strict` also escalates remaining `pip_audit` advisories.
5. If checkers report fixable issues, address and commit (use `/commit`).
6. If a checker flags an issue that is genuinely out of scope for the current PR (dead code from a prior refactor, a separate architectural concern, …), **file a follow-up tracking issue** (`gh issue create --label tech-debt,refactor`) BEFORE finalization. Reference its number in the wrap-up so the deferral is auditable. Never let a verifier finding land on the floor.

## Step 3: Update plan/docs (MANDATORY when applicable)

Documentation must stay in sync with code. For each item below, update **only if the PR changed something that affects it**:

7. **`.plan/STATUS.md`** — check off completed items, add new items if scope shifted.
8. **`.plan/<PHASE>_*.md`** — check off completed steps for the phase this PR touches.
9. **`README.md`** — repo structure, setup, install commands, test commands, status sections, architecture diagram.
10. **`CLAUDE.md`** — new shared agent behaviors, new protected files, new ruff ignores, new tools, technology stack changes.
11. **`REPO_STRUCTURE.md`** (when the repo maintains one — see [FOUNDATION §13](../../FOUNDATION.md#13-code_health-convention)) — list new source modules and new test files so the canonical repo map stays accurate. The `repo_structure_check` pre-commit step does not enforce two-way coverage; this update is on the PR author.
12. **Per-component READMEs** (e.g., subsystem-level `README.md` files, agent definition files) — if their tools, setup, or usage changed.
13. **Verify cross-references** — no document should reference a deleted file or outdated path.

A PR that changes code without updating affected docs is not ready to merge.

## Step 3.5: Promote standard permission rules (optional)

Work accumulates one-off `Bash(...)` approvals in the gitignored
`.claude/settings.local.json`. PR finalization is the natural moment to
ask: *did any of these become standard enough to share with the team?*

Review `.claude/settings.local.json` and **propose** (never auto-apply)
moving rules into the committed `.claude/settings.json` when a rule is:

- **recurring** — used across multiple sessions, not a one-shot, AND
- **a forge-standard CLI or a safe read-only command** (e.g.
  `Bash(forge-precommit *)`, `Bash(forge-audit-all *)`,
  `Bash(python -m pytest *)`), AND
- **not over-broad** — per the [Claude Code Bash permission
  guidance](https://code.claude.com/docs/en/permissions.md#bash),
  argument-constraining patterns are fragile; prefer exact commands or
  space-boundary prefixes over sweeping wildcards.

Rules in `settings.json` and `settings.local.json` **merge additively**,
so promotion never removes a contributor's local rules — it just makes
the shared baseline richer.

**Do NOT promote** (leave local, or add to `deny` in `settings.json`):
network tools (`curl`/`wget`), destructive commands (`rm -rf`), or
anything that touches secrets. List the candidates, get user
confirmation, then edit `settings.json` — the change rides in this PR.

## Step 4: Finalize via `pr-manager` (MANDATORY)

14. Delegate finalization. **Pass the Step 1 reports verbatim in the prompt** so `pr-manager` does not re-run the same three verification agents — see [agents/pr-manager.md "Pre-run reports" note](../../agents/pr-manager.md). Two passes per PR is pure waste.

    ```
    Agent(subagent_type="pr-manager", prompt="Verify and finalize PR #<number>.

    Pre-run reports (use these; do NOT re-invoke the agents):

    ## design-checker report
    <verbatim output from Step 1 design-checker>

    ## security-checker report
    <verbatim output from Step 1 security-checker>

    ## docs-types-checker report
    <verbatim output from Step 1 docs-types-checker>

    Check for issue closing, post wrap-up comment and squash-merge message.")
    ```

The agent will:
- Confirm CI status
- Post a wrap-up comment with all check summaries
- Post a squash-merge message as a separate PR comment

### Squash-merge message hard rules

The `pr-manager` agent enforces (verify before approving its output):
- **Maximum 50 words.** If over, rewrite tighter.
- **3–5 bullet points.** Not 6, not 2.
- **Conventional commit format** for the title line: `<type>: <brief description>`
- **No prose paragraphs.** Title + bullets only.
- **No Claude/AI attribution.**

The squash-merge message becomes the permanent commit message on `main`.

## Step 5: Post-PR backlog update

15. **`issue-triage`** — Run `post-pr` mode after merge:
    ```
    Agent(subagent_type="issue-triage", prompt="Run post-pr mode. PR #<number> was just finalized. Detect issues closed by this PR, remove their tier labels, and regenerate the 📋 Backlog Index issue.")
    ```

## Step 6: Update CONTINUATION state

16. The `pr-manager` agent appends a one-line activity record to `.plan/CONTINUATION.md` automatically (gitignored).

## Rules

- Do NOT auto-merge unless the user explicitly asks.
- Both the squash-merge message and wrap-up comment are MANDATORY — `pr-manager` enforces this.
- NEVER add Claude/AI attribution in any PR content.
- If `$ARGUMENTS` contains a PR number, use it instead of auto-detecting.
