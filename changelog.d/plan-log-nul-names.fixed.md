bump: patch

- `forge-plan-check drift`, `overlap` and `current` now read merge history with exact file names, so a merge that changed a file whose name holds non-ASCII characters (`docs/café.md`) is reported instead of silently reading as "nothing changed".
