# Smart-test — change-scoped test selection by import depth

`forge-smart-test` (skill `/forge-smart-test`) selects the tests a change set
affects — `forge.import_graph` reverse reachability from changed source
modules, unioned with directly-changed test files — and runs them in
escalating **depth tiers**: fast local feedback, then a CI ladder before a
full pass. Config reference:
[`forge-docs/configuration.md`](configuration.md#toolforgesmart_test--opt-in-change-scoped-test-gate).

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
- **Safe fallback.** A changed non-Python path the selector cannot map to
  tests escalates the run to `full` automatically — only paths matching
  `[tool.forge.smart_test].nonpython_ignore` (default: `*.md`, the plugin
  manifest, `.plan/*`, `.gitignore`, the stamp itself) are exempt. A change
  the graph does not understand is never silently under-selected.
  The guarantee holds under the default `safe_fallback = "escalate"`; a
  repo can trade it away knowingly. `"warn"` keeps the selected depth and
  logs every unmappable path, so the under-selection is visible but not
  prevented; `"off"` ignores them, with one warning saying the fallback
  is off. Both apply wherever the selector runs, CI included; the 48-hour
  truly-all run still happens. The narrower hatch is `nonpython_ignore`, which exempts named
  globs; `nonpython_ignore = ["*"]` exempts every non-Python path, the
  same effect as `"off"` but silent. Neither setting reaches a
  changed conftest outside the test roots, which always escalates: it is
  Python that rewires fixtures for tests the graph cannot see, so no
  narrower run is honest about it.
- **Conftests reach their tests.** A `conftest.py` is a dependency of
  every test in its folder and below, so editing it — or code it
  imports — selects that subtree at depth 0. A changed conftest outside
  the test roots (e.g. at the repo root) escalates to `full`. Only
  `test_*.py` / `*_test.py` files are ever selected as tests; conftests
  and helper modules are dependencies.
- **A test file git or pytest ignores is not selected.** Selection skips
  test files git ignores (a scratch copy, anything under an ignored
  directory) and those pytest's own config excludes — `--ignore`,
  `--ignore-glob` and `norecursedirs`, read from whichever file pytest
  itself would use: `pytest.toml`, `.pytest.toml`, `pytest.ini` or
  `.pytest.ini` first and whether or not they carry settings, then
  `pyproject.toml` with a `[tool.pytest]` or `[tool.pytest.ini_options]`
  table, then `tox.ini` with `[pytest]`, then `setup.cfg` with
  `[tool:pytest]`. Settings do not merge across files, so neither does
  this. Forge applies those rules to its own selection because it
  hands pytest explicit file paths, which pytest collects whatever its
  directory-level ignore settings say. An untracked file that is *not*
  ignored stays selected, so a test runs before it is `git add`-ed.
  Two deliberate asymmetries: only files that would be collected as
  tests are filtered, so an ignored conftest keeps its graph edges and
  the tests depending on it stay reachable; and when git cannot answer
  at all, nothing is filtered, because a filter that failed closed would
  drop every test and report a green run.
- **A tier says how much of the suite it runs.** Each tier's header
  reads `=== depth N: K of M test file(s) ===`, where K counts the files
  that tier adds and M the whole suite. When a run selects at least 90%
  of a suite of at least 20 test files, one warning says so before the
  first tier and names the cause — a changed `conftest.py`, a changed
  package `__init__.py`, or neither identifiable. It is measured once,
  over everything the run will execute, not per tier. That is the honest
  shape of a cheap tier about to cost a full run: the escalation is not
  prevented, only made visible.
- **Output arrives while the run happens.** Each tier's header, its
  warning and pytest's own output reach the console as they are produced,
  so a long run looks different from a hang. The run log receives the
  same text, and the output a caller gets back still holds everything,
  which is what the lifecycle, differential and exit-code rules read —
  with stderr interleaved where it was written rather than appended at
  the end. A `--telemetry` run cannot stream, because its capture goes
  through a spool file to avoid a pipe deadlock, so it shows its output
  in one piece when the run ends.
- **Running on the base branch tests the last commit.** When the base
  resolves to `HEAD` itself and the tree is clean — CI after a merge —
  the change set is `HEAD^1..HEAD`, not an empty diff; a root commit runs
  `full`. An explicit `--base` always wins, and local edits on the base
  branch are tested as usual.
- **A selection that collects nothing fails.** When named test files
  collect no tests (pytest exit 5), the run fails with an explanation
  instead of passing; a run with nothing selected, or a whole-suite run,
  still treats "no tests" as success.
- **Full-run cadence is CI's.** A periodic truly-all run is what makes
  deselection safe, and it belongs where the suite already runs: one
  scheduled or default-branch-push job calling
  `forge-smart-test --depth full --all-tests` (see
  [`ci-recipe.md`](ci-recipe.md)). Nothing times it locally. A tracked
  `.forge-full-run` stamp refreshed by a commit-time step used to carry
  this; that stamp sat 86 days stale in forge's own repo because its only
  reader was a step nobody had enabled, so the guarantee moved to the one
  place that cannot quietly stop running.
- **Lifecycle deselection is loud, bounded, and reversible.** Ordinary
  `full` runs deselect development-marked files
  (module-level `pytestmark = pytest.mark.development`) untouched for
  `lifecycle_skip_days` (default 30) — always reported as
  `lifecycle-skipped: N`; any edit to the file re-includes it, CI's
  truly-all job executes everything, and `--all-tests` forces it manually.
  Deletion is not part of the model (FOUNDATION §8 "Test lifecycle").
- **Differential check, record-only.** After each full run, failing files
  outside the would-be depth-2 selection are counted into
  `code_health/smart_test_history.log` (with wall time, file counts, and
  the development fraction) — evidence the tiers lose nothing; never a
  gate.

It writes `code_health/smart_test.log` (FOUNDATION §13). **No tests run at
commit time.** There is no `smart_test` pre-commit step: a test run is too
slow to sit in front of every commit, and the step that used to do it was
also the only writer of the cadence stamp — which is why the guarantee is
CI's now. Run it by hand, or through `/forge-smart-test`.

## Who carries the cadence — CI

CI, in every repo. The **classic schema** is two jobs: tiered smart tests
on PRs, and the full suite on every default-branch push — event-driven, so
the main path needs no clock. Add a `schedule:` trigger as a backstop for
quiet periods. Ready-made snippets: [`ci-recipe.md`](ci-recipe.md), "Smart-test
cadence in CI".

There is no mode to choose and nothing to configure. The guarantee is the
job: if it stops running, the build goes red, which is a signal people
already read — unlike a timestamp whose only reader was a pre-commit step.
A repo whose CI already runs the whole suite on every pull request has the
guarantee today and needs no extra job.

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
