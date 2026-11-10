bump: minor
- `forge-agent-profile` now ingests Mistral Vibe unified session journals
  (`~/.vibe/logs/session/unified/child-*/journal`) via new modules
  `vibe_metadata.py` + `vibe_sessions.py` and a `--vibe-sessions` flag, so
  Vibe subagent runs (agent type, turns, tool calls, token usage, wall and
  active time) feed the same report, history and digest as the existing
  harness runs, keyed `vibe:<session_id>` so the two never collide.
- Under Vibe these journals are the only record of subagent runs, since the
  `log_agent_timing` hook has no equivalent there.
