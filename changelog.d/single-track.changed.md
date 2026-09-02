bump: patch
- **forge is single-track now** — the dual-track dev/main model is retired: every PR targets `main`, every merge tags, and the `@dev` channel is deprecated (pin `@main`, or a tag for stability). Promotion machinery self-skips (`dev_branch == base_branch`) and will be deleted in a follow-up major.
