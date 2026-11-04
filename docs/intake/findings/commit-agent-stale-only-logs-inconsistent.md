# git-commit-push stops or proceeds on the same stale --only logs depending on the run

- surface: agents/git-commit-push.md
- release: forge 10.7.0
- seen: 2026-11-04
- branch: docs/contributing-policy

## Evidence

agents/git-commit-push.md:73 says to judge only the steps the latest full
run executed (`precommit_timing.log` rows) and that other logs are
leftovers. In one session, with the same kind of tree (prose edit after a
passing full run, evidence-pack logs from `forge-pr-plan --evidence` left
stale), one dispatch committed and the next stopped. The stopping hand-back:

```text
GIT-COMMIT-PUSH NOT DONE. I stopped before staging or committing.
Why I stopped: `forge-precommit --freshness` reports stale code_health logs.
No step reads FAIL in precommit_timing.log ...
The stale logs are: api_digest_check, audit_agents, ... pr_evidence,
precommit_only_timing, pytest, smart_test
```

None of those logs belongs to a full-run step.

## What it costs

A wasted dispatch and a re-brief with the rule restated; the caller cannot
predict which way a given run goes.

## What would fix it

A mechanical verdict instead of prose judgement: the agent runs
`forge-precommit --verdict` (already scoped to full-run steps) and commits
on PASS, so `--freshness` noise from `--only` logs never reaches the
decision.
