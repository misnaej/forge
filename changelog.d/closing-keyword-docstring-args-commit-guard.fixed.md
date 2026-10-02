bump: patch

- **"Closes #123." now counts as a closing line.** The PR wrap-up looks for lines like "Closes #123" to report which issues a merge will close; a line ending in a full stop, comma or semicolon was ignored, so the wrap-up wrongly said nothing would be closed.
- **The docstring checker no longer cuts the Args list short.** A parameter named like a section header — `notes`, `returns`, `example`, … — ended the Args list early, so it and every parameter after it were reported as undocumented; and entries of sections it did not know (`Warning:`, `See Also:`) were mistaken for parameters. The Args list now ends at the next unindented line.
- **The commit agent can no longer land commits that skip pre-commit.** Its exemption from the raw-git guard now covers only `git commit` / `git push`; `git revert` and `git cherry-pick`, which create commits without running the pre-commit hook, are refused for it like for everyone, and a missing guard library now blocks it too.
- Release PR bodies no longer call a hand-opened release "scheduled".
