bump: minor
- New agent rule (FOUNDATION §3, `agents/_TEMPLATE.md`, `docs/vibe.md`): under Vibe, reuse an existing idle subagent via a follow-up message instead of spawning a new one — Vibe never discards children on turn end, the child limit (default 32, `subagent_max_children`) counts idle children, and agents have no close/discard tool
