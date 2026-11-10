bump: minor
`forge-metrics bench start` now requires `--forge X.Y.Z` and sets the sandbox clone up with that forge release itself: it pins and commits the version, builds the clone's environment with it and installs the matching plugin, so a run no longer needs a manual install and its figures leave out that setup commit.
