# forge-test-writer edited a test file with shell commands instead of Edit/Write

- surface: forge-test-writer
- release: forge 16.2.3
- seen: 2026-11-26
- branch: feat/terse-bullets-and-fragment-cap

## Evidence

Agent's own report, unprompted: "I appended the new `test_text_budget.py`
tests and made one one-word `refuse_over` fix there with shell commands
(heredoc append and a Python replace) instead of Edit/Write. This breaks
the agent rule 'Edit/Write only'."

Content was correct; 626 tests passed. Self-reported, not caught by any
check. Rule broken: `src/forge/data/agents/test-writer.md:86`.

## What it costs

A heredoc rewrite of a test file is unreviewable in the transcript — the
diff never appears, so a truncation or a clobbered neighbouring test
shows up only as a later failure. Nothing refuses it: the agent holds
`Bash` because it must run pytest, and no hook separates running tests
from writing them.

## What would fix it

Unknown, and one sighting does not justify a gate. The agent cannot lose
`Bash` (pytest), so a tool restriction is not available. Candidates, in
order of cost: the agent doc stating WHY the restriction exists (it
currently states only the rule); a `block_*` hook refusing a write to
`tests/**` through a shell redirect or inline interpreter, which would
have to let pytest through.

Recorded, not filed (FOUNDATION §1: file once it recurs).
