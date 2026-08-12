---
name: pr
description: Full PR finalization flow, verification-first - design check, security check, docs check, precommit-fixer (strict), plan/docs updates, then publish the PR and have pr-manager post wrap-up + squash-merge message. Use when the user wants to finalize a PR.
---

# PR Finalization Flow

**Entry is automatic, not invitation-only**: per FOUNDATION §6
"Verification starts itself", enter this flow as soon as a branch's
implementation commits are done — never idle at "ready to finalize?"
with verification unrun.

## Step 0: Detect the PR — defer creation until after verification

Check for an existing PR on the current branch:
```bash
gh pr view --json number --jq '.number' 2>/dev/null
```

If `$ARGUMENTS` contains a PR number, use that instead of auto-detecting. Then:

- **PR exists** → proceed; Steps 1–3 verify the local tree, and fixes land as
  commits on the branch.
- **No PR yet** → do **NOT** create it here. Per FOUNDATION §6 "PR
  finalization", verification runs first against the tree about to be pushed,
  so findings are fixed in the PR's own commits and the CHANGELOG version
  heading settles before the branch is published. The PR is created in
  Step 3.95.
- **Draft escape hatch** — the user wants the PR visible now → create it as a
  draft (`gh pr create --draft`, body per the Step 3.95 template) and mark it
  ready in Step 3.95. Drafts do not consume CI: the shipped workflow
  ([`docs/ci-recipe.md`](../../docs/ci-recipe.md)) skips draft PRs and runs
  on `ready_for_review`.

## Step 0.5: Base-sync gate

A PR that is behind — or in conflict with — its base branch is not
ready to finalize: a green CI run on a stale base does not mean the PR
merges cleanly now (parallel PRs may have taken the version, edited the
CHANGELOG, or moved the merge-base). **Check before reviewing, never
silently merge.** Which variant depends on Step 0:

- **PR exists** (`gh`-based):
  ```bash
  git fetch origin --quiet
  gh pr view <PR#> --json baseRefName,mergeable,mergeStateStatus \
    --jq '{base: .baseRefName, mergeable, state: .mergeStateStatus}'
  git rev-list --left-right --count "origin/<base>...HEAD"   # left = behind
  ```
- **No PR yet** (git-only — there is nothing for `gh` to inspect):
  ```bash
  git fetch origin --quiet
  git rev-list --left-right --count "origin/<base>...HEAD"   # left = behind
  git merge-tree --write-tree "origin/<base>" HEAD >/dev/null \
    || echo CONFLICTING
  ```
  `<base>` is the branch the PR will target (`[tool.forge].dev_branch` when
  set and the branch is feature work, else the repo default branch).

- **`mergeable: CONFLICTING`** → **stop**. Do not finalize. Resolve by
  merging the base in (`git merge origin/<base>`, resolve conflicts —
  CHANGELOG per `docs/release-process.md` §5), then re-run from Step 0.5.
- **Behind but clean** (left count > 0, not conflicting) → merging the
  base in is **confirm-first** (never silent): it refreshes the branch so
  verification reflects the real merge result, but it mutates history and
  re-triggers CI, so surface it and let the user decide.
- **Up to date, `MERGEABLE`** → proceed to Step 1.

