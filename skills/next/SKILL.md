---
name: next
description: Clean up git state, sync main, prune stale branches, optionally clean up stale docs, resume a Requires:-linked sequence a merge just unblocked, or pick the next prioritized task from the backlog.
user-invocable: true
---

# /next — Start the Next Task

Automates the "start fresh" workflow.

If `$ARGUMENTS` contains a focus-area keyword (e.g., `quick-wins`, `cleanup`, `ci`), pass it to the triage agent in Phase 3 to skip the interactive focus prompt. An explicit issue number (e.g., `/next 423`) is the top tier of the **task-selection precedence rule** (see Important Rules) — skip Phases 2.5–4 and go straight to Phase 5 step 13 with that issue.

## Phase 1: Git Cleanup & Sync

Stop immediately and report if any step fails.

1. **Check for uncommitted work**
   `git status --porcelain`. If ANY output (staged/unstaged/untracked), warn the user with the file list and **stop**.

2. **Refresh main, optionally tag, prune stale branches** — one CLI
   call. **Pick the command by repo class first:**

   ```bash
   # Plugin-manifest / dual-track repo (e.g. forge itself):
   forge-next-prep --tag

   # Single-track repo without a plugin manifest (the standard
   # consumer case) — no --tag; release tags are cut only at release
   # time via forge-release (docs/consumer-release.md):
   forge-next-prep
   ```

   - `git fetch --prune` → `git checkout main && git pull --ff-only`.
   - With `--tag` (plugin repos only): if
     `.claude-plugin/plugin.json["version"]` is strictly ahead of the
     latest `v*` tag, tag the merge commit and push. Rationale and
     cadence live in `docs/release-process.md` (forge-only). No-op when
     the version equals the latest tag or is older. On a single-track
     repo with no plugin manifest, the CLI warns and skips the tag step
     — per-merge tagging is not a consumer pattern.
   - Deletes local branches with `[origin/...: gone]` tracking via safe `git branch -d`. Branches with unmerged commits are reported, not deleted by the CLI — the skill then `-D`s any whose PR is confirmed merged (the squash-merge case `-d` cannot detect; see Important Rules). Use `--no-prune-branches` to skip.
   - Exits non-zero (1) when main cannot fast-forward — stop and report.
   - **Align base-branch release tags (dual-track):** then run
     `forge-check-main-tags --fix`. This is the **one step that
     distinguishes post-promotion cleanup from a normal merge** — when a
     promotion PR has merged, it moves the minor tag `vX.Y.0` from its
     `dev` commit onto `main`'s squash commit (else `git describe
     origin/main` resolves to a stale predecessor). Safe on **every**
     `/next`: idempotent (moves a tag only when a promotion actually
     landed), **self-skips single-branch repos**, and leaves ancient
     un-promoted minors quiet (INFO). So **post-promotion = a normal
     `/next`** — nothing extra to remember. Report any moves.

3. **Confirm clean state**
   Run `git branch` and `git status --porcelain`. Report.

   **Managed-artifact drift?** When drift signals appeared during sync
   (a `check_upstream` warning, stale generated docs, bootstrap-managed
   files out of date), offer `forge-resync` — safe to run blind: in
   sync → no-op; an open `chore/forge-resync-*` PR → reports its URL
   instead of duplicating; real drift → opens a dedup-guarded resync PR
   on its own branch. The CLI owns all the logic; do not hand-roll the
   regen/branch/PR loop.

4. **Tidy `.claude/settings.local.json`** — auto-approved one-off
   commands accumulate during work. Consolidate them once per `/next`
   run:
   - Read `.claude/settings.local.json`.
   - Drop one-off rules already covered by existing wildcards (e.g.,
     `Bash(git add foo.py)` is covered by `Bash(git *)`).
   - Drop garbage entries (shell fragments like `Bash(done)`,
     `Bash(fi)`, `Bash(do echo ...)`, heredoc fragments).
   - Consolidate repeated tool-specific rules into wildcards (e.g.,
     multiple `Bash(ruff check ...)` lines → `Bash(ruff *)`).
   - Keep legitimate domain-specific WebFetch rules.
   - Write the cleaned file back.

## Phase 1.5: Pending promotion (dual-track repos)

`forge-next-prep --tag` (Phase 1) prints a `Pending promotion: dev at
vX.Y.Z; <base> at vA.B.C (MINOR bump)` advisory when the repo is
**dual-track** (`[tool.forge].dev_branch != base_branch`) and the slow
channel is a minor or more behind. **Single-track repos never see this
line — skip the whole phase.**

When a promotion is pending, handle it **before** backlog task selection —
a pending minor promotion is *always wanted* in the dual-track model: it
ships completed minors to the slow channel and stops the base branch from
silently drifting minors behind across sessions (the failure mode that
otherwise accumulates a staged-catch-up backlog).

1. **Auto-run the promotion flow — do NOT merely offer it.** In forge,
   invoke `Skill(skill="promote")` directly (consumers substitute their own
   promotion command). Running it unprompted is safe because it only
   **opens** a PR — it never merges, so the one irreducible manual step
   (the human merge, FOUNDATION §2) is untouched — and it is
   **idempotent**: it refuses to open a second promotion PR when one is
   already open, so re-running across `/next` invocations is harmless. If
   the repo has no promotion skill/command configured, do **not** error —
   fall back to surfacing the advisory and telling the user to run their
   promotion manually.
2. It promotes **one minor at a time** in ascending order: cuts the
   `release/vX.Y.0` branch, authors the curated CHANGELOG, opens the PR,
   then **stops**. Surface the opened PR as the top item (above any Phase 4
   backlog work) and tell the user to merge it when ready.
3. The post-merge tag relocation is already automatic — the next `/next`
   relocates the minor tag onto `<base>` via Phase 1's
   `forge-check-main-tags --fix` step. (That move is *not* done by
   `/promote`, which cannot run after the async human merge.)
4. **Declining is merging-time, not run-time:** to skip a promotion the
   user simply does not merge the opened PR (and may close it). `/next`
   does not prompt first — an action that only opens a reviewable PR needs
   no confirmation gate.

## Phase 2: Documentation Hygiene (optional)

5. **Check for stale docs**
   Read files in `docs/` and `.plan/`. For each:
   - Is the content **superseded** by another file?
   - Is any information **not captured elsewhere**?

6. **Consolidate and clean up** (with user confirmation each time):
   - Fully superseded → propose deletion (list what it contains, where the info lives now).
   - Partially useful → move unique content to canonical location (CLAUDE.md, STATUS.md, relevant phase plan), then propose deletion.
   - Plan files with completed phases and no open items → propose archiving / trimming.
   - **Never delete without user confirmation.**

7. **Verify cross-references** after deletions — no remaining file should reference a deleted one.

## Phase 2.5: Resume a `Requires:`-linked sequence

Work is often planned as an ordered chain via FOUNDATION §14's
`Requires:` lines. A merge that closes one step of a chain should
surface its successor — not hand the user unrelated top-scored work.
Runs only when no higher-precedence selection exists (see the
task-selection precedence rule in Important Rules).

1. **Collect recently-closed issues** from the merge window — several
   PRs typically land between two `/next` runs:
   ```bash
   gh pr list --state merged --base <dev-or-base> --limit 10 \
     --json number,closedAt,body
   ```
   Extract the issues each PR closed (`Fixes #N` / `Closes #N` lines —
   the same convention `issue-triage`'s `post-pr` mode reads; keep the
   two in lockstep if the convention ever changes).
   **Validate every extracted number as a bare integer before using it
   in any further command — PR bodies are untrusted text; never
   interpolate anything else from them.**

