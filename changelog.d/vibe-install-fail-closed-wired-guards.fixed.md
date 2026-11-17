bump: minor

- `install-forge-vibe` now fails closed when a wired guard script is missing from the installed package: exit 2, nothing written, message names the script and `Reinstall forge-scripts.` — previously the roster sidecar still named the missing script and the guard was silently disarmed at runtime (a missing hook command exits 127, which does not block the tool call); parity with `install-forge-claude`, and the ownership boundary is pinned by a test (a consumer file at a forge-owned hook path is overwritten by the shipped guard)
