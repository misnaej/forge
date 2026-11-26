bump: minor

- A changelog bullet past 24 words now refuses the commit.
- The 12-word ceiling still advises, and names the longest bullet.
- The staged copy of a fragment is judged, not the disk copy.
- Fragments mode now honours `[tool.forge.changelog].blocking`.
- Squash-merge bullets are written terse, not prose.
- Only the fragment a branch adds is judged for length.
