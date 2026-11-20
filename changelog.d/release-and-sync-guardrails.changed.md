bump: minor

- A merge into `main` no longer loses its release tag when another merge
  follows it closely. CI runs for `main` are kept separate per commit, so a
  later merge can neither cancel nor displace an earlier one.
- Syncing a branch that holds no commits of its own now fast-forwards it
  instead of recording an empty merge, so such a branch no longer reports a
  commit that contains nothing.
- A release marked as a major now has to carry upgrade notes. The check
  names the rename needed when it does not.
- Upgrading across a major release that documents no upgrade notes now says
  so, rather than printing nothing and leaving silence to be read as
  "nothing to do".
- Finalizing a pull request now states the word allowance as an upper bound
  and explains when it is lower, so the figure given while writing matches
  the one the finished text is measured against.
- Investigating a small defect found in passing is now part of the rule for
  handling it: it is sized before being recorded, so what one read could
  close is fixed rather than filed.
