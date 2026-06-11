# Contributing to Forge

For consumers adopting forge in their own repo, see the **Quickstart**
section of [README.md](README.md). This document is for people who are
modifying forge itself.

## Branching

Forge ships on a **dual-track** release model. Both branches publish
stable semver; the difference is cadence.

- `main` — stable, **minor-only releases** (`vX.Y.0`). Slow channel.
  Updated by squash-merging `dev → main` for a minor release, or by
  direct hotfix PR for a critical bug on a tagged minor.
- `dev` — stable, **every patch** (`vX.Y.Z` where `Z >= 1`). Fast
  channel. Default target for feature/fix PRs.

Working branches (off `dev` unless this is a hotfix off `main`):

- `feat/*` — feature branches, merged via PR.
- `fix/*` — fix branches.
- `refactor/*` — internal restructuring, no behavior change.
- `test/*` — test-only changes.
- `docs/*` — documentation-only changes.
- `chore/*` — release prep, config changes, housekeeping.

Default PR base = `dev`. **Hotfix** PRs target `main` directly — call
this out in the PR description so reviewers know the unusual base. A
hotfix landing on `main` is always followed by a forward-port PR
(`git merge main` into `dev`) so `dev` stays ahead.

All work goes through a PR. Direct pushes to `main` or `dev` are
blocked by the `block_protected_branches` Claude Code hook; PR merges
are blocked for agents by `block_pr_merge`. **The user merges PRs
themselves** (`! gh pr merge`).

GitHub branch-protection settings on both branches: required linear
history, no force-push, no deletion, required `ci` status check,
squash-merge only (repo-wide).

## Local development setup

```bash
git clone git@github.com:misnaej/forge.git
cd forge
./dev/setup.sh
conda activate forge
```

`dev/setup.sh` creates a conda env named `forge`, editable-installs the
package with test deps (`pip install -e ".[test]"`), then dogfoods
`install-forge-bootstrap --skip claude-md` — the umbrella installer
wires git hooks, installs labels, generates docs, and runs
`forge-doctor`. The `--skip claude-md` is forge-specific because
`FOUNDATION.md` at the repo root is a symlink to
`src/forge/data/FOUNDATION.md`; running `install-forge-claude-md`
would wrap the source with managed markers and corrupt it. Idempotent
— safe to re-run.

The `dev/` directory is **forge's own bootstrap rig**. Consumers don't
adopt this pattern; they use whatever Python env tool they prefer.
`dev/setup.sh` happens to use conda because that's what the maintainer
uses on this machine.

Overrides:

```bash
CONDA_ENV_NAME=forge-dev PYTHON_VERSION=3.12 ./dev/setup.sh
```

## Pre-commit checks (dogfood)

After setup, `git commit` runs `forge-precommit` automatically. The
sequence:

| Step | What it does |
|---|---|
| `ruff` | `fix-forge-ruff`: `ruff format` + `ruff check --fix --unsafe-fixes`, `git add` modified tracked files, write `code_health/ruff.log`. PASSes iff every violation cleared. |
| `docstrings` | `verify-forge-docstrings` on the diff vs main |
| `manifest_json` | Validates `.claude-plugin/*.json` parses |
| `plugin_version` | Guards `plugin.json` version > latest git tag |
| `pip_audit` | `pip-audit` dependency scan (non-blocking — warns only) |

`pytest` is intentionally **not** a pre-commit step (too slow for commit
time — it belongs in CI).

Per-step output goes to `code_health/<step>.log`. To run the full
sequence manually without committing:

```bash
forge-precommit
```

For a machine-readable summary:

```bash
forge-precommit --json
```

## Versioning