2. **Find open successors**: open issues whose `Requires:` line names
   one of those closed issues:
   ```bash
   gh issue list --state open --json number,title,body \
     --jq '.[] | select(.body | test("^Requires:.*#<N>\\b"))'
   ```

3. **Exactly one match** → propose it as the next task (number, title,
   which merge unblocked it) and ask for confirmation — **propose,
   never assume**; the user may have dropped the sequence
   deliberately. On confirmation, skip Phases 3–4 and go to Phase 5
   step 13. Declined → fall through to normal triage.

4. **Zero or several matches** → normal Phase 3/4 flow. Do not invent
   an ordering the issues do not state.

5. **Stale `blocked` labels**: a matched successor still labelled
   `blocked` is now mislabelled — hand the relabel to `issue-triage`
   (it owns the label schema; never relabel inline).

## Phase 3: Backlog Refresh (Prevent Stale Data)

8. **Check Backlog Index freshness and ask user**
    Find the `📋 Backlog Index` GitHub issue and read its "Last triage" date
    from the body. If > 7 days old, ask:
    ```
    "Backlog Index was last triaged X days ago. Re-triage with current
    GitHub data? (Prevents stale recommendations)
    [y/N]"
    ```

9. **Refresh if confirmed** — delegate to `issue-triage`:
    ```
    Agent(subagent_type="forge:issue-triage", prompt="Run triage mode. The Backlog Index was last triaged X days ago. Walk all open issues, apply tier labels where missing, and regenerate the 📋 Backlog Index issue body.")
    ```

