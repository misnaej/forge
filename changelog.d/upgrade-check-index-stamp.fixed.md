bump: patch

- `changelog_version` declared `reads_index`, so staging a fragment stales its log instead of reading fresh.
- `forge-upgrade --check` now warns on unassembled release notes and enumerates the affected tags.
