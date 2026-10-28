# precommit-fixer hands back while its delegated docs-types-checker is still running

- surface: forge:precommit-fixer (agents/precommit-fixer.md)
- release: forge 10.5.0 (session plugin)
- seen: 2026-10-28
- branch: fix/config-parity
- sibling: triage-handback-placeholder

## Evidence

`agents/precommit-fixer.md:91` — "`docstring_verification.log` | Task
**`docs-types-checker`** with the log path; wait for it."

The fixer's hand-back (verbatim excerpt):

```text
docstring_verification: 0 of 17 ERRORS confirmed resolved. Dispatched to
forge:docs-types-checker ... That agent had not reported when this run
ended, so its edits, if any, are unverified.
I launched a second forge:docs-types-checker agent ... as a no-op
"standby" placeholder. It reported no changes. It should not have been
spawned.
```

The delegated agent did finish its edits afterwards; the next commit
attempt failed on a stale log because nothing re-ran the step.

## What it costs

Two extra fixer and commit round trips; the caller cannot trust a
STUCK report that may describe work still in progress.

## What would fix it
