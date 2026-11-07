bump: minor
Vibe subagents now pin their forge model tier: install-forge-vibe writes
forge-owned [[models]] presets (forge-haiku/forge-sonnet/forge-opus) into
.vibe/config.toml and generated .vibe/agents/*.toml carry
active_model = "forge-<tier>", so the alias always resolves. post_agent is
no longer treated as blocking (Vibe forbids strict on it).
