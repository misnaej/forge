bump: patch

- FOUNDATION §6 now names the steps that pause for the user (merging, force operations, cutting a release or publishing a package); commit, push and opening the PR do not.
- FOUNDATION §6: a PR monitor that sees `fresh: null` checks again on its next poll instead of treating the wrap-up staleness signal as off; the first poll after finalization is expected to be null.
- `docs/consumer-release.md` says what one-fragment-per-PR means: several bullets in one file, the type names the whole PR, the strongest bump wins, the slug names the change, and requiring a fragment at all is the separate `changelog_updated` step.
- Corrected stale docs: the audit pack's sub-audit list, the README's pip_audit row, the CHANGELOG preamble's group names, and the CI recipe's retired `dev` branch.
