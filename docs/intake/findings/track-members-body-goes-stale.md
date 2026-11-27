# Track issue Members lists rot: the only agent allowed to maintain tracks may not edit issue bodies

- surface: forge-issue-triage
- release: forge 16.5.0
- seen: 2026-11-26
- branch: main

## Evidence

Two track issues hold a stale `## Members` list after one triage run:

- #1105 (`track:agent-shape`) still lists #954 as a member after #954 moved
  to a new track. Triage left a comment saying so; the body still names it.
- #1102 (`track:triage-guard`) does not list #1126 after #1126 joined. Same
  shape, opposite direction.

Both reported by the agent itself, unprompted: "body not edited" and "add
#1126 by hand". Not a lapse — `FOUNDATION.md:895` ("Issue body never
edited.") bars the edit, and `forge-docs/backlog-tracks.md:56` makes a
validated plan naming the track, not the body, the membership record.
`forge-issue-triage` is the only owner allowed to record it.

## What it costs

A track's membership is read two ways that now disagree: the label, which
triage maintains, and the body's `## Members` list, which nothing can.
`forge-docs/backlog-tracks.md` says membership is settled by the validated
plan naming the track, so the body is advisory — but it is what a human
reads first, and a planner reading a stale list plans the wrong set. The
drift is silent and one-directional: every track change makes it worse,
nothing makes it better.

## What would fix it

Candidates, cheapest first. State the members as `[forge-issue-triage]`
comments only, and have the body carry a pointer rather than a list, so
there is one home instead of two. Or generate the list — the track label
is queryable (`gh issue list --label track:<name>`), so a `forge-plan-check
track-members` render could be the single source and the body could say to
run it. Or carve a bounded exception to the never-edit-a-body rule for the
`## Members` block of a track issue the agent itself authored, which is
narrower than it sounds but reopens a rule that exists for good reasons.

Two sightings in one run, on two issues, in both directions.
