# forge-update-branch stopped on 1 conflict(s)

- surface: forge-update-branch
- release: forge 14.0.0
- seen: 2026-11-18
- branch: fix/rename-leftover-matchers

## What happened

Merging origin/main stopped on paths that need judgment:
- `src/forge/hook_registry.py`

Why they needed judgment: two PRs added to the same module's import
block. This branch added `generated_name` beside `shipped_path`; the
merged #1033 added `shipped_path` alone on the same line. Textually one
conflict, semantically none — both sides were pure additions, and the
resolution is the union.

This was predicted. The review of #1033 named this exact file and this
exact line as the single collision between the two branches, and said
whoever landed second would resolve it. That is what happened, so the
stop cost nothing beyond one delegation.

## What it costs

Nothing here, and the conflict itself is not the finding.

The finding is **what the stop made visible**: `HOOKS_RESOURCE: Final =
"data/hooks.toml"` is now declared **twice** in that module, at lines 33
and 46 of `origin/main`. Not a merge artifact — it is on main, verified
directly against `origin/main`. #1033's diff added a second copy while
the original stayed as context, and the values are identical, so nothing
misbehaves and nothing caught it: ruff does not flag a redefined
module-level constant with the same value, so the pre-commit gate passed
on main and on both branches.

A duplicate constant with matching values is harmless until someone
edits one of them, at which point the module silently uses whichever is
declared later. The cost is the next person's assumption that a
module-level constant appears once.

## Fix, if known

Delete the second declaration. One line, no behavior change.

Worth asking separately whether this class is worth catching: a check
for a module-level name bound twice at top level would have caught it,
and would have caught it in #1033's own pre-commit run rather than in a
merge two PRs later. Recorded here rather than filed, since one sighting
of a harmless duplicate does not yet justify a new gate (FOUNDATION §1:
file once it recurs).
