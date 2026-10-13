bump: patch

- **gh commands are now caught by the guards however their options are placed, not only when the subcommand follows `gh` directly.** gh accepts options before the subcommand and between a command group and its action, so `gh -X DELETE api …/refs/heads/main` slipped past the protected-branch delete guard, and `gh -X PUT api …/pulls/9/merge` or `gh pr -R owner/name merge 9` slipped past the merge guard. Every gh guard now looks past such options, and the squash-comment re-post no longer fires for a comment posted to a different repository with `-R`/`--repo`.
