# Smart-test — change-scoped test selection by import depth

`forge-smart-test` (skill `/forge:smart-test`) selects the tests a change set
affects — `forge.import_graph` reverse reachability from changed source
modules, unioned with directly-changed test files — and runs them in
escalating **depth tiers**: fast local feedback, then a CI ladder before a
full pass. Config reference:
[`docs/configuration.md`](configuration.md#toolforgesmart_test--opt-in-change-scoped-test-gate).

| Depth | Runs | Coverage | Typical use |
|---|---|---|---|
| `0` | Tests importing a changed module **directly** | no | Pre-commit / tight loop |
| `1` | Depth 0 + one import hop removed | no | First CI check on a PR push |
| `2` | Depth 0/1 + two import hops removed | no | Pre-merge gate |
| `full` | The **entire** suite | yes | Default-branch CI; release prep |

## Guarantees consumers can rely on

- **Conservative selection.** The walk errs toward including an extra test
  over skipping one a change could affect; a new or directly-changed test
  always runs at depth 0.
- **No false negatives only at `full`.** The smart tiers (`0`/`1`/`2`) are
  deliberately approximate; `full` runs everything.
- **Speed/coverage trade-off.** Coverage instrumentation (~3–5× slower) is
  reserved for `full` — the dominant speed difference between tiers.
- **Fail-fast.** A failing depth short-circuits higher depths and exits
  non-zero; the import cache is cleared between depths so a stale
  `__pycache__` can't mask a failure.
- **Determinism.** Same `git diff` + same tree → same selection; pytest's
  file order is sorted.
- **Import-root naming.** A changed source module is named by its real
  `sys.path` import root (top of its `__init__.py` chain), not by stripping
  `source_dirs` — so a package-rooted entry (`libs/…` → `libs.thing.core`)
  or a nested `*/src` root resolves to the name importers actually use. If
  it resolves to a name **no importer references**, smart-test warns rather
  than silently selecting zero tests.

It writes `code_health/smart_test.log` (FOUNDATION §13). The optional
`smart_test` pre-commit step is **off by default** (self-skips unless
`[tool.forge.smart_test].precommit_depth` is set) and **non-blocking**
unless `blocking = true`. Pytest stays out of the default sequence (too
slow); smart-test is the opt-in change-scoped bridge.

## Opt-in correctness extensions

The static graph **under-selects** when a test couples to code without an
`import`. Two opt-in extensions (default **off**) make the selector a
**safe superset** for mock-driven or dynamically-wired suites:

- **Mock-patch edges** (`follow_mock_patches = true`) — treats a test's
  `patch` string targets as graph edges; matters only for the patch-*only*
  case (e.g. a `sys.modules` fake against a deferred import). Orthogonal to
  module naming: it adds edges but does not fix a source-dir/import-root
  mismatch.
- **Coverage validation** (`coverage_validate = true` + `coverage_json`) —
  unions tests whose per-test coverage **contexts** touch a changed line,
  catching runtime-only links (fixtures, dynamic dispatch, `importlib`).
  Needs a fresh `coverage json --show-contexts` export
  (`pytest --cov-context=test`); regenerate on `full` runs.

A **CI directive** (`--from-commit-message`) drives the tier from a
`[depth-N]` / `[full]` commit tag (regex via `commit_directive_re`);
`--depth full` is the "run everything" escape for risky changes. With both
extensions on, smart-test is portable without losing mock- or
coverage-driven test↔code edges.
