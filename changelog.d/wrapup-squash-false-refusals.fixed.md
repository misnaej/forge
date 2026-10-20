bump: patch

- `forge-pr-wrapup post` no longer refuses a wrap-up because a CI job's name contains an AI vendor word (`claude-review`): the CI Status line forge writes is vetted on its own, and a credit phrase there is still refused.
- CI job names in the wrap-up drop invisible Unicode format characters (bidi overrides, zero-width spaces) before they are shown.
- A light-code wrap-up's Recommendation is one short line; `compose` prints the classifier's full reasons instead of spending the word budget on them.
- Issue Management lists only issues the PR body closes; commit messages are used, marked provisional, only before the PR exists.
- `forge-pr-squash-comment` refuses a bullet containing a line break, which could close the squash message's code fence.
