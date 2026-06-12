# Forge

AI agents write more code, faster — but without guardrails, quality
drifts: lint rules silently disabled, docstrings half-written,
standards left as prose nobody runs. Good output needs hard guardrails,
not good intentions.

Forge ships those guardrails as **deterministic tools** that run the
same way whether a human or an agent invokes them:

- A pip-installable Python package (`forge-scripts`) with CLIs that
  codify a repo's quality standards (lint, docstrings, test naming,
  GitHub labels, env diagnostics).
- A **drop-in pre-commit hook** that runs those CLIs in order on every
  commit, with no per-repo wiring.
- An optional **Claude Code plugin** (agents, skills, hooks) that wires
  the same CLIs into an agent session — an add-on, not a prerequisite.

Everything mechanical is a plain CLI or shell hook. The Claude Code
plugin is a thin orchestrator on top — the gate is the CLI, never the
model.

---

## At a glance: adopting forge

Adopting forge is three steps, roughly 1 minute of work. Step 3 is
optional. The numbered [Quickstart](#quickstart-adopt-forge-in-your-repo)
below has the full detail.

1. **Install `forge-scripts`** — `pip install
   "git+https://github.com/misnaej/forge.git@main"` puts every forge CLI
   on `PATH`. (~30s)
2. **Run `install-forge-bootstrap`** — one-shot umbrella: wires git
   hooks, scaffolds `FOUNDATION.md` / `CLAUDE.md`, installs the
   canonical GitHub label schema, generates `docs/api-digest.md` +
   `docs/cli-reference.md` + `code_health/audit_deps_tree.log`, and
   runs `forge-doctor`. Idempotent — safe to re-run after every forge
   upgrade. (~30s)
3. **(Optional) Install the Claude Code plugin** — only if your team
   uses Claude Code. (~30s)

Steps 1–2 are Claude-independent. After step 2, `forge-doctor` reports
the install status automatically.

---

## Why this exists

Most teams accumulate the same scaffolding in every repo — a ruff
config, a pre-commit hook, a `gh label` setup script, a doctor command —
and it drifts. Repo A runs strict ruff while repo B silently disabled
three rules. Contributors set up their env differently in each repo.
Standards live as stale prose nobody runs against.

Forge collapses that scaffolding into **one pip-installable package**
and **one drop-in pre-commit hook**. Adopt forge and you get:

- A canonical `ruff` configuration philosophy (`select = ["ALL"]`
  strict, per-file ignores only).
- A `verify-forge-docstrings` checker that validates Google-style
  Args/Returns against actual function signatures.
- A consistent set of GitHub labels installed by one command
  (`install-forge-labels`).
- A `forge-doctor` CLI that tells a contributor exactly what's wrong
  with their local install.
- A pre-commit hook running all of the above on every commit. New
  checks are added in forge and propagate to every consumer on the next
  version bump. (Pytest is deliberately NOT in the default sequence —
  too slow for pre-commit; run it in CI or wire it into
  `.githooks/pre-commit` yourself.)

Need something forge doesn't ship (custom mypy step, secret scan)? See
[`docs/customizing-precommit.md`](docs/customizing-precommit.md) —
edit `.githooks/pre-commit` directly. No plugin system, no config file.

---

## What you get today

| Category | Items |
|---|---|
| **CLIs** (pip package, no Claude required) | `install-forge-bootstrap` (one-shot umbrella), `forge-upgrade` (two-phase upgrade flow), `forge-precommit` (full sequence dispatcher), `fix-forge-ruff` (ruff phase), `verify-forge-docstrings`, `verify-forge-docstring-coverage`, `verify-forge-repo-structure`, `verify-forge-test-naming`, `verify-forge-manifest`, `verify-forge-plugin-version`, `verify-forge-cli-wiring`, `forge-continuation-append`, `forge-next-prep`, `install-forge-labels`, `forge-doctor`, `install-forge-githooks`, `install-forge-claude-md` |
| **Audit-pack CLIs** (pip package, optional `[audit]` extras) | `forge-audit-dup`, `forge-audit-deps`, `forge-audit-suppressions`, `forge-audit-orphans`, `forge-audit-data`, `forge-audit-claims`, `forge-audit-agents` (non-blocking template-conformance audit), `forge-audit-all` — see [`docs/audit-pack.md`](docs/audit-pack.md) |
| **Git hooks** (drop-in, no Claude required) | `.githooks/pre-commit` (dispatcher), `.githooks/post-merge` + `.githooks/post-checkout` (auto-warn on FOUNDATION.md drift) |
| **Process docs** | `docs/security.md`, `docs/audit-pack.md`, `docs/cli-reference.md` (generated CLI reference), `docs/api-digest.md` (generated index of all top-level functions/classes, public API + internal helpers); foundation engineering principles at `FOUNDATION.md` |
| **Claude Code plugin** (optional) | Agents (`pr-manager`, `precommit-fixer`, `git-commit-push`, `design-checker`, `docs-types-checker`, `security-checker`, `issue-triage`, `perf-optimizer`, `weekly-summary`, `knowledge-search`, `test-advisor`, `test-writer`); skills (`commit`, `pr`, `next`, `triage`, `weekly`, `fix`, `review`); Claude Code hooks (`block_protected_branches`, `block_force_push`, `block_pr_merge`, `block_no_verify`, `block_install_deps`, `block_claude_attribution`, `block_continuation_delete`, `block_protected_files`, `check_commit_format`, `check_foundation_sync`, `warn_pr_checks`, `block_raw_ruff`, `block_raw_git`) |

Everything in the first three rows is **Claude-independent** — works
from any shell, CI, or IDE.

The audit-pack CLIs are gated behind extras: `pip install
forge-scripts[audit]` pulls in `vulture`, `jsonschema`, and `PyYAML`;
`[audit-tach]` adds `tach` for dependency-graph checks. See
[`docs/audit-pack.md`](docs/audit-pack.md) for what each audit detects
and how findings land in `code_health/audit_*.log`.

---

## Quickstart: adopt forge in your repo

The full tutorial for the three steps in [At a glance](#at-a-glance-adopting-forge).

### 1. Install `forge-scripts`

Forge is a public repo, so no auth setup is needed. Install directly
from GitHub:

```bash
pip install --upgrade "git+https://github.com/misnaej/forge.git@main"
```

Pin to a specific version or channel:

| Pin | Cadence |
|---|---|
| `@main` | Slow channel — minor versions only |
| `@dev` | Fast channel — every patch |
| `@v1.3.0` | Frozen at a specific tag |

For a project's `pyproject.toml`:

```toml
[project.optional-dependencies]
dev = [
    "forge-scripts @ git+https://github.com/misnaej/forge.git@main",
    # ... your other dev deps
]
```

Then install editably:

```bash
pip install -e ".[dev]"
```

The forge CLIs are now on your `PATH`:

```bash
forge-precommit --help
forge-doctor
```

### 2. Run `install-forge-bootstrap` (recommended)

```bash
install-forge-bootstrap
```

One-shot umbrella that runs every installer + generator in dependency
order. Idempotent — re-run safely after every forge upgrade.

> Source of truth: the `STEPS` tuple in
> [`src/forge/install_bootstrap.py`](src/forge/install_bootstrap.py).
> The table below is rendered documentation; update the tuple, not this
> table, when adding or reordering steps.

| Step | CLI | What lands on disk |
|---|---|---|
| 1 | `install-forge-githooks` | `.githooks/pre-commit`, `.githooks/post-merge`, `.githooks/post-checkout` + `core.hooksPath` |
| 2 | `install-forge-claude-md` | `FOUNDATION.md` + scaffolded `CLAUDE.md` (if absent) + `.claude/` |
| 3 | `install-forge-labels` | Canonical GitHub label schema (skipped when `gh` missing or no remote) |
| 4 | `forge-gen-api-digest` | `docs/api-digest.md` (index of top-level functions / classes) |
| 5 | `forge-gen-cli-reference` | `docs/cli-reference.md` (generated from each CLI's `--help`) |
| 6 | `forge-audit-deps --tree` | `code_health/audit_deps_tree.log` (dependency tree) |
| 7 | `forge-doctor` | Verifies the install |

Flags: `--check` (dry-run), `--skip <slug>` (repeatable; slugs are
`githooks`, `claude-md`, `labels`, `api-digest`, `cli-reference`,
`audit-deps`, `doctor`), `--strict` (abort on first failure; default is
continue-on-fail).

Want to run an installer on its own? Each step is also a standalone CLI
— see [`docs/standalone-installers.md`](docs/standalone-installers.md).

**Self-updating hooks (wrapper pattern).** Each managed git hook is
a one-line wrapper that calls a forge-shipped CLI:

```bash
#!/usr/bin/env bash
# forge:githook-managed v2 body-sha=<hex>
set -euo pipefail
# (staleness preamble — advisory warning when installed forge is newer)
forge-post-merge "$@"
```

`forge-precommit`, `forge-post-merge`, and `forge-post-checkout`
carry the actual logic. The hook *file* is yours — you can add
repo-specific steps after the forge CLI call:

```bash
#!/usr/bin/env bash
# forge:githook-managed v2 body-sha=<hex>
set -euo pipefail
forge-post-merge "$@"
./scripts/install-editable.sh   # consumer step — survives forge upgrades
```

The marker embeds a SHA of the canonical body. On every `git pull`,
the `forge-post-merge` CLI backgrounds an `install-forge-githooks
--refresh --quiet` so consumers pick up new forge hook content
automatically — but auto-refresh **leaves modified wrappers alone**
(your repo-specific lines survive). Use `install-forge-githooks
--force` to override and rewrite a modified wrapper; the previous
content is saved as `.githooks/<name>.before-forge-vX.Y.Z.bak` so
nothing is lost.

**Cleaner: drop-in extension directories.** Rather than editing the
managed wrapper at all, drop an executable script into
`.githooks/post-merge.d/` (or `.githooks/post-checkout.d/`). After its
own work, `forge-post-merge` / `forge-post-checkout` runs every
executable `*.sh` in that directory in sorted filename order (`10-`,
`20-`, … the `cron.d` convention). The subdirectory is one
`install-forge-githooks` never writes, so your scripts survive every
refresh with no body-sha bookkeeping:

```bash
# .githooks/post-merge.d/10-refresh-deps.sh   (yours — remember chmod +x)
#!/usr/bin/env bash
./scripts/install-editable.sh
```

A failing extension logs a warning and is skipped — it never breaks
your `git pull` / `git checkout`. Extensions run only in interactive
contexts (skipped in CI, same posture as the drift check). Each script
needs a shebang and the executable bit; it runs with the repo root as
its working directory and inherits your shell environment. Because a
`.d/` script is committed and runs automatically, review it with the
same care as any executable hook — the directory is intentionally
outside forge's refresh cycle, so forge never inspects or rewrites it.

`git commit` now runs the canonical pre-commit sequence:

| Step | What it does | When it runs |
|---|---|---|
| `ruff` | `fix-forge-ruff` — runs `ruff format` (in-place) + `ruff check --fix --unsafe-fixes`, re-stages modified tracked files via `git add`, writes `code_health/ruff.log`. PASSes iff ruff cleared every violation; residue (rules without autofix) lands in the log. | Always (skipped if no `src/` or `tests/`) |
| `docstrings` | `verify-forge-docstrings` — validates Google-style Args/Returns against signatures | Always (CLI picks files via staged + unstaged + branch diff vs main; reports cleanly when nothing matches) |
| `docstring_coverage` | `verify-forge-docstring-coverage` — interrogate-based aggregate coverage %, per-file table, and a `MISSING:` symbol list for `forge:precommit-fixer`. | When `pyproject.toml` + a `src/` tree exist (self-skips otherwise). No `[tool.interrogate]` section required — forge defaults apply when it's absent. **Non-blocking**: reports only, never refuses the commit. |
| `test_naming` | `verify-forge-test-naming` — validates test file / function naming conventions | Always (CLI selects modified test files from the diff vs main). **Warning-only**: always exits 0, never refuses a commit. |
| `repo_structure` | `verify-forge-repo-structure` — asserts `REPO_STRUCTURE.md` matches the actual tree | Only when `REPO_STRUCTURE.md` exists |
| `manifest_json` | Validates `.claude-plugin/*.json` as parseable JSON | Only when `.claude-plugin/` exists |
| `cli_wiring` | `verify-forge-cli-wiring` — asserts every `[project.scripts]` entry is reachable from a wiring source (install/precommit/audit/hooks/agents/skills) | Opt-in via `[tool.forge.cli_wiring] enabled = true`; self-skips otherwise |
| `commit_types_parity` | `forge-gen-commit-types --check` — asserts the conventional-commit types in `claude-hooks/check_commit_format.sh` match the canonical `CONVENTIONAL_COMMIT_TYPES` tuple | Only when `claude-hooks/check_commit_format.sh` exists |
| `plugin_version` | Asserts `plugin.json["version"] > latest_tag` (semver) | Only when `.claude-plugin/plugin.json` exists and tags exist; skipped on the release commit |
| `pip_audit` | `pip-audit --skip-editable --desc` — dependency CVE scan | Always (skipped if `pip-audit` not on PATH). **Non-blocking**: failures render as yellow `WARN` and do NOT refuse the commit. |

Pytest is **not** in the default sequence — too slow for pre-commit.
Run it in CI, or add a `pytest -q` line to `.githooks/pre-commit`
directly. To customize further, see
[`docs/customizing-precommit.md`](docs/customizing-precommit.md).

Per-step stdout is captured to `code_health/<step>.log`. The hook exits
non-zero if any **blocking** non-skipped step fails. Non-blocking steps
(`pip_audit`, `docstring_coverage`) and warning-only steps
(`test_naming`) print but don't change the exit code.

---

## Wire it into your setup script

Two commands cover the lifecycle:

- **`install-forge-bootstrap`** — idempotent install + re-sync. Same
  command for first install and every subsequent forge upgrade.
- **`forge-upgrade --apply`** — full one-shot upgrade for human-run
  setup scripts: rewrites the pin, runs the force-reinstall pip
  command, then runs `install-forge-bootstrap`. Use this when your
  consumer pins forge to `@main` / `@dev` (a moving branch ref);
  without the force-reinstall, pip silently freezes at the first
  install — see ["About `@main` / `@dev` pins"](#about-main--dev-pins)
  below.

**`Makefile`**

```makefile
.PHONY: setup
setup:
	pip install -e ".[dev]"
	install-forge-bootstrap

.PHONY: upgrade
upgrade:
	forge-upgrade --apply
```

**`setup.sh`** (or any shell rig)

```bash
#!/usr/bin/env bash
set -euo pipefail
pip install -e ".[dev]"
install-forge-bootstrap
# After upgrades: `forge-upgrade --apply --channel main` (or @dev / @vX.Y.Z)
```

**`uv` project**

```bash
uv sync --all-extras
uv run install-forge-bootstrap
```

**CI** (GitHub Actions example)

```yaml
- run: pip install -e ".[dev]"
- run: install-forge-bootstrap --check    # CI fails if anything drifted
```

> Forge dogfoods this pattern in [`dev/setup.sh`](dev/setup.sh) (its
> own bootstrap rig). The one wrinkle there: forge runs
> `install-forge-bootstrap --skip claude-md` because forge IS the
> source of `FOUNDATION.md` (root file is a symlink to
> `src/forge/data/FOUNDATION.md`). Consumer repos don't need the
> `--skip`.

The CLI inspects on-disk state to decide what each step needs to do —
first-time install or refresh after a forge bump. No `--update` flag
needed.

### About `@main` / `@dev` pins

When your `pyproject.toml` pins forge via a branch ref
(`forge-scripts @ git+https://github.com/.../forge.git@main`), pip
**caches by `(package_name, version)`**. A branch ref doesn't bump
the version, so subsequent `pip install` runs silently no-op — even
if the upstream branch has advanced by months. Symptom: `forge-doctor`
reports everything healthy while you're actually N commits behind.

Three ways to handle this:

| Approach | When |
|---|---|
| **Pin to a tag** (`@v1.3.0`) | CI / production — version comparisons are reliable. |
| **`forge-upgrade --apply`** | Setup scripts / Makefile / human-driven upgrades. Wraps the `--force-reinstall --no-deps` pip command + re-sync in one call. |
| **Two-phase `forge-upgrade` + manual pip** | Claude Code agents — they're blocked from running `pip install`, so the agent rewrites the pin + prints the command; you run it. |

The channel-aware upstream warning in `install-forge-claude-md` also
fires a freeze hint when it detects a `.devN+gHASH` install (the
fingerprint of a branch-ref pip install).

---

## Running forge in CI

CI runners are not workstation dev loops. forge's defaults already
know this — every CI-relevant tool consults
[`forge.run_context`](src/forge/run_context.py)
([FOUNDATION §15](FOUNDATION.md#15-runtime-context-awareness)). Concretely
in a CI job:

- `forge-doctor` and `forge-audit-deps` **self-skip** under
  `install-forge-bootstrap` (no `--skip` flags needed).
- `install-forge-claude-md`'s upstream-version warning is
  suppressed — no value to a runner.
- `forge-upgrade` picks the pip URL form
  (`ssh` / `https-token` / `https-anonymous`) that the runner can
  actually authenticate against, and **aborts loudly** (exit 2) if it
  detects neither SSH keys nor `GITHUB_TOKEN` before subprocess hang.
- The post-merge / post-checkout git hooks short-circuit when a CI
  marker is set, so a CI checkout that fires them before
  `forge-scripts` is installed does not crash the runner.

### The recipe

Channel-pinned forge (`@main` / `@dev`) + a scheduled GitHub Actions
workflow that runs `forge-upgrade --apply` and opens a PR whenever
the upgrade produces a diff. No third-party bot, no per-version pin
maintenance, every upgrade exercised by your own CI before it
merges.

Per-PR CI step:

```yaml
- run: pip install -e ".[dev]"
- run: install-forge-bootstrap          # idempotent re-sync
- run: install-forge-bootstrap --check  # fail PR if anything drifted
- run: forge-precommit                  # full quality gate
```

Full pasteable workflows (per-PR CI + scheduled
`forge-upgrade --apply`) are in
[`docs/ci-recipe.md`](docs/ci-recipe.md). For private-fork auth
(deploy keys, PATs), see [`docs/ci-access.md`](docs/ci-access.md).

---

## How forge stays in sync

After bootstrap, three triggers check whether your repo's `CLAUDE.md`
foundation matches the installed `forge-scripts` version. None
auto-rewrite the file — they warn, and you run
`install-forge-claude-md` to apply when ready.

| Trigger | Source | When it fires | Behavior |
|---|---|---|---|
| `post-merge` git hook | `.githooks/post-merge` (written by `install-forge-githooks`) | After every `git pull` / merge | Runs `install-forge-claude-md --check --quiet`. One-line warning on drift. |
| `post-checkout` git hook | `.githooks/post-checkout` | On branch switch / clone (only when HEAD moves) | Same drift check. |
| `SessionStart` Claude Code hook | `claude-hooks/check_foundation_sync.sh` (plugin-shipped) | When you open a Claude Code session in the repo | Same drift check. Catches a `pip install -U` of `forge-scripts` without a `git pull`. |
| Manual / CI | `install-forge-claude-md --check` | Anytime | Exits non-zero on drift. Drop into CI to block merges with stale foundation. |

**Auto-rewrite is intentionally NOT done.** Hooks warn loudly; you run
the sync command, review the diff, and commit. This avoids silent
rewrites that surprise contributors after a `git pull`.

### Upstream-version drift warning

In addition to the local FOUNDATION drift check above, every
`install-forge-claude-md` run also queries the latest forge tag from
GitHub once per 24h (cached at `~/.cache/forge/upstream_check.json`)
and emits a `⚠` warning when your installed `forge-scripts` or Claude
plugin version is behind. The post-merge / post-checkout hooks inherit
this automatically — no consumer-side hook changes needed.

Strictly warning-only: network failures, missing `gh`, or stale-but-
working installs never change the exit code. Includes the exact upgrade
command in the warning line.

---

## Upgrading forge in your repo

### Pick a release channel

Forge ships on two channels — pin whichever fits your update cadence:

| Pin target | Cadence | Best for |
|---|---|---|
| `@main` | Minor-only (`vX.Y.0`) | Repos that want fewer, larger updates after dev-channel bake time |
| `@dev`  | Every patch (`vX.Y.Z`)  | Repos that want every fix as it ships |
| `@v1.2.3` | Frozen at a specific version | CI / production pinning |

Both channels publish stable semver. Neither is pre-release. The
difference is cadence, not stability tier.

### Upgrade flow

One command (`forge-upgrade`) wraps the multi-step flow. Two phases
because [FOUNDATION §2](FOUNDATION.md#2-core-safety-rules) forbids
agents from running `pip install`:

```bash
# Phase 1 — rewrite the pin + print the exact pip command.
forge-upgrade --channel main          # pin @main (slow channel)
forge-upgrade --channel dev           # pin @dev (every patch)
forge-upgrade --to v1.3.0             # pin a specific tag
forge-upgrade --check                 # dry-run; print without writing

# Phase 2 — after running the printed pip command, re-sync managed
#           artifacts (forge-doctor included).
forge-upgrade --continue
```

The phase-1 rewrite targets the `forge-scripts @ git+...` line in
`pyproject.toml`. Phase 2 runs `install-forge-bootstrap` under the
hood; if your project uses requirements.txt or a lockfile, run the
phase-1 pip command manually then `forge-upgrade --continue`.

The same upgrade run by hand:

```bash
# 1. Bump the pin in pyproject.toml.
#    "forge-scripts @ git+https://github.com/misnaej/forge.git@v1.3.0"   (or @main / @dev)
pip install --upgrade --force-reinstall --no-deps \
    "forge-scripts @ git+https://github.com/misnaej/forge.git@v1.3.0"

# 2. Re-sync every managed artifact in one call (idempotent):
install-forge-bootstrap

# 3. (If you use Claude Code) bump the plugin pin:
#    ~/.claude/installed_plugins.json → { "forge@forge": { "version": "v1.3.0" } }
#    Then: /plugin update forge@forge
#          /reload-plugins      # required: makes the new agents / hooks /
#                               # skills / MCP+LSP visible in this session
#    Note: monitor changes still need a full session restart.

# 4. Review the diff:
git diff FOUNDATION.md       # foundation content changes
git diff .githooks/          # hook template upgrades
git diff docs/api-digest.md docs/cli-reference.md
# Commit the updates.
```

Note: forge's `post-merge` hook already auto-runs
`install-forge-githooks --refresh --quiet` on every `git pull`, so the
`.githooks/` content stays current even if you forget step 2. The
explicit `install-forge-bootstrap` in step 2 still adds value for
generators (`docs/api-digest.md`, `docs/cli-reference.md`,
`code_health/audit_deps_tree.log`) and the GitHub label schema.

If `install-forge-claude-md` refuses to overwrite `FOUNDATION.md` (you
removed the managed markers or wrote your own), pass `--force` to
re-establish the managed file.

**Upgrading from v1.1.2?** A one-time `install-forge-claude-md
--migrate` step converts the old inline-block layout to the split
file. See [`docs/standalone-installers.md`](docs/standalone-installers.md)
for the full migration recipe.

If `install-forge-githooks` refuses to overwrite (you replaced a hook
with your own content sans marker), restore the marker or pass
`--force` (overwrites; re-apply your customizations).

---

## Further reading

Topic-specific docs (read what you need, skip what you don't):

| Doc | When to read |
|---|---|
| [`docs/standalone-installers.md`](docs/standalone-installers.md) | You want to run a single installer manually instead of `install-forge-bootstrap`. |
| [`docs/customizing-precommit.md`](docs/customizing-precommit.md) | You want to add a repo-specific step (mypy, secret scan, etc.) to the pre-commit hook. |
| [`docs/claude-code-plugin.md`](docs/claude-code-plugin.md) | You use Claude Code and want the agents / skills / hooks. |
| [`docs/ci-recipe.md`](docs/ci-recipe.md) | You want a pasteable GitHub Actions workflow for running forge in CI. |
| [`docs/ci-access.md`](docs/ci-access.md) | Your CI runner can't clone forge with implicit SSH. |
| [`docs/audit-pack.md`](docs/audit-pack.md) | You want to use the `forge-audit-*` CLIs for codebase health checks. |
| [`docs/security.md`](docs/security.md) | Security-sensitive coding standards forge enforces. |
| [`FOUNDATION.md`](FOUNDATION.md) | The engineering principles forge ships to every consumer (linked from your `CLAUDE.md`). |
| [`REPO_STRUCTURE.md`](REPO_STRUCTURE.md) | Map of every directory in this repo. |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | You're modifying forge itself (release process, versioning, dev rig). |

---

## License

MIT.
