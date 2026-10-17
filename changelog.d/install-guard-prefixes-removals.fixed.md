bump: patch

- `block_install_deps` now sees an install behind a `sudo`, `env` or `VAR=val` prefix, or launched by path (`/usr/bin/pip`), including pixi's allowlist check (`sudo pixi add`).
- It blocks removals too (`pip uninstall`, `conda remove`, `conda env remove`), covers mamba and micromamba under the `conda` opt-out name, and blocks `uv tool run` / `uvx`, which fetch and run any package.
- `pixi lock --check`, which only verifies the lock, is now allowed; a plain `pixi lock` still blocks.
