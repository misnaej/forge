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

## `[tool.forge.hooks]` — Claude Code safety hooks

Read by the **shell** safety hooks (not the Python config surface), so
`forge-config --list` does not enumerate this key.

| Key | Default | What it does | Set it when |
|---|---|---|---|
| `block_install_deps` | `true` | Controls the `block_install_deps` agent guard (FOUNDATION §2). `true` blocks every manager (pip / conda / pipenv / poetry / uv install/sync/lock/update + `<mgr> run pip install`); a **list** like `["pip", "conda"]` blocks only those managers; `false` disables it entirely. | A sandboxed/throwaway env legitimately wants agents to run setup, or you want to allow one manager (e.g. `pipenv`) while still blocking the rest. |

## `[tool.forge.precommit]` — turn steps on and off

The uniform lever over the pre-commit sequence. Applied on top of each
step's own self-skip (it never *weakens* a self-skip), and `disable` beats
`enable` when a name appears in both. The same effect for a single run is
available as `forge-precommit --only <names>` / `--skip <names>`.

| Key | Default | What it does | Set it when |
|---|---|---|---|
| `disable` | `[]` | Force-skip these default steps by name (e.g. `["pip_audit"]`). | You want a default step off repo-wide. |
| `enable` | `[]` | Opt into normally-off steps by name: `doctest`, `typecheck`, `doc_consistency`. | You want one of the opt-in steps below to run. |
| `scope` | `"all"` | Default file scope for the scope-aware steps — `"all"` (whole tracked source tree) or `"diff"` (only files modified vs main). | You want a faster, diff-only gate repo-wide (trades completeness for speed). |
| `scope_overrides` | `{}` | Per-step scope, overriding `scope`. Keys are step names; values are `"all"` / `"diff"`. | You want most steps full-repo but one (or vice-versa) on the diff. |

### Changing a step's scope

Three of forge's steps select files: **`ruff`**, **`docstring_verification`**,
and **`test_naming_check`**. Each runs over the **whole tracked tree by
default** (`scope = "all"`) — the strict floor. To scope one (or all) to the
diff vs main instead:

```toml
[tool.forge.precommit]
scope = "all"                 # global default (this is already the default)

[tool.forge.precommit.scope_overrides]
ruff = "diff"                 # lint only changed files
docstring_verification = "diff"
# test_naming_check stays "all" (inherits the global default)
```

Resolution order per step: `scope_overrides.<step>` → `scope` → `"all"`. An
unrecognised value falls back to `"all"`. The other steps are either
inherently whole-repo (`repo_structure_check`, `cli_wiring`, `manifest_json`,
…) or scoped by their own `paths` key (`doctest`, `typecheck`) — `scope` does
not apply to them. `forge-config --list` shows the resolved values.

> **Why default `all`?** A `diff`-only gate passes a commit while leaving
> violations elsewhere in the tree unchecked — the gate then reflects "what
> you touched," not "what's clean." `all` is the honest floor (FOUNDATION §4).
> Use `diff` deliberately when whole-tree runtime is the bottleneck.

## `[tool.forge.doctest]` — opt-in doctest step

Runs `pytest --doctest-modules` so docstring `>>>` examples are executed,
not just present. Enable via `[tool.forge.precommit] enable = ["doctest"]`.

| Key | Default | What it does | Set it when |
|---|---|---|---|
| `paths` | `["src"]` | Scan roots for `--doctest-modules`. | Your doctested code lives elsewhere. |
| `blocking` | `false` | Fail the commit on a broken example (else non-blocking WARN). | You want doctest drift to refuse a commit. |

## `[tool.forge.typecheck]` — opt-in type-check step

Runs [pyrefly](https://github.com/facebook/pyrefly) (Rust, stable,
pyproject-native, reads/migrates `[tool.mypy]`). Enable via
`[tool.forge.precommit] enable = ["typecheck"]`. When enabled but
`pyrefly` is absent, the step fails loudly (it does not silently pass).
Non-blocking by default — a type-checker false positive that refuses a
commit trains `--no-verify`.

| Key | Default | What it does | Set it when |
|---|---|---|---|
| `paths` | `["src"]` | Scan roots passed to pyrefly. | Your source lives elsewhere. |
| `blocking` | `false` | Fail the commit on a type error (else non-blocking WARN). | Your type baseline is clean and you want it enforced. |

The `doc_consistency` step (`verify-forge-doc-consistency`, enabled the
same way) has no config table — it checks that every `[project.scripts]`
CLI is documented in `docs/cli-reference.md`, and is always non-blocking.

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
