bump: minor
- **Manifest version schema check** — `verify-forge-manifest` now rejects a `plugin.json` `version` value that is not bare `X.Y.Z` semver (non-string, `v` prefix, suffixes, or JSON-escape payloads that smuggle injected keys past textual rewrites). Missing field stays legal; `marketplace.json` keeps plain-parse behavior.
