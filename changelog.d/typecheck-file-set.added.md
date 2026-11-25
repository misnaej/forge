bump: minor

- The `typecheck` step gains an opt-in `[tool.forge.typecheck].file_set = "tracked"` mode: forge selects the git-tracked files under the resolved roots (gitignored paths never selected, tracked dot-directory files included) instead of letting pyrefly walk the root directories itself. The default stays `"roots"`.
- `forge-precommit --list-files [--only <steps>]` prints the exact paths a step would hand its tool, one `<step>: <path>` per line, running nothing.
- The `typecheck` step's docstring and `configuration.md` now describe both modes accurately, including that pyrefly's `project_excludes` are ignored in explicit-file mode.
