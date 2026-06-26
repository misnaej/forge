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
| `source_dirs` | smart-detect | Repo **source** roots — the single definition every layout-aware tool scans (see below). Unset → smart auto-detect: `src/` if present, else top-level packages. | Your source lives outside `src/` — e.g. `source_dirs = ["src", "projects/lib"]`. |
| `test_dirs` | smart-detect | Repo **test** roots (added for tools that scan tests too). Unset → smart auto-detect of `tests/` then `test/`. | Your tests aren't under `tests/`. |

### Source-dir resolution — one definition, every tool

`source_dirs` / `test_dirs` are the **single definition of where your code
is**. Every layout-aware forge tool — **ruff**, **api-digest**,
**docstring-coverage**, **doctest**, **typecheck** — resolves its scan roots
the same way, in this order:

1. **Granular per-tool** — `[tool.forge.<tool>].paths` (e.g.
   `[tool.forge.ruff].paths`, `[tool.forge.docstring_coverage].paths`).
   A full override for that one tool.
2. **Repo-wide** — `[tool.forge].source_dirs` (plus `test_dirs` for tools
   that scan tests, like ruff and coverage). **Set this once and every tool
   follows it.**
3. **Smart auto-detect** — when neither is set: `src/` if it exists, else
   top-level importable packages (dirs with `__init__.py`); tests from
   `tests/` then `test/`. This replaced an older fixed name-list that
   scanned phantom dirs and ignored your config.

So to run all checks over a non-`src` layout, set `source_dirs` /
`test_dirs` once; reach for a per-tool `.paths` only when one tool needs a
different scope. `forge-config --list` shows the resolved keys.

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

## `[tool.forge.env_sync]` — install-freshness gate (default-on)

Runs **first** in the default sequence. A deadly-fast, in-process
`importlib.metadata` check (no subprocess, no network): every CLI declared
in this repo's `[project.scripts]` must be an installed console script.
Editable installs do **not** auto-register new entry points, so when a PR
adds a CLI, a contributor who hasn't reinstalled is silently missing it and
the gate runs old code. This catches that up front with the exact reinstall
command — it never installs anything itself (FOUNDATION §2). Self-skips when
there is no `[project.scripts]` table, the package isn't installed at all, or
the run is non-interactive / CI (a fresh runner checkout legitimately
predates install — FOUNDATION §15).

| Key | Default | What it does | Set it when |
|---|---|---|---|
| `blocking` | `true` | Refuse the commit when a declared CLI is missing from the install (else non-blocking WARN). | You want stale-install drift surfaced without blocking — e.g. a repo where contributors don't editable-install their own package. |

## `[tool.forge.docstring_coverage]`

Forge-specific keys for the docstring-coverage reporter. (The coverage *gate*
itself — threshold, excludes, ignores — lives in `[tool.interrogate]` below;
these are the keys interrogate has no concept of.)

| Key | Default | What it does | Set it when |
|---|---|---|---|
| `paths` | `[tool.forge].source_dirs + test_dirs` | Per-tool **override** of the scan roots for the coverage report and badge. Defaults to the repo-wide layout above; set this only when docstring-coverage should scan something different. Paths resolving outside the repo are rejected. | You want coverage scoped differently from the rest of forge — otherwise prefer setting `[tool.forge].source_dirs` once. |
| `badge` | `false` | Generate **interrogate's own** coverage badge (via `interrogate.badge_gen`) to `.badges/DocstringCoverage.svg` for README embedding. forge invokes interrogate as a library, so this opt-in triggers the badge programmatically. | You want a coverage badge in your README. |

## `[tool.forge.c4]` — C4 architecture model

