bump: patch
provider = "mistral": Vibe only counts a model entry as a whole model when
both name and provider are present, so the provider-less entries were
dropped as orphan overrides and every generated .vibe/agents/*.toml pinning
active_model = "forge-<tier>" was rejected at load ("not an alias defined in
[[models]]"). Re-running install-forge-vibe regenerates the forge-owned
marker block and repairs existing provider-less presets without touching
user-owned config above the marker; forge-doctor's vibe:models check now
fails on preset entries missing name/provider.
