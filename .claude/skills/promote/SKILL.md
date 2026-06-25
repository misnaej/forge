---
name: promote
description: Forge-only — open a dev→main promotion PR when a MINOR or MAJOR plugin bump just merged to dev. Detects the bump in HEAD vs parent, refuses duplicates, posts release-summary squash message. Forge-private (not shipped to consumers); other plugin authors use a different release model.
---

# Promote dev → main (forge-only)

> **Spec:** [`docs/release-process.md`](../../../docs/release-process.md) is the
> source of truth for the versioning + promotion model and its invariant→test
> contract. This skill is the *operational* runbook for that model.

Opens a `dev → main` promotion PR after a MINOR (`Y+1, Z→0`) or
MAJOR (`X+1, Y→0, Z→0`) bump to `.claude-plugin/plugin.json` lands on
`dev`. PATCH-only bumps (`Z+1`) do NOT trigger promotion — `dev`
accumulates patches between minor releases per
`docs/release-process.md` §1 (rolling-next).

**Scope: forge repo only.** Lives at
`.claude/skills/promote/SKILL.md` (project-local, not shipped via the
forge plugin). The dev/main two-branch release model and rolling-next
`plugin.json` convention are forge-specific; consumer plugin authors
may use trunk-based development, gitflow, or another model. Shipping
this step in `/forge:pr` would push forge's release ceremony onto
every consumer.

## When to invoke

After `gh pr merge` lands a PR on `dev` that bumped `plugin.json` past
a minor boundary, or as part of `/next` cleanup when `main` is behind
`dev` by one or more minors and a promotion PR is still missing.

## The cardinal rule: never merge `dev` directly into `main`

A promotion PR's head is **always a dedicated `release/vX.Y.Z` branch,
never `dev` itself.** `dev` is a long-lived integration branch; opening
a PR with `--head dev` merges its ref into `main`, couples the two
branches, and is forbidden here. Always cut a release branch and open
the PR from *that* — it is the release branch, not `dev`, that merges
into `main`.

**One release per minor — never lump.** When `main` is several minors
behind (promotions were skipped across sessions), promote **one minor
at a time, in ascending order**. Each minor lands on `main` as its own
clean squash commit; do not open a single PR that jumps `main` across
multiple minors. Finish (open → human merges) one release before
starting the next.

## Step 1: Identify the pending release(s)

Use the CLI — do not hand-roll the version comparison:

```bash
forge-next-prep --promotion-status
```

It fetches tags and prints the base/dev plugin versions plus the
ordered list of `v*` releases pending promotion, e.g.:

```text
main (origin/main): v1.17.0
dev (origin/dev): v1.19.0
Promotion pending — promote these in order (2):
  v1.18.0
  v1.19.0
```

Skip entirely when it reports **"Up to date — nothing to promote"**
(`main`'s minor ≥ `dev`'s minor; patch differences accumulate on `dev`
between releases per `docs/release-process.md` §1), or when a promotion PR
(base `main`) is already open (Step 2).

Set `$NEW` to the **first** (lowest) listed release and promote that one.

## Step 2: Refuse to open a duplicate

```bash
gh pr list --base main --state open --json number,headRefName --jq '.[] | "\(.number) \(.headRefName)"'
```

If a `release/*` promotion PR into `main` is already open, do NOT open
a second — comment on it instead. Promote serially.

## Step 3: Cut the release branch (branch from dev, merge main in)

Four standard-git steps — no special recipe:

```bash
# 1. Branch from the dev version being promoted
git switch -c "release/v$NEW" origin/dev        # (or the v$NEW dev commit)

# 2. Merge main INTO it — REQUIRED. This makes `main` an ancestor, so the
#    PR diff is exactly this release's delta. Omitting it is what makes a
#    promotion PR re-show all of dev's history against an ancient merge-base.
git merge origin/main

# 3. Fix merge conflicts — resolve CODE/test conflicts toward dev (dev is
#    strictly ahead of main). EXCEPTION: CHANGELOG.md — never resolve it
#    blindly toward either side; reconcile by hand (see "CHANGELOG.md
#    conflicts" below).

# 4. Reconcile CHANGELOG.md per the rule below, then add the curated
#    `## v$NEW — <date>` @main entry (group the release's dev PRs by
#    conventional-commit type, one bullet per theme); commit the merge.

git log origin/main..release/v$NEW --oneline    # SANITY: only this release's commits, not all of dev
```

**Why step 2 is the whole game:** promotions are squash merges, so
`main`'s commits are not ancestors of `dev`. Branching from `dev` *without*
merging `main` leaves the merge-base at the pre-squash common ancestor, so
GitHub's three-dot diff re-shows everything since then (a v2.4.0 attempt
showed 98 files vs the true 15). Merging `main` in restores ancestry —
plain git, no tree-reconstruction, no special logic.

The release branch ends up diverging from the `v$NEW` tag only by the
curated `CHANGELOG.md` entry, which the post-merge tag relocation tolerates
via the **release fingerprint** (`docs/release-process.md` §2, §5).

### CHANGELOG.md conflicts — never resolve blindly (the one exception)

`CHANGELOG.md` is the **single exception** to "resolve toward dev." It is
the **`@main` source of record**: every `## vX.Y.0` heading on `main` is the
curated release note for that minor, and `dev`'s copy is *allowed to lag*
(`docs/release-process.md` §5). So a CHANGELOG merge conflict **must never**
be resolved with a blind `git checkout --ours` / `--theirs` — that silently
**erases main's curated history** when dev's copy is behind, or **drops a
genuine dev-side addition** when dev's copy is ahead. It always needs a
human read. Reconcile by hand:

