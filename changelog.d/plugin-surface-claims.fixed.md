bump: patch
- The continuation-note budget check now also reports after the note is rewritten by a shell command, not only by the Write and Edit tools. Registered for those two alone, it stayed silent in sessions whose file edits go through the shell.
- FOUNDATION's agent roster named thirteen foundation agents and omitted `forge:token-optimizer`, which shipped in the previous release. The Vibe artifact table hard-coded the same stale count; it no longer names a number.
- The agent template and the repo map both claimed the template's underscore prefix keeps it out of Claude Code's plugin discovery. It does not: `agents/_TEMPLATE.md` is listed as a spawnable agent, and the prefix governs forge's own tooling only. Nothing routes to it because it has no frontmatter.
- Removed seven redundant lint suppressions from the docstring and test-naming verifiers. No behaviour change.
- A hook group repeating another group's matcher within one event no longer silently loses its hooks in the Vibe roster: the roster merges them and warns, and `verify-forge-manifest` (the `manifest_json` step) now rejects the repeat in `plugin.json`, naming the event and matcher.
