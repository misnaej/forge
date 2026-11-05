bump: minor

- Agents may now run `pixi lock` in repos that install from a pixi lock: it writes down what the committed manifest already asks for and keeps existing pins, so the `block_install_deps` guard allows it (with any flags). Verbs that change what the project asks for — `pixi add`, `pixi update` and the like — stay blocked. FOUNDATION §2 now also says a repo's own bootstrap script that builds only that checkout's environment is not a forbidden install.
- `block_install_deps` now also checks a pixi command run inside another manager's `run` (`pixi run pixi add x`, `uv run pixi update`): its verb must be on the same allowlist. Before, only the outer verb was read, so wrapping let any pixi verb through.
