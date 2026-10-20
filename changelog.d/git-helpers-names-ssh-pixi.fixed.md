bump: patch

- Changed-file lists no longer drop files with non-ASCII or unusual names (`café.py`, names with spaces): every changed-file list is read from NUL-separated output, as the `ls-files` helpers already did.
- `forge-agent-profile --edits` no longer drops a file whose name starts or ends with a space from the edits receipt.
- Background git calls that reach a remote (fetch, ls-remote, push, pull) run ssh in batch mode unless you set your own ssh command, so they fail instead of hanging on a passphrase or host prompt.
- The tree-freshness stamp no longer reads a same-size edit made in the same second as clean.
- `forge-upgrade` tells a repo that installs from a pixi lock to run `pixi lock && pixi install --locked` instead of `pip install`, and `--apply` refuses for such a repo rather than run pip behind the lock.
