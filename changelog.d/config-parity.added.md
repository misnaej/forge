bump: minor

- `forge-docs/configuration.md` carries one generated **All keys and defaults** table, rendered from forge-config's key list by the new `forge-gen-config-doc` (`--check` verifies it); the hand-written tables no longer repeat defaults, so each default is stated once and cannot drift. The new blocking pre-commit step `config_doc_check` runs the check in forge's own repo only.
- `forge-config --list` now lists keys forge already read — `env_sync.rebuild_command`, the `paths` override of `slow_tests_report`, `c4`, `docstring_verification` and `test_naming_check`, and `c4.direction` / `edges` / `container_edges` / `component_edges` — prints each `[section]` once, and corrects the `badges.workflow` default and the `plugin_sync.blocking` description.
- A test scans forge's source for every `[tool.forge.*]` read and fails when forge-config's list lacks one, replacing two substring checks that could not tell keys apart.
