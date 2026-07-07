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
- **Local patch (forge):** the upstream adapter hardcodes the ELK layout
  spacing (`spacing.baseValue: 35` on the root graph, `30` on subgraph nodes)
  and the model-order options (`forceNodeModelOrder: true`, no
  `considerModelOrder`), forwarding neither from `config.elk` — so
  `forge-gen-c4`'s render config could not tune node/layer gaps nor override
  node ordering under ELK (issue #146). Before re-bundling,
  `dist/chunks/mermaid-layout-elk.core/render-*.mjs` is patched to read these
  from `config.elk`: `spacing.baseValue` ← `config.elk?.baseValue ?? <default>`
  (both sites); `spacing.nodeNode` / `elk.layered.spacing.nodeNodeBetweenLayers`
  ← `config.elk?.nodeSpacing` / `?.layerSpacing` (injected only when set);
  `elk.layered.crossingMinimization.forceNodeModelOrder` ←
  `config.elk?.forceNodeModelOrder ?? true`; and
  `elk.layered.considerModelOrder.strategy` ← `config.elk?.considerModelOrder`
  (injected only when set). Inert when unset (rendered output byte-identical to
  upstream), so defaults are unchanged.
- **SHA-256:** `8d7b281b00030c344a9790e1c63f1f8307e6a65aa3efc609975d3c9153abc014`
- **Bytes:** 1535121
- **Bundled transitive deps:** `@mermaid-js/layout-elk@0.1.8` declares
  `elkjs ^0.9.3` and `d3 ^7.9.0`; both are inlined into the IIFE (the whole
  point of the re-bundle — zero runtime imports). The exact resolved patch
  versions are whatever npm resolved at bundle time within those ranges; a
  future re-bundle should commit the `elk-build/package-lock.json` to pin
  them exactly (tracked in #127).

To update: re-bundle with esbuild and refresh the SHA-256 + version above.
**Re-apply the forge spacing patch** (see "Local patch" above) to the freshly
installed `dist/chunks/mermaid-layout-elk.core/render-*.mjs` before bundling —
the `.` package export resolves to the unminified `.core` chunk, so patch that
one:

```sh
mkdir elk-build && cd elk-build && npm init -y
npm install @mermaid-js/layout-elk@<version>
# Patch dist/chunks/mermaid-layout-elk.core/render-*.mjs. At both
# `spacing.baseValue` sites (root graph + subgraph node.layoutOptions):
#   "spacing.baseValue": data4Layout.config.elk?.baseValue ?? <35 or 30>,
#   ...(data4Layout.config.elk?.nodeSpacing != null ? { "spacing.nodeNode": data4Layout.config.elk.nodeSpacing } : {}),
#   ...(data4Layout.config.elk?.layerSpacing != null ? { "elk.layered.spacing.nodeNodeBetweenLayers": data4Layout.config.elk.layerSpacing } : {}),
# And on the root graph's layoutOptions (model order):
#   "elk.layered.crossingMinimization.forceNodeModelOrder": data4Layout.config.elk?.forceNodeModelOrder ?? true,
#   ...(data4Layout.config.elk?.considerModelOrder != null ? { "elk.layered.considerModelOrder.strategy": data4Layout.config.elk.considerModelOrder } : {}),
printf 'export { default } from "@mermaid-js/layout-elk";\n' > entry.mjs
npx esbuild entry.mjs --bundle --format=iife --global-name=elkLayouts \
  --minify --legal-comments=none --outfile=mermaid-layout-elk.iife.min.js
```
