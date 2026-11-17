# Nothing detects a leftover plugin install after v14 retired it

- surface: forge-doctor
- release: forge 14.0.0
- seen: 2026-11-17
- branch: main

## Evidence

Doctor's integration check is described at `src/forge/doctor.py:10`
(`claude:integration`: compares generated files only, never plugin installs).

Session restarted on forge-scripts 14.0.0 in this repo. Generated
integration correct: 15 `forge-*` agents, 19 `forge-*` skills, 36 guard
scripts, 41 hook commands all `${CLAUDE_PROJECT_DIR}`-rooted, zero
plugin-rooted. Guards running are v14's.

Plugin still installed anyway, scope `project`, and still loaded:

    SessionStart hook: "forge plugin loaded: 13.0.0"
    installed_plugins.json: forge@forge -> cache/forge/forge/10.4.0
    live slot 10.4.0: agents=14 skills=16 hooks=30

Two versions named in the same breath (hook says 13.0.0, install record
says 10.4.0) — worth a look on its own.

`forge-doctor` exit 0, and its only relevant line:

    [✓] claude:integration  generated agents, skills, hooks and settings are current

True, and beside the point. Nothing in the report mentions a plugin.
14 cached plugin copies, 166 MB, also unmentioned.

## What it costs

v14's changelog says **"Action required: uninstall the old plugin once"**
and nothing checks whether anyone did. A consumer who skips it keeps a
second, stale fleet addressable under the old `forge:<name>` form — here
10.4.0, nearly four majors behind — while every report says the
integration is current, because by its own definition it is.

The names do not collide, which is the prefix working as designed, so
this is not a correctness bug. The cost is that the stale fleet stays
*reachable and unreported*: an agent or a person can still invoke
`forge:design-checker` and get four-major-old behavior with nothing
saying so. The retirement removed the three plugin checks that would have
been the natural place to notice.

Not urgent here — the user has decided to live with it for now, knowing
the above.

## What would fix it

Doctor already knows how to read Claude Code's install records; the
retired checks did exactly that. One advisory line when an install of
the retired plugin is recorded for this checkout or the user scope:
name it, name the version, name the uninstall command. Advisory, not
blocking — it disarms nothing, and the guards come from the repo now.

Open question for whoever picks this up: whether the same line should
report the stale cache (14 copies, 166 MB), or whether that belongs in
whatever already reaps session clutter. `/forge-next` explicitly does not
reap it and calls stale plugin copies doctor's to report, so the pointer
exists and the check does not.
