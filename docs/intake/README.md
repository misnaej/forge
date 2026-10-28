# Intake

One file per sighting of a problem not yet filed (FOUNDATION §1, §12).
Write entries with `forge-intake new`; never edit one to bump a count —
a second sighting is a second file. Counting: all files from one
feature branch are one sighting; on `main` (or with no branch) each
file is its own.

- `lessons/` — a trap in this repo's own process.
- `findings/` — a forge-shipped process misbehaving. Anonymised: no
  private names, hosts or paths; `forge-intake check` refuses them.

Entry: `# <title>`, then `- key: value` fields, then sections.

- Lesson: `seen` required; `branch` set by `new`.
- Finding: `surface`, `release`, `seen` required, plus `## Evidence`
  citing a `file:line`; `branch` set by `new`.
- Optional: `sibling: <slug>[, <slug>]` links entries word overlap
  misses; `occurrences: N` with `branch: migrated` marks a lesson moved
  from the old `docs/lessons.md`, standing for N earlier sightings.

`forge-intake group` shows recurrences; `/report-to-forge` files a
finding group and `forge-intake retire` records it in `_LEDGER.md`.
