# The conventional c4.toml branch reads a symlink without the repo-root check its sibling applies

- surface: src/forge/config.py resolve_model_section
- release: forge 16.5.3.dev1+gd2e6e42be
- seen: 2026-11-27
- branch: feat/c4-advisory-required-edges

## Evidence

`resolve_model_section` has two file branches. Explicit
`[tool.forge.c4].config` branch resolves the path and refuses it when
`is_relative_to(repo_root)` fails — `src/forge/config.py:616`. Conventional
branch reads `repo_root / "c4.toml"` with no such check —
`src/forge/config.py:625`. Fixed filename, so config cannot
redirect it — but `c4.toml` as a symlink out of the repo is followed by
`is_file()` and `read_text()`.

Pre-existing; untouched by the merge rewrite that found it. Security review
of that rewrite confirmed the `config` branch still refuses escapes on every
path, and flagged this one LOW.

## What it costs

Nothing observed. No incident. Reading an out-of-repo TOML needs an attacker
who already has repo write access, which already grants code execution
through hooks and tests. Worst case: a model read from outside the tree, so a
diagram and a drift verdict describe something the repo does not contain.

## What would fix it

Same two lines the sibling branch uses: resolve the candidate, refuse when
`is_relative_to(repo_root.resolve())` fails. One call site.

Not filed as an issue: FOUNDATION §1 keeps hardening with no incident out of
the backlog until it recurs. File once seen for real.
