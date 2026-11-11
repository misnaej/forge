# Skipping the main suite on a verified tree has no scheduled full run behind it

- surface: .github/workflows/ci.yml
- release: forge 12.0.1.dev2+g42ee3e279.d20261111
- seen: 2026-11-11
- branch: feat/ci-skip-verified-tree

## Evidence

`forge-tree-verified` lets the `main` push skip pytest when the tree
matches the one its pull request tested (`.github/workflows/ci.yml:109`).
Sound only while the suite is tree-determined. Checked once, by hand:
whole suite twice in a `forge-scratch-repo` snapshot, same commit, branch
renamed between runs, 6188 passed both. NOT probed: clock, commit SHA,
tag distance.

Nothing enforces it afterwards. A test that starts reading `git
describe`, the 30-day lifecycle window or the 48-hour stamp breaks the
assumption with no diff anywhere near the gate, and the symptom is a
green `main` that ran nothing.

`forge-docs/ci-recipe.md` already names the backstop for exactly this —
"add a scheduled full run if you want that signal on a timer". Forge
does not have one: `.github/workflows/` holds `ci.yml` and
`tag-release.yml` only.

## What it costs

A skipped run is also a run that never meets today's dependency
releases or runner image, so the two gaps share a fix. Cost today: zero,
the assumption holds. Cost when it stops holding: a `main` that reports
green on a suite nobody ran, discovered at the next unrelated failure.

## What would fix it

A `schedule:` workflow running the suite unconditionally (the gate
self-skips: its event is not `push`). Cheap, mechanical, and it holds
with nobody remembering, which is the test CLAUDE.md sets.

Second, smaller item from the same work: `ruff.toml` marks `T201`
`unfixable` for forge only. Consumers on FOUNDATION §5's
`select = ["ALL"]` baseline hit the same deleting autofix with nothing
to stop it, and forge ships no `ruff.toml` template. The surface would
be FOUNDATION §5 (at its word budget) or
`forge-docs/configuration.md`. Deferred to the user deliberately, not
forgotten.
