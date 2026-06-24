# Vendored third-party assets

Provenance for non-forge files shipped under `src/forge/data/`.

## mermaid.min.js

- **Library:** Mermaid (diagram renderer)
- **License:** MIT
- **Version:** 11.6.0 (pinned)
- **Source:** https://cdn.jsdelivr.net/npm/mermaid@11.6.0/dist/mermaid.min.js
- **Bundle:** UMD all-in-one (exposes `globalThis.mermaid`); renders fully
  client-side with no network — `forge-gen-c4 --format html` copies it next
  to the emitted HTML so the diagram renders offline.
- **SHA-256:** `3a93016a73dc82ba890d919f9bbb176f3da9d98341650c0b517f2595cc68fef8`
- **Bytes:** 2666850

To update: download the new pinned version from the URL above, replace the
file, and refresh the SHA-256 + version here (`shasum -a 256`).
