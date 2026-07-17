# Step invocation — the orchestrator is the contract

How a `forge-precommit` step invokes its underlying tool, and when a step
warrants a standalone forge CLI. Contributor-facing: read this before adding
or changing a pre-commit step. Consumers never need this page — their
interface is `forge-precommit` (and `--only <step>` for a single check).

## The rule

**`forge-precommit --only <step>` is the single standard way to run any
step.** Uniformity for consumers and agents is delivered by the
orchestrator — one command, one config surface (`[tool.forge.*]`), one log
convention (`code_health/<step>.log`) — not by shipping one CLI per tool.

A step ships a **standalone forge CLI** only when at least one of these
holds:

1. **The CLI *is* the tool** — forge-authored logic with no third-party
   binary behind it (e.g. `verify-forge-docstrings`,
   `verify-forge-test-naming`, `verify-forge-repo-structure`).
2. **Real orchestration** — the step does meaningful work beyond
   invoke-and-mirror-exit-code (e.g. `fix-forge-ruff`: format + fix +
   re-stage + log in one transaction; `verify-forge-docstring-coverage`:
   drives interrogate as a library to add per-file tables, `MISSING:`
   dispatch lines, and badge generation).

Everything else — a third-party binary that already has a good CLI of its
own — is invoked **directly by the step function** (e.g. `pyrefly` for
`typecheck`, `pytest` for `doctest`). An in-process forge module also
qualifies when the wrapping adds real logic (e.g. `pip_audit` runs through
`forge.pip_audit_json` for JSON parsing and advisory formatting).

Why not wrap everything: every `[project.scripts]` entry is permanent
public surface — a MINOR bump to add, a MAJOR to remove, plus recurring
ceremony (`cli_wiring` reachability, `cli-reference.md`, the FOUNDATION §2
fail-loudly contract). A pass-through wrapper buys none of that back.

## Current mechanisms

| Mechanism | When | Steps |
|---|---|---|
| Forge CLI subprocess | Criteria 1 or 2 above | `ruff` (`fix-forge-ruff`), `docstring_verification`, `docstring_coverage`, `test_naming_check`, `repo_structure_check`, `manifest_json`, `commit_types_parity`, `c4`, `cli_wiring`, `agent_doc`, `plugin_version`, `smart_test`, `changelog_history`, `doc_consistency`, `cve_usage`, `regen_docs` |
| Third-party binary, direct | Good standalone CLI, no forge-added orchestration | `typecheck` (`pyrefly`), `doctest` (`pytest`) |
| In-process forge module | Wrapping logic without a CLI-worthy surface | `pip_audit` (`forge.pip_audit_json`) |
| Pure in-process check | No external tool at all — plain Python over the repo tree / git metadata | `env_sync`, `release_tag_guard`, `vendored_integrity` |

A pure in-process check stays inline in `precommit.py` and never gets a
CLI: there is no third-party binary to wrap and no standalone surface a
consumer would invoke outside the hook. Criterion 1 does not apply to it —
"the CLI *is* the tool" describes forge-authored *checkers with a
reusable standalone surface* (an AST walker you'd run over any tree), not
a few dozen lines of orchestrator-specific glue. Special case:
`auto_rebuild` shells out to a *consumer-configured* command — it invokes
whatever the repo's config names, not a forge-selected tool, so no
mechanism row fits it by design.

Existing per-tool CLIs are grandfathered under criteria 1/2 — none are
removed retroactively (removal is a breaking change for zero consumer
benefit).

## Direct invocation — the invariants you must keep

A forge-CLI subprocess gets correctness guarantees for free: `_run(cmd,
cwd=repo_root)` gives the child its own cwd and a fresh process (so
process-cached globals like `forge.git_utils.repo_root()` resolve against
the right repo). A **direct-invoked step runs in the `forge-precommit`
process** and must uphold those invariants itself:

- **Thread `repo_root` explicitly.** Never rely on cwd-derived,
  process-cached helpers; pass the step's `repo_root` argument into every
  helper that touches git or the filesystem (the `repo_root=` keyword on
  `get_modified_files` / `get_tracked_files` / `get_untracked_files`
  exists for exactly this).
- **Guard the argv.** File lists derived from config or git go after a
  `--` end-of-options separator, and path-like config values pass through
  the resolver's existence checks — never raw config text into argv.
- **Skip before `require_cli`.** When scope resolution finds nothing to
  check, return a skipped `StepResult` without demanding the binary be
  installed.
- **`run_context` still applies** (FOUNDATION §15) — no inline CI checks.

### Diff-scope selection is centralized

Do not hand-roll `get_modified_files` post-processing. Every diff-scoped
step — CLI-backed or direct — routes through
[`forge.config.select_diff_files`](../src/forge/config.py), the single home
for turning a git diff into a step's file list. Its knobs stay per-step *by
design*: `roots` (restrict to scan roots, or whole diff), `apply_exclude`
(the `[tool.forge].exclude` globs — only the two whole-tree steps set it;
ruff/typecheck own their exclusions elsewhere), and `drop_deleted` (default
on — a file deleted in the diff still appears in `git diff --name-only` but
errors when handed to a tool that opens it). This is what threads `repo_root`
correctly and drops deletions uniformly, so a new diff-scoped step gets both
guarantees for free instead of re-deriving them.

## Promotion path

A direct-invoked tool is promoted to a forge CLI **the moment it gains
genuine orchestration needs** (auto-fix + re-staging, multi-command
transactions, forge-added reporting) — not before, and not for symmetry.
Promotion is a MINOR bump; plan it deliberately.
