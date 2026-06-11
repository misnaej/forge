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
a minor boundary, or as part of `/next` cleanup when the bump landed
in an earlier session and a promotion PR is still missing.

## Step 1: Detect a bump

On the `dev` branch, after `git pull`:

```bash
NEW=$(git show HEAD:.claude-plugin/plugin.json | python -c 'import json,sys; print(json.load(sys.stdin)["version"])')
OLD=$(git show HEAD~1:.claude-plugin/plugin.json 2>/dev/null | python -c 'import json,sys; print(json.load(sys.stdin)["version"])' 2>/dev/null || echo "0.0.0")
new_major=$(echo "$NEW" | cut -d. -f1)
old_major=$(echo "$OLD" | cut -d. -f1)
new_minor=$(echo "$NEW" | cut -d. -f2)
old_minor=$(echo "$OLD" | cut -d. -f2)
if [ "$new_major" != "$old_major" ] || [ "$new_minor" != "$old_minor" ]; then
  echo "MINOR or MAJOR bump detected ($OLD → $NEW); promotion PR needed"
else
  echo "PATCH-only or no change; skip promotion"
  exit 0
fi
```

Skip entirely when:

- The current branch is not `dev` (this skill is only for the `dev → main` direction).
- The plugin version did not bump, or only the patch digit moved.
- A promotion PR `dev → main` is already open.

## Step 2: Refuse to open a duplicate

```bash
gh pr list --base main --head dev --state open --json number --jq '.[].number'
```

If the list is non-empty, an open promotion PR already exists —
append a comment summarizing the additional changes since that PR
was opened, do NOT create a second one.

## Step 3: Open the promotion PR

```bash
gh pr create --base main --head dev \
  --title "release: v$NEW — promote dev to main" \
  --body "Promotes \`dev\` to \`main\` for the v$NEW plugin minor release.

**Merge strategy: squash-and-merge.** Use GitHub's \"Squash and merge\" button so \`main\` keeps one release commit per promotion. The squash-merge message is posted as a separate PR comment below — copy it verbatim into the squash dialog.

## Included since v$OLD
$(git log --oneline main..dev)

## Release-commit checklist (run after merge)
- [ ] \`git tag v$NEW\` at the merge commit on \`main\`
- [ ] \`git push origin v$NEW\`
- [ ] Open the next PR bumping \`.claude-plugin/plugin.json[\"version\"]\` to the next rolling-next version (per CLAUDE.md \"plugin manifest version is rolling-next\")
"
```

## Step 4: Post the release-summary squash-merge message

Post as a separate PR comment so the user can copy it into the
GitHub squash dialog.

The promotion-PR squash message is a **release summary**, not a
per-PR conventional-commit message — the per-PR rules (50-word cap,
3–5 bullets, enforced by `forge-pr-squash-comment`) do not apply.
Authoring rules for the release summary:

- **Title:** `release: v<NEW> — promote dev to main`.
- **Body:** group `git log --oneline --no-merges main..dev` by
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
main..dev` and the merged-PR titles; merge related commits under one
theme bullet. Post the comment via:

```bash
gh pr comment <PR#> --body "<above content>"
```

## Step 5: Stop

The promotion PR is a normal `gh` PR — CI runs against it, and a
human merges it. Do NOT auto-merge. After the user merges with
`Squash and merge`, the checklist in the PR body lists the
post-merge steps (tag, push, next rolling-next bump).
