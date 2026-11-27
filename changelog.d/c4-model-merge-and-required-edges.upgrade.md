bump: major

- Inline `[tool.forge.c4]` keys now overlay the model file, no longer inert
- **Action:** review that table before upgrading — inert keys start taking effect
- `forge-config --list` reports C4 keys from the resolved model table
- `[tool.forge.c4].blocking = false` downgrades every `c4` finding to a warning
- `[[relationship]]` accepts `required = true`; `--check` fails when imports never reach
- All 27 `[tool.forge.c4.render]` keys reach the generated reference and `forge-config`
- A model file resolving outside the repository is refused, symlinks included
