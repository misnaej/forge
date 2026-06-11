# Audit Pack — `forge-audit-*`

Deterministic CLI scripts that surface design issues code review keeps
missing. Each script writes its findings to `code_health/audit_<name>.log`;
the [`forge:design-checker`](../agents/design-checker.md) agent reads
those logs as mandatory **Investigation Recipes** instead of pattern-
matching abstract principles file-by-file.

## Why this exists

Principle-based design review reads files one at a time and pattern-
matches against abstract rules — so it routinely misses **semantic,
cross-file, and domain-knowledge defects**. Examples that slip through:

- a duplicate function defended by a plausible-looking docstring;
- a claim stated correctly in one file and inverted in another;
- a CSV column silently misaligned by an unquoted comma;
- a stale `# noqa` whose underlying problem was never fixed.

The audit pack catches each of these classes **deterministically** —
they are mechanical checks, not judgement calls. The agent's job is then
to interpret the findings, not to discover them.

The scripts are runnable by any human or CI without involving an agent.
The agent's role is **interpretation** — for every substantive finding,
articulate the design problem and propose a fix.

## Installation

```bash
pip install -e ".[audit]"
```

This installs `vulture`, `jsonschema`, and `PyYAML` (all MIT). For
declarative module-dependency rules, also install the tach extra:

```bash
pip install -e ".[audit,audit-tach]"
```

`tach` (MIT) is consulted automatically when a `tach.toml` sits at the
repo root.

## CLIs

| CLI | What | Maps to (Martin, Clean Architecture Ch. 13–14) |
|---|---|---|
| `forge-audit-dup`           | duplicate / near-duplicate / name-collision functions | CRP, CCP |
| `forge-audit-deps`          | module cycles + Ca / Ce / I / A / D metrics + optional tach | ADP, SDP, SAP |
| `forge-audit-suppressions`  | every `# noqa` / `# type: ignore` / `# pragma: no cover` with rule lookup | KISS / YAGNI surface |
| `forge-audit-orphans`       | unused symbols (vulture wrapper) | YAGNI |
| `forge-audit-data`          | CSV / JSON / TOML / YAML integrity + jsonschema | data invariants |
| `forge-audit-claims`        | docstring/comment claims for verification by `forge:knowledge-search` | domain truthfulness |
| `forge-audit-all`           | run everything; aggregate summary | — |

Every CLI accepts `--scope full|changed`, `--roots <dirs>`, `--output <path>`.

## Per-script reference

### `forge-audit-dup`

**What.** Walks every `.py` file, parses with `ast`, extracts each
`FunctionDef` / `AsyncFunctionDef` (including methods), normalizes its
body by stripping the docstring and folding non-keyword identifiers to
`ID` (so `score`/`total` no longer differ structurally). Computes a
SHA-256 hash for exact-duplicate grouping and k-gram (default k=5) token
shingles for Jaccard similarity (default ≥ 0.85) for near-duplicates.
Also reports same bare name appearing in multiple files with diverging
bodies as a `LOW` informational finding.

**Severity.**

| Class | Severity |
|---|---|
| Exact body in 3+ files | CRITICAL |
| Exact body in 2 files | HIGH |
| Exact body within one file | MEDIUM |
| Near-duplicate (cross-file) | MEDIUM |
| Near-duplicate (same-file) | LOW |
| Name collision (bodies differ) | LOW |

**Tunables.** `--min-tokens` (default 30), `--jaccard-threshold`
(default 0.85), `--shingle-size` (default 5).

**Catches.** Two identical helper functions defended by a docstring.

### `forge-audit-deps`

**What.** Builds the module dependency graph (stdlib `ast` parses every
`.py`, resolves both absolute and relative imports). Detects cycles
(ADP) via Tarjan SCC. Per module computes:

- `Ca` — afferent couplings (modules depending on us)
- `Ce` — efferent couplings (modules we depend on)
- `I = Ce / (Ca + Ce)` — instability
- `A = abstract_classes / total_classes` — abstractness (`ABC` /
  `abc.ABC` / `@abstractmethod` heuristic)
- `D = |A + I − 1|` — distance from the "main sequence"

When `tach.toml` exists at the repo root AND `tach` is on PATH, the
script also runs `tach check` and merges declared-rule violations as
HIGH-severity findings.

**Severity.**

| Class | Severity |
|---|---|
| Cyclic dependency (≥ 2 modules) | CRITICAL |
| Distance D above threshold (default 0.7) | MEDIUM |
| `tach check` violation | HIGH |

**Tunables.** `--distance-threshold` (default 0.7).

**Catches.** Architecture rot — leaf modules importing orchestration,
stable-concrete modules that should be abstracted, accidental cycles.

### `forge-audit-suppressions`

