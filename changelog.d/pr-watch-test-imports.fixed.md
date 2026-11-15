bump: patch
- Fix the ruff PLC0415 failures that PR #994's `tests/test_pr_watch.py` introduced on main: the five function-level `import argparse` statements moved to the module top level. This was failing `main`'s own CI and every open PR's pre-commit (e.g. #995).
