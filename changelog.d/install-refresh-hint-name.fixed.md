bump: patch

The doc-regeneration pre-commit step no longer crashes with a `NameError` when it holds `docs/cli-reference.md` back because some CLI help could not be captured; it prints its refresh-the-install advice as intended.
