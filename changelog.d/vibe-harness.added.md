bump: minor
Added Mistral Vibe integration for forge's agent surface:
- `agent_harness.py`: Frozen harness profiles mapping Claude Code/Vibe to guard contract
- `hook_dispatch.py`: Guard dispatcher CLI (`forge-hook-dispatch`) for Vibe hooks
- `gen_vibe_hooks.py`: Generator CLI (`forge-gen-vibe-hooks`) for `.vibe/hooks.toml`
- `gen_vibe_agents.py`: Skills publisher CLI (`forge-gen-vibe-agents`) for `.agents/skills/`
- `install_vibe.py`: Main installer CLI (`install-forge-vibe`) for full Vibe setup
- `instruction_files.py`: packaged FOUNDATION text + recursive `@`-include resolution + AGENTS.md flatten (Vibe has no include directive; markdown links stay lazy pointers)
- `install-forge-vibe` regenerates AGENTS.md on every run and on post-merge/post-checkout (opt-in `[tool.forge.vibe] enabled`), prints a session-restart notice when artifacts changed, and `--verify` fails on drift
- `claude-hooks/block_vibe_config.sh`: blocks agent edits/deletes of .vibe/, .agents/ and root AGENTS.md
- `forge-doctor --harness {auto,all,claude,vibe}` checks the active harness (auto skips absent ones); docs/vibe.md documents the integration
