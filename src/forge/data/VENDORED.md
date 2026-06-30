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

## mermaid-layout-elk.iife.min.js

- **Library:** `@mermaid-js/layout-elk` (Mermaid v11 ELK layout loader, bundling
  elkjs) — gives the Container view a layout engine that routes cross-cluster
  (subgraph-boundary) edges cleanly, where Mermaid's default dagre tangles them.
- **License:** MIT
- **Version:** 0.1.8 (pinned; peer `mermaid@^11.0.2`, matches the 11.6.0 above)
- **Source:** https://www.npmjs.com/package/@mermaid-js/layout-elk
- **Bundle:** **Re-bundled to a classic-script IIFE** (global `elkLayouts`) so it
  loads from `file://`. The published package is **ESM-only** and its entry
  uses dynamic `import()` for the heavy elkjs chunk — neither works from
  `file://` (browsers block module + dynamic imports there), so the offline
  HTML could never load the upstream build. The IIFE inlines every chunk (0
  dynamic imports). The page registers it via `mermaid.registerLayoutLoaders`
  and selects `layout: elk`, falling back to dagre if the global is absent.
- **SHA-256:** `64be3e0fd87f39939319071c16d505458757012585a82b628308dcc47b736249`
- **Bytes:** 1534525
- **Bundled transitive deps:** `@mermaid-js/layout-elk@0.1.8` declares
  `elkjs ^0.9.3` and `d3 ^7.9.0`; both are inlined into the IIFE (the whole
  point of the re-bundle — zero runtime imports). The exact resolved patch
  versions are whatever npm resolved at bundle time within those ranges; a
  future re-bundle should commit the `elk-build/package-lock.json` to pin
  them exactly (tracked in #127).

To update: re-bundle with esbuild and refresh the SHA-256 + version above:

```sh
mkdir elk-build && cd elk-build && npm init -y
npm install @mermaid-js/layout-elk@<version>
printf 'export { default } from "@mermaid-js/layout-elk";\n' > entry.mjs
npx esbuild entry.mjs --bundle --format=iife --global-name=elkLayouts \
  --minify --legal-comments=none --outfile=mermaid-layout-elk.iife.min.js
```
