# Which other `select = ["ALL"]` rules have an autofix that deletes behaviour?

- surface: ruff.toml
- release: forge 12.0.1.dev2+g42ee3e279.d20261111
- seen: 2026-11-11
- branch: feat/ci-skip-verified-tree

## Evidence

One such rule is now known, from a real incident. A new CLI
(`src/forge/ci_tree_gate.py`) wrote its one line of output with
`print(...)`. `T201` (bare print) is active under `select = ["ALL"]`, and
its fix is to DELETE the statement. `forge-precommit --only ruff`
reported `All checks passed.`; the CLI printed nothing, exit 0, and the
file held

    if args.as_json:
        pass
    else:
        pass

Both emitting branches gone, a green lint verdict over the top. Found
only by running the binary — no step noticed. That one is closed:
`T201` is `unfixable` in `ruff.toml:16`, so it now fails instead of
deleting, and `docs/step-invocation.md` names `forge.git_utils.emit` as
the way to write stdout.

## What it costs

The class is what is unswept. A lint autofix that removes a program's
behaviour while the gate reports success is the worst shape a check can
have: the reader stops checking, and `select = ["ALL"]` means forge opts
into every such rule the moment ruff ships it, including ones nobody has
read. The dangerous version is a CLI whose output something else
consumes, deleted the same way behind a green pre-commit.

Cost so far: one debugging cycle.

## What would fix it

Enumerate ruff's fixable rules whose fix removes code rather than
rewriting it, and decide per rule whether it belongs in `unfixable`.
`ruff rule --all --output-format json` flags which rules have a fix at
all — for `T201` the whole `fix` field reads "Fix is sometimes
available." — so it narrows the list but does not say which fixes
delete. That part needs a read or an experiment per candidate, which is
why this is not a one-command sweep.

Worth doing when the ruff pin next moves, since that is when new rules
arrive under `ALL`.
