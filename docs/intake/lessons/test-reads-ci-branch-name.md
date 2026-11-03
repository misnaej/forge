# A test passed locally and failed in CI because CI branch name leaked into it

- seen: 2026-11-03
- branch: chore/intake-retire-879-no-version

## What happened

`tests/test_precommit.py::test_step_changelog_updated_deferred_warns_when_blocking_also_false`
faked the branch lookup in `forge.precommit` only. The no-version check
in `forge.changelog` resolves the branch itself and, in a `tmp_path`
that is not a git repo, falls back to `GITHUB_HEAD_REF`. In CI that held
the PR's own branch, `…-no-version`, so the step short-circuited on the
opt-out and the assertion failed. Locally the env var was unset and the
test passed.

## What it costs

One CI round trip; the failure looked like a regression in the change
under review.

## Fix, if known

A test that runs a step reading the branch clears `GITHUB_HEAD_REF`
(`monkeypatch.delenv`), or fakes the branch in every module that
resolves it. Reproduce locally with `GITHUB_HEAD_REF=<branch> pytest …`.
An earlier case (a protected-branch hook row that only failed on `main`
CI) was recorded outside the intake.