1. **Keep every `## vX.Y.0` entry that exists on `main`.** A more-recent
   `dev` copy does **not** erase a curated main entry just because it is
   ahead in history. Diff first when unsure:
   `git show origin/main:CHANGELOG.md`.
2. **Fold in any legitimate new content** dev's copy carries that main lacks.
3. **Append the new `## v$NEW` curated entry** for the release being
   promoted (step 4).

Rule of thumb: for already-released versions the **main-side body wins**;
the new minor's entry is authored fresh.

Push the branch via the `forge:git-commit-push` agent (direct `git push`
is hook-blocked for agents). Resolve conflicts + make the merge commit
locally, then let the agent push.

## Step 4: Open the promotion PR (from the release branch)

```bash
gh pr create --base main --head "release/v$NEW" \
  --title "release: v$NEW — promote dev to main" \
  --body "Promotes \`dev\` → \`main\` for the v$NEW release, via the \`release/v$NEW\` branch (never a direct \`dev\` merge).

**Merge strategy: squash-and-merge** — \`main\` keeps one release commit per minor. The squash message is posted as a separate comment below; copy it verbatim.

## Included
$(git log --oneline --no-merges origin/main..release/v$NEW)

## After merge
- [ ] Relocate the minor tag onto main's squash commit. **The next \`/next\` does this automatically** (Phase 1 runs \`forge-check-main-tags --fix\`, matched by release fingerprint so the curated CHANGELOG divergence is tolerated). To relocate immediately without waiting for a \`/next\`, run \`forge-check-main-tags --fix\` now. Verify \`git describe --tags origin/main\` → \`v$NEW\`. See \`docs/release-process.md\` §2.
- [ ] (Optional) Mirror \`dev\`'s CHANGELOG to \`main\` via a back-merge PR — NOT required: the merge in Step 3 already brought main's curated log onto the release branch so nothing regresses. Do it only if you want dev's local copy current.
- [ ] If more minors remain behind, promote the next one (repeat from Step 1).
"
```

Because Step 3 merges `main` into the release branch, `main` is an
ancestor and `origin/main..release/v$NEW` is the real release delta — use
it directly for the "Included" list and the Step 5 summary.

## Step 5: Post the release-summary squash-merge message

Post as a separate PR comment so the user can copy it into the
GitHub squash dialog.

The promotion-PR squash message is a **release summary**, not a
per-PR conventional-commit message — the per-PR rules (50-word cap,
3–5 bullets, enforced by `forge-pr-squash-comment`) do not apply.
Authoring rules for the release summary:

- **Title:** `release: v<NEW> — promote dev to main`.
- **Body:** group `git log --oneline --no-merges origin/main..release/v$NEW`
  by conventional-commit type (`feat`, `fix`, `refactor`, `docs`,
  `chore`, `test`). Within each type, summarize related commits
  into one bullet per theme — not one bullet per commit. Reference
  issue numbers (`#NN`) where the commits already cite them.
- **Length:** target 10–15 bullets across all sections combined.
  Group ruthlessly — one bullet per *theme*, not per commit, and
  merge minor themes into a parent bullet rather than spawning
  sub-bullets. Sections that have only one bullet should be folded
  into an adjacent section's heading (`## Docs / Refactor` rather
  than separate `## Docs` + `## Refactor`). When in doubt, fewer
  bullets is better.
- **No Claude/AI attribution.**
- **Wrap the body in a literal triple-backtick fence** so the user
  pastes it verbatim into GitHub's squash dialog.

Skeleton:

````markdown
Squash-merge message — copy verbatim into the GitHub "Squash and merge" dialog:

```
release: v<NEW> — promote dev to main

## Features
- <theme> (#NN, #MM)
- <theme> (#KK)

## Fixes
- <theme> (#NN)

## Docs / Refactor / Chore
- <theme>
```
````

Generate the body by inspecting `git log --oneline --no-merges
origin/main..release/v$NEW` and the merged-PR titles; merge related
commits under one theme bullet. Post the comment via:

```bash
gh pr comment <PR#> --body "<above content>"
```

## Step 6: Stop

The promotion PR is a normal `gh` PR — CI runs against it, and a
human merges it. Do NOT auto-merge. After the user merges with
`Squash and merge`, the checklist in the PR body lists the post-merge
steps. If more minors remain behind `main`, return to Step 1 and
promote the next one — one release per minor, in order.
