bump: patch
- `parse_requires` now joins soft-wrapped `Requires:` continuation lines (paragraph join) before parsing, so a wrapped `Requires: PR #5` / `and #7` yields both refs instead of silently dropping #7; recorder field lines (`Bump:`, `track:`) end the wrap, and unparseable residue still fails closed
