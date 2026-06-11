"""Forge: shared engineering foundation.

Process docs, pre-commit verification scripts, git hooks, and an optional
Claude Code plugin. Works with or without Claude Code installed.
"""

from importlib.metadata import PackageNotFoundError, version


try:
    __version__ = version("forge-scripts")
except PackageNotFoundError:
    # Not pip-installed (e.g. running from a source checkout without
    # `pip install -e .`). Match setuptools-scm's fallback.
    __version__ = "0.0.0+unknown"
