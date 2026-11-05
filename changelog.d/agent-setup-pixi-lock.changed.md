bump: minor

- Agents may now run `pixi lock` in repos that install from a pixi lock: it writes down what the committed manifest already asks for and keeps existing pins, so the `block_install_deps` guard allows it (with any flags). Verbs that change what the project asks for — `pixi add`, `pixi update` and the like — stay blocked. FOUNDATION §2 now also says a repo's own bootstrap script that builds only that checkout's environment is not a forbidden install.
