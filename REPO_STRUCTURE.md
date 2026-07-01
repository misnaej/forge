# Repo Structure

This overview summarizes the current layout of the Forge repository. Keep
this file up to date as the repo evolves — `verify-forge-repo-structure`
checks it against the actual tree on every commit.

## Overview

Forge is a shared engineering foundation: process docs, pre-commit
verification scripts, git hooks, and an optional Claude Code plugin. It
ships as the `forge-scripts` pip package and works with or without Claude
Code.

## Core Components

- **Python package (`src/forge/`)**: verification CLIs, the pre-commit
  dispatcher, installers, and the `audit/` subpackage.
- **Claude Code plugin (`.claude-plugin/`, `agents/`, `skills/`,
  `claude-hooks/`)**: agents, slash-command skills, and safety hooks
  discovered by Claude Code after install.
- **Git hooks (`.githooks/`)**: pre-commit and post-* hooks installed by
  `install-forge-githooks`.
- **Tests (`tests/`)**: pytest suite mirroring the package layout.

## Forge Package (`src/forge/`)

1. **CLI Modules**
   - precommit.py: `forge-precommit` — pre-commit dispatcher; most steps shell out to their own SRP CLI, a few (env_sync, pip_audit) run in-process for speed / single-invocation sharing
   - next_prep.py: `forge-next-prep` — refresh main, optional rolling-next tag bump, prune stale branches; used by `/next` skill
   - continuation_append.py: `forge-continuation-append` — single source of truth for `.plan/CONTINUATION.md` append format; called by `forge:git-commit-push` and `forge:pr-manager`
   - pr_squash_comment.py: `forge-pr-squash-comment` — validates + posts the squash-merge comment; canonical `CONVENTIONAL_COMMIT_TYPES` source
   - pr_delta.py: shared delta-mode thresholds/regex for the pr-manager agent
   - slow_tests_report.py: `forge-slow-tests-report` — parses pytest `--durations` sections from a log (or stdin), merges across batches, prints the slowest tests; read-only CI/local reporter (exempt in `cli_wiring_exempt.toml`)
   - forge_config.py: `forge-config` — lists every `[tool.forge.*]` key forge reads (value/default + description), names native sections like `[tool.interrogate]`, and advises on recommended-but-unset config; read-only, surfaced by `install-forge-bootstrap`
   - fix_ruff.py: `fix-forge-ruff` — runs `ruff format` + `ruff check --fix --unsafe-fixes`, re-stages modified tracked files, writes `code_health/ruff.log`
   - verify_docstrings.py: `verify-forge-docstrings` — docstring accuracy
   - verify_docstring_coverage.py: `verify-forge-docstring-coverage` — full-codebase docstring coverage % (interrogate wrapper) + optional `.badges/docstring-coverage.svg`
   - verify_repo_structure.py: `verify-forge-repo-structure` — repo
     structure drift check
   - verify_test_naming.py: `verify-forge-test-naming` — test naming check
   - verify_manifest.py: `verify-forge-manifest` — `.claude-plugin/*.json` JSON validation
   - verify_cli_wiring.py: `verify-forge-cli-wiring` — checks every `[project.scripts]` CLI is reachable from a wiring source; backs the `cli_wiring` step
   - verify_doc_consistency.py: `verify-forge-doc-consistency` — checks every `[project.scripts]` CLI is documented in `docs/cli-reference.md`; backs the opt-in `doc_consistency` pre-commit step (non-blocking)
   - verify_cve_usage.py: `verify-forge-cve-usage` — usage-scoped second stage on `pip_audit`; intersects live pip-audit CVE IDs with a consumer `cve_usage_patterns.toml` map and greps source for the patterns; backs the opt-in `cve_usage` pre-commit step (non-blocking). `--audit-json` reuses the `pip_audit` step's scan (one pip-audit run/commit); `--list-inactive` reports dormant map entries (read-only)
   - pip_audit_json.py: shared single-invocation pip-audit JSON helper (`run_json` + `ids_from_data` / `has_vulns` / `render_report`); the neutral seam both `precommit.step_pip_audit` and `verify_cve_usage` depend on so pip-audit runs once per commit
   - install_readme_badges.py: `install-forge-readme-badges` — write/verify a drift-aware README status-badge managed block (shields.io + local docstring-coverage SVG); opt-in via `[tool.forge.badges]`; `--check` mode
   - verify_plugin_version.py: `verify-forge-plugin-version` — rolling-next guard (plugin.json["version"] > latest git tag)
   - verify_main_tags.py: `forge-check-main-tags` — verify/repair minor-boundary (`vX.Y.0`) tag placement on the base branch
   - verify_changelog_history.py: `verify-forge-changelog-history` — guard that a promotion branch (base merged in) retains every curated `## vX.Y.0` CHANGELOG heading on the base branch
   - gen_cli_reference.py: `forge-gen-cli-reference` — CLI reference
     doc generator
   - gen_api_digest.py: `forge-gen-api-digest` — public-symbol API
     digest generator
   - gen_c4.py: `forge-gen-c4` — emits a C4 architecture model from the import graph + a `[tool.forge.c4]` / `c4.toml` model skeleton; `--format dsl` (Structurizr + managed README block), `--format html` (self-contained offline **per-view tabbed** Mermaid view laid out by the **ELK** engine, vendored `mermaid.min.js` + ELK loader, dagre fallback; `direction`/`edges` config; any-element `[[relationship]]` endpoints), `--format mermaid` (raw); `--check` drift mode backs the opt-in `c4` pre-commit step; opt-in, self-skips when unconfigured
   - gen_commit_types.py: `forge-gen-commit-types` — generates the conventional-commit type list managed block (parity with pr_squash_comment)
   - gen_common.py: shared drift-check helper for the `forge-gen-*`
     doc generators
   - doctor.py: `forge-doctor` — environment diagnostics
   - install_githooks.py: `install-forge-githooks` — git hook installer (managed marker carries `body-sha` only — never the forge version, so wrappers stay byte-stable across bumps; the version lives in the gitignored `.forge-hook-version` sidecar; modified wrappers survive refresh)
   - post_merge.py: `forge-post-merge` — managed post-merge git-hook entrypoint (foundation drift check + backgrounded self-refresh of hook wrappers)
   - post_checkout.py: `forge-post-checkout` — managed post-checkout git-hook entrypoint (branch-flag-guarded foundation drift check)
   - _hook_helpers.py: private shared helper used by `post_merge` and `post_checkout` (drift-check sequence)
   - install_claudemd.py: `install-forge-claude-md` — CLAUDE.md scaffolder
   - install_claude_settings.py: `install-forge-claude-settings` — write/verify `.claude/settings.json` per-repo plugin enablement (marketplace + `enabledPlugins`); ref tracks the pip pin; idempotent + merge-preserving; `--check` mode
   - claude_settings_schema.py: shared `.claude/settings.json` forge-block schema (marketplace key path, `forge@forge` id, scaffold) — single source of truth for the write side (install_claude_settings) and read side (install_claudemd channel detection)
   - install_labels.py: `install-forge-labels` — GitHub label installer
   - install_bootstrap.py: `install-forge-bootstrap` — one-shot umbrella that runs every installer + generator in dependency order
   - upgrade.py: `forge-upgrade` — two-phase consumer upgrade flow (rewrite pin → user runs pip → `--continue` re-syncs artifacts)
   - git_utils.py: shared git helpers and CLI logging setup
   - import_graph.py: `forge.import_graph` — shared AST import primitives (`extract_import_targets`, `resolve_module_name`) used by `audit.deps` (and the planned `smart_test`, #8)
   - run_context.py: `forge.run_context` — CI vs workstation detection (`is_non_interactive`, `git_auth_mode`, `progress_logger`) per FOUNDATION §15

2. **Audit Subpackage (`src/forge/audit/`)**
   - common.py: shared helpers (scope enum, file iteration)
   - all.py: `forge-audit-all` — run every audit check
   - agents.py: `forge-audit-agents` — agent-template conformance audit (word count, FOUNDATION restatements, missing sections; non-blocking)
   - claims.py: `forge-audit-claims` — documentation claim verification
   - data.py: `forge-audit-data` — data file audit
   - deps.py: `forge-audit-deps` — dependency audit
   - dup.py: `forge-audit-dup` — duplicate code detection
   - orphans.py: `forge-audit-orphans` — dead code detection
   - suppressions.py: `forge-audit-suppressions` — noqa/ignore audit

3. **Smart-test Subpackage (`src/forge/smart_test/`)** — `forge-smart-test`, change-driven test selection by import depth (#8)
   - git_helpers.py: diff-base resolution + changed-`.py` enumeration (committed delta + staged/unstaged/untracked), layered on `git_utils`
   - dependencies.py: reverse test→source import graph (built on `import_graph`) + depth expansion; `SelectionPlan`, `render_plan`
   - runner.py: import-cache hygiene + a single deterministic `pytest` invocation per batch (coverage only on `full`)
   - coverage.py: opt-in coverage-validated selection — maps changed lines → covering tests via per-test coverage contexts (json or `.coverage` DB); unioned into the static pass
   - cli.py: `forge-smart-test` — `--depth 0/1/2/full`, `--show-files`, `--coverage`, `--base`, `--coverage-db`, `--from-commit-message`; depth batching with fail-fast; writes `code_health/smart_test.log`

4. **Package Data (`src/forge/data/`)**
   - FOUNDATION.md: shipped copy of the foundation document (symlink)
   - CHANGELOG.md: shipped copy of the changelog (symlink) — read by `forge-upgrade` to surface consumer-action upgrade notes
   - mermaid.min.js: vendored Mermaid UMD bundle (MIT, pinned) — copied next to `forge-gen-c4 --format html` output so the diagram renders offline
   - mermaid-layout-elk.iife.min.js: vendored Mermaid v11 ELK layout loader, re-bundled to a classic-script IIFE (esbuild, chunks inlined) so it loads from `file://` where the upstream ESM build can't; the HTML registers it for clean cross-cluster layout with a dagre fallback (MIT, pinned)
   - VENDORED.md: provenance record (URL, version, SHA-256, rebuild command) for vendored third-party assets under `data/`

## Agents Directory (`agents/`)

Foundation agents shipped via the Claude Code plugin. ``_TEMPLATE.md``
documents the canonical agent shape (frontmatter, length budget,
ownership model) — see [FOUNDATION §11](FOUNDATION.md#11-agent-boundary-protocol).

- _TEMPLATE.md: canonical agent template (excluded from plugin auto-discovery via underscore prefix)
- design-checker.md: design review agent
- docs-types-checker.md: docs and type-hint checker agent
- git-commit-push.md: commit and push agent
- issue-triage.md: GitHub issue triage agent
- knowledge-search.md: grounded knowledge retrieval agent
- perf-optimizer.md: performance optimization agent
- pr-manager.md: PR lifecycle agent
- precommit-fixer.md: pre-commit report dispatcher (reads `code_health/*.log`, delegates per failure type)
- security-checker.md: security review agent
- test-advisor.md: test coverage planning + review agent
- test-writer.md: test implementation agent
- weekly-summary.md: weekly activity summary agent

## Skills Directory (`skills/`)

Slash-command skills auto-discovered by the Claude Code plugin. Each
subdirectory holds a single `SKILL.md`:

- commit/: standard commit flow
- fix/: invoke precommit-fixer to clear all pre-commit failures
- next/: clean up state and pick next task
- pr/: full PR finalization flow
- review/: address PR review comments
- c4/: build a C4 architecture model — reason out context/containers/components into c4.toml, then run forge-gen-c4
- test/: write tests via the test agents (advisor → writer → review → precommit-fixer)
- triage/: issue backlog triage
- weekly/: weekly summary report

## Claude Hooks Directory (`claude-hooks/`)

Shell hooks referenced by `plugin.json` for Claude Code safety
enforcement:

- block_claude_attribution.sh: block AI attribution in commits
- block_continuation_delete.sh: protect `.plan/CONTINUATION.md`
- block_force_push.sh: block force pushes
- block_install_deps.sh: block dependency installation
- block_protected_branches.sh: block direct pushes to protected branches (`[tool.forge].base_branch` + `dev_branch`)
- block_no_verify.sh: block `--no-verify`
- block_pr_merge.sh: block autonomous PR merges
- block_protected_files.sh: protect foundation-owned files
- check_commit_format.sh: enforce conventional commit format
- check_foundation_sync.sh: verify FOUNDATION.md sync
- warn_pr_checks.sh: warn on PR check status
- block_raw_git.sh: hard-block raw `git commit` / `git push` from agents (bypass: `git-commit-push` subagent)
- block_raw_ruff.sh: hard-block raw `ruff check` / `ruff format` from agents (no bypass — agents use forge-precommit)

## Plugin Manifest (`.claude-plugin/`)

- plugin.json: Claude Code plugin manifest (rolling-next version)
- marketplace.json: marketplace listing manifest

## Git Hooks Directory (`.githooks/`)

- install.sh: configure `core.hooksPath` to this directory
- pre-commit: pre-commit gate (delegates to `forge-precommit`)
- post-checkout: post-checkout hook
- post-merge: post-merge hook

## Tests Directory (`tests/`)

Pytest suite mirroring the `src/forge/` layout:

1. **Package Tests**
   - conftest.py: shared pytest fixtures
   - test_config.py: tests for config (model/tool-root resolution)
   - test_continuation_append.py: tests for continuation_append
   - test_doctor.py: tests for doctor
   - test_fix_ruff.py: tests for fix_ruff
   - test_gen_api_digest.py: tests for gen_api_digest
   - test_gen_c4.py: tests for gen_c4 (C4 / Structurizr DSL generator)
   - test_gen_cli_reference.py: tests for gen_cli_reference
   - test_gen_commit_types.py: tests for gen_commit_types
   - test_gen_common.py: tests for gen_common shared helpers
   - test_git_utils.py: tests for git_utils (shared CLI helpers)
   - test_import_graph.py: tests for import_graph (shared AST import primitives)
   - test_install_bootstrap.py: tests for install_bootstrap
   - test_install_claudemd.py: tests for install_claudemd
   - test_install_claude_settings.py: tests for install_claude_settings
   - test_claude_hooks.py: black-box tests for the `claude-hooks/*.sh` safety hooks (subprocess + JSON stdin)
   - test_claude_settings_schema.py: tests for the shared claude_settings_schema module (scaffold copy, write/read round-trip)
   - test_upgrade.py: tests for upgrade (forge-upgrade CLI)
   - test_install_githooks.py: tests for install_githooks
   - test_hook_helpers.py: tests for _hook_helpers shared drift-check helper
   - test_post_merge.py: tests for post_merge (forge-post-merge CLI)
   - test_post_checkout.py: tests for post_checkout (forge-post-checkout CLI)
   - test_install_labels.py: tests for install_labels
   - test_manifests.py: tests for plugin manifests
   - test_next_prep.py: tests for next_prep
   - test_pr_delta.py: tests for pr_delta shared thresholds/regex
   - test_pr_squash_comment.py: tests for pr_squash_comment
   - test_precommit.py: tests for precommit dispatcher
   - test_run_context.py: tests for run_context (CI vs workstation detection)
   - test_smart_test_git_helpers.py: tests for smart_test.git_helpers
   - test_smart_test_dependencies.py: tests for smart_test.dependencies
   - test_smart_test_runner.py: tests for smart_test.runner
   - test_smart_test_cli.py: tests for smart_test.cli
   - test_verify_docstrings.py: tests for verify_docstrings
   - test_verify_docstring_coverage.py: tests for verify_docstring_coverage
   - test_verify_manifest.py: tests for verify_manifest
   - test_verify_doc_consistency.py: tests for verify_doc_consistency
   - test_verify_cve_usage.py: tests for verify_cve_usage (active/inactive CVE, usage/no-usage, comment + self exclusion, pip-audit-missing skip, `--audit-json` sidecar reuse, `--list-inactive` reporter)
   - test_pip_audit_json.py: tests for pip_audit_json (run_json binary-missing/parse paths, ids_from_data alias collection + malformed-shape filtering, render_report primary-id-only, advisory-count invariant)
   - test_install_readme_badges.py: tests for install_readme_badges (badge sources, drift-aware injection, opt-in gating, --check)
   - test_verify_plugin_version.py: tests for verify_plugin_version
   - test_verify_main_tags.py: tests for verify_main_tags
   - test_verify_changelog_history.py: tests for verify_changelog_history
   - test_verify_repo_structure.py: tests for verify_repo_structure
   - test_verify_test_naming.py: tests for verify_test_naming

2. **Audit Tests (`tests/audit/`)**
   - test_agents.py: tests for audit.agents
   - test_all.py: tests for audit.all (orchestrator)
   - test_claims.py: tests for audit.claims
   - test_common.py: tests for audit.common
   - test_data.py: tests for audit.data
   - test_deps.py: tests for audit.deps
   - test_dup.py: tests for audit.dup
   - test_orphans.py: tests for audit.orphans
   - test_suppressions.py: tests for audit.suppressions

## Dev Directory (`dev/`)

Forge's own bootstrap tooling (not a consumer pattern):

- README.md: dev environment documentation
- setup.sh: conda env + editable install + hooks + doctor
- test-matrix.sh: multi-version test matrix runner

## Documentation (`docs/`)

- api-digest.md: generated public-symbol index (`forge-gen-api-digest`)
- audit-pack.md: audit suite documentation
- c4-architecture.md: design & rationale for forge-gen-c4 + the /c4 skill (the implemented C4 generator)
- ci-access.md: how a consumer's CI runner pulls forge
- claude-code-plugin.md: optional Claude Code plugin install + extension
- cli-reference.md: generated CLI reference (`forge-gen-cli-reference`)
- adopting.md: modular adoption guide — three independent install tracks (CLIs / + git hooks / + plugin) + "what lands on disk" table + drift/upgrade explainer
- configuration.md: complete `[tool.forge.*]` config reference + setup guide (written counterpart to `forge-config --list`)
- release-process.md: forge-only single source of truth for versioning + dev→main promotion + the invariant→test contract
- customizing-precommit.md: adding repo-specific steps to `.githooks/pre-commit`
- security.md: security policy and review documentation
- standalone-installers.md: per-installer reference for manual usage (sibling of `install-forge-bootstrap`)

### Proposals (`docs/proposals/`)

Architecture RFCs — aspirational, not descriptions of current behavior. Each carries its own accept/reject status.

- rust-core.md: RFC for splitting forge into a Rust governance-core binary + optional Python analysis pack

## Configuration Files

1. **Python Package Configuration**
   - pyproject.toml: package metadata, dependencies, entry points

2. **Code Quality**
   - ruff.toml: ruff lint and format configuration (strict, ALL rules)
   - pyrefly.toml: pyrefly type-checker config for the opt-in `typecheck` step (strict return-type checking; interrogate's attrs `__init__` silenced via `replace-imports-with-any`)
   - c4.toml: standalone C4 architecture-model skeleton consumed by `forge-gen-c4` (kept out of pyproject; pointed at by `[tool.forge.c4].config`)

3. **Documentation**
   - CLAUDE.md: project guidance for Claude Code and developers
   - FOUNDATION.md: shared engineering principles (single source of truth)
   - README.md: main repository documentation
   - REPO_STRUCTURE.md: this file
   - CHANGELOG.md: main-only release history (Keep a Changelog format)
   - CONTRIBUTING.md: contribution guidelines
   - LICENSE: MIT license
   - docs/architecture.dsl: generated C4 model (Structurizr DSL) — `forge-gen-c4` output

## Additional Directories

1. **GitHub Infrastructure (`.github/`)**
   - workflows/: GitHub Actions CI workflows

2. **Code Health (`code_health/`)**
   - Pre-commit check logs (gitignored): `ruff.log`,
     `docstring_verification.log`, `test_naming_check.log`,
     `repo_structure_check.log`, etc.

3. **Continuation State (`.plan/`)**
   - `CONTINUATION.md`: cross-session handoff state (gitignored).
