bump: patch
- **`regen_docs` stale-install advisory** — the pre-commit doc-regen step now WARNs (never blocks) when declared console scripts are absent from the install `docs/cli-reference.md` is regenerated from, instead of silently omitting new CLIs in non-interactive runs.
