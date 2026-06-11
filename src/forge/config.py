"""Read forge-internal config from a repo's ``pyproject.toml``.

Loader for the ``[tool.forge]`` table. **Forge sets this in its own
repo to support its internal release workflow; consumer repos do not
need a ``[tool.forge]`` block.** Defaults collapse every CLI to
standard single-branch behaviour pointing at ``main``, so consumers
who never touch this stay on the conventional flow.

```toml
[tool.forge]
base_branch = "main"   # default
dev_branch  = "main"   # default — set to "dev" for forge's own repo
```

The shell hook ``claude-hooks/block_protected_branches.sh`` carries an
intentionally parallel inline-Python implementation that reads the
same two keys (so the hook has no ``forge-scripts`` dependency at
git-invocation time). If you add a new ``[tool.forge]`` key here,
mirror it there.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from pathlib import Path
    from types import ModuleType


# Same guarded-import pattern as forge.audit.common — tomllib is stdlib
# on Python 3.11+ but a forge consumer running 3.10 falls back to
# defaults silently.
TOMLLIB: ModuleType | None
try:
    import tomllib

    TOMLLIB = tomllib
except ImportError:
    TOMLLIB = None


logger = logging.getLogger(__name__)


# Default to single-branch flow: every CLI / hook treats both
# "channels" as the same branch unless the consumer's pyproject opts
# into dual-track by setting ``dev_branch`` to something other than
# ``base_branch``. Backwards-compatible with every existing consumer
# repo that has no ``[tool.forge]`` block.
DEFAULT_BASE_BRANCH = "main"
DEFAULT_DEV_BRANCH = "main"


@dataclass(frozen=True)
class ForgeConfig:
    """Branch-name configuration sourced from ``[tool.forge]``.

    The release-channel semantics (what each branch represents,
    cadence trade-offs) live in FOUNDATION §6. This class just
    carries the names.

    Attributes:
        base_branch: Name of the slow channel (typically ``"main"``).
        dev_branch: Name of the fast channel (typically ``"dev"``).
            Equal to ``base_branch`` when the consumer hasn't opted
            into dual-track.
    """

    base_branch: str = DEFAULT_BASE_BRANCH
    dev_branch: str = DEFAULT_DEV_BRANCH

    @property
    def dual_track(self) -> bool:
        """Return ``True`` when base and dev are distinct branches.

        Single source of truth for "should the dual-track UX kick in?"

        Returns:
            ``True`` when the repo has opted into the dual-track model
            by setting ``dev_branch`` to a name other than
            ``base_branch``; ``False`` otherwise (single-branch flow).
        """
        return self.base_branch != self.dev_branch


def load_config(repo_root: Path) -> ForgeConfig:
    """Read ``[tool.forge]`` from *repo_root*'s ``pyproject.toml``.

    Returns the defaults when the file is missing, can't be read,
    lacks a ``[tool.forge]`` table, or doesn't parse as TOML.
    Deliberately forgiving — config reads happen in hot paths (hooks,
    agents, pre-commit) and any failure should degrade to default
    behaviour, not block the workflow.

    Args:
        repo_root: Git repo root.

    Returns:
        Populated :class:`ForgeConfig`. With no overrides, both
        ``base_branch`` and ``dev_branch`` default to ``"main"`` —
        ``dual_track`` is ``False``, every CLI collapses to
        single-branch flow. Override ``dev_branch`` in
        ``[tool.forge]`` to opt in.
    """
    pyproject = repo_root / "pyproject.toml"
    if not pyproject.is_file() or TOMLLIB is None:
        return ForgeConfig()
    try:
        text = pyproject.read_text()
    except OSError as exc:
        logger.debug("forge.config: could not read %s (%s)", pyproject, exc)
        return ForgeConfig()
    try:
        data = TOMLLIB.loads(text)
    except ValueError as exc:
        logger.debug("forge.config: could not parse %s (%s)", pyproject, exc)
        return ForgeConfig()
    section = data.get("tool", {}).get("forge", {})
    return ForgeConfig(
        base_branch=section.get("base_branch", DEFAULT_BASE_BRANCH),
        dev_branch=section.get("dev_branch", DEFAULT_DEV_BRANCH),
    )