`forge-scripts` uses [setuptools-scm](https://setuptools-scm.readthedocs.io).
The package version comes from the latest git tag. There is no manual
`version = "x.y.z"` to bump in `pyproject.toml`.

### Rolling-next plugin.json

`.claude-plugin/plugin.json` is a static JSON file — Claude Code reads
it as-is, no setuptools-scm. To keep the manifest from drifting from
the git tag, `forge-precommit` runs `step_plugin_version`: it asserts
`plugin.json["version"]` is strictly greater than the latest semver
git tag. The guard skips on the release commit itself (HEAD == tag's
commit) and when `.claude-plugin/plugin.json` is absent.

**Convention:** every PR bumps `plugin.json["version"]` to the version
about to be tagged. Patches on `dev` bump the patch level
(`1.3.1` → `1.3.2`); minor promotions on `main` bump the minor
(`1.3.x` → `1.4.0`).

### Release flow (dual-track)

Patches (every fix/feature on `dev`):

1. Feature/fix PR targets `dev`. First commit bumps `plugin.json`
   to next patch level. CI green → user merges.
2. `forge-next-prep --tag` (run from any branch) tags `vX.Y.Z` at
   the merge commit on dev and pushes the tag.

Minor promotion (`dev → main` release):

1. Branch `release/vX.Y.0` off `dev`. Bump `plugin.json` to the new
   minor (e.g. `1.3.0`).
2. PR base = `main`, head = `release/vX.Y.0`. Body summarises the
   patches included since the last minor.
3. User merges; `forge-next-prep --tag --target base` tags
   `vX.Y.0` on main + pushes.
4. Forward-port: tiny PR on a branch off `dev` bumping
   `plugin.json` to the next patch line (`X.Y.1`) so `dev` stays
   ahead of `vX.Y.0`.

Hotfixes (critical bug on a tagged minor):

1. Branch off `main` directly. Bump `plugin.json` minor-patch
   (`1.3.0` → `1.3.1`). PR base = `main`.
2. User merges; tag `vX.Y.Z` on main.
3. Forward-port main → dev (same shape as the minor-promotion
   forward-port).

For breaking changes, document the migration in the PR description
and the GitHub release notes before tagging.

### Semver rules

- **MAJOR**: rename or removal of a public CLI / agent / skill / hook
  event. Breaking import paths.
- **MINOR**: new CLI / agent / skill / hook / doc section. Additive.
- **PATCH**: fix to existing prompt or hook. Safe to update.

## Adding a new CLI

1. New module at `src/forge/<name>.py`.
2. Add entry point in `pyproject.toml`:
   ```toml
   [project.scripts]
   forge-<name> = "forge.<name>:main"
   ```
3. Re-run `pip install -e ".[test]"` to register the entry point.
4. Add tests under `tests/test_<name>.py`.
5. Update README's "What you get today" table.
6. If the new CLI produces a consumer-facing artifact and should run as
   part of the standard onboarding flow, add it to `STEPS` in
   `src/forge/install_bootstrap.py` (dependency order) and to
   `_UNDERUSED_ARTIFACTS` in `src/forge/doctor.py` (so `forge-doctor`
   surfaces an INFO advisory when the artifact is missing).
7. Open PR.

## Adding a new agent / skill / Claude Code hook

1. Add the file under `agents/`, `skills/`, or `claude-hooks/`.
2. For Claude Code hooks: also add an entry to
   `.claude-plugin/plugin.json` (inlined since Claude Code 2.1.x).
3. For agents: ensure frontmatter has `name`, `description`, `tools`,
   `model`.
4. Open PR. CI plugin-load test (Tier 2, planned) will verify.

## Naming conventions

- Agents: `kebab-case.md`; name in frontmatter matches filename.
- Skills: directory `kebab-case/` containing `SKILL.md`.
- Claude Code hooks: `block_<thing>.sh`, `check_<thing>.sh`,
  `warn_<thing>.sh`. Always lowercase, underscored.
- Python modules: `snake_case`. Package name `forge` (pip dist name is
  `forge-scripts` because `forge` is taken on PyPI).

## CLIs are Python by design

Every forge CLI (`verify-forge-*`, `install-forge-*`, `forge-doctor`,
`forge-precommit`) is a Python entry point even when the underlying
work is mostly shelling out to `gh` / `git` / `ruff`. The reasons:

- **Pip-shippable.** `pip install forge-scripts @ git+ssh://...@vX.Y.Z`
  puts every console script on PATH automatically. Bash equivalents
  would need a separate install path (clone forge, `chmod +x`, manage
  PATH yourself).
- **Testable.** Idempotency, managed-marker logic, version-drift
  detection, regex validation — all easy in Python + pytest, brittle
  in bash.
- **State.** Hook installers and the `install-forge-claude-md`
  splice-block logic are state machines; Python keeps them readable.

If you're tempted to write a new CLI as a `.sh` and ask consumers to
clone-then-run, default to Python and a pip entry point unless you
have a specific reason not to.

## Consumer CI access to the foundation pip package

Forge is public — no auth needed in CI. See
[`docs/ci-access.md`](docs/ci-access.md) for the install line + fork-
specific options.

## Forge-internal layout (dogfood, not for consumers)

A few items exist only to develop **forge itself**. Consumers do NOT
need them.

| Forge-internal | What it is |
|---|---|
| `dev/setup.sh` | Conda env + `pip install -e .` + hook wiring + doctor. Bootstraps a fresh fork of forge. Consumers use whatever Python env tool they prefer. |
| `dev/README.md` | Documents what `dev/` is. |
| `.claude-plugin/` | Plugin manifest forge ships TO the marketplace. Forge ships a plugin; consumers consume it. Consumer repos don't ship `.claude-plugin/` unless they also publish a plugin. |
| `claude-hooks/` (top-level) | **Claude Code hook scripts** that ship via the plugin. Consumers get these by installing the plugin. Don't confuse with `.githooks/` (git-side). |
| `FOUNDATION.md` (root, symlinked into `src/forge/data/`) | The engineering-principles doc forge ships TO consumers. Forge dogfoods it via its own `CLAUDE.md`. Consumers reference it (don't duplicate). |

The `.githooks/pre-commit` `manifest_json` step validates
`.claude-plugin/*.json`. It **self-skips** in repos without a
`.claude-plugin/` directory — harmless for consumers, useful when
forge runs on itself.

## Backward compatibility

- **PATCH** is safe — consumers' tests should still pass.
- **MINOR** adds capability — consumers opt in.
- **MAJOR** breaks — consumers must update their pins and may need to
  migrate code. Document the migration in the PR description and the
  GitHub release notes.

Never silently break a consumer. Pre-release validation (planned in
`.github/workflows/pre-release.yml`, automated for v2) catches this by
opening draft PRs in each consumer repo bumping the foundation version
and waiting for their CI to pass.
