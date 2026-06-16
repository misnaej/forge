# Configuring forge in your repo

forge reads all of its configuration from your repo's **`pyproject.toml`** —
there is no separate `forge.toml` or config file to manage. This page is the
complete reference.

**For a live view of what forge reads in *your* repo right now** — current
values, unset defaults, and what you should add — run:

```bash
forge-config --list
```

`install-forge-bootstrap` prints the same advisor as a post-install nudge. The
two are the runtime counterpart to this page: this doc is the reference, the
CLI is the per-repo answer.

---

## Where forge config lives — two homes, on purpose

1. **`[tool.forge.*]`** — forge's own namespace. Every key forge invented.
2. **A third-party tool's native section** (e.g. `[tool.interrogate]`) — when
   forge wraps a real tool, it reads the tool's **own** config section directly
   and does **not** copy it under `[tool.forge.*]`. Re-exposing a tool's whole
   config under a forge namespace would be a needless wrapper; forge defers to
   the native section, exactly as it reads `ruff.toml` rather than duplicating
   it. These native sections are listed below so you know forge reads them.
   (FOUNDATION §8 "config-home rule".)

You never edit forge's source to configure it — only `pyproject.toml`.

---

## Quick start

Most repos need only this:

```toml
[tool.forge]
base_branch = "main"
dev_branch  = "dev"   # omit for a single-branch repo — it defaults to base_branch ("main")
```

Everything else has a sensible default and is opt-in. Run `forge-config --list`
to see what, if anything, is worth adding for your repo.

---

## `[tool.forge]`

| Key | Default | What it does | Set it when |
|---|---|---|---|
| `base_branch` | `"main"` | Slow-channel / release branch. Protected from direct agent push; the `dev → main` promotion target. | Your release branch isn't `main`. |
| `dev_branch` | `"main"` (= `base_branch`) | Fast-channel integration branch. Protected from direct agent push. **Defaults to `base_branch`** → single-track, only one branch protected. | You run dual-track — set e.g. `dev_branch = "dev"` to opt in. |
| `source_dirs` | `["src"]` | Repo **source** roots — the single ground truth for your project layout, consumed by layout-aware tools (e.g. docstring-coverage scan roots). | Your source lives outside `src/` — e.g. `source_dirs = ["src", "projects"]`. |
| `test_dirs` | `["tests"]` | Repo **test** roots. Kept separate from `source_dirs` so a source-only tool doesn't pull test dirs in. | Your tests aren't under `tests/`. |

## `[tool.forge.cli_wiring]`

| Key | Default | What it does | Set it when |
|---|---|---|---|
| `enabled` | `false` | Opt into the `cli_wiring` pre-commit step: every `[project.scripts]` entry must be reachable from a wiring source (install bootstrap, pre-commit, audit, hooks, agents, skills…). | Your repo ships `[project.scripts]` and follows forge's layout and you want unreachable CLIs caught. |

## `[tool.forge.docstring_coverage]`

Forge-specific keys for the docstring-coverage reporter. (The coverage *gate*
itself — threshold, excludes, ignores — lives in `[tool.interrogate]` below;
these are the keys interrogate has no concept of.)

| Key | Default | What it does | Set it when |
|---|---|---|---|
| `paths` | `[tool.forge].source_dirs + test_dirs` | Per-tool **override** of the scan roots for the coverage report and badge. Defaults to the repo-wide layout above; set this only when docstring-coverage should scan something different. Paths resolving outside the repo are rejected. | You want coverage scoped differently from the rest of forge — otherwise prefer setting `[tool.forge].source_dirs` once. |
| `badge` | `false` | Generate **interrogate's own** coverage badge (via `interrogate.badge_gen`) to `.badges/DocstringCoverage.svg` for README embedding. forge invokes interrogate as a library, so this opt-in triggers the badge programmatically. | You want a coverage badge in your README. |

## `[tool.interrogate]` — native section, read by forge

This is **interrogate's own** config section (not forge's). forge reads it
directly for the docstring-coverage gate; you configure it exactly as you would
for standalone interrogate.

| Key | Default | What it does |
|---|---|---|
| `fail-under` | `90` | Coverage threshold. Set to your current passing baseline, raise over time (FOUNDATION §4, §8). |
| `exclude` | – | Globs to exclude from the scan. |
| `ignore-*` | `false` | The standard interrogate ignore flags (`ignore-init-method`, `ignore-nested-functions`, …) — forge passes them through. |

Example:

```toml
[tool.interrogate]
fail-under = 100
ignore-nested-functions = true
```

---

## Discovering and verifying your setup

```bash
forge-config --list
```

prints every `[tool.forge.*]` key forge reads (current value or `<default>`),
names the native sections like `[tool.interrogate]` it reads, and lists a
**Suggested setup** block for recommended-but-unset keys — so you (or an agent)
know precisely what to add. Run it after `install-forge-bootstrap`, after a
`forge-upgrade`, or any time you're unsure what forge is reading.
