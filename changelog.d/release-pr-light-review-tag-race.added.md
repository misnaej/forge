bump: minor

- A release-assembly PR — only the manifest's version bump, `CHANGELOG.md` and consumed changelog fragments — now takes a new `light-release` finalization path: only the docs reviewer reads it, instead of the full three-reviewer round. Eligibility is checked on content (the manifest may change its version value and nothing else; fragments may only be deleted), and `forge-pr-create` re-earns it at publish, failing closed.
- `forge-release --from-changelog` no longer reports success when its tag push fails: after a failed push it asks the remote whether the tag landed, instead of finding the tag it had just created locally.
