bump: minor

- A merge into `main` no longer loses its release tag when another merge
  follows it closely. CI runs for `main` are kept separate per commit, so a
  later merge can neither cancel nor displace an earlier one.
- Background PR monitors now reach you. What a monitor saw is reported by
  `forge-pr-watch digest`, which the task-start flow runs, so a pull request
  that conflicted, failed its checks or merged while no session was open is
  no longer recorded only in a file nothing opened.
- A monitor repeats itself no longer: a condition that stays true is
  reported once per revision rather than once a minute, and a monitor stops
  when its pull request is merged or closed, naming the cleanup then owed.
- New comments on a watched pull request are now reported. They were named
  as a watched signal but never actually polled.
- Starting a monitor without naming a pull request works again; it had been
  querying for one in a way that could never match and always gave up.
- The end-of-review report now opens each point with its outcome — shipped,
  fixed, verified, deferred or blocked — so what landed and what is still
  owed are clear at a glance instead of being read out of paragraphs.
- Syncing a branch that holds no commits of its own now fast-forwards it
  instead of recording an empty merge, so such a branch no longer reports a
  commit that contains nothing.
- A release marked as a major now has to carry upgrade notes, and the check
  names the rename needed when it does not. Upgrading across a major that
  documents none now says so, rather than printing nothing and leaving
  silence to be read as "nothing to do".
- Finalizing a pull request states the word allowance as an upper bound and
  explains when it is lower, so the figure given while writing matches the
  one the finished text is measured against.
- Asking the user a question no longer stops the work when the answer was
  not needed to continue, and a question is now written for someone who has
  not read the code it concerns. Investigating a small defect found in
  passing is part of the rule for handling it: it is sized before being
  recorded, so what one read could close is fixed rather than filed.
