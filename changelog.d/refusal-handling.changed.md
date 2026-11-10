bump: minor

- FOUNDATION §2: a refusal from the host's own permission layer (such as Claude Code's auto-mode classifier) is not a forge guard. Agents retry it once, or its suggested safer form; if it is still refused they quote it and ask, and never prescribe a `!` command or a settings change for it. `forge-docs/git-safety.md` documents it next to the two gate kinds and notes the list is not exhaustive.
- FOUNDATION §2: once the user says yes to an issue-state change, the agent runs the `gh` command itself.
- FOUNDATION §11 and the agent template: an agent's report states what it did, found and was refused — never who may act, what the user should run or which setting should change — and the parent tests such claims instead of relaying them.
- The force-push divergence check and the branch-protection check moved from FOUNDATION §2 to `forge-docs/git-safety.md`, unchanged.
