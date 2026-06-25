---
name: next
description: Clean up git state, sync main, prune stale branches, optionally clean up stale docs, and pick the next prioritized task from the backlog.
user-invocable: true
---

# /next — Start the Next Task

Automates the "start fresh" workflow.

If `$ARGUMENTS` contains a focus-area keyword (e.g., `quick-wins`, `cleanup`, `ci`), pass it to the triage agent in Phase 3 to skip the interactive focus prompt. If `$ARGUMENTS` contains an issue number (e.g., `/next 423`), skip Phase 3 and go straight to Phase 4 with that issue.

## Phase 1: Git Cleanup & Sync

Stop immediately and report if any step fails.

1. **Check for uncommitted work**
   `git status --porcelain`. If ANY output (staged/unstaged/untracked), warn the user with the file list and **stop**.

2. **Refresh main, optionally tag, prune stale branches** — one CLI call:
   ```bash
   forge-next-prep --tag
   ```
   - `git fetch --prune` → `git checkout main && git pull --ff-only`.
   - With `--tag`: if `.claude-plugin/plugin.json["version"]` is strictly ahead of the latest `v*` tag, tag the merge commit and push (the rolling-next release pattern). No-op when plugin.json is absent, the version equals the latest tag, or the version is older. Drop `--tag` for repos that don't ship a plugin manifest or that don't follow the rolling-next pattern.
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
   already open, so re-running across `/next` invocations is harmless.
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
    Agent(subagent_type="issue-triage", prompt="Run triage mode. The Backlog Index was last triaged X days ago. Walk all open issues, apply tier labels where missing, and regenerate the 📋 Backlog Index issue body.")
    ```

## Phase 4: Task Selection

10. **Delegate to `issue-triage` in `recommend-next` mode**:
    ```
    Agent(subagent_type="issue-triage", prompt="Run recommend-next mode. Return the top 3 issues with: number, title, labels, tier, rationale, and estimated scope.")
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
    - **Recent activity** — **preserve as-is.** This append-only log is the
      audit trail; never truncate it.

    If `.plan/CONTINUATION.md` does not exist, create it from the
    FOUNDATION §10 template. `.plan/CONTINUATION.md` is gitignored.

## Important Rules

- **Always fetch from remote** before assuming branch / PR state.
- **Tag the merge before pruning branches** — version-tracked repos need the release tag at the merge commit, not at some later commit. The tag step (Step 2's `--tag` bullet) runs after `git pull`, before stale-branch cleanup, so the tag points at the canonical release commit.
- **Auto-open a pending promotion before backlog selection** (Phase 1.5) — on dual-track repos, run the promotion flow (which only opens a PR, never merges) ahead of picking new work, so the slow channel never drifts minors behind. No confirm prompt: declining is just not merging the opened PR. Silent on single-track repos.
- **Force-delete (`-D`) only `MERGED` branches.** `forge-next-prep` deletes merged branches with safe `-d` and reports any it skips for "unmerged commits." A squash-merge makes `-d` refuse (the squashed commits are not ancestors of the base), so for each skipped branch, confirm its PR state is `MERGED` (`gh pr view <n> --json state` → `MERGED`) and then `git branch -D <branch>`. A `CLOSED`-but-unmerged PR means the work never landed — **leave it for the user; never `-D` it.** Never `-D` a branch with no merged PR.
- **Never proceed with dirty git state** — always stop and let the user decide.
- **Never delete `.plan/CONTINUATION.md`** — carry it forward in place (Phase 6).
- **Skip Phases 3–4 when the user has already chosen the next task.** /next defaults to recommending from the backlog, but if the user's prior turn names a specific carry-over, follow-up, or open PR finding to work on, treat that as the chosen task and go straight to Phase 5 step 13 (branch creation) — do NOT delegate to `issue-triage`.
- **Delegate task selection** to `issue-triage` only when there is no pre-existing direction.
- **Never delete docs without user confirmation.**
