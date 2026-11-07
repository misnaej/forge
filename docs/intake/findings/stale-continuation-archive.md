# Stale CONTINUATION.md should be archived at session start

- surface: continuation
- release: forge 9.1.2.dev93+gc5c07b9c9.d20261105
- seen: 2026-11-05
- branch: feat/portable-agent-harness

## Evidence

Session resumed 2026-11-05 on branch feat/portable-agent-harness; .plan/CONTINUATION.md still described PR #692 from 2026-10-06. `forge-continuation check` reports "written section 197/60 lines (over budget)" — the auto-generated condensed history counts toward the written-section budget, so the budget is unreachable in any repo with history. The count is emitted unconditionally at `src/forge/continuation.py:187`.

## What it costs

Next session reads stale status as current; line-budget check is noise (always over), so real overruns go unnoticed.

## What would fix it

- On session start (SessionStart hook or `forge-continuation state`), if the newest dated entry is older than N days, move CONTINUATION.md to CONTINUATION-archive.md and start a fresh note with a one-line pointer.
- `forge-continuation check` should exclude the auto-generated condensed-history section from the written-section line count.
