bump: patch

- `forge-plugin-sync` no longer reports a failure when the release it should install is not tagged on GitHub yet — the window between a release-assembly merge and CI pushing its tag. The `post-merge` and `post-checkout` hooks stay silent and a plain `forge-plugin-sync` says the tag is not published yet; both exit 0 (`--check` still exits 1 while the checkout is not on its release). An unreachable GitHub, or an answer it cannot read, still exits 2.
