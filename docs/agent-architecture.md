# Forge agent architecture

> **Maintained by hand, guarded by a check.** Nodes (agents / skills / hooks /
> CLIs) are discovered from the repo; edges, phases, and enforcer badges are
> hand-curated and source-verified. Two layers keep it honest: the `agent_doc`
> pre-commit step gates node **coverage + no dangling refs** on every commit,
> and at PR review `docs-types-checker` runs `verify-forge-agent-doc --diff
> <base>` to surface the **edge** changes a PR made so they get reconciled
> here. Still eyeball it when `agents/`, `skills/`, or `claude-hooks/` change.

How forge's AI **subagents** interact with the **skills** that invoke them,
the **hooks** that guard them, and the deterministic **CLIs** they drive —
all orchestrated by the **main agent** the developer talks to. The fleet is
sliced into workflow-phase subviews so each stays readable.

> Auto-derived nodes (agents/skills/hooks/CLIs discovered from the repo) +
> hand-curated, source-verified edges. Regenerate alongside the README's link.

## How to read these diagrams

- **Node kind (colour):** 🟨 person · 🟦 main agent · 🟪 AI agent · 🟧 skill ·
  🟥 hook · 🟩 CLI · ⬜ policy (FOUNDATION §)
- **Agent border:** *dashed* = **reporter** (read-only, no `Write`/`Edit`) ·
  *thick* = **mutator** (writes files or git/GitHub state)
- **Badge:** ⚖️ = **FOUNDATION-enforcer** — an agent whose primary job is to
  *verify or remediate* compliance with a rule (not merely follow it)
- **Edge:** `──▶` solid = a wired call / delegation ·
  `╌╌▶` dotted = a **FOUNDATION guideline** the main agent is told to follow
  (e.g. §3 "review before editing") but which **no skill actually wires**

## ⚖️ FOUNDATION-enforcers (cross-cutting)

The agents whose job is compliance, and the rules they enforce.

```mermaid
graph LR
  classDef person fill:#fef9c3,stroke:#ca8a04,color:#713f12
  classDef agent fill:#ede9fe,stroke:#7c3aed,color:#4c1d95
  classDef skill fill:#ffedd5,stroke:#ea580c,color:#7c2d12
  classDef hook fill:#fee2e2,stroke:#dc2626,color:#7f1d1d
  classDef cli fill:#dcfce7,stroke:#15803d,color:#14532d
  classDef policy fill:#f1f5f9,stroke:#64748b,color:#334155
  classDef orchestrator fill:#c7d2fe,stroke:#4338ca,color:#1e1b4b,stroke-width:2px
  classDef reporter stroke-dasharray:5 3
  classDef mutator stroke-width:3px
  design_checker["⚖️ design-checker<br/>AI agent"]
  docs_types_checker["⚖️ docs-types-checker<br/>AI agent"]
  issue_triage["⚖️ issue-triage<br/>AI agent"]
  precommit_fixer["⚖️ precommit-fixer<br/>AI agent"]
  security_checker["⚖️ security-checker<br/>AI agent"]
  test_advisor["⚖️ test-advisor<br/>AI agent"]
  docs_security_md(["docs/security.md<br/>[policy]"])
  _14_issue_schema(["§14 issue schema<br/>[policy]"])
  _4_pre_commit_gate(["§4 pre-commit gate<br/>[policy]"])
  _5_ruff(["§5 ruff<br/>[policy]"])
  _5_ruff___complexity(["§5 ruff & complexity<br/>[policy]"])
  _7_design_principles(["§7 design principles<br/>[policy]"])
  _8_docs_standards(["§8 docs standards<br/>[policy]"])
  _8_testing_standards(["§8 testing standards<br/>[policy]"])
  design_checker -->|enforces| _7_design_principles
  design_checker -->|enforces| _5_ruff___complexity
  precommit_fixer -->|enforces| _4_pre_commit_gate
  precommit_fixer -->|enforces| _5_ruff
  docs_types_checker -->|enforces| _8_docs_standards
  test_advisor -->|enforces| _8_testing_standards
  security_checker -->|enforces| docs_security_md
  issue_triage -->|enforces| _14_issue_schema
  class design_checker agent
  class design_checker reporter
  class docs_types_checker agent
  class docs_types_checker mutator
  class issue_triage agent
  class issue_triage mutator
  class precommit_fixer agent
  class precommit_fixer mutator
  class security_checker agent
  class security_checker reporter
  class test_advisor agent
  class test_advisor reporter
  class docs_security_md policy
  class _14_issue_schema policy
  class _4_pre_commit_gate policy
  class _5_ruff policy
  class _5_ruff___complexity policy
  class _7_design_principles policy
  class _8_docs_standards policy
  class _8_testing_standards policy
```

