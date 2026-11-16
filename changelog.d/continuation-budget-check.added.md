bump: minor
- `forge-continuation check` now also reports how many written-section lines name landed work (a closed issue or merged PR, resolved with one batched GraphQL query), and gains `--strict`, which exits 1 when over budget or any line names landed work. Without a usable `gh` the staleness line reads `unavailable`, never zero.
- New advisory `check_continuation_budget.sh` PostToolUse hook (Write/Edit matchers) prints the budget check after every write to `.plan/CONTINUATION.md`; it reports, never blocks.
- FOUNDATION §10 gains the note's out-list — never hold what git, GitHub or another file re-derives — and the rule that a thin note means the work landed: read the log.
