bump: minor

- The CVE scan records when it ran inside `code_health/pip_audit.json`, so copying or restoring that file no longer makes an old scan look fresh; both reuse checks read that one time, and an unreadable, unusable or future-dated one means scan.
- When a stale install would make the regenerated `docs/cli-reference.md` drop CLI sections the branch documents, `regen_docs` no longer stages it and blocks the commit, leaving the regenerated file in the worktree; refresh the install, regenerate and stage it.
- Stale-install messages no longer tell repos other than forge to run forge's own `./dev/setup.sh`.
- New `[tool.forge.smart_test].safe_fallback` (`escalate` by default, `warn`, `off`) sets what an unmappable non-Python change does; a changed conftest outside the test roots still always runs the full suite.
- "Is this path under a declared root" has one definition, so the slow-test coverage ranking no longer counts files from a same-named directory deeper in an absolute path (`/x/vendor/src/…` is not under `src`).
- The typecheck docs show how to pin the interpreter pyrefly checks against, for the stale-parent-`.venv` case that reports phantom missing imports.
