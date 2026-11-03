# forge-intake retire writes "Occurrences: N, …" into a column already headed Occurrences

- surface: forge-intake retire
- release: forge 10.6.1
- seen: 2026-11-03
- branch: chore/intake-retire-879-no-version

## Evidence

`src/forge/intake.py:945` fills the ledger's Occurrences cell with the
full issue header (`occurrences = occurrence_header(entries)`), and
`src/forge/intake.py:951` already heads that column `Occurrences`. The
first ledger row reads:

```text
| Occurrences | ... |
| Occurrences: 2, on 2 branches | ... |
```

Flagged independently by the design and docs reviewers of the first
retire PR.

## What it costs

Cosmetic: every ledger row repeats the column name.

## What would fix it

Write the cell as `2, on 2 branches` (the header without its prefix).
