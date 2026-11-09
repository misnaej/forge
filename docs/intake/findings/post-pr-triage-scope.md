# post-pr triage pass should be scoped to PR-filed issues only

- surface: issue-triage
- release: forge 11.2.0.dev (post #911)
- seen: 2026-11-07
- branch: main

## Evidence

`agents/issue-triage.md:99` post-pr mode runs overlap sweep + placement + Backlog Index
regen over the whole backlog on every merge (spawned after PR #911). Costs
tokens on issues unrelated to the merged PR.

## What it costs

Per-merge token spend proportional to backlog size, not to the PR's own
follow-ups.

## What would fix it

Scope post-pr to issues the PR filed/closed (`forge-plan-check tracks
--from-pr <N>` output only); full-backlog sweep + Backlog Index regen move
to manual `/triage` runs. User decision: full triage stays manual for now.