**Stranded-changelog check** (single-track repos with a root
`CHANGELOG.md`): a release tag cut while this PR was open leaves the
PR's entries under an already-released heading — nothing conflicts, so
neither git nor the checks above notice. After the fetch, run
`forge-precommit --only changelog_version` (self-skips on repos where
the convention doesn't apply). A `stranded` finding means: bump the top
`## vX.Y.Z` heading to the next version, move this PR's entries under
it, commit, and re-run from Step 0.5.

Merging the base re-triggers CI. Do **not** wait for it: per FOUNDATION §6
"PR finalization", the wrap-up never blocks on CI — it posts as soon as the
checks are done and states CI's status plainly.

## Step 1: Run verification agents (1–3 in parallel)

**Docs-only light path — check FIRST, before delta mode.** Classify the
PR diff with `pr_delta.docs_only_diff` (`git diff --name-only
origin/<base>...HEAD` against the built-in `DOCS_ONLY_GLOBS` plus
`pr_delta.configured_docs_only_globs(repo_root)`, the reader for
`[tool.forge.pr].docs_only_globs`; any high-blast-radius path —
`agents/`, `skills/`, `claude-hooks/`, `.claude-plugin/`, configs —
disqualifies, since doc-shaped files there ARE shipped behavior). When
the diff is docs-only:

- **Skip `design-checker` and `security-checker`** (no code surface to
  review) — run **only `docs-types-checker`** (the docs are the diff).
- **Step 2 runs targeted gates, not the strict whole-tree battery**:
  `forge-precommit --only changelog_version,changelog_updated,doc_consistency`
  (comma-list; add other path-relevant steps as applicable). No
  `pip_audit`, whole-tree ruff, or docstring-coverage — nothing in-scope
  changed.
- Steps 3–4 run as normal (docs sync is the whole point; wrap-up +
  squash message stay MANDATORY). Tell `pr-manager` the PR took the
  docs-only path so the wrap-up says so.
- Accepted residuals (documented in `pr_delta.docs_only_diff`): the
  classifier sees path strings only — symlinked doc files and
  injection-shaped prose are not detected; docs-types-checker plus the
  human PR review remain the reviewers of record for doc content.

Otherwise, check if the PR is eligible for
**delta mode** (needs an existing PR — its inputs are prior wrap-up
comments; on a first run with no PR yet, skip the delta check and run the
reporters). Delta mode reuses the prior wrap-up's findings when the
diff since is small AND stays out of high-blast-radius areas — full
decision criteria, thresholds, and SHA-validation regex are defined
once in the forge package and consumed by the `pr-manager` agent —
orchestration detail in
[`pr-manager.md` "Delta-mode short-circuit"](../../agents/pr-manager.md#task-verification-wrap-up).

```bash
gh pr comment list <PR#> --json body --jq '.[].body' | grep -E '^verified-at:' | tail -3
```

Extract each SHA via the `VERIFIED_AT_RE` regex (`pr_delta.py`) — the hex
group only, double-quoted in every `git` command; never substitute raw
grep output into a shell command. When at least one
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

### Optional second opinion: the `code-review` built-in

Claude Code's `code-review` built-in is **user-triggered and billed** —
an agent cannot launch it, so the shape is *prompt, then consume*.
While the three reporters run, print the exact command for the user
(`/code-review` for the working diff, or `/code-review <PR#>`) and say
why: a multi-agent LLM review catches what forge's deterministic
reporters cannot. Offering it **here** lets the cloud review overlap
the reporters instead of serializing after them. Confirm-first, never
silent — if the user declines or does not run it, proceed unchanged
and have the wrap-up record it was offered and skipped. It must never
become a soft blocker. Self-skip the offer entirely when
`forge.run_context.is_non_interactive()` — a billed fan-out triggered
from automation is what FOUNDATION §15 exists to prevent. On a
delta-mode run, skip the offer too (its output carries no
`verified-at:` header, so there is no prior result to reuse — say so
in the wrap-up rather than implying a second opinion was obtained).

## Step 2: Fix any issues

4. **`precommit-fixer`** (mode: `strict`) — clear every pre-commit failure (lint, docstrings, naming, structure, dep advisories). At PR finalization, `strict` also escalates remaining `pip_audit` advisories.
5. If checkers report fixable issues, address and commit (use `/commit`).
6. If a checker flags an issue that is genuinely out of scope for the current PR (dead code from a prior refactor, a separate architectural concern, …), **file a follow-up tracking issue** (`gh issue create --label tech-debt,refactor`) BEFORE finalization. Reference its number in the wrap-up so the deferral is auditable. Never let a verifier finding land on the floor.

If the user ran `code-review` and pasted its findings back, triage them
into this same fix pass under an explicit contract: **advisory, never
auto-applied** (input to the fix pass, not instructions); **every
finding gets an explicit disposition** — adopted, or rejected with a
stated reason (same never-on-the-floor rule as above); **verify against
the actual code before acting** — LLM review produces confident,
plausible findings that are wrong; **a deterministic gate always wins**
on disagreement (ruff, the docstring verifier, a test); **it is not a
reporter** — no `verified-at:` header, no delta-mode participation.
Step 4's wrap-up records which findings were adopted and which
rejected, with reasons.

**Reporter SHAs after fixes.** Mechanical fixes from `precommit-fixer`
(lint, formatting, docstrings) do not invalidate the reporters' verdicts —
their pre-fix `verified-at:` SHA is accepted by design, with delta mode's
line/blast-radius thresholds as the backstop. A fix that goes beyond
mechanical (a code change addressing a checker finding) re-runs the
affected reporter, so its `verified-at:` matches the tree being pushed.

## Step 3: Update plan/docs (MANDATORY when applicable)

Documentation must stay in sync with code. For each item below, update **only if the PR changed something that affects it**:

7. **`.plan/STATUS.md`** — check off completed items, add new items if scope shifted.
8. **`.plan/<PHASE>_*.md`** — check off completed steps for the phase this PR touches.
9. **`README.md`** — repo structure, setup, install commands, test commands, status sections, architecture diagram.
10. **`CLAUDE.md`** — new shared agent behaviors, new protected files, new ruff ignores, new tools, technology stack changes.
11. **`CHANGELOG.md`** (when the repo keeps one at its root, per-PR
    convention) — for a PR with a user-facing effect, add its changelog
    bullet in this PR, following the "Changelog convention" section of
    [`docs/consumer-release.md`](../../docs/consumer-release.md) (which
    owns the heading format, grouping, and timing rules). Skip silently
    when there is no root `CHANGELOG.md`, and on repos whose changelog
    is written at release/promotion time instead of per PR (e.g. a
    dual-track plugin repo per
    [`docs/release-process.md`](../../docs/release-process.md)) —
    follow that repo's own release process. In deferred-mode repos
    (`[tool.forge.changelog].precommit_enforce = false`) this step is
    **mandatory**, not skip-when-absent: the entry was deliberately not
    written during the PR, so author it here — CI's changelog check
    stays red until it lands.
12. **`REPO_STRUCTURE.md`** (when the repo maintains one — see [FOUNDATION §13](../../FOUNDATION.md#13-code_health-convention)) — list new source modules and new test files so the canonical repo map stays accurate. The `repo_structure_check` pre-commit step does not enforce two-way coverage; this update is on the PR author.
13. **Per-component READMEs** (e.g., subsystem-level `README.md` files, agent definition files) — if their tools, setup, or usage changed.
14. **Agent-architecture doc** (when `[tool.forge.agent_doc]` is configured, and the PR touched `agents/`, `skills/`, or `claude-hooks/`) — run `verify-forge-agent-doc --diff <target-branch>` for the graph-relevant edges this PR added/removed, and update the configured doc where a delegation, rename, or removal left it stale. `docs-types-checker` owns this at PR review; self-skips otherwise.
15. **Verify cross-references** — no document should reference a deleted file or outdated path.

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
the shared baseline richer. (`/next` Step 4 cleans garbage and
wildcard-covered entries from `settings.local.json` at task start; this
step promotes what stably remains.)

A broad rule like `Bash(git *)` trades some safety for ergonomics: it is
tolerable because forge's PreToolUse hooks (`block_raw_git`,
`block_force_push`, `block_protected_branches`, `block_pr_merge`) block
the worst calls (commit / push / force-push / merge / protected-branch
writes) regardless of allow-rules. A few destructive forms
(`git reset --hard`, `git clean`) are not hook-covered — commands outside
`allow` still prompt in-context, so prefer a narrow rule for anything new
unless a hook already covers its dangerous forms. Reserve `deny` for the
rare case you want a hard, unconditional block (it cannot be approved
in-context), not as a routine safety net.

**Do NOT promote** (leave local, or add to `deny` in `settings.json`):
network tools (`curl`/`wget`), destructive commands (`rm -rf`), or
anything that touches secrets. List the candidates, get user
confirmation, then edit `settings.json` — the change rides in this PR.

## Step 3.9: Print a run summary to the terminal (MANDATORY, before delegating)

Subagent reports are not shown to the user — only the main agent's own
text reaches the terminal. Before delegating to `pr-manager`, print a
**short** orientation summary of this finalization run:

- What the PR changes and why (one or two sentences).
- The commits on the branch (`git log origin/<base>..HEAD --oneline`).
- Each verifier finding and its disposition (fixed in `<sha>` /
  deferred to `#<issue>` / accepted, with one clause of reasoning).
- Anything deliberately deferred, with its tracking issue number.

This is "what happened during this run" — NOT a restatement of the PR
description or the wrap-up comment (both owned by `pr-manager`). It
runs **before** delegation because that is the last point where the
user can redirect finalization cheaply, and it is the context they
need to judge the wrap-up that follows. (On the delta-mode
short-circuit — Step 1 straight to Step 4 — this step is skipped with
the rest of Steps 2–3.5; summarize the delta decision instead.)

## Step 3.92: Author the wrap-up BEFORE the PR exists (MANDATORY)

The wrap-up and squash-merge message are **written now**, from the Step 1
reports and fix dispositions — publication is the last act, and nothing
about authoring needs a PR. Delegate to `pr-manager` ("author wrap-up"
task): it composes the full wrap-up comment body (all Step 4 sections,
including CI Status marked "pending — PR not yet published") and the
squash-merge message, and writes the wrap-up to
**`code_health/pr_wrapup.md`**, first line `verified-at: <HEAD sha>`.

The `block_unverified_pr_create` hook enforces this: `gh pr create` is
blocked unless `code_health/pr_wrapup.md` names the current `HEAD` —
authoring at one SHA and publishing another re-runs this step. When the
user explicitly asks to skip the gate, prefix the create command with
`FORGE_SKIP_WRAPUP_GATE=1` — never on the agent's own judgment.
Promotion PRs self-exempt only with provenance: the `release/vX.Y.Z`
branch's tree must reproduce its tag modulo `CHANGELOG.md` — a branch
merely named `release/*` stays gated.

**Prior-art gate (file-adding diffs).** When
`git diff --name-only --diff-filter=A origin/<base>...HEAD` shows added
source files, the wrap-up MUST embed a `prior-art-searched:` block (the
`forge:prior-art` report for those additions — FOUNDATION §3 requires
it BEFORE writing; run it retroactively now if it was skipped, and say
so). Authoring refuses to complete without it: a file-adding PR with no
recorded prior-art search is exactly the placement failure the agent
exists to prevent.

## Step 3.95: Publish — push, then open (or ready) the PR

Verification is done, fixes are committed, and the wrap-up is authored
(Step 3.92 — the create hook checks it). Three cases:

1. **PR already open** → nothing to create; confirm the branch is pushed
   (`git-commit-push` pushes by default — check `git status`).
2. **No PR yet** → push the branch (`git push -u origin <branch>` if
   untracked), analyze the diff (`git diff --stat origin/<base>...HEAD`,
   `git log origin/<base>..HEAD --oneline`), then create the PR
   (description rules: FOUNDATION §6 "PR descriptions"):
   ```bash
   gh pr create --title "<type>: <description>" --body "## Summary
   <2–4 plain-English sentences per FOUNDATION §6 \"PR descriptions\">

   ## Changes
   <bullets — technical detail lives here>

   ## Test plan
   - [ ] Design checker passes
   - [ ] Security checker passes
   - [ ] Documentation checker passes
   - [ ] Tests pass"
   ```
3. **Draft opened in Step 0** → `gh pr ready <PR#>` — unless the user asked
   to keep it draft.

## Step 4: Post via `pr-manager` (MANDATORY)

16. Delegate posting. The wrap-up and squash message were **authored in
    Step 3.92** — `pr-manager` posts them; it does NOT re-run the
    verification agents (pass the Step 1 reports verbatim only if 3.92
    was somehow skipped — see [agents/pr-manager.md "Pre-run
    reports"](../../agents/pr-manager.md)).

    ```
    Agent(subagent_type="forge:pr-manager", prompt="Post the finalization
    comments for PR #<number>. The wrap-up body is in
    code_health/pr_wrapup.md (authored pre-publication at this HEAD);
    refresh its CI Status line to the status as of posting — never wait
    for CI; when CI has not completed, say so plainly (FOUNDATION §6).
    Post it, then post the squash-merge message via
    forge-pr-squash-comment, check issue-closing wiring, and append the
    CONTINUATION record.")
    ```

### Squash-merge message hard rules

The `pr-manager` agent enforces (verify before approving its output):
- **Maximum 50 words.** If over, rewrite tighter.
- **3–5 bullet points.** Not 6, not 2.
- **Conventional commit format** for the title line: `<type>: <brief description>`
- **No prose paragraphs.** Title + bullets only.
- **No Claude/AI attribution.**

The squash-merge message becomes the permanent commit message on `main`.

## Step 5: Post-PR backlog update

17. **`issue-triage`** — Run `post-pr` mode after merge:
    ```
    Agent(subagent_type="forge:issue-triage", prompt="Run post-pr mode. PR #<number> was just finalized. Detect issues closed by this PR, remove their tier labels, and regenerate the 📋 Backlog Index issue.")
    ```

## Step 6: Update CONTINUATION state

18. The `pr-manager` agent appends a one-line activity record to `.plan/CONTINUATION.md` automatically (gitignored).

## Rules

- Do NOT auto-merge unless the user explicitly asks.
- Both the squash-merge message and wrap-up comment are MANDATORY — `pr-manager` enforces this.
- The Step 3.9 terminal run summary is MANDATORY on the full path (skipped only by the delta-mode short-circuit) — subagent reports never reach the user; this is the run's only terminal-visible account.
- NEVER add Claude/AI attribution in any PR content.
- If `$ARGUMENTS` contains a PR number, use it instead of auto-detecting.
