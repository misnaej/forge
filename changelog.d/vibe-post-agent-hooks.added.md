bump: minor
Vibe-only post_agent hooks shipped in src/forge/data/vibe-extra-hooks.toml and
merged into .vibe/hooks.toml at generation: vibe_session_start.sh replaces
the SessionStart continuation load (once per session, deny-inject reminder),
vibe_continuation_state.sh replaces the PreCompact state write (status panel
refreshed every main-agent turn).
