bump: minor
Added Mistral Vibe integration for forge's agent surface:
- `agent_harness.py`: Frozen harness profiles mapping Claude Code/Vibe to guard contract
- `hook_dispatch.py`: Guard dispatcher CLI (`forge-hook-dispatch`) for Vibe hooks
- `gen_vibe_hooks.py`: Generator CLI (`forge-gen-vibe-hooks`) for `.vibe/hooks.toml`
- `gen_vibe_agents.py`: Skills publisher CLI (`forge-gen-vibe-agents`) for `.agents/skills/`
- `install_vibe.py`: Main installer CLI (`install-forge-vibe`) for full Vibe setup
