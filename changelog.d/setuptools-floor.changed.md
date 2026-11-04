bump: patch

- Forge's own development install (`./dev/setup.sh`, the `[dev]` extra) now requires the setuptools release that fixes GHSA-xmj3-9gf7-h52w (a Windows-only wheel-install path issue). Consumers are unaffected: forge's build requirements and runtime dependencies are unchanged.
