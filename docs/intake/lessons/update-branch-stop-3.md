# forge-update-branch stopped on 3 conflict(s)

- surface: forge-update-branch
- release: forge 14.0.2.dev1+gf8e1e8fd2.d20261118
- seen: 2026-11-18
- branch: fix/push-gated-on-verification

## What happened

Merging origin/main stopped on paths that need judgment:
- `REPO_STRUCTURE.md`
- `src/forge/commit.py`
- `src/forge/data/skills/commit/SKILL.md`
Why they needed judgment: all three files describe `forge-commit` in prose or docstring, and both sides rewrote the same lines — origin/main's #1041 changed the message budget (50-word refused ceiling → 45-word advisory, local `COMMIT_MAX_WORDS` instead of the squash module's `MAX_WORDS`), while this branch (#1026) changed the push semantics (commit local by default, `--push`/`--push-only` gated on publication evidence, `--no-push` removed). A mechanical merge cannot keep both rewrites of one sentence; each hunk had to be read against both parents' intent and recombined by hand.

## What it costs

Three hand-resolved hunks (one per file) plus a read of both sides' diffs against the merge base; the stop itself is cheap, the judgment is the cost.

## Fix, if known

None — the conflict is inherent to two PRs editing the same descriptive lines. Resolution kept both intents: #1041's 45-word advisory budget and #1026's local-by-default push with the publication-evidence gate.
