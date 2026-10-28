# Plugin staleness

How a running session ends up on an old forge plugin, how to spot it, how to recover. Policy: [FOUNDATION §11 "Plugin staleness — symptoms and recovery"](../FOUNDATION.md#11-agent-boundary-protocol).

## Loud symptom: missing agent

Release renames or adds an agent → running session keeps the **cached**
plugin from startup.

- Symptom: `Agent type 'forge:<name>' not found` though the agent is on disk —
  this checkout's install is behind its release.
- Recovery: `forge-plugin-sync` (post-merge / post-checkout hooks usually ran
  it already), then **restart the session**. Reload does not reliably rebind
  skills or monitors.
- `check_upstream` warning (`install-forge-claude-md` + `post-merge` /
  `post-checkout` / `SessionStart` hooks) surfaces version lag automatically.
- This recovery is for the loud case only. "Restart once something looks
  stale" does not generalise: usual case gives nothing to notice (next).

## Quiet symptom: stale contract (worse, far more common)

Stale slot usually holds a skill or agent that still works but encodes an
older contract. Session follows instructions correct a release ago. Nothing
errors.

Observed case: session ran a `/pr` skill predating a command rename, kept
invoking the raw form its own installed hook had begun refusing, while every
version check reported green.

## Restart is the terminal step

Any upgrade that moved the plugin ends in a restart; an agent that moved it
stops there (FOUNDATION §2 "On deviation: STOP and report", for a deviation
the agent caused). Agent must:

1. Report the upgrade.
2. Say session still runs the version it replaced.
3. Ask for a restart.
4. Say anything already produced this session ran under the *previous* guard
   set — a command a newer hook would refuse may already have succeeded.
   Worth a look, not a re-audit.

`/reload-plugins` is not a substitute: it reports whether a skill is
*present*, not *current* — its `0 skills` count is a delta for that
pass, and a slot several releases old resolves the same way.

## No check confirms the restart

Forge reads which slot is *installed*; nothing exposes which slot a running
session *loaded*. Every version check stays green across exactly this
failure.

- Green check = evidence about the filesystem, never the session
  (FOUNDATION §1, absence of evidence).
- Do not compare plugin vs package version numbers: that check was built,
  shipped and removed once already — the command it named truthfully answered
  that nothing was out of date.

## Wrong copy: global install shadows the checkout

Symptom: plugin loads fine, behaves like an old release, every update says
current; restart changes nothing.

- Cause: **global (user-scope) install** beside the checkout's own, shadowing
  it.
- Recovery: `claude plugin uninstall forge@forge --scope user`.
  `forge-plugin-sync` refuses to install until it is gone.
- Not a cause: shared machine-wide marketplace registration (once let one
  repo's release overwrite another's). `forge-plugin-sync` installs from a
  marketplace named after the release (`forge-vX-Y-Z`), so each release has
  its own.

## Where staleness shows

`forge-doctor` reports on this checkout's own install, never a slot shared
between repos:

- `plugin:installed` (with `plugin.json`, `marketplace.json` and `plugin/…`
  lines under it) — this checkout's `forge@forge-vX-Y-Z` record. None → hard
  failure naming `forge-plugin-sync`.
- `plugin:checkout` — whether that record is the release the checkout names.
- `plugin:global_install` — a user-scope forge, reported beside them.

`plugin_sync` pre-commit step (advisory by default) judges the same thing as
`plugin:checkout`. Both name `forge-plugin-sync (then restart the session)` as
the remedy.
