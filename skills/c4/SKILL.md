---
name: c4
description: Build or refresh a C4 architecture model for this repo — interactively interview the user to define the System Context, Containers, and Components (the parts not in the code), write c4.toml, then run forge-gen-c4. Use when the user wants an architecture diagram from their code.
---

# C4 Architecture Model

Produce a C4 model as Structurizr DSL (+ an offline HTML or raw Mermaid view) via
`forge-gen-c4`. The work splits in two: a **machine-derived** half (the CLI
reads the import graph for component-to-component edges) and a
**human-declared** half (everything the code does *not* encode). This skill
owns the human half — and for a repo you have not modeled before, that means
**interviewing the user**, not guessing.

## Core principle: ask about what the code cannot tell you

An import graph shows how modules reference each other. It does **not** reveal:

- **Who/what uses the system** — human roles, other teams, upstream systems.
- **External systems** — databases, third-party APIs, queues, object stores,
  auth providers. An `import requests` does not say *which* service or *why*.
- **Containers** — the deployable/runtime units. One repo can be a CLI **plus**
  a web service **plus** a worker **plus** a DB; code structure alone cannot
  partition them.
- **Runtime edges** — subprocess calls, HTTP/RPC, message passing, a shared
  database. All invisible to a static import graph.
- **Component boundaries + intent** — which modules form one meaningful
  component, and what each is *for*.

So: **derive what you can, then interview the user for the rest.** When unsure,
ask — a wrong guess produces a confidently misleading diagram.

## Steps

1. **Detect mode.** If `c4.toml` (or `[tool.forge.c4]`) already exists → this is
   a **refresh**: read it, re-derive, and ask only about gaps or changes. If
   absent → this is a **first adoption**: run the full interview below.

2. **Orient (machine side).** Read `REPO_STRUCTURE.md`, `docs/api-digest.md`,
   and `code_health/audit_deps_tree.log` (run `forge-audit-deps --tree` if
   absent). For large repos, delegate the read to the `Explore` agent. Form a
   *draft* component grouping from package/directory boundaries — a proposal to
   react to, not the answer.

3. **Interview (human side).** Ask in focused rounds — use `AskUserQuestion`
   for structured choices, open questions where the answer is free-form. Cover:
   - **System** — one sentence: what is this system, in plain terms?
   - **People / actors** — who uses it (end users, developers, operators, other
     teams)? For each: a one-line role + what they do with it.
   - **External systems** — what does it integrate with? Probe explicitly:
     "Does it talk to a database? a third-party API? GitHub? a message queue?
     cloud storage? an auth provider?" The code will not reliably show these.
   - **Containers** — "Is this one deployable unit, or several?" Offer
     candidates (CLI, service, worker, DB, frontend). Confirm each container's
     name + technology.
   - **Components** — present your draft grouping; ask the user to confirm /
     rename / merge / split. Get a one-line **description** and a **technology**
     per component (C4 wants meaningful boxes, not bare names). When there is
     more than one container, ask **which container each component belongs to**
     (set `container = "<container name>"` on the component; omit it to default
     to the first container).
   - **Runtime / subprocess edges** — "Do any parts call each other at runtime
     in a way that is not a Python import — shelling out, HTTP, a queue, a
     shared DB?" These become `[[relationship]]` entries.

4. **Confirm coverage before writing.** Ensure every source module falls under
   exactly one component prefix; flag leftovers and ask where they belong (or
   whether to exclude them, with a documented reason).

5. **Write `c4.toml`** at the repo root from the answers (top-level tables:
   `system`, `[[person]]`, `[[external]]`, `[[container]]`, rich `[[component]]`
   with `name` / `description` / `technology` / `modules` / optional
   `container`, and `[[relationship]]`). A root `c4.toml` is auto-detected; set
   `[tool.forge.c4].config = "<path>"` only if you put the model elsewhere.
   To embed the diagram in the README, add the
   `<!-- forge:c4:start -->` / `<!-- forge:c4:end -->` markers where it should
   appear and set `readme = "README.md"`.

6. **Generate, view, iterate.** Run `forge-gen-c4` (fix any unmatched-module
   warning), then `forge-gen-c4 --format html && open docs/architecture.html`
   to view. Show the user and loop back to step 3 until the model reflects
   reality.

## Rules

- **Never invent context, external systems, or containers from the code alone**
  — those are the user's to confirm. Ask.
- **Never hand-edit `docs/architecture.dsl`** — it is generated; edit `c4.toml`
  and regenerate (`forge-gen-c4 --check` enforces drift, and the `c4`
  pre-commit step fails on it).
- Component groupings and runtime edges are judgement calls — propose, then
  confirm with the user.
- Keep the model honest: a real relationship invisible to the import graph
  belongs in `[[relationship]]`; do not omit it.
