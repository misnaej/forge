# forge-pr-wrapup compose writes a wrap-up that its own post refuses for length

- surface: forge-pr-wrapup
- release: forge 10.7.0
- seen: 2026-11-04
- branch: docs/contributing-policy

## Evidence

`compose --design/--security/--docs <file>` embeds each reporter summary
verbatim under its section. A passing summary of a few sentences counts as
a clean section, so the budget stays at its base, and `post` then refuses
(src/forge/pr_wrapup.py:352):

```text
pr-wrapup: 338 words over a budget of 120 (120 + 40 x 0 findings); clean checks are one line
pr-wrapup: 138 words over a budget of 120 (120 + 40 x 0 findings); clean checks are one line
```

Two PRs in one session hit it; each time the composed file had to be
rewritten by hand to one line per clean section before `post` accepted it.

## What it costs

A refused post and a hand rewrite of a generated file on every full-mode
PR whose reporters pass with any explanation; the rewrite also drops the
evidence the reporters gave.

## What would fix it

`compose` renders a clean reporter section as one line (`PASS (verified-at
<sha>)`) and keeps detail only for sections with findings, so its output
always fits the budget `post` enforces.
