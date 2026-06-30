---
name: smart-test
description: Run only the tests a change set affects, in escalating import-depth tiers (0/1/2/full) with fail-fast. Use when the user wants a fast change-scoped test run instead of the whole suite, or asks to "smart test" / "run affected tests".
user-invocable: true
---

# /forge:smart-test — change-scoped test run

Run the tests affected by the current change set at a chosen import depth,
via the `forge-smart-test` CLI. Depth grows the blast radius: `0` runs only
tests that import a changed module directly, `1` adds one import hop, `2`
adds two, and `full` runs the entire suite with coverage. Lower depths must
pass before higher ones run (fail-fast).

## Parse the depth from `$ARGUMENTS`

- empty → depth `1` (the default)
- `0`, `1`, `2` → that depth
- `full` or `infinity` → the whole suite + coverage
- `files` (or `--show-files`) → print the selected-test plan and run nothing

## Steps

1. **Show the plan first** when the user wants to see what would run, or to
   sanity-check selection before a long run:
   ```bash
   forge-smart-test --show-files --depth <depth>
   ```
   This prints a `📋 Tests covering changed code` block (one `  - <path>`
   line per test) and exits without running pytest.

2. **Run the tier(s):**
   ```bash
   forge-smart-test --depth <depth>
   ```
   - Selection is computed from `git diff` vs the integration branch plus
     staged / unstaged / untracked edits — no extra arguments needed.
   - `--coverage` adds coverage to the smart tiers (it is always on for
     `full`).
   - `--base <ref>` overrides the diff base (e.g. a PR target branch in CI).

3. **Report the outcome.** On failure the run stops at the first failing
   depth and exits non-zero; the full output is also written to
   `code_health/smart_test.log` for `forge:precommit-fixer`. Surface the
   failing depth and the failing tests; do not escalate to a higher depth
   until the current one is green.

## Notes

- This is selection, not a coverage guarantee: the smart tiers are
  deliberately approximate and conservative (they err toward running an
  extra test). `full` is the only no-false-negatives tier.
- For an opt-in depth-0 gate on every commit, set
  `[tool.forge.smart_test].precommit_depth = 0` in `pyproject.toml` (see
  `docs/configuration.md`); the `changelog_history`-style self-skipping
  `smart_test` pre-commit step then runs it. The depth model and the
  speed/coverage trade-off are documented in FOUNDATION §17.
