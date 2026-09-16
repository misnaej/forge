bump: patch

- `CLAUDE.md` no longer tells contributors to bump `.claude-plugin/plugin.json`
  in an ordinary PR. The rolling-next summary now carries the fragment-mode
  carve-out that `docs/release-process.md` §1 already documented, so its two
  release bullets agree on which PR writes the manifest.
- The semver-policy bullet is stated against a PR's fragment `bump:` level —
  the decision that actually exists in fragment mode — instead of a post-tag
  manifest bump PR that this repo never opens.
- `docs/release-process.md` §1 names `forge-changelog release-pr` as what opens
  the release PR, matching §3.