## Design & Development

```mermaid
graph LR
  classDef person fill:#fef9c3,stroke:#ca8a04,color:#713f12
  classDef agent fill:#ede9fe,stroke:#7c3aed,color:#4c1d95
  classDef skill fill:#ffedd5,stroke:#ea580c,color:#7c2d12
  classDef hook fill:#fee2e2,stroke:#dc2626,color:#7f1d1d
  classDef cli fill:#dcfce7,stroke:#15803d,color:#14532d
  classDef policy fill:#f1f5f9,stroke:#64748b,color:#334155
  classDef orchestrator fill:#c7d2fe,stroke:#4338ca,color:#1e1b4b,stroke-width:2px
  classDef reporter stroke-dasharray:5 3
  classDef mutator stroke-width:3px
  human(["Developer<br/>person"])
  main_agent{{"Main agent<br/>orchestrator"}}
  human -->|drives| main_agent
  hk_block_force_push[/"block_force_push<br/>hook"/]
  hk_block_no_verify[/"block_no_verify<br/>hook"/]
  sk_commit(["/commit<br/>skill"])
  design_checker["⚖️ design-checker<br/>AI agent"]
  docs_types_checker["⚖️ docs-types-checker<br/>AI agent"]
  sk_fix(["/fix<br/>skill"])
  cli_forge_continuation_append[("forge-continuation-append<br/>CLI")]
  cli_forge_precommit[("forge-precommit<br/>CLI")]
  cli_forge_smart_test[("forge-smart-test<br/>CLI")]
  git_commit_push["git-commit-push<br/>AI agent"]
  knowledge_search["knowledge-search<br/>AI agent"]
  perf_optimizer["perf-optimizer<br/>AI agent"]
  precommit_fixer["⚖️ precommit-fixer<br/>AI agent"]
  sk_smart_test(["/smart-test<br/>skill"])
  sk_test(["/test<br/>skill"])
  test_advisor["⚖️ test-advisor<br/>AI agent"]
  test_writer["test-writer<br/>AI agent"]
  main_agent -->|runs| sk_commit
  main_agent -->|runs| sk_fix
  main_agent -->|runs| sk_smart_test
  main_agent -->|runs| sk_test
  main_agent -. §3 pre-write review .-> design_checker
  precommit_fixer -->|delegates| docs_types_checker
  precommit_fixer -->|delegates| design_checker
  design_checker -->|delegates| knowledge_search
  perf_optimizer -->|delegates| design_checker
  sk_commit -->|invokes| precommit_fixer
  sk_commit -->|invokes| git_commit_push
  sk_fix -->|invokes| precommit_fixer
  sk_test -->|invokes| test_advisor
  sk_test -->|invokes| test_writer
  sk_test -->|invokes| precommit_fixer
  sk_test -->|chains| sk_commit
  precommit_fixer -->|invokes| cli_forge_precommit
  git_commit_push -->|invokes| cli_forge_continuation_append
  sk_smart_test -->|invokes| cli_forge_smart_test
  git_commit_push -.->|guarded by| hk_block_no_verify
  git_commit_push -.->|guarded by| hk_block_force_push
  class human person
  class main_agent orchestrator
  class hk_block_force_push hook
  class hk_block_no_verify hook
  class sk_commit skill
  class design_checker agent
  class design_checker reporter
  class docs_types_checker agent
  class docs_types_checker mutator
  class sk_fix skill
  class cli_forge_continuation_append cli
  class cli_forge_precommit cli
  class cli_forge_smart_test cli
  class git_commit_push agent
  class git_commit_push mutator
  class knowledge_search agent
  class knowledge_search reporter
  class perf_optimizer agent
  class perf_optimizer mutator
  class precommit_fixer agent
  class precommit_fixer mutator
  class sk_smart_test skill
  class sk_test skill
  class test_advisor agent
  class test_advisor reporter
  class test_writer agent
  class test_writer mutator
```

## Review

