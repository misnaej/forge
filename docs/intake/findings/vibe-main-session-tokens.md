# Under Vibe, forge-agent-profile --tokens has no main-session row and forge-metrics records 0 tokens for Vibe-merged PRs

- surface: forge-agent-profile / forge-metrics (Vibe harness)
- release: forge 12.0.1.dev3+ga5bf90f62
- seen: 2026-11-12
- branch: main

## Evidence

- `load_sessions` reads only Claude transcript dirs (`src/forge/agent_profile.py:1942`); `runs_from_vibe_sessions` skips `kind == "root"` sessions (`src/forge/vibe_sessions.py:91`), so no Vibe root journal reaches `SessionTokens` (`src/forge/agent_profile_tokens.py:220`).
- `forge-agent-profile` default report under Vibe: 38 `vibe:`-prefixed subagent runs appear automatically (works).
- `forge-agent-profile --tokens` under a Vibe session: subagent rows carry journal tokens, but the "Main session, per active skill" row only shows old Claude transcripts from `~/.claude/projects` on this machine; the current Vibe root session's tokens are not parsed (`load_sessions` reads only Claude transcript dirs; `join_sessions` brief/handback linking needs Claude meta sidecars).
- `forge-metrics record --pr` for Vibe-merged PRs #915/#924/#930 recorded "0 new tokens" — pr_metrics attributes tokens via Claude transcript `gitBranch` records, which Vibe journals don't carry (meta.json git_branch is null). Falls back to size/review rounds only, as documented.

## What it costs

- `/perf --tokens` under Vibe misses the main session's token share (the biggest row on big tasks) and per-skill split.
- Per-PR metrics DB has zero token rows for Vibe-driven PRs; version comparisons across harnesses are impossible.

## What would fix it

- Parse Vibe root-session journals (`logs/session/unified/<root-id>/journal`) into `SessionTokens` in `agent_profile.load_sessions` (usage per completion already summed; skill-of-call from `skill` tool calls in the journal).
- For `forge-metrics`: attribute Vibe runs to PRs by cwd + time window + branch from the run's own journal (Vibe records the session's PR in the footer/API, not in the journal — may need `gh` cross-reference), or accept the documented fallback.
