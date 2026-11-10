bump: minor

- `forge-commit --wip-sync -m "wip-sync: <what>"` makes the sync ladder's checkpoint commit: it stages the whole working tree, untracked files included, and commits with the pre-commit gate deferred to the next real commit. Agents can now secure dirty work before merging the base branch in; before, the only documented form was a raw `git commit` the agent guards refuse. FOUNDATION §2 and `forge-docs/git-safety.md` point to it.
