---
name: plan-issue
description: Human-in-the-loop planning for one backlog issue - investigate read-only, confirm scope and approach with the user, then record the validated plan as a plan-ready execution spec. Use when an issue surfaced as a needs-plan candidate, or the user names an issue to plan.
---

# Plan an Issue (human-validated)

Turn one screened backlog issue into a validated execution spec that
`/sentinel` may execute autonomously. Policy:
[FOUNDATION §14 "Plan-readiness pipeline"](../../FOUNDATION.md#14-issue-tracking--triage).
Screening lives in `issue-triage`'s `plan-readiness` mode; this skill
is the human gate between screening and execution. It never writes
code and never creates a branch.

## Step 1: Verify readiness

```bash
gh issue view <N> --json title,body,labels,state,comments
```

Confirm the issue is open, unblocked (its `Requires:` line names
nothing open), and non-colliding with open issues / PRs. Not ready →
report why and stop. Already `plan-ready` → surface the existing
`plan-validated` comment and ask whether to re-plan.

## Step 2: Investigate read-only

Per FOUNDATION §1 "Read before proposing": read the touched modules,
their callers, and the relevant docs; treat the issue's suggested
names/paths as hypotheses to validate against the current layout. No
edits.

## Step 3: Confirm plan elements with the user

Systematically confirm the judgment calls via targeted questions
(`AskUserQuestion`-style, one decision each) **before** finalizing:

- scope boundaries (in / out, follow-ups to file separately)
- approach, when more than one is reasonable (recommend one)
- edge-case and failure handling
- versioning / blast radius (semver bump class, consumer impact)
- test expectations

## Step 4: Explicit validation

Present the complete plan (files, order, side effects, bump class).
Proceed only on the user's explicit validation — silence, partial
answers, or "looks fine so far" are not validation.

## Step 5: Record via `issue-triage`

Issue-state mutation has one owner (FOUNDATION §3) — delegate, never
run `gh` label/comment commands here:

```
Agent(subagent_type="forge:issue-triage", prompt="Record a validated
plan for issue #<N>: post the plan below as a comment opening with
`[issue-triage] plan-validated:` and apply the `plan-ready` label.
<validated plan text>")
```

The issue body — the original ask — is never edited. Report the
recorded comment URL, append it as a one-line record to
`.plan/CONTINUATION.md` (an audit trail `/sentinel` can cross-check at
pickup), and stop; execution belongs to `/sentinel` or a later
session.
