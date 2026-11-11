# No general guard on gh api writes that name the protected base branch

- surface: claude-hooks/block_protected_branches.sh
- release: forge 12.0.0
- seen: 2026-11-11
- branch: fix/block-server-side-ref-force

## Evidence

Found while closing the ref-write holes on this branch. That change guards
one endpoint family — `git/refs`, for every ref — at
`claude-hooks/block_force_push.sh:83`. Nothing guards the protected branch
specifically through the API: these other endpoints write it and stay
allowed, none probed:

- `POST repos/{o}/{r}/merges` — merges a head into the base.
- `PUT repos/{o}/{r}/contents/{path}` with `branch=<base>` — commits a file
  straight onto it.
- `PUT repos/{o}/{r}/branches/<base>/protection` — removes the protection
  itself.
- `POST repos/{o}/{r}/branches/<base>/rename` — renames it out from under
  every guard that matches on the name.
- GraphQL `createCommitOnBranch` (its `branchName` is a literal, unlike
  `updateRef`'s opaque `refId`), `mergeBranch`, `createRef`, `deleteRef`.
- `PATCH repos/{o}/{r}` with `default_branch` — moves which branch is
  protected.

## What it costs

Endpoint whack-a-mole. Each guarded path leaves the next one open, and the
guard's own docs then read as broader than they are.

The real invariant is one sentence: no `gh api` write may name the
protected branch. The per-endpoint stages approximate it.

Hypothetical for now (FOUNDATION §1): no agent has reached for any of
these, and no forge skill, doc or CLI suggests them. Recorded, not filed.

## What would fix it

One generic stage: any `gh api` call carrying a method or body flag whose
text names a protected ref or branch. Like the `git/refs` stage it must
read no flag values (a parser that did was bypassed), and a branch named
only inside a file or variable stays invisible to it — the same tripwire
limit `forge-docs/git-safety.md` states for the ref stage.

Not done with the ref fix deliberately: it widens the false-block surface
well beyond the reported gap, which is a policy choice for the maintainer
rather than part of a gap fix.
