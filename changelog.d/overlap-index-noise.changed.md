bump: minor
- `forge-plan-check overlap` flags only open issues with a validated plan; issues without one that name a changed file are counted in one `note:` line instead of each being flagged for re-planning.
- The Backlog Index lists each issue once: an issue in Plan-Ready, Needs Recheck, Needs Endorsement or Blocked / Waiting leaves its tier section and shows its tier on the status line. Needs Recheck lists exactly the issues labelled `needs-recheck`, so the same backlog always renders the same size, and the line standing in for dropped entries now says "trimmed".
- `forge-backlog-index --recheck` is removed; a script passing it must drop the flag.