```mermaid
graph LR
  classDef person fill:#fef9c3,stroke:#ca8a04,color:#713f12
  classDef agent fill:#ede9fe,stroke:#7c3aed,color:#4c1d95
  classDef skill fill:#ffedd5,stroke:#ea580c,color:#7c2d12
  classDef hook fill:#fee2e2,stroke:#dc2626,color:#7f1d1d
  classDef cli fill:#dcfce7,stroke:#15803d,color:#14532d
  classDef policy fill:#f1f5f9,stroke:#64748b,color:#334155
  classDef orchestrator fill:#c7d2fe,stroke:#4338ca,color:#1e1b4b,stroke-width:2px
  classDef reporter stroke-dasharray:5 3
  classDef mutator stroke-width:3px
  human(["Developer<br/>person"])
  main_agent{{"Main agent<br/>orchestrator"}}
  human -->|drives| main_agent
  hk_block_pr_merge[/"block_pr_merge<br/>hook"/]
  hk_block_unverified_pr_create[/"block_unverified_pr_create<br/>hook"/]
  design_checker["⚖️ design-checker<br/>AI agent"]
  docs_types_checker["⚖️ docs-types-checker<br/>AI agent"]
  cli_forge_continuation_append[("forge-continuation-append<br/>CLI")]
  cli_forge_pr_squash_comment[("forge-pr-squash-comment<br/>CLI")]
  sk_pr(["/pr<br/>skill"])
  pr_manager["pr-manager<br/>AI agent"]
  precommit_fixer["⚖️ precommit-fixer<br/>AI agent"]
  sk_pr_comments(["/pr-comments<br/>skill"])
  security_checker["⚖️ security-checker<br/>AI agent"]
  main_agent -->|runs| sk_pr
  main_agent -->|runs| sk_pr_comments
  pr_manager -->|delegates| design_checker
  pr_manager -->|delegates| security_checker
  pr_manager -->|delegates| docs_types_checker
  pr_manager -->|delegates| precommit_fixer
  precommit_fixer -->|delegates| docs_types_checker
  precommit_fixer -->|delegates| design_checker
  sk_pr -->|invokes| design_checker
  sk_pr -->|invokes| security_checker
  sk_pr -->|invokes| docs_types_checker
  sk_pr -->|invokes| precommit_fixer
  sk_pr -->|invokes| pr_manager
  sk_pr_comments -->|invokes| pr_manager
  pr_manager -->|invokes| cli_forge_pr_squash_comment
  pr_manager -->|invokes| cli_forge_continuation_append
  pr_manager -.->|guarded by| hk_block_pr_merge
  pr_manager -.->|guarded by| hk_block_unverified_pr_create
  sk_pr -.->|guarded by| hk_block_unverified_pr_create
  class human person
  class main_agent orchestrator
  class hk_block_pr_merge hook
  class hk_block_unverified_pr_create hook
  class design_checker agent
  class design_checker reporter
  class docs_types_checker agent
  class docs_types_checker mutator
  class cli_forge_continuation_append cli
  class cli_forge_pr_squash_comment cli
  class sk_pr skill
  class pr_manager agent
  class pr_manager mutator
  class precommit_fixer agent
  class precommit_fixer mutator
  class sk_pr_comments skill
  class security_checker agent
  class security_checker reporter
```

## Release

```mermaid
graph LR
  classDef person fill:#fef9c3,stroke:#ca8a04,color:#713f12
  classDef agent fill:#ede9fe,stroke:#7c3aed,color:#4c1d95
  classDef skill fill:#ffedd5,stroke:#ea580c,color:#7c2d12
  classDef hook fill:#fee2e2,stroke:#dc2626,color:#7f1d1d
  classDef cli fill:#dcfce7,stroke:#15803d,color:#14532d
  classDef policy fill:#f1f5f9,stroke:#64748b,color:#334155
  classDef orchestrator fill:#c7d2fe,stroke:#4338ca,color:#1e1b4b,stroke-width:2px
  classDef reporter stroke-dasharray:5 3
  classDef mutator stroke-width:3px
  human(["Developer<br/>person"])
  main_agent{{"Main agent<br/>orchestrator"}}
  human -->|drives| main_agent
  cli_forge_check_main_tags[("forge-check-main-tags<br/>CLI")]
  cli_forge_next_prep[("forge-next-prep<br/>CLI")]
  sk_next(["/next<br/>skill"])
  sk_promote(["/promote<br/>skill"])
  main_agent -->|runs| sk_next
  main_agent -->|runs| sk_promote
  sk_next -->|chains| sk_promote
  sk_next -->|invokes| cli_forge_next_prep
  sk_next -->|invokes| cli_forge_check_main_tags
  class human person
  class main_agent orchestrator
  class cli_forge_check_main_tags cli
  class cli_forge_next_prep cli
  class sk_next skill
  class sk_promote skill
```

