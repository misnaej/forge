bump: minor

- New `forge-plugin-sync`: installs each checkout's own copy of the forge release it pins (a consumer's `forge-scripts` tag pin, or forge's own manifest version) from a marketplace named after that release (`forge-vX-Y-Z`, pinned to the tag's commit), so repos on one machine can no longer overwrite each other's plugin release through the single shared `forge` marketplace. It runs from `install-forge-bootstrap` (new `plugin` step) and from the post-merge / post-checkout hooks; only the session restart stays manual.
- `install-forge-claude-settings` now keeps the shared plugin off: it removes `extraKnownMarketplaces.forge` and writes `enabledPlugins["forge@forge"] = false`. Its `--ref` flag is removed. `forge-upgrade` applies the change through bootstrap.
- `forge-doctor` reports `plugin:checkout`, the `plugin_sync` pre-commit step judges the checkout's own install, and every plugin remedy now names `forge-plugin-sync` instead of `/plugin update`.
