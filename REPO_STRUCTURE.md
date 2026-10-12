# Repo Structure

Checked on every commit by `verify-forge-repo-structure`: every listed file and folder exists; every backticked file path in a description (a known file type, or ending in `/`) exists; and every folder with its own heading lists all its files and subfolders that git does not ignore, except hidden ones, `__init__.py`, `conftest.py` and build or cache output (`build`, `dist`, `tmp`, `code_health`, `*.egg-info`, compiled and editor-backup files), unless the heading is marked `<!-- summary -->`.

## Forge Package (`src/forge/`)

1. **CLI Modules**
   - precommit.py: `forge-precommit` — pre-commit dispatcher; most steps shell out to their own SRP CLI, a few (env_sync, pip_audit) run in-process for speed / single-invocation sharing; every full run wall-clocks each step and writes `code_health/precommit_timing.log` (per-step elapsed + total, `elapsed_s` in `--json`; a `--only` run writes `code_health/precommit_only_timing.log`); `--verdict` reports whether every enabled step passed on the current tree
   - regen_docs.py: when `regen_docs` must rebuild the generated docs — input fingerprints from index blob shas + the installed forge, compared with a per-clone record under `.git/forge/` of the last successful build
   - next_prep.py: `forge-next-prep` — refresh main, optional rolling-next tag bump, prune stale branches; used by `/next` skill
   - emergency_state.py: dependency-free reader for the `forge-emergency` sentinel (fails closed: absent, unreadable or corrupt means "not armed"), split out so pre-commit gates can check it without importing the CLI's PR-planning surface
   - emergency.py: `forge-emergency` — one-shot deferred-verification bypass (start/status/consume/end): ledger issue first, gitignored sentinel, `forge-pr-create` consumes the single allowed `wrapup-mode: emergency` publication, retroactive verification closes the ledger after delivery; pre-commit and §2 hooks never relieved
   - rebump.py: `forge-rebump` — mechanical post-merge version-slot resolver: classifies a feature branch's bump intent from its fork-point manifest delta (merge stages mid-merge, merge-base on a clean tree), takes the next open slot above the latest tag, restacks/retitles the CHANGELOG in shared-heading mode (fragments mode: no-op), stages, never commits; refuses on unrelated conflicts
   - release.py: `forge-release` — single-track release orchestrator for tag-versioned (setuptools-scm) consumer repos: guards (clean tree, on base branch, single-track model, CHANGELOG entry) → annotated tag + push (exempt in `cli_wiring_exempt.toml`)
   - continuation.py: `forge-continuation` — `state [--with-pr] [--attempt …]` rewrites `.plan/CONTINUATION.md`'s generated status panel (PR mergeability, CI and wrap-up freshness read from GitHub with `--with-pr`, otherwise carried forward with their as-of SHA); `check` reports the written section's line usage against `judgment_max_lines`; called by `forge-pr-wrapup post` and the PreCompact hook
   - continuation_state.py: the status panel's format, git-only fields and byte-preserving rewrite (sanitised values, fixed size, legacy-ledger migration, gitignore and CI guards) — split from `continuation.py` so `forge-precommit` records every commit attempt in-process without importing the GitHub readers, whose modules import `forge.precommit`
   - pr_create.py: `forge-pr-create` — publishes a pull request from the checkout holding the branch, so the branch is where the process stands rather than inferred from command text; verifies that checkout's wrap-up against its own HEAD, re-runs the classifier for a `wrapup-mode: light` wrap-up and consumes the sentinel for an `emergency` one, and forwards only an allowlist of extra flags so nothing it sets itself can be overridden
   - pr_wrapup.py: `forge-pr-wrapup` — `compose` renders `code_health/pr_wrapup.md` with fill-in slots for the judgment parts; `validate` refuses unfilled slots and enforces the report-by-exception rule (one line per clean section, one summary line, findings-scaled word budget, no AI attribution); `post` refuses a stale head or a branch that conflicts with or is behind its base (exit 3), refreshes CI Status and Issue Management, posts with a marker (collapsing earlier wrap-ups, keeping the squash comment newest) and refreshes the CONTINUATION status panel
   - pr_wrapup_compose.py: pure wrap-up rendering for `forge-pr-wrapup compose` — header and mode line, per-mode reporter sections, Code Quality by exception, CI rollup summary, closing-keyword line, and the `<!-- forge:fill … -->` slots
   - gh_comments.py: shared GitHub comment plumbing for the PR-comment CLIs — paginated marker listing, post, delete, edit, and the FOUNDATION §2 attribution gate
   - pr_squash_comment.py: `forge-pr-squash-comment` — validates + posts the squash-merge message (title and body in separate fences), forces the PR title to match, and keeps that comment the PR's newest; canonical `CONVENTIONAL_COMMIT_TYPES` source
   - changelog_fragments.py: `forge-changelog` — changelog fragments (changelog.d/): per-PR `<slug>.<type>.md` files with level-only `bump:` front-matter, validated by the fragment gate and assembled into CHANGELOG.md once at release (single writer; zero merge conflicts by construction); `next-version` prints the computed next release, tag-aware (fragments already in a tag's tree assemble under that tag; only unreleased ones mint: latest tag + their max level) and `release` assembles per tag then under the minted version, writes plugin.json (when present), and stages everything (never commits); `restrand` subcommand mechanically repairs stranded entries in shared-heading repos (no manifest needed; stages, never commits); `auto-tag` cuts and pushes the tag-per-merge release tag in CI (fragments not in latest tag's tree -> max level -> next tag)
   - pr_delta.py: the finalization-path classification primitives — every threshold, glob, and predicate (delta, docs-only, regen-only, light-code) consumed by `forge-pr-plan`, the pr-manager agent, and the wrap-up publish hook — plus the closing-keyword finder and fence stripper the wrap-up CLIs share
   - pr_plan.py: `forge-pr-plan` — deterministic finalization-path classifier for the `/pr` skill; composes the pr_delta primitives over the real diff and emits the JSON plan (mode/reporters/precommit_scope/reasons); `--freshness --pr N` is the read-only wrap-up-staleness verdict the FOUNDATION §6 monitor polls (and forge-emergency's repayment check reuses); `--evidence` also writes the review evidence pack
   - pr_evidence.py: the review evidence pack `forge-pr-plan --evidence` writes to `code_health/pr_evidence.log` — PR identity and diff, log freshness and pre-commit step markers, dup/layering findings at changed scope plus other audit logs' freshness, generated-artifact checks, api-digest changes, closing keywords and fragment presence; each item rendered `unavailable` on failure, the audits and generated-artifact checks under a timeout
   - slow_tests_report.py: `forge-slow-tests-report` — parses pytest `--durations` sections from a log (or stdin), merges across batches, prints the slowest tests; `--baseline`/`--update-baseline` compare against the committed `.forge-test-durations.json` (WARN-shaped, never gates; absent or malformed baseline = one skip line, not a wall of new-slow); `--coverage-json` ranks test functions by unique covered statements per second from a `coverage json --show-contexts` export; wired via the `/perf` skill
   - telemetry.py: `forge-telemetry` — process-tree RSS + host CPU sampler around a wrapped command; per-run log/plot artifacts, append-only `code_health/telemetry_history.log`, `--history` trend reader
   - agent_profile.py: `forge-agent-profile` — where agent and subagent time goes: pairs the `log_agent_timing` hook ledger (`code_health/agent_timing.jsonl`) with the subagent transcripts it names into per-agent-type wall/active time, slowest runs, per-tool cost, loop suspects, and `forge:precommit-fixer` cap breaches; append-only `code_health/agent_profile_history.log`; wired via `/perf` and `/report-to-forge`
   - ledger.py: the one writer + parser for the append-only `key=value` ledgers under `code_health/` (telemetry_history.log, smart_test_history.log, agent_profile_history.log)
   - config.py: loader for the `[tool.forge]` table in a repo's `pyproject.toml` (base branch and the other forge settings, defaulting to single-branch `main` behaviour)
   - memory_audit.py: `forge-memory-audit` — `status` counts agent memory notes added since the last audit stamp and says whether to offer `/memory-audit`; `stamp` rewrites the stamp after an audit
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
   - verify_agent_doc.py: `verify-forge-agent-doc` — keeps a hand-maintained agent-architecture doc in sync (coverage of every agent/skill + no dangling hook/CLI/skill refs; `--diff` Layer-2 helper); backs the self-skipping `agent_doc` step
   - verify_cve_usage.py: `verify-forge-cve-usage` — usage-scoped second stage on `pip_audit`; intersects live pip-audit CVE IDs with a consumer cve_usage_patterns.toml map and greps source for the patterns; backs the opt-in `cve_usage` pre-commit step (non-blocking). `--audit-json` reuses the `pip_audit` step's scan (one pip-audit run/commit); `--list-inactive` reports dormant map entries (read-only)
   - pip_audit_json.py: shared single-invocation pip-audit JSON helper (`run_json` + `ids_from_data` / `has_vulns` / `render_report`); the neutral seam both `precommit.step_pip_audit` and `verify_cve_usage` depend on so pip-audit runs at most once per invocation
   - install_readme_badges.py: `install-forge-readme-badges` — write/verify a drift-aware README status-badge managed block (shields.io + local docstring-coverage SVG); opt-in via `[tool.forge.badges]`; `--check` mode
   - verify_plugin_version.py: `verify-forge-plugin-version` — rolling-next guard (plugin.json["version"] > latest git tag)
   - gen_cli_reference.py: `forge-gen-cli-reference` — CLI reference
     doc generator
   - gen_api_digest.py: `forge-gen-api-digest` — public-symbol API
     digest generator
   - gen_c4.py: `forge-gen-c4` — emits a C4 architecture model from the import graph + a `[tool.forge.c4]` / `c4.toml` model skeleton; `--format dsl` (Structurizr + managed README block), `--format html` (self-contained offline **per-view tabbed** Mermaid view laid out by the **ELK** engine, vendored `data/mermaid.min.js` + ELK loader, dagre fallback; `direction`/`edges` config; any-element `[[relationship]]` endpoints), `--format pdf` (vector PDF via an already-installed headless browser) / `--format svg` (one vector SVG per view, same browser path), `--format mermaid` (raw); `--check` drift mode backs the opt-in `c4` pre-commit step; opt-in, self-skips when unconfigured
   - gen_commit_types.py: `forge-gen-commit-types` — generates the conventional-commit type list managed block (parity with pr_squash_comment)
   - gen_common.py: shared drift-check helper for the `forge-gen-*`
     doc generators
   - doctor.py: `forge-doctor` — environment diagnostics
   - version_surfaces.py: the three version surfaces of one forge install (pip package, git-hook sidecar, cached Claude Code plugin) plus the editable-install origin and the per-surface remediation strings — read once here for `forge-doctor`'s advisory and `forge-precommit`'s `env_sync` / `plugin_sync` gates
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
   - resync.py: `forge-resync` — regenerate forge-managed artifacts and open a dedup-guarded resync PR (companion to `upgrade.py`'s pin-rewrite flow); `--resolve-conflicts [--dry-run]` resolves a merge whose only conflicts are forge-generated artifacts by regenerating each from the merged tree (`pr_delta.REGEN_COMMANDS`), verifying with its `--check`, and staging — refusing if any other path conflicts
   - git_utils.py: shared git helpers and CLI logging setup (public API for consumers: `latest_v_tag`, `parse_semver`, `next_version`, `run_git`, `configure_cli_logging`)
   - changelog.py: shared `## vX.Y.Z` CHANGELOG heading recognition (`release_headings`, `changelog_lacks_entry`) — single source for release and the changelog_updated step; public API for consumers
   - import_graph.py: `forge.import_graph` — shared AST import primitives (`extract_import_targets`, `resolve_module_name`, `closest_known`) used by `audit.deps` and `smart_test.dependencies`
   - run_context.py: `forge.run_context` — CI vs workstation detection (`is_non_interactive`, `git_auth_mode`, `progress_logger`) per FOUNDATION §15
   - scratch_repo.py: `forge-scratch-repo` — isolated scratch copies for agent experiments (FOUNDATION §11 "Probing"): `snapshot [--ref REF | --worktree]` copies the checkout into a fresh one-commit repo, `build --spec` makes a repo from a JSON spec of commits; reads the source checkout only, refuses a non-ignored target inside a work tree

2. **Audit Subpackage (`src/forge/audit/`)**
   - common.py: shared helpers (scope enum, file iteration)
   - all.py: `forge-audit-all` — run every audit check
   - agents.py: `forge-audit-agents` — agent-template conformance audit (word count, FOUNDATION restatements, missing sections; non-blocking)
   - claims.py: `forge-audit-claims` — documentation claim verification
   - data.py: `forge-audit-data` — data file audit
   - deps.py: `forge-audit-deps` — dependency audit
   - dup.py: `forge-audit-dup` — duplicate code detection
   - layering.py: `forge-audit-layering` — positive layer-composition contracts (`[[tool.forge.layering.layer]]` `composes_all_of`, per direct child over the transitive import closure); blocking only on added/moved modules; backs the opt-in `layering` pre-commit step
   - orphans.py: `forge-audit-orphans` — dead code detection
   - suppressions.py: `forge-audit-suppressions` — noqa/ignore audit

3. **Smart-test Subpackage (`src/forge/smart_test/`)** — `forge-smart-test`, change-driven test selection by import depth (#8)
   - git_helpers.py: diff-base resolution + changed-`.py` enumeration (committed delta + staged/unstaged/untracked), layered on `git_utils`
   - dependencies.py: reverse test→source import graph (built on `import_graph`) + depth expansion; `SelectionPlan`, `render_plan`
   - runner.py: import-cache hygiene + a single deterministic `pytest` invocation per batch (coverage only on `full`)
   - run_log.py: single-writer lock (refuses a concurrent run, takes over a dead holder) and the incremental log sink — stamped at run start, appended per tier, closed with a `# complete:` line
   - lifecycle.py: test-lifecycle mechanics — development-marker detection, 30d lifecycle-skip filter, the tracked `.forge-full-run` 48h stamp, full-run history ledger, depth-2 differential check
   - coverage.py: opt-in coverage-validated selection — maps changed lines → covering tests via per-test coverage contexts (json or `.coverage` DB); unioned into the static pass
   - cli.py: `forge-smart-test` — `--depth 0/1/2/full`, `--show-files`, `--coverage`, `--base`, `--coverage-db`, `--from-commit-message`; depth batching with fail-fast; writes `code_health/smart_test.log`

4. **Package Data (`src/forge/data/`)**
   - FOUNDATION.md: shipped copy of the foundation document (symlink)
   - CHANGELOG.md: shipped copy of the changelog (symlink) — read by `forge-upgrade` to surface consumer-action upgrade notes
   - mermaid.min.js: vendored Mermaid UMD bundle (MIT, pinned) — copied next to `forge-gen-c4 --format html` output so the diagram renders offline
   - mermaid-layout-elk.iife.min.js: vendored Mermaid v11 ELK layout loader, re-bundled to a classic-script IIFE (esbuild, chunks inlined) so it loads from `file://` where the upstream ESM build can't; the HTML registers it for clean cross-cluster layout with a dagre fallback (MIT, pinned)
   - plugin-roster.toml: shipped roster of forge's plugin skills and hooks, read by `verify-forge-agent-doc` so consumer agent docs can name them; regenerated and drift-checked by `tests/test_verify_agent_doc.py`
   - docs/: symlinks to the `forge-docs/` reference pages, shipped so `install-forge-claude-md` can write them into consumer repos
   - VENDORED.md: provenance record (URL, version, SHA-256, rebuild command) for vendored third-party assets in this folder

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
- prior-art.md: run before creating a file or top-level symbol — REUSE / EXTEND / NEW verdict grounded in named queries against the api-digest and dup log
- precommit-fixer.md: pre-commit report dispatcher (reads `code_health/*.log`, delegates per failure type)
- security-checker.md: security review agent
- test-advisor.md: test coverage planning + review agent
- test-writer.md: test implementation agent
- weekly-summary.md: weekly activity summary agent

## Skills Directory (`skills/`)

Slash-command skills auto-discovered by the Claude Code plugin. Each
subdirectory holds a single SKILL.md:

- c4/: build a C4 architecture model — reason out context/containers/components into c4.toml, then run forge-gen-c4
- commit/: standard commit flow
- fix/: invoke precommit-fixer to clear all pre-commit failures
- memory-audit/: audit agent memory against the repo's rule surface
- next/: clean up state and pick next task
- perf/: opt-in perf read-side — analyze ledgers/baseline, file findings as issues (report), re-check open performance issues (watch)
- plan-batch/: coordinator that drafts several screened issues at once via drafter agents forbidden to mutate anything, relaying each draft for explicit validation
- plan-issue/: human-validated planning for one issue — records a plan-validated execution spec + plan-ready label
- pr/: full PR finalization flow
- pr-comments/: address PR review comments
- report-to-forge/: turn an observed defect in a shipped forge process into a filed upstream issue (versions captured, evidence verbatim, redaction confirmed)
- sentinel/: autonomous executor of plan-ready issues — to PR wrap-up, never merging
- smart-test/: run only the tests a change set affects, in depth tiers
- test/: write tests via the test agents (advisor → writer → review → precommit-fixer)
- triage/: issue backlog triage
- weekly/: weekly summary report

## Claude Hooks Directory (`claude-hooks/`)

Shell hooks referenced by `.claude-plugin/plugin.json` for Claude Code safety
enforcement:

- block_branch_deletion.sh: block agent deletion of protected remote branches (no bypass)
- block_claude_attribution.sh: block AI attribution in commits
- block_continuation_delete.sh: protect `.plan/CONTINUATION.md`
- block_force_push.sh: block force pushes
- block_forge_docs_edits.sh: block agent edits inside the forge-managed forge-docs/ mirror
- block_git_rebase.sh: block `git rebase` and `git pull --rebase` from agents (no bypass — sync via plain base merge)
- block_install_deps.sh: block dependency installation (pip / conda / pipenv / poetry / uv / pixi; pixi governed by a verb allowlist that fails closed, the other five by denylists)
- block_protected_branches.sh: block direct pushes to the protected base branch (`[tool.forge].base_branch`)
- block_no_verify.sh: block `--no-verify`
- block_pr_merge.sh: block autonomous PR merges
- block_protected_files.sh: protect foundation-owned files
- check_commit_format.sh: enforce conventional commit format
- check_foundation_sync.sh: verify FOUNDATION.md sync
- warn_pr_checks.sh: warn on PR check status
- block_git_destructive.sh: block destructive git recovery verbs from agents — all `git reset` forms, forced `git clean`, literal `git checkout .` / `git restore .`, `git stash drop`/`clear`, untracked-including stash (`-u`/`-a`) (no bypass — stop-and-report is the sanctioned recovery)
- block_amend_pushed_commit.sh: block `git commit --amend` when `HEAD` already exists on a remote-tracking ref — the single-commit form of a rebase; unpushed amends stay allowed (no bypass; live-state check anchored to the payload cwd)
- git_anchor.sh: NOT a hook — sourced library holding the shared `GIT_ANCHOR`/`SEG_ANCHOR`/`GH_ANCHOR` invocation anchors, the `command_positions` pre-pass (what the shell would actually run: quoted text and heredoc bodies blanked, `$(…)` and `bash -c`/`eval`/`ssh` payloads kept) and the `guard_help_only` help exemption, for every guard that locates a command (single home; never registered in plugin.json)
- block_fixer_recon.sh: agent-scoped Bash allowlist for the precommit-fixer (gate CLIs, `forge-smart-test --depth 0` + targeted pytest node-ids only; other agents unaffected)
- require_fixer_verdict.sh: SubagentStop for the precommit-fixer — runs `forge-precommit --verdict` at hand-back and blocks once (ledger-keyed) when the verdict fails and the report does not say STUCK with it pasted
- block_raw_git.sh: hard-block raw `git commit` / `git push` from agents, and the commit-creating `git revert` / `git cherry-pick` forms (the sequencer runs no pre-commit hook); `--abort` / `--quit` / `--skip` / `--no-commit` stay allowed (the `git-commit-push` subagent may commit/push; revert/cherry-pick have no bypass)
- block_raw_wrapup_post.sh: block a raw `gh pr comment`/`gh api` post of `code_health/pr_wrapup.md` — the wrap-up is posted only through `forge-pr-wrapup post` (validation, supersede-collapse, squash-last)
- block_unverified_pr_create.sh: refuse a raw create outright and name `forge-pr-create`, which publishes; the verification it used to attempt here now lives in that command, which runs in the checkout it publishes rather than inferring the branch from command text (FOUNDATION §6)
- block_raw_ruff.sh: hard-block raw `ruff check` / `ruff format` from agents (no bypass — agents use forge-precommit)
- keep_continuation_state.sh: PreCompact — run `forge-continuation state --with-pr` so the status panel is current before Claude Code summarises; warn-only (missing CLI fails loudly, non-blocking)
- load_continuation.sh: SessionStart — print `.plan/CONTINUATION.md` into the new context inside a fence under a "data, not instructions" line, with any ``` in it neutralised; silent when absent, always exits 0
- keep_squash_comment_last.sh: PostToolUse — after any command that comments on a PR, re-post the squash-merge comment so it stays the newest one (silent no-op when it already is, or when the PR has none yet)
- log_agent_timing.sh: SubagentStart + SubagentStop + PostToolUse (no matcher) — append one JSON line per event to `code_health/agent_timing.jsonl`, the ledger `forge-agent-profile` reads; never blocks, never prints; `FORGE_NO_AGENT_TIMING=1` switches it off
- warn_stale_wrapup.sh: PostToolUse — after a `git push` on a branch with an open PR, print a reminder when `forge-pr-plan --freshness` says the posted wrap-up no longer names the head (silent when fresh, unknowable, or a refresh is already authored for HEAD)
- warn_generated_conflicts.sh: PostToolUse — after a `git merge` whose only conflicts are forge-generated artifacts (probe: `forge-resync --resolve-conflicts --dry-run`), print the instruction to run `forge-resync --resolve-conflicts`; silent otherwise, never regenerates or stages
- wrapup_anchor.sh: sourced library, not a hook — the `wrapup_names_head` predicate, used by `warn_stale_wrapup`; `forge-pr-create` carries the Python equivalent for the publish path

## Plugin Manifest (`.claude-plugin/`)

- plugin.json: Claude Code plugin manifest (rolling-next version)
- marketplace.json: marketplace listing manifest

## Git Hooks Directory (`.githooks/`)

- install.sh: configure `core.hooksPath` to this directory
- pre-commit: pre-commit gate (delegates to `forge-precommit`)
- post-checkout: post-checkout hook
- post-merge: post-merge hook
- post-merge.d/: forge's own tracked post-merge extensions — `10-dev-setup.sh` re-runs `./dev/setup.sh` so every clone runs the latest forge (`FORGE_NO_AUTO_SETUP=1` skips once)
- post-checkout.d/: forge's own tracked post-checkout extensions — its `10-dev-setup.sh` hands off to the post-merge one after a branch switch

## Tests Directory (`tests/`)

Pytest suite mirroring the `src/forge/` layout:

1. **Package Tests**
   - conftest.py: shared pytest fixtures
   - test_config.py: tests for config (model/tool-root resolution)
   - test_continuation.py: tests for continuation (the `forge-continuation` CLI, PR-field carry-forward, `check`)
   - test_continuation_state.py: tests for continuation_state (status-panel format, byte-preserving rewrite, migration, guards)
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
   - command_positions_snapshot.json: saved output of the shared command scanner over a command corpus, pinned by `tests/test_claude_hooks.py`; regenerate with `FORGE_UPDATE_SNAPSHOT=1`
   - test_claude_settings_schema.py: tests for the shared claude_settings_schema module (scaffold copy, write/read round-trip)
   - test_upgrade.py: tests for upgrade (forge-upgrade CLI)
   - test_resync.py: tests for resync (forge-resync CLI)
   - test_install_githooks.py: tests for install_githooks
   - test_hook_helpers.py: tests for _hook_helpers shared drift-check helper
   - test_post_merge.py: tests for post_merge (forge-post-merge CLI)
   - test_post_checkout.py: tests for post_checkout (forge-post-checkout CLI)
   - test_install_labels.py: tests for install_labels
   - test_manifests.py: tests for plugin manifests
   - test_next_prep.py: tests for next_prep
   - test_emergency.py: tests for emergency (forge-emergency start/status/consume/end, ledger parsing, TTL clamping)
   - test_rebump.py: tests for rebump (forge-rebump post-merge version-slot + changelog resolver)
   - test_release.py: tests for release (forge-release CLI guards + tagging)
   - test_release_e2e.py: end-to-end single-track consumer release fixture (recipes + changelog steps)
   - test_changelog.py: tests for changelog (release_headings / changelog_lacks_entry)
   - test_pr_delta.py: tests for pr_delta classification primitives (delta, docs-only, regen-only, light-code)
   - test_pr_evidence.py: tests for pr_evidence (the `forge-pr-plan --evidence` review pack: sections, partial pack on failure, sanitization, stdout contract)
   - test_pr_squash_comment.py: tests for pr_squash_comment
   - test_precommit.py: tests for precommit dispatcher
   - test_run_context.py: tests for run_context (CI vs workstation detection)
   - test_scratch_repo.py: tests for scratch_repo (forge-scratch-repo snapshot/build)
   - test_smart_test_git_helpers.py: tests for smart_test.git_helpers
   - test_smart_test_dependencies.py: tests for smart_test.dependencies
   - test_smart_test_runner.py: tests for smart_test.runner
   - test_smart_test_cli.py: tests for smart_test.cli
   - test_smart_test_run_log.py: tests for smart_test.run_log (lock, incremental sink)
   - test_verify_docstrings.py: tests for verify_docstrings
   - test_verify_docstring_coverage.py: tests for verify_docstring_coverage
   - test_verify_manifest.py: tests for verify_manifest
   - test_verify_doc_consistency.py: tests for verify_doc_consistency
   - test_verify_agent_doc.py: tests for verify_agent_doc
   - test_verify_cve_usage.py: tests for verify_cve_usage (active/inactive CVE, usage/no-usage, comment + self exclusion, pip-audit-missing skip, `--audit-json` sidecar reuse, `--list-inactive` reporter)
   - test_pip_audit_json.py: tests for pip_audit_json (run_json binary-missing/parse paths, ids_from_data alias collection + malformed-shape filtering, render_report primary-id-only, advisory-count invariant)
   - test_install_readme_badges.py: tests for install_readme_badges (badge sources, drift-aware injection, opt-in gating, --check)
   - test_verify_plugin_version.py: tests for verify_plugin_version
   - test_verify_repo_structure.py: tests for verify_repo_structure
   - test_verify_test_naming.py: tests for verify_test_naming
   - test_agent_profile.py: tests for agent_profile (where agent and subagent time goes)
   - test_changelog_fragments.py: tests for changelog_fragments (fragment validation, discovery, assembly, and the `forge-changelog` CLI)
   - test_dev_setup_hook.py: tests for the `dev/setup.sh` auto-refresh git-hook extensions, run as real bash subprocesses
   - test_forge_config.py: tests for forge_config
   - test_gh_comments.py: tests for gh_comments (shared PR-comment plumbing: paging, filtering, attribution)
   - test_ledger.py: tests for ledger (the append-only `key=value` ledger shape)
   - test_memory_audit.py: tests for memory_audit (new-memory counting and the audit stamp)
   - test_pr_create.py: tests for pr_create (publishing a PR from the branch it verifies, against real ephemeral git repos)
   - test_pr_plan.py: tests for pr_plan (the `/pr` finalization-path classifier, `--freshness` and `--evidence`)
   - test_pr_wrapup.py: tests for pr_wrapup (validate/compose/post lifecycle and the CLI)
   - test_pr_wrapup_compose.py: tests for pr_wrapup_compose (pure wrap-up rendering)
   - test_release_tag_invariant.py: enforcing test for the release-tag invariant in `docs/release-process.md` (every tag's own tree carries its version in the manifest and the changelog)
   - test_slow_tests_report.py: tests for slow_tests_report
   - test_smart_test_coverage.py: tests for smart_test.coverage (coverage-validated selection)
   - test_smart_test_lifecycle.py: tests for smart_test.lifecycle (development markers, lifecycle skips, full-run stamp and history)
   - test_smart_test_superset.py: regression harness checking the union of forge-smart-test's three selection channels covers every guaranteed test-to-code link
   - test_telemetry.py: tests for telemetry (the resource-profiling wrapper)
   - test_version_surfaces.py: tests for version_surfaces (the three install-version readers)

2. **Audit Tests (`tests/audit/`)**
   - test_agents.py: tests for audit.agents
   - test_all.py: tests for audit.all (orchestrator)
   - test_claims.py: tests for audit.claims
   - test_common.py: tests for audit.common
   - test_data.py: tests for audit.data
   - test_deps.py: tests for audit.deps
   - test_dup.py: tests for audit.dup
   - test_layering.py: tests for audit.layering (layer-composition enforcement)
   - test_orphans.py: tests for audit.orphans
   - test_suppressions.py: tests for audit.suppressions

## Dev Directory (`dev/`)

Forge's own bootstrap tooling (not a consumer pattern):

- README.md: dev environment documentation
- setup.sh: conda env + editable install + hooks + doctor
- get_conda_env_name.sh: sourced helper resolving this clone's conda env name (`$CONDA_ENV_NAME`, then a `.conda_env_name` file at the clone root, then the caller's default) so several clones can run side by side
- test-matrix.sh: multi-version test matrix runner

## Shipped reference set (`forge-docs/`)

Canonical home of the consumer-mirrored reference pages FOUNDATION.md links
to; shipped via `src/forge/data/docs/` symlinks and written into consumer
repos by `install-forge-claude-md` (guarded there by a README notice + the
`block_forge_docs_edits` hook).

- configuration.md: complete `[tool.forge.*]` config reference + setup guide (written counterpart to `forge-config --list`)
- ci-recipe.md: consumer CI recipe (channel pin + per-PR workflow + scheduled upgrade PR)
- smart-test.md: usage guide for `forge-smart-test` — depth model, consumer guarantees, opt-in correctness extensions

## Documentation (`docs/`)

- api-digest.md: generated public-symbol index (`forge-gen-api-digest`)
- audit-pack.md: audit suite documentation
- c4-architecture.md: design & rationale for forge-gen-c4 + the /c4 skill (the implemented C4 generator)
- agent-architecture.md: hand-maintained agent × skill × hook × CLI interaction subviews by workflow phase + a FOUNDATION-enforcers view; gated by the `agent_doc` step, tracked for full drift-check by #163
- ci-access.md: how a consumer's CI runner pulls forge
- claude-code-plugin.md: optional Claude Code plugin install + extension
- cli-reference.md: generated CLI reference (`forge-gen-cli-reference`)
- adopting.md: modular adoption guide — three independent install tracks (CLIs / + git hooks / + plugin) + "what lands on disk" table + drift/upgrade explainer
- release-process.md: forge-only single source of truth for versioning + tag-on-merge releases + the invariant→test contract
- consumer-release.md: single-track (tag-versioned/setuptools-scm) consumer release recipe — `forge-release` usage + the stable public Python import surface
- customizing-precommit.md: adding repo-specific steps to `.githooks/pre-commit`
- step-invocation.md: contributor rule for how pre-commit steps invoke their tools — orchestrator is the contract; standalone CLI only when it *is* the tool or does real orchestration
- security.md: security policy and review documentation
- standalone-installers.md: per-installer reference for manual usage (sibling of `install-forge-bootstrap`)
- telemetry.md: usage guide for `forge-telemetry` / `forge-smart-test --telemetry` — example chart + how to read a profile (what warrants investigation)
- images/: committed doc image assets (currently the telemetry example chart)

### Proposals (`docs/proposals/`)

Architecture RFCs and research reports — aspirational or advisory, not descriptions of current behavior. RFCs carry their own accept/reject status; research reports feed a maintainer decision.

- rust-core.md: RFC for splitting forge into a Rust governance-core binary + optional Python analysis pack
- test-lifecycle.md: research report for the test-suite lifecycle policy (issue #396) — baseline profile, prior art, five proposals awaiting decision

## Configuration Files

1. **Python Package Configuration**
   - pyproject.toml: package metadata, dependencies, entry points

2. **Code Quality**
   - ruff.toml: ruff lint and format configuration (strict, ALL rules)
   - pyrefly.toml: pyrefly type-checker config for the opt-in `typecheck` step (strict return-type checking; interrogate's attrs `__init__` silenced via `replace-imports-with-any`)
   - c4.toml: standalone C4 architecture-model skeleton consumed by `forge-gen-c4` (kept out of pyproject; pointed at by `[tool.forge.c4].config`)
   - .forge-full-run: tracked one-line ISO stamp of the last truly-all test run — the 48h full-run cadence guarantee, rewritten and restaged by the `smart_test` pre-commit step

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
   - workflows/: GitHub Actions workflows — `ci.yml` (the pre-commit checks on every PR and push to `main`) and `tag-release.yml` (cuts the release tag after CI succeeds on the assembly merge)
   - dependabot.yml: monthly Dependabot updates for the SHA-pinned GitHub Actions

2. **Code Health (`code_health/`)**
   - Pre-commit check logs (gitignored): `ruff.log`,
     `docstring_verification.log`, `test_naming_check.log`,
     `repo_structure_check.log`, etc.

3. **Continuation State (`.plan/`)**
   - `CONTINUATION.md`: cross-session handoff state (gitignored).
