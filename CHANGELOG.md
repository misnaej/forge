# Changelog

Notable changes to forge, by release on **`main`**.

forge's slow channel (`@main`) ships **minor releases only** — patches
accumulate on `dev` between minors and fold into the next minor's
promotion. Pin `@main` to track the entries below; pin `@dev` for every
patch. Each entry corresponds to one `dev → main` promotion.

**Reading this as a forge consumer.** You're usually jumping several
minors at once: read every entry newer than your current version, top to
bottom, and read each **⚠️ Upgrade notes** lane first — that's the
actions your repo may need (breaking changes, config, new mandatory
behavior). Releases without that lane are additive or internal and need
nothing from you.

**Format.** Per release: an optional **⚠️ Upgrade notes** lane, then
change groups by conventional-commit type (**Features / Fixes / Refactor
/ Tooling / Docs / Chore**) mirroring the promotion squash message.
Follows [Keep a Changelog](https://keepachangelog.com/) in spirit;
versions follow forge's rolling-next convention.

## v1.23.0 — 2026-06-17

### Features
- `forge-config --list` advisor + repo-wide `[tool.forge].source_dirs` /
  `test_dirs` layout keys + `docs/configuration.md`; `[tool.interrogate]`
  stays native (no wrapper).
- New `/forge:test` skill chaining the test agents (advisor → writer →
  review → precommit-fixer).

### Fixes
- Rolling-next version guard now skips when `HEAD`'s tree reproduces
  **any** published `v*` tag (not only the latest), unblocking staged
  promotion of a minor that sits below the global-max tag.

### Docs
- `docs/release-process.md` — single source of truth for versioning,
  `dev → main` promotion, and the invariant→test contract.

## v1.22.0 — 2026-06-17

### ⚠️ Upgrade notes
- **`block_protected_branches` now also protects `dev` by default.**
  Direct pushes to `[tool.forge].dev_branch` (default `dev`) are blocked
  for agents — open a PR instead. Single-track repos are unaffected
  (`dev_branch` defaults to the base branch).

### Fixes
- `forge-next-prep --promotion-status` lists pending **minors only**
  (`X.Y.0`); interleaved patch tags fold into the next minor.

### Refactor / Tooling
- The version guard and the auto-tagger now resolve "latest release" the
  same way (global semver-max `v*` tag), fixing dual-track disagreement
  where a tag on `main` is absent from `dev`'s history.

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

### ⚠️ Upgrade notes
- **Python floor raised to 3.11.** `forge-scripts` no longer installs on
  Python 3.10 (it uses `datetime.UTC` / `tomllib`, both 3.11+ stdlib).
  Move your repo and CI to Python ≥ 3.11 before upgrading forge.
- **Slow-tests CI recipe changed.** If you adopt the slow-tests report,
  pass `--durations` explicitly on the pytest command —
  `pytest --durations=25 --durations-min=1.0 | tee code_health/pytest.log`.
  A bare `pytest` yields an empty report: the durations flags live in
  forge's *own* `pyproject.toml`, not yours.

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
  Additive and opt-in; drop scripts in those dirs to use it.

## v1.18.0 — 2026-06-12

### ⚠️ Upgrade notes
- **New `block_branch_deletion` hook.** Claude Code agents can no longer
  delete a protected remote branch (`base_branch` / `dev_branch`). No
  action unless you relied on an agent doing that — run the delete
  yourself with `! …` instead.

### Features
- `block_branch_deletion` hook — blocks agents from deleting protected
  remote branches.

## v1.17.0 — 2026-06-12

### ⚠️ Upgrade notes
- **Hook-version sidecar.** Managed git hooks now read their version from
  a per-clone `.githooks/.forge-hook-version` file (keeps tracked
  `.githooks/*` byte-stable across bumps). Add `.githooks/.forge-hook-version`
  to your `.gitignore` — the installer does not write the ignore rule for
  you.
- **Two new foundation agents** — `forge:test-advisor` + `forge:test-writer`
  become available after `/plugin update forge@forge` + `/reload-plugins`.

### Features
- Add the `forge:test-advisor` + `forge:test-writer` foundation agents
  and the testing-documentation policy they enforce (fixtures excluded
  from `Args`, structured mock docs, Null-Objects-over-Mock; interrogate
  `ignore-nested-functions` + ruff `D417` in tests) — 12 foundation
  agents total.
- Per-clone conda env name via `.conda_env_name`, so parallel forge
  clones each get their own environment (opt-in: drop a `.conda_env_name`
  file at the repo root).

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