Configures `forge-gen-c4`, which emits a [C4](https://c4model.com) model
(Structurizr DSL by default, plus an offline HTML or raw Mermaid view) from
forge's import graph + a human-authored model. Opt into the `c4` pre-commit
step (`forge-gen-c4 --check` — keeps the committed diagram in sync with the
import graph) **by presence** of this table; it self-skips otherwise.

| Key | Default | What it does | Set it when |
|---|---|---|---|
| `config` | _unset_ | Path to a standalone model file; a root `c4.toml` is auto-detected when present. | You keep the verbose model in its own file (like `ruff.toml`) rather than inline. |
| `output` | `"docs/architecture.dsl"` | Where the generated Structurizr DSL is written. | You want the DSL artifact elsewhere. |
| `readme` | _unset_ | README path for the managed Mermaid block; unset → no README block is written. | You want the diagram embedded in a README. |
| `direction` | `"LR"` | Graph direction for every generated diagram (`"LR"` or `"TB"`); threads into the Mermaid `graph` header and the DSL `autolayout`. | Layered container models read better top-to-bottom. |
| `edges` | `"imports"` | Whether import-**derived** ("depends-on") edges are drawn: `"imports"` / `"both"` draw them, `"declared"` draws only hand-authored `[[relationship]]` edges. Declared edges always render. | You want a curated conceptual flow instead of the noisy import graph. |
| `container_edges` / `component_edges` | inherit `edges` | Per-view override of `edges` for the Container view / the per-container Component views. | You want the Container view to show a clean curated flow while Component views keep real import coupling. |

The model itself — the `system` / `person` / `external` / `container` /
`component` / `relationship` tables — lives in the external `c4.toml` (pointed
at by `config`) or inline under `[tool.forge.c4]`. The component-to-component
edges are machine-derived from the import graph; everything else is
human-declared.

**`--format html` renders each C4 view (System Context / Containers / one per
container's Components) on its own scrollable tab**, at intrinsic size — large
models pan rather than shrink. Actors are grouped into an "Actors" band. The
diagrams are laid out by the **ELK engine** (vendored offline alongside Mermaid,
with a dagre fallback), which routes the Container view's dense cross-cluster
`container → external` edges far more cleanly than Mermaid's default dagre. The
emitted HTML is **fully offline**: it sidecars `mermaid.min.js` and
`mermaid-layout-elk.iife.min.js` next to itself, so keep all three files
together if you move the page. `[tool.forge.c4].direction` (above) orients every
diagram.

**Modeling reach.** `[[person]]` accepts an optional `container = "<name>"`
(now resolving to a container **or** a component) to target a subsystem rather
than the system as a whole. A `[[relationship]]`'s `source` / `destination`
may name **any** declared element — person, container, component, or external
system — so you can express container↔container edges, an actor → a specific
component, and produce/consume data flows (a component "publishes results to"
an external store, another "reads results from" it). An endpoint matching no
element warns and is skipped. When an external is the destination of a declared
relationship, the generic `system → external` edge is suppressed in the views
where the specific edge renders (Container / flat); the System Context view
keeps its clean radial `system → external`.

See [`docs/c4-architecture.md`](c4-architecture.md) for the design and
rationale, and [`skills/c4/SKILL.md`](../skills/c4/SKILL.md) for building a
model interactively.

## `[tool.forge.cve_usage]` — usage-scoped CVE filter

A **second stage** on top of `pip_audit`. `pip_audit` flags vulnerable
*packages* (every CVE in your dependency tree); `verify-forge-cve-usage`
flags vulnerable *usage* — it greps your source for the patterns of CVEs
`pip-audit` is **currently** reporting, so you only see a warning when the
vulnerable code path is actually present.

**Opt in by presence** of a `cve_usage_patterns.toml` map at the repo root —
a `CVE-ID → {package, patterns, risk, mitigation}` table (your config; every
repo's vulnerable surface differs). The step self-skips when the map (or
pip-audit) is absent. It is **non-blocking** (advisory). Self-maintaining: a
pattern is checked only while its CVE is live, so upgrading the package makes
the warning disappear — no stale list to prune.

Matching is line-based and skips **full-line comments** only — a pattern
mentioned in a trailing `# …` comment on a code line can still match, so keep
patterns specific to the genuinely vulnerable call.

```toml
# cve_usage_patterns.toml (repo root)
["CVE-2024-0001"]
package = "lxml"
patterns = ['lxml\.etree', 'from lxml import etree']
risk = "only exploitable parsing untrusted XML"
mitigation = "ensure XML sources are trusted"
```

| Key | Default | What it does | Set it when |
|---|---|---|---|
| `paths` | `source_dirs` + `test_dirs` | Per-tool override of the scan roots; otherwise the shared layout. | The vulnerable surface lives outside your normal source roots. |

## `[tool.forge.badges]` — README status badges

`install-forge-readme-badges` writes a **drift-aware managed block**
(`<!-- forge:badges:start/end -->`) of status badges into your README;
content outside the markers is preserved on re-run. shields.io URLs where a
hosted source exists (CI, Python version, Ruff, license, forge channel,
Claude Code) and the local `.badges/DocstringCoverage.svg` when present.
Wired into `install-forge-bootstrap`.

| Key | Default | What it does | Set it when |
|---|---|---|---|
| `enabled` | `false` | Opt into writing the badge block. | You want forge to maintain a badge row in your README. |
| `readme` | `"README.md"` | README file the block is written into. | Your readme has a different name/path. |

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
