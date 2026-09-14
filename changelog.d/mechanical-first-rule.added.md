bump: minor
- **FOUNDATION §7 "Mechanical first"** — a design principle every consumer inherits: each step of new work is classified as mechanical or judgment, and a mechanical step becomes a CLI (preferably a subcommand of an existing one) instead of agent instructions. A CLI that performs a guarded effect must enforce that guard itself, because hooks never see its internal calls.
- **Plans name the classification**: FOUNDATION §1's plan contract and `/plan-issue` (interactive and draft-only) now list which planned steps are mechanical and which need judgment.
- **Review lens, never a gate**: `forge:design-checker` gains a mechanical-first lens for diffs that add or change agents, skills or skill steps.
