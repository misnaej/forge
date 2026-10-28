# issue-triage hands back the word "placeholder" instead of its report

- surface: forge:issue-triage (agents/issue-triage.md)
- release: forge 10.5.0 (session plugin); forge-scripts 10.6.0 installed
- seen: 2026-10-28
- branch: main

## Evidence

`agents/issue-triage.md:288` — "Every mode ends with a report line naming
the mode and the counts ("N triaged, M respected, Backlog Index
updated"), plus whatever the mode decided".

A `post-pr` run for a merged PR, delegated through the forge-local
wrapper `issue-triage-forge` (FOUNDATION §16 Pattern A), returned only:

```text
placeholder
```

The wrapper then re-read GitHub itself: the Backlog Index had been
regenerated and no labels changed, so the work landed; only the report
was missing.

## What it costs

The caller gets no verdict and must re-verify on GitHub by hand; a
mode that did change labels would hand back nothing to audit.

## What would fix it

Unknown from one sighting. Possibly the same family as a delegating
agent spawning a "standby" or placeholder subagent and handing back
before it finishes (seen once from `forge:precommit-fixer` the same day,
not yet recorded).
