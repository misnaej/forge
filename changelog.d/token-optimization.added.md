bump: minor
- New shipped doc `forge-docs/token-optimization.md`: provider-declared prompt-cache pricing (Anthropic and Mistral, with source URLs), cache-busting causes, how to measure with `forge-agent-profile --tokens`, and ranked cost levers
- New agent `token-optimizer`: reports ranked billed-token drains (fresh vs cache-read split, per-harness cache%, waste flags) with concrete actions, per the new doc