**What.** Scans every `.py` via `tokenize` (so the patterns inside
docstrings / regex source aren't false-flagged). Detects:

- `# noqa[: CODE,CODE,...]`
- `# type: ignore[…]`
- `# pragma: no cover`

For each ruff rule code in a `# noqa: CODE`, resolves the rule name and
one-line summary via `ruff rule <CODE> --output-format=json` (cached
per code per run).

**Severity.**

| Class | Severity |
|---|---|
| Bare `# noqa` (no code) | HIGH |
| `# noqa: CODE` (specific) | MEDIUM |
| Bare `# type: ignore` | MEDIUM |
| `# type: ignore[…]` (specific) | LOW |
| `# pragma: no cover` | LOW |

**Catches.** A pre-existing `# noqa` left in place; the underlying
design problem (often a too-many-args function that wants a dataclass)
never investigated.

The agent reading the log is expected to ask, **for every suppression**,
whether the rule being suppressed hides a design problem:

- `PLR0913` → missing dataclass / config object
- `F841` → dead code, missing wiring
- `E501` → missing helper, over-long signature
- `C901` → function doing too much

### `forge-audit-orphans`

**What.** Wraps `vulture` (optional `[audit]` extra). Reports symbols
flagged unused with confidence ≥ `--min-confidence` (default 80).
Fails loudly if `vulture` is not importable.

**Severity.**

| Class | Severity |
|---|---|
| Confidence ≥ 95 | MEDIUM |
| Confidence ∈ [80, 95) | LOW |

Vulture is blind to dynamic dispatch / plugin entry points / runtime
introspection — false positives expected; verify before deletion.

**Tunables.** `--min-confidence` (default 80).

### `forge-audit-data`

**What.** Scans `.csv`, `.json`, `.toml`, `.yaml`, `.yml` under the scan
roots. Skips lock files (`package-lock.json`, `yarn.lock`, …).

- CSV: every data row must have the header's column count. Catches
  unquoted commas inside description fields.
- JSON: must parse. If `<file>.schema.json` sits alongside AND
  `jsonschema` is importable, validate.
- TOML: must parse (Python 3.11+). Graceful skip on 3.10.
- YAML: must parse. PyYAML ships with the `[audit]` extra, so it is
  present in any environment set up per [Installation](#installation);
  if it is somehow missing, YAML files are skipped gracefully.

**Severity.**

| Class | Severity |
|---|---|
| CSV column-count mismatch | HIGH |
| JSON / TOML / YAML parse error | HIGH |
| jsonschema validation error | MEDIUM |
| Parser unavailable (skipped) | LOW |

**Catches.** A CSV row with unquoted commas inside a description,
splitting silently mid-row.

For repo-root scans (top-level `pyproject.toml`, `.github/workflows/*.yml`,
etc.), pass `--roots .` explicitly.

### `forge-audit-claims`

**What.** AST-walks every `.py` and extracts docstrings (module / class
/ function) + line comments. For each line, matches:

- **Comparison** — `lower X = more Y`, `higher … leads to …`, etc.
- **Causation** — `causes`, `leads to`, `results in`, `implies`, …
- **Equation** — bare `X = Y`

Then filters lines that contain at least one **domain term** from the
active lexicon. The built-in default is a small CS/math seed
(`gradient`, `loss`, `accuracy`, `latency`, `throughput`, `iteration`,
`complexity`, `stability`); repos extend via `forge-audit-claims.toml`
at the repo root:

```toml
lexicon = ["kl", "rmsd", "sie", "conserved", "stable", "folded"]
```

Each match is emitted at **REVIEW** severity — extraction only, no
verification. The `forge:design-checker` agent batches these into a
single `forge:knowledge-search` query against the repo's methodology doc
and external sources, mapping verdicts to severity:

| `forge:knowledge-search` verdict | Severity in design-check report |
|---|---|
| CONTRADICTED | CRITICAL |
| UNCERTAIN | MEDIUM |
| SUPPORTED | none (informational) |

**Tunables.** `--no-default-lexicon` disables the built-in seed.

**Catches.** A factual claim repeated consistently across several
files but stated logically backwards.

### `forge-audit-all`

Runs every sub-script in order (`suppressions`, `dup`, `deps`, `orphans`,
`data`, `claims`) and writes a single summary log at
`code_health/audit_summary.log`. Exit code is the maximum of the
sub-scripts' exit codes. `--only <name> [<name>...]` runs a subset.

## Field standard — Martin's package principles

The dep + dup audits operationalize the package-level coupling
principles from Robert C. Martin's *Clean Architecture* (Ch. 13–14):

- **REP** (Reuse / Release Equivalence) — module of reuse = module of release
- **CCP** (Common Closure) — group what changes together
- **CRP** (Common Reuse) — group what's used together
- **ADP** (Acyclic Dependencies) — no cycles
- **SDP** (Stable Dependencies) — depend toward more stable modules
- **SAP** (Stable Abstractions) — stable modules should be abstract

`forge-audit-dup` enforces CRP / CCP (helpers sharing a body share a
reason to change and a reason to be reused — they belong in one place).
`forge-audit-deps` enforces ADP (cycles), SDP (instability direction),
SAP (abstractness alignment).

## Convention

Logs follow the foundation `code_health/<check>.log` convention. The
header carries a generation timestamp and finding count; the body lists
findings ordered by severity. Format is stable across the suite — agents
parse all six logs with one schema.

```text
# forge-audit-<name>
# generated: 2026-05-15T07:32:37+00:00
# findings: N

## Summary
<one-line aggregate>

## Findings
[SEVERITY] path:line message
    optional indented evidence line(s)

…
```