## Backlog

```mermaid
graph LR
  classDef person fill:#fef9c3,stroke:#ca8a04,color:#713f12
  classDef agent fill:#ede9fe,stroke:#7c3aed,color:#4c1d95
  classDef skill fill:#ffedd5,stroke:#ea580c,color:#7c2d12
  classDef hook fill:#fee2e2,stroke:#dc2626,color:#7f1d1d
  classDef cli fill:#dcfce7,stroke:#15803d,color:#14532d
  classDef policy fill:#f1f5f9,stroke:#64748b,color:#334155
  classDef orchestrator fill:#c7d2fe,stroke:#4338ca,color:#1e1b4b,stroke-width:2px
  classDef reporter stroke-dasharray:5 3
  classDef mutator stroke-width:3px
  human(["Developer<br/>person"])
  main_agent{{"Main agent<br/>orchestrator"}}
  human -->|drives| main_agent
  cli_install_forge_labels[("install-forge-labels<br/>CLI")]
  issue_triage["⚖️ issue-triage<br/>AI agent"]
  issue_triage_forge["⚖️ issue-triage-forge<br/>AI agent"]
  sk_next(["/next<br/>skill"])
  sk_triage(["/triage<br/>skill"])
  main_agent -->|runs| sk_next
  main_agent -->|runs| sk_triage
  issue_triage_forge -->|delegates| issue_triage
  sk_next -->|invokes| issue_triage
  sk_triage -->|invokes| issue_triage
  issue_triage -->|invokes| cli_install_forge_labels
  class human person
  class main_agent orchestrator
  class cli_install_forge_labels cli
  class issue_triage agent
  class issue_triage mutator
  class issue_triage_forge agent
  class issue_triage_forge mutator
  class sk_next skill
  class sk_triage skill
```

## Docs & Reporting

```mermaid
graph LR
  classDef person fill:#fef9c3,stroke:#ca8a04,color:#713f12
  classDef agent fill:#ede9fe,stroke:#7c3aed,color:#4c1d95
  classDef skill fill:#ffedd5,stroke:#ea580c,color:#7c2d12
  classDef hook fill:#fee2e2,stroke:#dc2626,color:#7f1d1d
  classDef cli fill:#dcfce7,stroke:#15803d,color:#14532d
  classDef policy fill:#f1f5f9,stroke:#64748b,color:#334155
  classDef orchestrator fill:#c7d2fe,stroke:#4338ca,color:#1e1b4b,stroke-width:2px
  classDef reporter stroke-dasharray:5 3
  classDef mutator stroke-width:3px
  human(["Developer<br/>person"])
  main_agent{{"Main agent<br/>orchestrator"}}
  human -->|drives| main_agent
  sk_c4(["/c4<br/>skill"])
  cli_forge_gen_c4[("forge-gen-c4<br/>CLI")]
  sk_weekly(["/weekly<br/>skill"])
  weekly_summary["weekly-summary<br/>AI agent"]
  main_agent -->|runs| sk_c4
  main_agent -->|runs| sk_weekly
  sk_weekly -->|invokes| weekly_summary
  sk_c4 -->|invokes| cli_forge_gen_c4
  class human person
  class main_agent orchestrator
  class sk_c4 skill
  class cli_forge_gen_c4 cli
  class sk_weekly skill
  class weekly_summary agent
  class weekly_summary mutator
```

## Maintenance

```mermaid
graph LR
  classDef person fill:#fef9c3,stroke:#ca8a04,color:#713f12
  classDef agent fill:#ede9fe,stroke:#7c3aed,color:#4c1d95
  classDef skill fill:#ffedd5,stroke:#ea580c,color:#7c2d12
  classDef hook fill:#fee2e2,stroke:#dc2626,color:#7f1d1d
  classDef cli fill:#dcfce7,stroke:#15803d,color:#14532d
  classDef policy fill:#f1f5f9,stroke:#64748b,color:#334155
  classDef orchestrator fill:#c7d2fe,stroke:#4338ca,color:#1e1b4b,stroke-width:2px
  classDef reporter stroke-dasharray:5 3
  classDef mutator stroke-width:3px
  human(["Developer<br/>person"])
  main_agent{{"Main agent<br/>orchestrator"}}
  human -->|drives| main_agent
  sk_memory_audit(["/memory-audit<br/>skill"])
  main_agent -->|runs| sk_memory_audit
  class human person
  class main_agent orchestrator
  class sk_memory_audit skill
```
