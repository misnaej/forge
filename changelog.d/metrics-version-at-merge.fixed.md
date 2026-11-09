bump: patch
`forge-metrics` now records the forge version each PR was actually built with, read from its merge commit (the release tag it contains in forge, the forge-scripts pin in a consumer), instead of the version installed when recording; it stays empty when it cannot be told. `backfill --refresh` corrects the version of PRs already recorded without touching their other figures.
