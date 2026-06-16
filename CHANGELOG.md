# Changelog

Notable changes to forge, by release on **`main`**.

forge's slow channel (`@main`) ships **minor releases only** — patches
accumulate on `dev` between minors and fold into the next minor's
promotion. Pin `@main` to track the entries below; pin `@dev` for every
patch. Each entry corresponds to one `dev → main` promotion.

The format follows [Keep a Changelog](https://keepachangelog.com/);
forge versions follow the rolling-next convention (`plugin.json` always
names the next release about to be tagged).

## v1.21.0 — 2026-06-12

### Features
- Require a `Requires:` line atop every issue (FOUNDATION convention).

### Refactor / Tooling
- Promotion model: a dedicated `release/vX.Y.Z` branch is now required
  (never a direct `dev → main` merge), with staged catch-up one minor at
  a time, surfaced by the new read-only `forge-next-prep
  --promotion-status` CLI.
- Remove dead `tomllib` import guards now that the Python floor is 3.11.

## v1.20.0 — 2026-06-12

### Features
- `forge-slow-tests-report` CLI: parses pytest `--durations`, merges
  across batches, and ranks the slowest tests — a read-only reporter for
  CI and local runs (#29).
- Raise the Python floor to 3.11 — `requires-python >= 3.11`, ruff target
  `py311` (#29).

### Tests / Docs
- Test-doc audit fixes; document the dev tag cadence; the CI recipe now
  passes `--durations` explicitly so the slow-tests report works in any
  consumer repo regardless of its pytest config (#27, #29).

## v1.19.0 — 2026-06-12

### Features
- Consumer hook-extension directories — `post-merge.d` / `post-checkout.d`
  run consumer `*.sh` scripts after the managed hook (sorted,
  failure-tolerant, interactive-only, and surviving hook refresh).

## v1.18.0 — 2026-06-12

### Features
- `block_branch_deletion` hook — blocks agents from deleting protected
  remote branches.

## v1.17.0 — 2026-06-12

### Features
- Add the `forge:test-advisor` + `forge:test-writer` foundation agents
  and the testing-documentation policy they enforce (fixtures excluded
  from `Args`, structured mock docs, Null-Objects-over-Mock; interrogate
  `ignore-nested-functions` + ruff `D417` in tests) — 12 foundation
  agents total.
- Per-clone conda env name via `.conda_env_name`, so parallel forge
  clones each get their own environment.

### Fixes
- `forge-post-merge` now accepts git's squash-flag positional argument
  (it had been exiting 2 on every merge, killing the drift check and the
  hook self-refresh).
- Store the git-hook version in a gitignored sidecar so tracked
  `.githooks/*` stay byte-stable across version bumps.

### Docs / Chore
- Complete the README CLI and pre-commit reference tables.
- Share forge-standard CI permissions; allow `-D` for merged branches.

## v1.16.1 — 2026-06-11

### Chore
- Initial published artifacts: git hooks, `docs/api-digest.md`, and
  `docs/cli-reference.md` generated at forge 1.16.1; README refreshed
  around the guardrails thesis.
