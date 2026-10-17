bump: patch

- `block_install_deps` now sees an install behind a `sudo`, `env` or `VAR=val` prefix, launched by path (`/usr/bin/pip`), or with dash flags before the verb (`pip -q install`, `uv -q add`), including pixi's allowlist check (`sudo pixi add`).
- It blocks removals and syncs too (`pip uninstall`, `conda remove`, `conda env remove`, `uv pip uninstall`, `uv pip sync`, `uv tool uninstall`), covers mamba and micromamba under the `conda` opt-out name, and blocks `uv tool run` / `uvx`, which fetch and run any package.
- The exact form `pixi lock --check`, which only verifies the lock, is now allowed; a plain `pixi lock`, or `--check` with anything beside it, still blocks.
