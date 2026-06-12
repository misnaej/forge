---
name: promote
description: Forge-only — open a dev→main promotion PR when a MINOR or MAJOR plugin bump just merged to dev. Detects the bump in HEAD vs parent, refuses duplicates, posts release-summary squash message. Forge-private (not shipped to consumers); other plugin authors use a different release model.
---

# Promote dev → main (forge-only)

Opens a `dev → main` promotion PR after a MINOR (`Y+1, Z→0`) or
MAJOR (`X+1, Y→0, Z→0`) bump to `.claude-plugin/plugin.json` lands on
`dev`. PATCH-only bumps (`Z+1`) do NOT trigger promotion — `dev`
accumulates patches between minor releases per CLAUDE.md "plugin
manifest version is rolling-next."

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

```bash
git fetch origin --tags
MAIN_V=$(git show origin/main:.claude-plugin/plugin.json | python -c 'import json,sys; print(json.load(sys.stdin)["version"])')
DEV_V=$(git show origin/dev:.claude-plugin/plugin.json  | python -c 'import json,sys; print(json.load(sys.stdin)["version"])')
echo "main=$MAIN_V  dev=$DEV_V"
git tag --sort=v:refname | sed -n "/v$MAIN_V/,/v$DEV_V/p"   # the minors to promote, in order
```

Skip entirely when:

- `main`'s minor ≥ `dev`'s minor (nothing to promote, or only patch
  differences — `dev` accumulates patches between minor releases per
  CLAUDE.md "rolling-next").
- A promotion PR (base `main`) is already open (Step 2).

Set `$NEW` to the **lowest** un-promoted minor (e.g. if main is `1.17.0`
and dev is `1.19.0`, promote `v1.18.0` first, then `v1.19.0`).

## Step 2: Refuse to open a duplicate

```bash
gh pr list --base main --state open --json number,headRefName --jq '.[] | "\(.number) \(.headRefName)"'
```

If a `release/*` promotion PR into `main` is already open, do NOT open
a second — comment on it instead. Promote serially.

## Step 3: Cut the release branch (never `--head dev`)

Build a `release/v$NEW` branch whose diff against `origin/main` is
**exactly that one release**:

```bash
# main is exactly one minor behind → branch at dev's tip:
git switch -c "release/v$NEW" origin/dev

# main is several behind → branch from main and cherry-pick only that
# minor's commit(s) (squash-merges of earlier promotions break ancestry,
# so a plain dev→main diff would redundantly re-show already-promoted
# minors). Resolve the plugin.json version during the pick:
#   git switch -c "release/v$NEW" origin/main
#   git cherry-pick -n <sha-of-that-minor>

git log origin/main..release/v$NEW --oneline   # SANITY: only this release's commits
```

Push the branch via the `forge:git-commit-push` agent (direct `git push`
is hook-blocked for agents).

## Step 4: Open the promotion PR (from the release branch)

```bash
gh pr create --base main --head "release/v$NEW" \
  --title "release: v$NEW — promote dev to main" \
  --body "Promotes \`dev\` → \`main\` for the v$NEW release, via the \`release/v$NEW\` branch (never a direct \`dev\` merge).

**Merge strategy: squash-and-merge** — \`main\` keeps one release commit per minor. The squash message is posted as a separate comment below; copy it verbatim.

## Included
$(git log --oneline origin/main..release/v$NEW)

## After merge
- [ ] \`v$NEW\` tag — usually already created on \`dev\` by \`/next\` (\`forge-next-prep --tag\`); create + push it only if absent.
- [ ] If more minors remain behind, promote the next one (repeat from Step 1).
"
```

## Step 5: Post the release-summary squash-merge message

Post as a separate PR comment so the user can copy it into the
GitHub squash dialog.

The promotion-PR squash message is a **release summary**, not a
per-PR conventional-commit message — the per-PR rules (50-word cap,
3–5 bullets, enforced by `forge-pr-squash-comment`) do not apply.
Authoring rules for the release summary:

- **Title:** `release: v<NEW> — promote dev to main`.
- **Body:** group `git log --oneline --no-merges origin/main..release/v$NEW` by
  conventional-commit type (`feat`, `fix`, `refactor`, `docs`,
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
