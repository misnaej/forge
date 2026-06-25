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

## v2.11.0 — 2026-06-25

### ⚠️ Upgrade notes
- **Docstring-coverage badge SVG renamed.** With
  `[tool.forge.docstring_coverage] badge = true`, forge now writes
  `.badges/docstring-coverage.svg` (was `.badges/DocstringCoverage.svg`) so
  the filename matches the by-responsibility config name. **If you embed the
  badge in a README by path, update the link** — the badge content is
  unchanged. The old `.badges/DocstringCoverage.svg` is no longer written;
  delete the stale file (#81).

### Features
- **`env_sync` forge-scripts version-pin WARN.** When a repo pins
  `forge-scripts==X.Y.Z` in `[project.dependencies]` and the installed
  version is older, the `env_sync` pre-commit step emits a **non-blocking**
  WARN naming the reinstall command. Bounded to the exact `==` form;
  self-skips channel pins, range specifiers, editable/dev builds, and no pin.
  The blocking entry-point freshness check still takes priority (#107).

### Docs / Refactor
- Clarify the docstring-coverage naming — `[tool.forge.docstring_coverage]`,
  the badge SVG, and "the interrogate badge" are one interrogate-powered
  artifact; canonical name `docstring_coverage` (#81).
- Dedup `_GIT_ENV` / `init_git_repo` shared git-test helpers into
  `tests/conftest.py` (#85).

## v2.10.0 — 2026-06-25

Additive — a `/next` release-workflow change for dual-track repos;
single-track repos are unaffected.

### Changed
- **`/next` auto-opens a pending promotion PR.** Phase 1.5 now runs the
  promotion flow itself when a minor is pending (dual-track repos) instead of
  offering it confirm-first. It only **opens** the `release/vX.Y.0` PR — never
  merges, so the human merge stays the one manual step (FOUNDATION §2) — and
  is idempotent (refuses a duplicate open promotion PR). Removes the manual
  `/promote` step from the per-minor loop; declining is just not merging the
  opened PR (#113).

## v2.9.0 — 2026-06-25

Additive — no consumer action required.

### Features
- **`forge-gen-c4` — per-component container assignment.** A rich
  `[[component]]` may name its owning container via `container =
  "<container name>"`; each declared container then renders with its own
  components **and its own component view**. A component that omits
  `container` attaches to the first declared container, so models with no
  `container` keys render byte-identically. Unknown container names — and
  duplicate container names — fail loudly; import-graph edges still render
  across container boundaries (#106).

## v2.8.0 — 2026-06-25

Additive — a new default-on pre-commit step that self-skips unless a
declared CLI is genuinely missing from your install.

### Features
- **`env_sync` pre-commit step** — a deadly-fast, in-process
  install-freshness gate that runs **first**: every CLI declared in
  `[project.scripts]` must be an installed console script, else the editable
  install is stale (a new entry point was added but not reinstalled) and the
  gate may run old code. Blocks by default with the exact reinstall command
  (`./dev/setup.sh` / `pip install -e`); `[tool.forge.env_sync].blocking =
  false` downgrades it to a non-blocking WARN. Self-skips when there is no
  `[project.scripts]` table, the package isn't installed, or the run is
  non-interactive / CI. Never auto-installs (#82).

## v2.7.0 — 2026-06-25

Additive — release-tooling only; dual-track repos benefit, single-track
repos are unaffected (every new step self-skips when `dev == base`).

### Changed
- **Simplified `dev → main` promotion.** `/promote` is now four
  standard-git steps — branch from `dev`, `git merge origin/main`,
  resolve toward `dev`, rewrite the curated CHANGELOG — replacing the
  earlier tree-reconstruction recipe. The merge-in step is what keeps a
  promotion PR's diff to the real release delta instead of re-showing all
  of dev's history (#94).
- **`/next` self-aligns base-branch tags on every run.** Phase 1 now runs
  `forge-check-main-tags --fix`, so after a promotion PR merges the minor
  tag relocates onto `main` automatically — post-promotion is just a
  normal `/next`. Idempotent; self-skips single-branch repos.
- **`forge-check-main-tags` quiets ancient gaps.** Un-promoted minor tags
  *below* the base branch's current line (never promoted, can't backfill)
  log at INFO instead of warning on every run; genuinely pending minors
  above the line still warn (#94).

## v2.6.0 — 2026-06-24

Additive — no consumer action required. The C4 generator is opt-in and
self-skips when `[tool.forge.c4]` (or a `c4.toml`) is absent.

### Features
- **`forge-gen-c4` + the `/c4` skill** — generate a
  [C4](https://c4model.com/) architecture model from the import graph plus a
  human-authored `c4.toml`: emits Structurizr DSL (default), a self-contained
  **offline** HTML view (vendored Mermaid — no Docker/Java/Graphviz/network),
  or raw Mermaid. Component dependencies are machine-derived; context,
  containers, and runtime/subprocess edges are human-declared. Keeps a managed
  diagram block in the README in sync, and adds an opt-in `c4` pre-commit step
  that fails on architecture-diagram drift. `[tool.forge.c4]` config is
  documented in `docs/configuration.md`; design rationale in
  `docs/c4-architecture.md` (#99).

## v2.5.0 — 2026-06-24

Additive — no consumer action required.

### Features
- **`verify-forge-cve-usage --list-inactive`** — read-only reporter listing
  mapped CVEs no longer in pip-audit's live report, i.e. dormant prune
  candidates in `cve_usage_patterns.toml`. Exits 0, writes nothing, never edits
  the map (transient drop-offs make auto-deletion unsafe) (#80).

### Tooling
- **pip-audit now runs once per commit.** The `pip_audit` step writes a
  `code_health/pip_audit.json` sidecar that the `cve_usage` step reuses via a
  new shared `forge.pip_audit_json` seam, instead of each step invoking
  pip-audit independently — halving the OSV round-trips for CVE-usage
  adopters. `verify-forge-cve-usage --audit-json` exposes the same reuse for
  standalone runs (#78).

## v2.4.0 — 2026-06-24

Additive — no consumer action required.

### Fixes
- **Release matching tolerates a curated `@main` CHANGELOG.**
  `forge-check-main-tags` and the `plugin_version` pre-commit guard now
  compare a **release fingerprint** (tree content minus `CHANGELOG.md`),
  so a `release/vX.Y.Z` branch may finalize its condensed `@main`
  CHANGELOG entry without breaking CI or blocking the minor-tag
  relocation. Any non-`CHANGELOG.md` difference is still rejected. Fixes
  the main-tag-alignment feature (v2.3.0) for repos that curate a `@main`
  CHANGELOG (#88, #90).

### Docs
- Document the modified-release-branch pattern and the two `dev`-CHANGELOG
  sync options in `docs/release-process.md` §2–§5 and the `/promote`
  runbook; add the Rust governance-core split RFC under `proposals/` (#42).

## v2.3.0 — 2026-06-24

Additive — no consumer action required (the new check self-skips
single-branch repos).

### Features
- **`forge-check-main-tags`** (`verify-forge-main-tags`) — verifies, and
  with `--fix` repairs, that every minor release tag `vX.Y.0` sits on the
  base branch's squash commit, matched by **tree equality**. Enforces the
  dev/main minor-boundary invariant so `git describe` on `@main` resolves
  to the right minor. Wired as a pre-commit step that self-skips
  single-branch repos (#84).

### Refactor / Tooling
- Consolidated git/tag plumbing into `forge.git_utils` (`run_git`,
  `get_tree_sha`, `read_plugin_version_at_ref`, `read_local_plugin_version`),
  reused by `verify-forge-plugin-version` and `forge-next-prep` (#84).
- `pip_audit` blocking is now configurable via
  `[tool.forge.pip_audit] blocking` (#84).

## v2.2.0 — 2026-06-22

All additive and opt-in — no consumer action required to upgrade.

### Features
- **CVE-usage check** — a usage-scoped second stage on top of `pip_audit`.
  Where `pip_audit` flags vulnerable *packages*, `verify-forge-cve-usage`
  flags vulnerable *usage*: it intersects the CVE IDs pip-audit is currently
  reporting with a consumer `cve_usage_patterns.toml` map and greps the
  source for the patterns, surfacing only real matches with `file:line` +
  risk + mitigation. Self-maintaining (a pattern is checked only while its
  CVE is live), non-blocking, opt-in by presence of the map. New
  `step_cve_usage` pre-commit step (#3).
- **README status badges** — `install-forge-readme-badges` writes a
  drift-aware managed block (`<!-- forge:badges:start/end -->`) of status
  badges (CI, Python version, Ruff, license, forge channel, Claude Code, and
  the local docstring-coverage SVG when present). shields.io URLs where a
  hosted source exists; the existing `.badges/DocstringCoverage.svg` is
  referenced rather than regenerated (DRY). Opt-in via
  `[tool.forge.badges] enabled = true`; wired into `install-forge-bootstrap`
  (#64).

## v2.1.0 — 2026-06-22

### ⚠️ Upgrade notes
- **ruff now honors `[tool.forge].source_dirs` (scope change).** Source-dir
  resolution is unified across every layout-aware tool (ruff, api-digest,
  docstring-coverage, doctest, typecheck) behind one resolver:
  `[tool.forge.<tool>].paths` → `[tool.forge].source_dirs` (+ `test_dirs`)
  → smart auto-detect. ruff previously scanned a fixed broad name-list
  (`src, test, tests, scripts, tools, projects, agents, lib`) and **ignored
  `source_dirs`** (#70). If you set `source_dirs` *and* keep lintable Python
  in a dir outside it (e.g. `scripts/`), add that dir to `source_dirs` (or a
  `[tool.forge.ruff].paths`) so ruff keeps linting it. **This also applies
  when `source_dirs` is unset:** the old broad list scanned `scripts/`,
  `tools/`, `agents/`, `lib/`, and `projects/` too, and smart auto-detect
  does not — if you keep lintable Python in any of those, add it to
  `source_dirs` / `[tool.forge.ruff].paths`. Repos whose Python lives only
  under `src/` (or top-level packages) + `tests/` need no action.
- **New `release_tag_guard` pre-commit step (dual-track repos only).** Blocks
  a commit when `plugin.json` is more than one rolling-next step ahead of the
  latest `v*` tag — i.e. an intermediate release was bumped past without
  being tagged (the failure that shipped v1.25.0 untagged, #66). **Self-skips
  for single-track repos, repos without `.claude-plugin/plugin.json`, and
  when `plugin.json` isn't strictly ahead** — so consumers see nothing. Fix a
  trip by running `forge-next-prep --tag`.

### Features
- **Unified, granular source-dir resolution** — `[tool.forge].source_dirs` /
  `test_dirs` are now the single definition every layout-aware tool scans,
  with optional per-tool `[tool.forge.<tool>].paths` overrides (new for
  `ruff` and `api_digest`; existing for coverage / doctest / typecheck). The
  unset default is **smart auto-detect** (`src/` or top-level packages; then
  `tests/` / `test/`), replacing a fixed name-list that scanned phantom dirs
  (#68, #70).
- **`release_tag_guard`** — pre-commit backstop enforcing the dev release-tag
  cadence (#66).

### Fixes
- **`block_protected_branches` now blocks pushes by their refspec
  destination** — a push from an unprotected branch to a protected one
  (`git push origin HEAD:dev`, `feature:dev`, `feature:refs/heads/dev`,
  `+dev`) was previously missed (only the *current* branch was checked).
  This destination guard has **no agent bypass**: not even
  `forge:git-commit-push` may push directly to a protected branch (matching
  `block_branch_deletion`). The agent bypass now covers only normal
  feature-branch pushes (#74).
- **`block_install_deps` now catches `conda run conda install`** (and the
  general `<mgr> run <mgr> install` wrapper) plus `conda env update` (direct
  and wrapped) — the manager-wrapping-a-manager gaps surfaced in review
  (#62).

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
- **The CVE scan now actually runs by default.** `pip-audit` ships as a
  core dependency (it backs the default `pip_audit` step), so after
  upgrading + reinstalling, the dependency-vulnerability scan runs where it
  previously **silently no-op'd** if `pip-audit` wasn't separately installed
  (#71). It is **non-blocking** (advisory `WARN`), but you may now see CVE
  advisories on commit that were invisible before — review them, or
  `disable = ["pip_audit"]` under `[tool.forge.precommit]` to turn the step
  off deliberately.

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
- **`pip_audit` no longer silently no-ops.** `pip-audit` is now a core
  dependency, and a missing binary renders as a loud non-blocking `WARN`
  instead of a silent skip — a security gate that quietly does nothing gave
  false assurance (#71).

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
