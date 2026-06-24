# C4 architecture-diagram generator — design & rationale

> **Status:** Implemented (shipped in #99). This is the design-rationale
> doc for `forge-gen-c4` + the `/c4` skill — why it works the way it does,
> what was deliberately left out, and the deferred future work in
> [§9](#9-future-work). For *how to configure it* see
> [`docs/configuration.md`](configuration.md) (`[tool.forge.c4]`); for the
> interactive build flow see [`skills/c4/SKILL.md`](../skills/c4/SKILL.md).
> Whether forge keeps growing it is the decision gate in
> [§8](#8-decision-gate-is-this-worth-forges-while).

## 1. Problem & motivation

Forge already understands a consumer's code structure — it builds an
internal Python import graph (`forge-audit-deps`), a public-symbol index
(`docs/api-digest.md`), and a directory map (`REPO_STRUCTURE.md`). None
of that is *visual*. The [C4 model](https://c4model.com/) is the
de-facto-standard way to describe software architecture at four zoom
levels (System Context → Container → Component → Code).

The question this RFC answers: **can forge ship a config-driven tool
that turns what it already knows into a C4 diagram, without reinventing
a diagramming engine and without locking consumers into one vendor?**

The honest framing up front (see §8): forge's *need* for this is
limited — it is one small Python package, not a sprawling system. The
value is (a) a genuinely reusable consumer feature and (b) a forcing
function that dogfoods forge's own analysis artifacts.

## 2. Research summary — don't reinvent the wheel

A multi-source survey (see the companion research in the PR thread)
established:

- **C4 is notation- and tooling-independent by design**
  ([c4model.com/introduction](https://c4model.com/introduction)). Only
  the bottom two levels are derivable from source: **Component**
  dependencies come from an import graph, **Code** from class structure.
  **System Context and Container are human-authored** — they encode
  intent, ownership, and runtime topology that source code does not
  contain.
- **No tool auto-derives clean C4 Components from Python.** C4InterFlow
  (MIT) does source→C4 but is **C#/.NET only**. `pyreverse` (pylint)
  emits **UML, not C4**. `grimp` exposes a Python import graph but no C4
  mapping. The unfilled gap is *clustering import-graph modules into
  named Components* — which needs human declaration.
- **Diagram-as-code target — Structurizr DSL is the safest bet.** The
  DSL repo is **Apache-2.0**; the Structurizr Lite renderer is **MIT**.
  It cleanly separates *model* (content) from *views* (presentation),
  stores an open JSON format, is git-diffable, and the Structurizr CLI
  re-exports one model to PlantUML, Mermaid, and others — so emitting it
  is **not** lock-in.
  - **Mermaid C4** is officially *experimental* ("syntax can change") —
    fine as a secondary export, wrong as the primary target.
  - **C4-PlantUML** is mature but single-renderer.
- **Python model-builders (`pystructurizr`, `buildzr`, both MIT)** are
  *manual* — they let you write a model in Python but do **no** source
  analysis. Adopting one buys a fluent builder and a dependency, while
  forge already has the graph and can emit DSL text directly.

**Conclusion:** forge should **emit a Structurizr DSL text artifact**
from its existing analysis + a config block, and **render nothing
itself** — exactly mirroring `forge-gen-api-digest` /
`forge-gen-cli-reference`. Consumers render with Structurizr Lite (MIT)
or the Structurizr CLI.

## 3. The deterministic / reasoned split

The core design insight: C4 maps cleanly onto "what a machine can derive"
vs. "what a human must declare".

| C4 element | Source | Owner |
|---|---|---|
| System Context (actors, external systems) | not in code | **human** → `[tool.forge.c4]` config |
| Containers (deployable units) | not in code | **human** → config |
| Component **membership** (which modules form a component) | ambiguous | **human** → config (with directory-default heuristic) |
| Component **dependencies** (`A -> B`) | import graph | **machine** → `forge-audit-deps` graph |
| Component internals (Code level) | class/symbol structure | machine (`api-digest`) — out of scope for v1 |

So the system is two pieces:

1. **`forge-gen-c4`** — *deterministic* CLI. Given the config + the
   import graph, it emits Structurizr DSL. Pure function of inputs; has a
   `--check` drift mode like the other generators. **Ships in v1.**
2. **`/c4` skill** — *reasoned* layer. An agent digs into the repo
   (`REPO_STRUCTURE.md`, `api-digest.md`, the dep tree, the source) and
   **proposes the human half**: the system context, containers, and
   component groupings, written into `[tool.forge.c4]`. Then it runs the
   CLI. **Ships in v1 as a simple skill** (no sub-agents yet — see §8).

The division is the whole point: the boring, drift-prone half (component
edges) is automated and deterministic; the judgement half (what *is* a
component here, who are the actors) is where reasoning adds value.

## 4. Config schema (`[tool.forge.c4]`)

Mirrors every other `[tool.forge.*]` tool — read via the canonical
`read_pyproject_raw`. Human-declared:

```toml
[tool.forge.c4]
system = "Forge"
description = "Python CI/CD & code-quality foundation"
output = "docs/architecture.dsl"   # default

# External actors (C4 "person") and systems — the System Context level.
[[tool.forge.c4.person]]
name = "Forge developer"
description = "Maintains forge"
uses = "develops + runs pre-commit"

[[tool.forge.c4.external]]
name = "GitHub"
description = "Hosts repos, PRs, issues, labels"
relationship = "reads/writes via gh"

# One or more Containers (deployable units).
[[tool.forge.c4.container]]
name = "forge-scripts"
technology = "Python pip package"
description = "Deterministic CLIs + pre-commit dispatcher"

# Component membership: name -> module prefixes. Dependencies between
# these are auto-derived from the import graph.
[tool.forge.c4.components]
"Pre-commit dispatcher" = ["forge.precommit"]
"Audit suite"           = ["forge.audit"]
"Installers"            = ["forge.install_bootstrap", "forge.install_githooks"]
"Doc generators"        = ["forge.gen_api_digest", "forge.gen_cli_reference", "forge.gen_c4"]
```

Machine-derived (no config): the `->` edges between components, computed
by mapping each import-graph edge `m1 -> m2` to `component(m1) ->
component(m2)` when they differ.

## 5. What v1 emits

A single deterministic `.dsl` file: a `workspace` with a `model`
(persons, the system, its containers, the components, and all
relationships) and `views` (systemContext, container, and one component
view per container, each `autolayout`). Identifiers are slugified from
names; output is sorted so the file is diff-stable and `--check`-able.

Scope boundaries for v1:
- **Code level is skipped** — C4 calls it optional, and verification
  showed Structurizr DSL does not cleanly cover it; leave it to
  `pyreverse` if ever wanted.
- **One import graph → components within containers.** Multi-container
  dependency inference (which container a component talks to across a
  boundary) stays human-declared.
- Modules matching no component prefix are **reported, not silently
  dropped** (a coverage warning), so the picture can't quietly lie.

### Output formats (v1, implemented)

`forge-gen-c4 --format`:

- **`dsl`** (default) — writes `docs/architecture.dsl` (canonical,
  committed) and, when `[tool.forge.c4].readme` is set, keeps a managed
  Mermaid block in the README (`<!-- forge:c4:start/end -->`) in sync.
- **`html`** — a self-contained **offline** view: a Mermaid flowchart in
  an HTML page that references a vendored `mermaid.min.js` (MIT, shipped
  as forge package data, copied next to the HTML). Needs only
  `pip install` — no Docker, Java, Graphviz, or network. The generated
  HTML + sidecar are gitignored (on-demand).
- **`mermaid`** — raw canonical Mermaid to stdout (for embedding).

Every relationship line carries a label: derived import edges read
**"imports"**; human-declared `[[relationship]]` edges carry their own
phrase (the runtime/subprocess "uses" the import graph can't see). Boxes
carry **description + technology** via the rich `[[component]]` config
(the simple `[components]` map remains a quick-start shorthand).

### Drift wiring (the diagram updates at each PR)

The opt-in **`c4` pre-commit step** runs `forge-gen-c4 --check`,
verifying both `docs/architecture.dsl` and the README block against the
current import graph. A structural change that isn't regenerated fails
the commit — so the diagram refreshes at each PR exactly when the
architecture actually changed. Self-skips when no `[tool.forge.c4]`.

## 6. Why this fits forge's existing patterns

- **Reuses `forge.audit.deps`** via a new public `build_module_graph`
  seam (single source of truth for the graph; `forge-audit-deps` uses
  the same function).
- **Generator + `--check` drift** mirrors `gen_api_digest` /
  `gen_cli_reference` (shared `gen_common.check_doc_drift`).
- **`[tool.forge.c4]` config** read the canonical way; surfaced by
  `forge-config`.
- **Opt-in bootstrap step** — self-skips when `[tool.forge.c4]` is
  absent, like `readme-badges`.
- **`/c4` skill** auto-discovered by the plugin, like every other skill.

## 7. Risks & limitations

- **Component clustering is only as good as the config.** A bad grouping
  yields a misleading diagram. The skill mitigates by proposing a
  sensible default from package structure, but a human must review.
- **Structurizr Lite is in maintenance mode** (consolidating into
  "Structurizr vNext"). The **DSL/JSON format it consumes is stable**
  and Apache-2.0, so the emitted artifact stays renderable; forge stays
  renderer-agnostic and does not depend on Lite.
- **Forge itself barely needs this** (§8). The feature is justified by
  consumer value, not forge's own size.

## 8. Decision gate: is this worth forge's while?

Keep + grow this only if **a real consumer wants architecture diagrams
from their code**. For forge's own one-package repo the diagram is a
toy. The v1 spike exists to (a) prove the deterministic/reasoned split
works end-to-end and (b) dogfood the analysis artifacts. If no consumer
demand materialises, the honest call is to keep `forge-gen-c4` as a
small, self-skipping opt-in and **not** invest in multi-container
inference, Code-level views, or a sub-agent reasoning fleet.

## 9. Future work

Deferred enhancements (the feature shipped without these by design):

1. Directory/package-boundary auto-clustering (cheap, from
   `REPO_STRUCTURE`) vs. fully explicit config groupings — how much
   manual config before the tool stops saving effort?
2. Should forge depend on the external Structurizr CLI (a Java tool) for
   downstream PlantUML/Mermaid conversion, or emit DSL only and leave
   rendering entirely to the user? (v1: **DSL only**.)
3. Is "Structurizr vNext" fully free/OSS and DSL-compatible long-term?
4. Should the `/c4` skill grow into a skill + sub-agent fleet (one
   agent per C4 level) or stay a single reasoning pass? (v1: single.)
5. **Shape/style per element type** — C4's reference notation uses
   distinct shapes/colors for person vs. system vs. container vs.
   component vs. external. v1's Mermaid flowchart uses simple boxes
   (persons as stadiums, externals as subroutine boxes) without the full
   C4 visual vocabulary. A later pass could emit Mermaid `classDef`
   styling or switch the HTML view to Mermaid's C4 diagram type (once its
   experimental status settles) for shape fidelity.
