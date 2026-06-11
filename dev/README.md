# `dev/` — forge self-test rig

This directory holds tooling **used to develop and validate forge itself**,
not shipped to consumers via the pip package or the Claude Code plugin.

Forge's whole job is to define and ship standards (pre-commit hooks, ruff
config, label schemas, doctor checks, etc.). To prove those standards
actually hold, forge needs to run them against its own code. That's what
`dev/` is for.

## Contents

- `setup.sh` — idempotent bootstrap: create conda env, editable-install
  the pip package with test deps, wire git hooks via
  `.githooks/install.sh`, run `forge-doctor`. One command:
  ```bash
  ./dev/setup.sh
  ```
  Override env name with `CONDA_ENV_NAME=<name>`, Python version with
  `PYTHON_VERSION=<ver>` (defaults to 3.13).
- `test-matrix.sh` — run `pytest tests/` across every supported Python
  version (3.10, 3.11, 3.12, 3.13). Creates one throwaway conda env
  per version. Use this before tagging a release to verify forge
  still works on the full supported range:
  ```bash
  ./dev/test-matrix.sh                  # all versions
  PY_VERSIONS="3.12 3.13" ./dev/test-matrix.sh
  ```

## Why a separate directory?

`src/forge/`, `agents/`, `skills/`, `claude-hooks/`, `.githooks/`, and
`.claude-plugin/` are all part of the **shipped surface** — consumers see
them via pip or via the plugin marketplace. `dev/` is explicitly the
opposite: local-only, contributor-facing, no version contract.

If a piece of dev-only infrastructure starts to feel useful to consumers
too, promote it to a shipped surface (CLI in `src/forge/`, hook in
`.githooks/`, etc.) — don't grow `dev/` into a second public API.
