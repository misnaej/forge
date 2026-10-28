# Intake

One file per sighting of a problem not yet filed (FOUNDATION §1, §12).
Write entries with `forge-intake new`; never edit one to bump a count —
a second sighting is a second file.

- `lessons/` — a trap in this repo's own process.
- `findings/` — a forge-shipped process misbehaving. Anonymised: no
  private names, hosts or paths; `forge-intake check` refuses them.

Entry: `# <title>`, then `- key: value` fields (`surface`, `release`,
`seen`, `branch`; `sibling: <slug>` links another entry by hand), then
sections. A finding needs `## Evidence` citing a `file:line`.

`forge-intake group` shows recurrences; `/report-to-forge` files a
finding group and `forge-intake retire` records it in `_LEDGER.md`.
