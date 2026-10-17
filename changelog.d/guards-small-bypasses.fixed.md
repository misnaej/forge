bump: patch

- `block_continuation_delete` sees `rm` or a wrapper launched by path (`/bin/rm .plan/CONTINUATION.md`, `/usr/bin/env rm …`).
- `block_raw_git` no longer reads python's `-I` (isolated mode) as taking a value, so `python -I -c '…git…'` is refused; php's `-r` is read as the inline code it is.
- `block_pr_merge` refuses a GraphQL merge through `gh api graphql` (`mergePullRequest`, `enablePullRequestAutoMerge`, `enqueuePullRequest`).
- `block_git_destructive` no longer refuses a checkout that creates a branch after a `cd` (`cd x && git checkout -q -b feat`); a checkout or restore of paths there still fails closed.