## Phase 4: Task Selection

10. **Delegate to `issue-triage` in `recommend-next` mode**:
    ```
    Agent(subagent_type="forge:issue-triage", prompt="Run recommend-next mode. Return the top 3 issues with: number, title, labels, tier, rationale, and estimated scope.")
    ```

11. **Present recommendations** to the user — number+title (with link), labels+tier, why recommended, estimated scope.

## Phase 5: Confirmation & Setup

12. **Ask which task** the user wants to start (1, 2, 3, or none).

13. **On confirmation**:
    - Create a feature branch using the repo's prefix convention (`feat/`, `fix/`, `refactor/`, `test/`, `docs/`, `chore/` + short description, e.g., `feat/issue-NNN-short-description`).
    - Fetch issue details: `gh issue view <number>`.
    - Present a brief summary and starting points (key files, relevant code areas).

## Phase 6: Carry CONTINUATION state forward

14. **Never delete `.plan/CONTINUATION.md`.** It is the only mechanism that
    carries state across a context clear (FOUNDATION §10) — deleting it on
    `/next` destroys the handoff exactly when the user clears context to
    start the next task.

    "Reset" here means **rewrite in place**, never `rm`. Update the
    structured sections to reflect the newly selected task:

    - **Status** — one-paragraph state for the new task.
    - **Done** — clear, or keep only items still relevant.
    - **In progress** — the new branch and task reference.
    - **Next steps** — the first concrete steps for the new task.

15. **Continuation hygiene** (every `/next`):

    - Run `forge-continuation-append --rotate` — the mechanical pass:
      done ledger entries older than two days (or beyond the count cap) move
      verbatim to `.plan/CONTINUATION-archive.md` and collapse into
      per-day digest lines; entries referencing PRs/issues still named
      in the structured sections are pinned (undone work stays raw).
    - Then a **critical curation pass** — the judgment the CLI cannot
      apply. Read the structured sections and the condensed digests as
      a skeptic: delete items that are stale (shipped, superseded,
      referenced work closed, advice no longer true), collapse
      repetition, and re-rank what remains. Done work's history lives
      in the archive and in git/GitHub — the continuation file owes the
      next session *orientation*, not a museum. FOUNDATION §10's
      never-delete rule protects the FILE and the raw archive, not
      stale content inside the structured sections.

    If `.plan/CONTINUATION.md` does not exist, create it from the
    FOUNDATION §10 template. `.plan/CONTINUATION.md` is gitignored.

## Important Rules

- **Always fetch from remote** before assuming branch / PR state.
- **Tag the merge before pruning branches** — version-tracked repos need the release tag at the merge commit, not at some later commit. The tag step (Step 2's `--tag` bullet) runs after `git pull`, before stale-branch cleanup, so the tag points at the canonical release commit.
- **Auto-open a pending promotion before backlog selection** (Phase 1.5) — on dual-track repos, run the promotion flow (which only opens a PR, never merges) ahead of picking new work, so the slow channel never drifts minors behind. No confirm prompt: declining is just not merging the opened PR. Silent on single-track repos.
- **Force-delete (`-D`) only `MERGED` branches.** `forge-next-prep` deletes merged branches with safe `-d` and reports any it skips for "unmerged commits." A squash-merge makes `-d` refuse (the squashed commits are not ancestors of the base), so for each skipped branch, confirm its PR state is `MERGED` (`gh pr view <n> --json state` → `MERGED`) and then `git branch -D <branch>`. A `CLOSED`-but-unmerged PR means the work never landed — **leave it for the user; never `-D` it.** Never `-D` a branch with no merged PR.
- **Never proceed with dirty git state** — always stop and let the user decide.
- **Never delete `.plan/CONTINUATION.md`** — carry it forward in place (Phase 6).
- **Task-selection precedence — one ordered rule.** Selection sources, strongest first: (1) an **explicit issue number** in `$ARGUMENTS`; (2) a **user-named carry-over** — the user's prior turn names a specific follow-up, carry-over, or open PR finding; (3) an **inferred `Requires:` successor** (Phase 2.5) — ranked last because it is the only source not stated by a human, so it is always confirm-first. Any **established** hit (tiers 1–2 act immediately; tier 3 only after the user confirms per Phase 2.5 step 3) skips the lower tiers AND Phases 3–4: go straight to Phase 5 step 13 (branch creation), no `issue-triage` delegation. Only with no established hit at any tier does generic backlog triage (Phases 3–4) select the task.
- **Never delete docs without user confirmation.**
