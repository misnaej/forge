bump: patch

- **`regen_docs` only refreshes generated docs your commit feeds.** If you relied on unrelated commits quietly bringing `docs/api-digest.md` or `docs/cli-reference.md` up to date, you now get a non-blocking warning instead, naming the generator to run; commit its output on its own. A repo that keeps a CLI reference sees this most: outside forge's own repo nothing in your tree feeds that page, so after a forge upgrade it warns until you regenerate and commit it.
- **`forge-gen-cli-reference` exits 1 and writes nothing when any CLI's help cannot be captured**, in both write and `--check` mode. A CI job running `cli_reference_check` against a stale install now fails instead of passing; refresh the install it runs from.
