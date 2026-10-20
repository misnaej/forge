bump: patch

- The git guards (reset, clean, destructive checkout and restore, stash, force push, rebase, and the rest of the family) now see git launched through a wrapper option that takes its own value — `sudo -u root git clean -fd`, `env -C /tmp git reset --hard`, `nice -n 5 …` — which they previously let through unchecked. The same goes for the install, PR-merge, ruff and continuation-file guards (`sudo -u x pip install`, `sudo -u x gh pr merge`, `sudo -u root rm .plan/CONTINUATION.md`).
- More wrappers are recognised: `timeout 5`, `setsid`, `ionice`, `stdbuf`, `taskset`, `chrt`, `doas` and `xargs`, plus the command `find … -exec` / `-execdir` / `-ok` / `-okdir` runs and the command an `env -S '…'` string runs.
- A variable standing in for git (`$G reset --hard`, `$G push -f`) is now refused by the destructive-recovery and force-push guards, since what it runs cannot be checked; spell `git` literally.
- Accepted cost: a value-less wrapper option followed by another program that runs git (`sudo -E somecmd git reset --hard`) is now read as the wrapper running git and blocked.
