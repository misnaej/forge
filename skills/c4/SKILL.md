---
name: c4
description: Build or refresh a C4 architecture model for this repo — reason out the System Context, Containers, and Component groupings into c4.toml, then run forge-gen-c4 to emit Structurizr DSL. Use when the user wants an architecture diagram from their code.
---

# C4 Architecture Model

Produce a [C4 model](https://c4model.com/) for this repo as Structurizr DSL.
The work splits into a **reasoned** half (you) and a **deterministic** half
(`forge-gen-c4`). See `docs/proposals/c4-generator.md` for the full design.

- **You reason** about what the import graph cannot encode: the System
  Context (who uses the system, which external systems it talks to), the
  Containers (deployable units), and which modules form which named
  Component. You write these into the model file.
- **`forge-gen-c4` derives** the component-to-component dependency edges
  from the import graph and emits the DSL. It is deterministic — never
  hand-write the `.dsl`.

## Steps

1. **Orient.** Read `REPO_STRUCTURE.md`, `docs/api-digest.md`, and
   `code_health/audit_deps_tree.log` (run `forge-audit-deps --tree` if
   absent) to learn the module layout and import structure. For larger
   repos, delegate the read to the `Explore` agent rather than skimming.

2. **Reason out the model.** Decide:
   - **System + context** — one sentence on what the system *is*; the
     people/roles that use it; the external systems it integrates with.
   - **Containers** — the deployable units (a pip package, a service, a
     CLI, a database). Most small repos have one.
   - **Components** — group modules into a handful of cohesive,
     named components. Prefer package/directory boundaries; aim for
     5–10 components, not one-per-module. Every source module should
     fall under exactly one component prefix.

3. **Write the model file.** Create or update `c4.toml` at the repo root
   (top-level tables: `system`, `[[person]]`, `[[external]]`,
   `[[container]]`, `[components]`, `[[relationship]]`). Point
   `[tool.forge.c4].config = "c4.toml"` in `pyproject.toml` if not already.
   Add `[[relationship]]` entries for **runtime/subprocess edges the import
   graph can't see** (e.g. a dispatcher shelling out to workers).

4. **Generate + review coverage.** Run `forge-gen-c4`. It warns about any
   module matching no component prefix — fix the groupings until coverage
   is complete (or deliberately exclude with a documented reason). Inspect
   the emitted `docs/architecture.dsl`.

5. **Render (optional, the user's choice).** The DSL renders in
   [Structurizr Lite](https://docs.structurizr.com/lite) (MIT) or via the
   Structurizr CLI (which also exports PlantUML/Mermaid). Forge emits the
   text and renders nothing itself.

## Rules

- **Never hand-edit `docs/architecture.dsl`** — it is generated; edit
  `c4.toml` and regenerate. `forge-gen-c4 --check` enforces drift.
- **Component groupings are a judgement call** — propose a sensible
  default from the package structure, but surface it for the user to
  confirm; a bad grouping yields a misleading diagram.
- Keep the model honest: if a relationship is real but invisible to the
  import graph, declare it in `[[relationship]]`; don't omit it.
