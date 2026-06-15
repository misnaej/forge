# Proposal: Rust core + Python analysis plugin

> **Status:** Proposal / RFC — not yet accepted. This document describes
> a target architecture and a phased migration. It commits forge to
> nothing until the decision gate in
> [§11](#11-decision-gate-when-this-is-worth-doing) is cleared.

## 1. Problem

Forge ships as a single pip package, `forge-scripts`. That is the right
default for its primary audience — Python repos, where a Python
interpreter is present by definition, so the package costs nothing to
host.

The cost appears when forge is pointed at a **non-Python repo** (a
Node/TypeScript service, a SQL-heavy data repo, a shell-tooling repo).
There:

- The repo must install a Python toolchain solely to host forge's
  orchestration, even though the repo has no other Python.
- Forge's distinctive checks — docstring verification, docstring
  coverage, the dependency/orphan audits — are Python-source analysis
  and self-skip. The consumer gets the governance layer (hooks, PR
  discipline, labels, `REPO_STRUCTURE.md` drift) but pays a Python tax
  for it.

So "adopt forge here" currently means "add Python to a repo that
doesn't want it, to run checks that don't apply." The governance layer
is genuinely language-agnostic; only the *analysis* layer is
Python-bound.

## 2. Goals and non-goals

**Goals**

- Let a non-Python repo adopt forge's governance layer with **no Python
  interpreter** required.
- Keep forge's Python-analysis checks working unchanged for Python
  repos.
- Ship the governance layer as a **single, cross-platform, dependency-
  light artifact** (the property a static binary gives that an
  interpreter does not).
- Preserve every behavioural contract consumers already depend on:
  managed git hooks, the `code_health/<step>.log` convention, the
  pre-commit step sequence, the self-refresh wrappers.

**Non-goals**

- Porting the Python-source analysis to another language. AST-based
  checks stay in Python; they only matter where Python is already
  present.
- Changing the Claude Code plugin (agents/skills/hooks). It is already
  shell + markdown and is orthogonal to this split.
- A big-bang rewrite. The migration is incremental and reversible at
  each phase.

## 3. The enabling insight: the split follows an existing seam

Forge's pre-commit dispatcher is **already subprocess-based**.
`forge-precommit` does not import the verifiers — it shells out to each
step CLI, then reads the result back as an exit code plus a
`code_health/<step>.log` file (FOUNDATION §13). Installers and audits
follow the same shape: invoke `git`/`gh`/`ruff` as subprocesses, parse
output, write a log.

That means the Rust/Python boundary does not require rewriting the
checks. It re-draws an existing line: the **orchestrator** and the
**generic validators** become a binary; the **Python-source
analyzers** stay Python tools the binary invokes exactly as it already
invokes `ruff`. The IPC contract (exit code + `code_health` log) is the
API between them, and it exists today.

## 4. Target architecture

Three artifacts, each with one job:

```
┌──────────────────────────────────────────────────────────────┐
│ forge (Rust binary, multi-call / git-style subcommands)        │
│  • forge precommit        • forge install githooks|claude-md   │
│  • forge doctor           • forge install labels|bootstrap     │
│  • forge post-merge|checkout  • forge upgrade                  │
│  • generic validators: repo-structure, manifest, plugin-ver,   │
│    pr-squash-comment, gen-commit-types, continuation-append    │
│  • generic audits: agents, claims, suppressions, all           │
│                                                                │
│  Invokes external analyzers as subprocesses (unchanged seam):  │
│   ruff · biome · sqlfluff · shellcheck · the Python pack below │
└───────────────┬──────────────────────────────────────────────┘
                │ exit code + code_health/<step>.log  (stable IPC)
┌───────────────▼──────────────────────────────────────────────┐
│ forge-analysis (Python pip pkg, OPTIONAL — installed only on   │
│ repos with Python to analyze)                                  │
│  • verify-docstrings (AST)    • audit-deps (import graph)      │
│  • verify-docstring-coverage  • audit-orphans (vulture)        │
│  • gen-api-digest (AST)       • audit-dup                      │
└──────────────────────────────────────────────────────────────┘
┌──────────────────────────────────────────────────────────────┐
│ forge Claude Code plugin (UNCHANGED — shell hooks + markdown   │
│ agents/skills). Calls the binary instead of pip console-scripts│
└──────────────────────────────────────────────────────────────┘
```

The relationship is the one forge already has with `ruff`/`biome`: the
core **orchestrates** analyzers, it does not **contain** them. The
Python pack becomes just another external analyzer that activates only
when there is Python in the repo.

## 5. Component split

Derived from the current `[project.scripts]` table. "Rust core" = the
binary; "Python pack" = the optional `forge-analysis` package;
"borderline" = mechanically portable but Python-*purpose* (only
meaningful on a Python repo), so it stays in the Python pack initially.

| Component (current CLI) | Lands in | Why |
|---|---|---|
| `forge-precommit` (dispatcher) | **Rust core** | Pure orchestration over the subprocess seam |
| `install-forge-githooks` / `-claude-md` / `-labels` / `-bootstrap` | **Rust core** | File I/O, templating, body-sha markers, `gh` calls |
| `forge-post-merge` / `-post-checkout` | **Rust core** | Drift check + backgrounded self-refresh |
| `forge-doctor` | **Rust core** | Environment diagnostics |
| `forge-next-prep` | **Rust core** | `git` orchestration (refresh, tag, prune) |
| `forge-upgrade` | **Rust core** | Pin/version management (semantics change — see §6) |
| `verify-forge-repo-structure` | **Rust core** | Tree walk + markdown diff, language-agnostic |
| `verify-forge-manifest` | **Rust core** | JSON validation (`serde_json`) |
| `verify-forge-plugin-version` | **Rust core** | Semver compare vs git tags |
| `forge-pr-squash-comment` | **Rust core** | Message validation + `gh` post |
| `forge-gen-commit-types` / `forge-gen-cli-reference` | **Rust core** | Generation/parity checks |
| `forge-continuation-append` | **Rust core** | Append-format string work |
| `verify-forge-cli-wiring` | **Rust core** | Grep over wiring sources (forge-internal) |
| `forge-audit-agents` / `-claims` / `-suppressions` / `-all` | **Rust core** | Markdown/grep; `suppressions` even generalizes across ecosystems (noqa, biome-ignore, eslint-disable, shellcheck-disable) |
| `fix-forge-ruff` | **Rust core** | Pure subprocess wrapper around `ruff` (itself a binary) |
| `verify-forge-docstrings` | **Python pack** | Python AST: signature ↔ docstring matching |
| `verify-forge-docstring-coverage` | **Python pack** | Wraps `interrogate` |
| `forge-gen-api-digest` | **Python pack** | Python AST symbol index |
| `forge-audit-deps` | **Python pack** | Python import graph |
| `forge-audit-orphans` | **Python pack** | Wraps `vulture` |
| `forge-audit-dup` | **Python pack** | Token/AST duplicate detection (Python) |
| `verify-forge-test-naming` | **Python pack** (borderline) | Python test-naming conventions |
| `forge-slow-tests-report` | **Python pack** (borderline) | Parses `pytest --durations` output |
| `block_*` / `check_*` claude-hooks | **Unchanged (shell)** | Run in Claude Code's hook context |
| `.githooks/*` wrappers | **Unchanged (shell)** | Now call `forge <sub>` instead of a console-script |

Rough split: ~20 entry points to the Rust core, ~8 staying in the
Python pack, the hook shells untouched.

## 6. Distribution changes

This is the heaviest part of the proposal — the binary buys consumer
simplicity at the cost of maintainer release machinery.

### 6.1 From `git tag` to a release pipeline

Today a release is `git tag vX.Y.Z && git push`, with the version
derived by setuptools-scm. A binary needs cross-compiled artifacts for
every target (macOS x86_64/arm64, Linux x86_64/arm64, Windows x86_64),
macOS signing + notarization, and publication to each install channel.
The tidy tag-cadence flow in `CLAUDE.md` becomes a CI build-and-publish
matrix triggered by the tag.

### 6.2 Install channels

| Channel | Audience | Notes |
|---|---|---|
| **npm** (per-platform `optionalDependencies`) | Node/TS repos | The decisive one: `npm i -D @forge/cli` ships the binary with **no Python, no pip**. The ruff/biome/esbuild pattern. |
| **Homebrew tap** | macOS/Linux devs | `brew install forge` |
| **`cargo install`** | Rust-native devs | From crates.io or git |
| **`curl \| sh` installer** | CI, generic Unix | Pins a version, drops the binary on PATH |
| **GitHub Releases** | everyone / air-gapped | Raw per-platform archives + checksums |
| **PyPI (`forge-analysis`)** | Python repos | The optional analysis pack only |

### 6.3 Three versioned artifacts, not two

Forge already coordinates a pip version and a rolling-next plugin
version. The binary adds a third axis:

- **binary version** × **`forge-analysis` version** × **plugin
  version**.

The binary ↔ analysis-pack boundary is the `code_health` + exit-code
IPC, so that interface becomes a **stability contract** with its own
semver discipline (a breaking change to a step's log format or CLI
surface is a MAJOR of the contract). `forge-doctor` grows a
responsibility: assert all installed pieces are present and mutually
compatible, and point at the exact fix when they drift.

### 6.4 `forge-upgrade` semantics change per install method

Today `forge-upgrade` rewrites a `forge-scripts @ git+...` pin in
`pyproject.toml`. With a binary there is no pin to rewrite — "upgrade"
becomes `brew upgrade` / `npm update` / re-download, branching on how
the binary was installed. The agent-safe two-phase flow (FOUNDATION §2:
agents may not install; print the command and stop) still holds — only
the printed command changes. `forge doctor` detects the install method
to print the right one.

### 6.5 CI gets simpler; private-fork auth mostly disappears

`docs/ci-access.md` exists because pulling forge from a private git
remote needs SSH/PAT juggling. Fetching a public binary from GitHub
Releases / npm / brew removes most of that, and skips the Python env +
git clone entirely — faster runners, fewer moving parts. The scheduled
`forge-upgrade --apply` recipe becomes "bump the pinned binary version."

### 6.6 Git hooks barely change

The managed wrappers currently call `forge-post-merge` (a pip
console-script on PATH). They would call `forge post-merge` (the binary
on PATH). The body-sha marker, the self-refresh, and the drop-in
`*.d/` extension dirs are unchanged. Only the "is forge installed"
probe changes from "console script present" to "binary present."

## 7. Compatibility & versioning policy

- **`forge-analysis` keeps semver from git tags**, unchanged from today.
- **The binary gets its own semver**, published per release.
- **The IPC contract is versioned independently** and documented in
  this repo (a `docs/ipc-contract.md` to be added in Phase 1): the set
  of step names, their exit-code semantics, and the `code_health` log
  shape. The binary advertises the contract version it speaks; the
  analysis pack advertises the version it emits; `forge doctor` refuses
  mismatches with a clear remediation.
- **No backwards-compat shims** beyond the explicit transition window
  in §9 (FOUNDATION §2). The legacy pip package is supported for one
  documented deprecation window, then removed in a clean break.

## 8. Testing strategy

- **Rust core**: unit + integration tests in-tree (`cargo test`),
  including golden-file tests for generated artifacts (hooks,
  `FOUNDATION.md` scaffolding) so byte-stability is enforced the way the
  body-sha sidecar enforces it today.
- **`forge-analysis`**: keeps the existing `pytest` suite verbatim —
  these are the AST checks, untouched.
- **Cross-platform matrix**: CI runs the core's test suite on
  macOS/Linux/Windows. This is new surface (the pip package is
  effectively platform-agnostic today) and is the main new testing
  cost.
- **Contract tests**: a small suite that runs the binary against a
  fixture repo with the Python pack installed and asserts every step
  still produces the expected `code_health` log + exit code — the
  regression guard for the IPC boundary.

## 9. Migration sequence (incremental, reversible)

Strangler-fig, not big-bang. The pip package keeps working throughout;
the binary takes over one subcommand at a time.

- **Phase 0 — spike (de-risk).** Port the two lowest-risk,
  zero-analysis commands — `forge doctor` and `forge install githooks`
  — to a prototype binary. They are pure orchestration with no Python
  analysis and exercise the hardest cross-platform paths (filesystem,
  markers, `gh`). Outcome: confidence on the build/sign/distribute
  pipeline before committing to the full port. **Reversible**: prototype
  lives on a branch, ships nothing.
- **Phase 1 — core orchestration + IPC contract.** Port
  `forge-precommit` (dispatcher only — it already shells out), the
  installers, and the post-merge/checkout hooks. Write
  `docs/ipc-contract.md`. The dispatcher invokes the **existing pip
  console-scripts** for every analysis step, so nothing in the Python
  pack changes yet. Ship the binary on npm + GitHub Releases as a
  preview channel.
- **Phase 2 — generic validators + audits.** Move the language-agnostic
  verifiers (`repo-structure`, `manifest`, `plugin-version`,
  `pr-squash-comment`, the generic audits) into the binary. Generalize
  `audit-suppressions` across ecosystems while doing so.
- **Phase 3 — slim the Python package to `forge-analysis`.** Strip the
  now-duplicated orchestration from the pip package, leaving only the
  AST/Python-tool analyzers. Publish as `forge-analysis`. The binary
  detects and invokes it when present; absent, the analysis steps
  self-skip (the existing skip behaviour).
- **Phase 4 — switch defaults + deprecate.** Make the binary the
  documented install path. Mark the legacy all-in-one pip package
  deprecated with a one-window timeline; `forge doctor` warns. After the
  window, remove it (clean break).

Each phase is independently shippable and leaves a working forge. At any
phase the project can stop and hold a stable hybrid.

## 10. Risks & tradeoffs

- **Dual codebase, permanently.** Two languages, two toolchains, a
  versioned contract between them. This is the standing cost; it does
  not go away after migration.
- **Release machinery.** Cross-compilation, macOS notarization, four+
  publish channels. Materially heavier than `git tag`.
- **Windows is new ground.** The pip package is platform-agnostic; a
  binary makes Windows a first-class test+sign target for the first
  time.
- **Reimplementation risk.** The installers carry subtle, well-tested
  logic (idempotency, body-sha, semver, `gh` integration). Re-porting
  must preserve it; the golden-file + contract tests in §8 are the
  guard.
- **Ecosystem confusion.** "Which forge do I install?" Mitigation:
  `forge doctor` as the single source of truth on what's installed and
  what to run; one canonical install doc per consumer type.

## 11. Decision gate: when this is worth doing

The architecture is sound and the migration is de-riskable. The
question is **not** technical — it is whether the payoff justifies a
permanent dual-codebase.

Proceed **only if** forge's roadmap includes a **growing set of
non-Python consumer repos** for which the Python tax is a real adoption
barrier. The binary's whole value is "governance layer, any repo, no
interpreter."

Do **not** proceed if the non-Python demand is one or two repos. For
that, the cheaper answer is native per-repo tooling (e.g. a hook
runner + a conventional-commit linter + the repo's existing linters,
porting only forge's *conventions*, not its code) — no fork, no dual
codebase, no release pipeline.

Recommended gate: **a documented count of ≥ N non-Python repos**
(suggest N ≥ 3) committed to adopting forge's governance layer, before
Phase 0 is funded. Phase 0 itself is cheap enough to run as a
time-boxed spike to validate the pipeline regardless.

## 12. Open questions

- npm scope/name for the binary package, and whether the PyPI package
  renames to `forge-analysis` or keeps `forge-scripts` with a slimmed
  surface.
- Whether `forge-next-prep`'s dual-track tag logic stays in the binary
  or is simple enough to remain a thin script.
- How the Claude plugin pins/declares the minimum binary version it
  needs (plugin → binary compatibility is a fourth edge).
- Whether to keep a pure-Python fallback implementation of the core for
  environments that cannot run an arbitrary binary (locked-down CI),
  or document "use the pip package on the legacy channel" for that case.
