bump: patch

- **A changelog fragment counts before it is staged.** In fragments mode the `changelog_updated` check only saw files git already tracks, so a fragment that was written and valid still failed the check until someone ran `git add`. An untracked fragment under `changelog.d/` now counts; one in a gitignored path still does not, and an invalid one still fails.
- **A failed pre-commit verdict now says why on its last line.** `forge-precommit --verdict` ended with a bare `verdict: FAIL`, leaving the reader to scan dozens of step rows for the cause — and a harmless WARN was blamed more than once. The line now reads `verdict: FAIL — evidence describes another tree; …` when the logs describe an older tree, or names the failing and missing steps.
- **The pre-commit fixer agent is told the order that works:** its own edits after a full run make every log stale, a narrow re-run never restores the verdict, so it finishes all edits, then runs the full pass once, then reads the verdict.
