bump: minor

- FOUNDATION.md rewritten terse — about 22% shorter (80k → 62k characters, 12.0k → 8.7k words) with every rule, number, hook and CLI name kept and every section heading unchanged, so existing links and citations still resolve. Policy stays; step-by-step procedure moved to pages agents open on demand:
  - New `forge-docs/git-safety.md` — gate kinds, guard-bypass mechanics, the destructive-git forms (the `block_git_destructive` hook stays the authority) and the dirty-tree sync ladder.
  - New `forge-docs/plugin-staleness.md` — symptoms, restart as the terminal step, the global-install shadow and the `forge-doctor` lines. Both pages ship to consumers through `install-forge-claude-md`.
  - `/plan-issue` Step 1 now holds the plannability checks (the permission call, `lastEditedAt`, fail-closed); `/pr` Step 4 holds the squash-message mechanics.
- New FOUNDATION §8 rule **Write terse**: prose drops articles, filler and hedging, keeping names, numbers and commands exact; a dependency pinned for an advisory is commented with the advisory id only; config comments and file headers take one or two lines. `forge:docs-types-checker` flags violations as warnings.
- A blocking test keeps FOUNDATION.md from growing past its current word count; the baseline only goes down.
