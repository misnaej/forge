bump: minor

- `/report-to-forge` ends every issue it files with a filing marker (`<!-- forge:report-to-forge vX.Y.Z -->`, the installed forge version). Forge's own triage uses it to tell reports filed through forge from issues opened by hand: an outside issue without it is labelled `needs-forge-report` and answered by a maintainer.
