# forge-upgrade --help still offers the retired dev channel

- surface: forge-upgrade
- release: forge 10.7.0
- seen: 2026-11-04
- branch: docs/readme-compact

## Evidence

src/forge/upgrade.py:1080 still accepts and describes a `dev` channel,
although forge ships single-track and the `dev` branch no longer exists:

```text
choices=("main", "dev"),
help="Pin to a channel — `main` (slow, minor-only) or `dev` (every patch).",
```

The generated `docs/cli-reference.md` repeats it, and `main` is no longer
"slow, minor-only": it is every release.

## What it costs

A user who picks `--channel dev` pins forge to a branch that does not
exist; the help text misdescribes `main` for everyone who reads it.

## What would fix it

Drop `dev` from the choices and describe `main` as "every release";
regenerate the CLI reference.
