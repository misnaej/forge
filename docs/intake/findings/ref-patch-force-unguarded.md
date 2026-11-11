# block_force_push does not cover a server-side ref rewrite via gh api

- surface: claude-hooks/block_force_push.sh
- release: forge 12.0.0
- seen: 2026-11-11
- branch: fix/block-gh-update-branch-rebase

## Evidence

Found while closing the `gh pr update-branch --rebase` gap (#957). Probed
`block_force_push.sh` directly:

    gh api -X PATCH repos/o/r/git/refs/heads/main -F sha=abc -F force=true

exits 0 — allowed. Guard matches a `git push` verb only
(`claude-hooks/block_force_push.sh:42`), so no stage sees a ref moved
through the REST API.

Same class as the gap #957 closed: server-side history rewrite, no local
git, no push. Not probed further (GraphQL equivalents, tag refs).

## What it costs

Force-moving any ref, protected branch included, from an agent. Agent
credentials bypass server-side rulesets (FOUNDATION §2), so the server is
not a second line of defence here.

Hypothetical for now: no agent has been seen taking this route, and it is
not an idiom any forge skill or doc suggests. Filed as a sighting, not an
issue, per §1 — promote if it recurs or if an agent reaches for it.

## What would fix it

A stage in `block_force_push.sh` on `gh api` with a `git/refs/` endpoint
carrying `force=true`, modelled on `block_pr_merge.sh`'s api stage. Check
`block_branch_deletion.sh` first — it already covers
`gh api -X DELETE …/branches/…`, so the endpoint-matching idiom exists and
the two guards may want one shared helper.
