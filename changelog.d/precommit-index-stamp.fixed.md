bump: patch

- `forge-precommit` freshness stamps now carry an `index=` component alongside `tree=`, and steps that read staged state (`regen_docs`, `changelog_updated`) are judged against it — staging a file no longer leaves a verdict reading `fresh` over stale evidence, and a `SKIP` from an index-reading step fails once the index moves. Old stamps without `index=` still parse and read `unknown` for those steps, forcing one full re-run.
