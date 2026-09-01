bump: minor
- **Changelog fragments end the shared-heading conflict grind.** Opt-in
  `[tool.forge.changelog].mode = "fragments"`: each PR ships one unique
  `changelog.d/<slug>.<type>.md` file (level-only `bump:` front-matter —
  version numbers are gate-rejected), so parallel PRs cannot conflict and
  the stranded-entries race cannot occur. New `forge-changelog` CLI
  (`check` = the fragment gate; `assemble --delete` = the single
  release-time writer that collates fragments into CHANGELOG.md and
  stages their deletion). The changelog gates become fragment gates in
  this mode; the release fingerprint tolerates fragment deletions on
  promotion branches. Forge dogfoods — this entry is the first fragment.
