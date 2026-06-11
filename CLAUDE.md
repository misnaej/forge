# CLAUDE.md

<!-- Layout: forge dogfoods its own split-layout convention.
     FOUNDATION.md is the source of truth for foundation rules,
     shipped via pip from src/forge/data/FOUNDATION.md (symlink).
     This CLAUDE.md only carries forge-specific developer rules. -->

@FOUNDATION.md

---


## Forge-specific rules

- **Version derivation**: pip package version comes from the latest git tag via setuptools-scm. There is no manual `version = "x.y.z"` in `pyproject.toml`. Release flow: `git tag vX.Y.Z && git push origin vX.Y.Z`.
- **Plugin manifest version is rolling-next**: `.claude-plugin/plugin.json["version"]` always names the version about to be released. `step_plugin_version` in `forge-precommit` enforces `plugin.json > latest_tag` on every commit (skipped on the release commit itself). Workflow:
  1. On the release PR, set `plugin.json["version"]` to the version about to be tagged (e.g. `"1.1.2"`).
  2. Merge the PR, then `git tag vX.Y.Z` at the merge commit. The guard skips that single commit (`HEAD` == tag commit).
  3. Immediately after tagging, the next PR must bump `plugin.json` to `"X.Y.(Z+1)"` (or a higher minor/major) — otherwise commits fail the guard.
- **`/promote` skill (forge-only)**: lives at `.claude/skills/promote/SKILL.md` — project-local, **not** shipped via the forge plugin. After merging a PR to `dev` that bumped `plugin.json` past a minor boundary (MINOR or MAJOR), invoke `/promote` to open the `dev → main` promotion PR with a release-summary squash message. Forge-private because the dev/main two-branch model + rolling-next convention are specific to forge; consumer plugin authors may follow trunk-based, gitflow, or other release models.
- **Semver policy for the plugin bump** — when the next-PR bump in step 3 above lands, choose the increment deliberately, not reflexively. The plugin's public surface is: every CLI in `[project.scripts]`, every agent name + canonical Output shape under `agents/`, every Claude Code hook under `claude-hooks/`, every skill under `skills/`, every FOUNDATION rule a consumer can rely on.
  - **PATCH (Z+1)** — internal-only changes: bug fix in an existing CLI / agent / hook with no behavior change visible to consumers; refactor with identical externals; doc typo fix; non-blocking new audit check; CLAUDE.md edits that affect only forge contributors.
  - **MINOR (Y+1, Z→0)** — additive, backward-compatible: new CLI, new agent, new hook, new skill, new mandatory rule in FOUNDATION or `_TEMPLATE.md` that does NOT break existing consumer artifacts (e.g. a new reporter-agent header that older agents simply lack — audit flags it but does not refuse a commit), new mode/flag on an existing CLI or agent, new pre-commit step that self-skips when not opted in.
  - **MAJOR (X+1, Y→0, Z→0)** — breaking: CLI renamed / removed / argument-incompatible; agent renamed / removed / Output shape changed in a way that breaks existing parsers (e.g. `pr-manager`); hook semantics inverted (allow → block); FOUNDATION rule promoted from soft to blocking; manifest layout change. Any consumer upgrade that requires action beyond `forge-upgrade` is MAJOR.
  - **When in doubt** between minor and patch: lean minor. Tags are free; misleading patch bumps obscure real changes from consumers reading `git log v1.3.4..v1.3.5`.
- **`dev/` is forge-only**: `dev/setup.sh` (conda env named `forge`, editable install, hooks, doctor) is forge's own bootstrap. Not a consumer pattern. Consumers use whatever Python env tool they prefer + `install-forge-githooks`.
- **Plugin layout**: `.claude-plugin/` (manifest), `agents/`, `skills/` (auto-discovered), `claude-hooks/` (referenced via `${CLAUDE_PLUGIN_ROOT}/claude-hooks/...` in `plugin.json`). Do not confuse with `.githooks/` (git hooks, separate domain).
- **FOUNDATION.md location**: canonical file at repo root for contributors. Shipped via pip as `src/forge/data/FOUNDATION.md` (symlink). `install-forge-claude-md` reads from `importlib.resources`.
- **`manifest_json` step** in `forge-precommit` fires here because forge ships a plugin (`.claude-plugin/` present). It self-skips in consumer repos that don't ship a plugin.
- **`pip_audit` step is non-blocking** (since v1.1.3): renders as yellow `WARN` if CVEs are found, never fails the overall pre-commit. Use `StepResult(non_blocking=True)` for any future advisory step.
- **`docstring_coverage` step is non-blocking**: mirrors the `pip_audit` pattern — `StepResult(non_blocking=True)`. See FOUNDATION §8 "Docstring coverage — three layered enforcers" for the full rationale and the `MISSING: <path>:<line>:<name>` dispatch contract with `forge:precommit-fixer`.
- **CLAUDE.md is consumer-owned** (since v1.1.3): foundation lives in `FOUNDATION.md`, included via `@FOUNDATION.md` near the top of this file. `install-forge-claude-md` only writes/updates `FOUNDATION.md` and the initial CLAUDE.md scaffold. Once scaffolded, this file is forge-developer territory.
- **Logging convention** (forge's documented equivalent of FOUNDATION §9): forge has no `common.logging` module. Forge CLIs use `from forge.git_utils import configure_cli_logging` once at module load + `logger = logging.getLogger(__name__)` for the module logger. `configure_cli_logging()` configures the root logger with `INFO` level and a bare-message formatter. Module-level `logging.getLogger(__name__)` is the deliberate pattern here, not a §9 violation — per §9's "consumer repos either adopt this module or document their own logging entry point in their CLAUDE.md".
- **CLI wiring is enforced by reachability** (`verify-forge-cli-wiring`, pre-commit step `cli_wiring`): every `[project.scripts]` entry in `pyproject.toml` must appear at least once in a wiring source — `src/forge/install_bootstrap.py` (STEPS list), `src/forge/precommit.py` (step functions), `src/forge/audit/` (audit orchestrator + steps), `.githooks/`, `claude-hooks/`, `dev/`, `agents/`, or `skills/`. The verifier greps these paths and fails the pre-commit when a script name is unreachable, excluding the CLI's own implementation file. Why: forge has many invocation paths and a new `[project.scripts]` line can land without being wired into any of them, producing an installed-but-uncalled CLI. The grep IS the contract — no metadata registry to keep in sync. If a CLI is intentionally unwired (release tooling, CI-only utility), add it to `cli_wiring_exempt.toml` at the repo root with `[exempt."<name>"] reason = "..."`; the verifier exempts it and reports stale entries when the script is later removed. The step self-skips when the consumer repo has no `[project.scripts]` table.
- **Approved `# noqa` exceptions** (FOUNDATION §5 normally permits only `E402`):
  - `# noqa: N802` — for `ast.NodeVisitor` `visit_*` methods, stdlib forces PascalCase suffixes like `visit_FunctionDef` (e.g. `verify_docstrings.py`).
  - `# noqa: PLC0415` — for guarded optional-dependency imports under `try / except ImportError` inside a helper, where moving the import to module level would break consumers without the extra (e.g. `audit/orphans.py:78` for `vulture`). Module-level guarded imports are preferred when feasible (see `audit/common.py:TOMLLIB`); reserve PLC0415 for cases where the optional dep is only needed inside one function.
