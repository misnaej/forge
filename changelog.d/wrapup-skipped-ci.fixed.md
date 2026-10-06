bump: patch

The PR wrap-up no longer reports "✅ passed" when no CI actually ran. When every check was skipped — typically a workflow that skips draft PRs — the CI Status line now reads "⚪ CI not run — N skipped" (naming the draft when the PR is one), partly skipped runs show how many checks actually ran, and `forge-pr-wrapup post` prints a note that local verification is the only evidence.
