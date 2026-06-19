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

## v2.0.0 — 2026-06-19

### ⚠️ Upgrade notes
- **Pre-commit steps now default to whole-tree scope (BREAKING).** The
  three file-selecting steps — `ruff`, `docstring_verification`,
  `test_naming_check` — now run over the **entire tracked source tree**
  by default, not the diff vs main. `ruff` already did; the change affects
  `docstring_verification` (blocking) and `test_naming_check` (warning).
  **A consumer whose tree has pre-existing docstring/signature mismatches
  outside the current diff will be newly blocked on the next commit.** To
  restore the old diff-only behavior, set in `pyproject.toml`:
  ```toml
  [tool.forge.precommit]
  scope = "diff"                       # global, or:
  [tool.forge.precommit.scope_overrides]
  docstring_verification = "diff"
  ```
  See [`docs/configuration.md`](docs/configuration.md) "Changing a step's
  scope". Resolution: `scope_overrides.<step>` → `scope` → `all`.
- **`install-forge-bootstrap` now writes `.claude/settings.json`.** A new
  `claude-settings` step enables the forge Claude Code plugin **per repo**
  (marketplace + `enabledPlugins`), so the plugin loads only where you
  adopt forge — not globally, where its agents would error in repos without
  `forge-scripts`. Idempotent + merge-preserving. Opt out with
  `install-forge-bootstrap --skip claude-settings`.

### Features
- **Configurable per-step scope** — `[tool.forge.precommit].scope`
  (`all` | `diff`, default `all`) plus `scope_overrides` for per-step
  control, wired through a new `--scope` flag on `fix-forge-ruff`,
  `verify-forge-docstrings`, and `verify-forge-test-naming`. New
  `git_utils.get_tracked_files` is the whole-tree counterpart of
  `get_modified_files` (#65).
- **`install-forge-claude-settings`** — write / verify the per-repo plugin
  enablement; the marketplace `ref` tracks your `forge-scripts` pip pin
  (override `--ref`; `--check` verifies without writing). Wired into
  `install-forge-bootstrap` (#63).

### Fixes
- **`block_claude_attribution` hook** now catches the canonical Claude Code
  footer `Generated with [Claude Code](…)` (the markdown `[` defeated the
  old adjacent-words regex) and the `🤖` emoji signature — the exact
  attribution the harness emits by default no longer slips into history.
- **`forge-gen-api-digest` honors `[tool.forge].source_dirs`** when `--roots`
  is omitted (falling back to `src/` auto-detect when unset), so a multi-root
  repo gets a complete digest and agrees with `verify-forge-docstring-coverage`
  on where the source roots are (#67).

### Refactor
- **Shared `claude_settings_schema` module** — the `.claude/settings.json`
  marketplace key path, `forge@forge` id, and empty-hook scaffold now live
  in one place, consumed by both the write side
  (`install-forge-claude-settings`) and the read side
  (`install-forge-claude-md` channel detection). Fixes a standalone
  fresh-repo path that dropped the hook scaffold.

## v1.25.0 — 2026-06-19

### ⚠️ Upgrade notes
- **`block_install_deps` now also blocks pipenv, poetry, and uv** (and the
  `<mgr> run pip install` wrappers), closing a gap where an agent in a
  pipenv/poetry/uv repo could re-resolve unpinned dependencies. If a
  trusted flow legitimately needs an agent to run one, opt out — per
  manager (`[tool.forge.hooks] block_install_deps = ["pip", "conda"]`) or
  entirely (`= false`). The default stays block-all (FOUNDATION §2).

### Features
- **`docs/adopting.md`** — modular adoption guide: three independent
  install tracks (CLIs only / + git hooks / + plugin), a "what lands on
  disk" table, and a drift/refresh/upgrade explainer (#33).
- **`forge-upgrade` surfaces upgrade notes** — after a successful upgrade
  it prints the recent `⚠️ Upgrade notes` so you see the consumer-action
  items; the CHANGELOG now ships as package data to make this work (#34).
- **post-merge tag advisory** — `forge-post-merge` warns on the dev branch
  when `plugin.json` is ahead of the latest tag (a rolling-next release
  that was never tagged), advisory only (#21).
- **`forge-doctor` checks enabled-step tools** — flags when a step in
  `[tool.forge.precommit] enable` lacks its tool (typecheck→pyrefly,
  doctest→pytest) before the commit-time failure (#57).

## v1.24.0 — 2026-06-17

All additive and opt-in — no consumer action required to upgrade.

### Features
- **Pluggable pre-commit step framework** — `[tool.forge.precommit]
  enable` / `disable` (plus `forge-precommit --only` / `--skip`) turn any
  step on or off uniformly, on top of each step's own self-skip (#6).
- **Opt-in `doctest` step** — `pytest --doctest-modules` over
  `[tool.forge.doctest].paths` (default `["src"]`); non-blocking by
  default (#5).
- **Opt-in `typecheck` step** — runs `pyrefly` over
  `[tool.forge.typecheck].paths`; non-blocking by default (#48).
- **Opt-in `doc_consistency` step** + `verify-forge-doc-consistency` CLI —
  checks that every `[project.scripts]` CLI is documented in
  `docs/cli-reference.md`; non-blocking (#4).

### Tooling
- `forge-config --list` now enumerates the new
  `[tool.forge.precommit/doctest/typecheck]` keys, and a drift test
  couples `CONFIG_KEYS` to its readers so the registry can't silently go
  stale (#46).

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
