"""Shared helpers for forge's managed git-hook entrypoints.

Private module backing :mod:`forge.post_merge` and
:mod:`forge.post_checkout`. Both entrypoints need the same
"hard-fail when forge-scripts isn't installed, then run the
foundation drift check, then log on a non-zero result" sequence;
this module owns that sequence so the two CLIs stay thin.

The CI / non-interactive bypass is intentionally NOT centralized
here. Each entrypoint applies its own short-circuit at the top of
``main()`` so the bypass remains visible at the call site, and so
post-merge can also skip its backgrounded self-refresh under the
same guard.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess

from forge.git_utils import repo_root


logger = logging.getLogger(__name__)


def run_foundation_drift_check(hook_name: str) -> int:
    """Run ``install-forge-claude-md --check --quiet``.

    Hard-fails (returns ``1``) when the CLI is not on PATH so the
    contributor learns they need to ``pip install -e ".[dev]"``.

    Args:
        hook_name: The calling hook's short name (``"post-merge"`` /
            ``"post-checkout"``) — embedded in the error message so
            the contributor can grep the source quickly.

    Returns:
        ``1`` when ``install-forge-claude-md`` is not on PATH —
        forge-scripts is not installed in the active environment.
        ``0`` on normal completion. A non-zero exit from the drift
        CLI itself is logged at INFO level but not propagated; the
        managed git hook must not fail a ``git pull`` over an
        advisory drift warning.
    """
    if shutil.which("install-forge-claude-md") is None:
        logger.error("[forge] %s: install-forge-claude-md not on PATH.", hook_name)
        logger.error('[forge] Run `pip install -e ".[dev]"` and retry.')
        return 1

    proc = subprocess.run(
        ["install-forge-claude-md", "--check", "--quiet"],
        check=False,
    )
    if proc.returncode != 0:
        logger.info("  → run `install-forge-claude-md` to sync.")

    return 0


def run_hook_extensions(hook_name: str) -> None:
    """Run consumer extension scripts under ``.githooks/<hook_name>.d/``.

    A sanctioned extension point: consumers drop executable ``*.sh``
    files into ``.githooks/<hook_name>.d/`` to layer repo-specific logic
    onto a managed hook without editing the forge-owned wrapper, which
    ``install-forge-githooks --refresh`` rewrites on every run. The
    installer only writes the named hook files, so the subdirectory and
    its scripts survive every refresh.

    Scripts run in sorted filename order (use a numeric prefix like
    ``10-`` to order them, the ``cron.d`` convention). Each is executed
    directly, so it must carry a shebang (e.g. ``#!/usr/bin/env bash``).
    Non-executable files are skipped silently. Failure-tolerant: a
    non-zero exit is
    logged to stderr and the next script still runs — a managed git
    hook must never fail the underlying git operation over a consumer
    extension, the same posture as the foundation drift check.

    Callers invoke this only inside their interactive / branch-move
    guards, so extensions inherit the CI no-op behaviour (FOUNDATION
    §15) and never fire during file-level checkouts.

    Args:
        hook_name: Managed hook short name (``"post-merge"`` /
            ``"post-checkout"``) — selects the ``.d`` directory.
    """
    try:
        repo = repo_root()
    except SystemExit:
        return
    ext_dir = repo / ".githooks" / f"{hook_name}.d"
    if not ext_dir.is_dir():
        return
    for script in sorted(ext_dir.glob("*.sh")):
        if not os.access(script, os.X_OK):
            continue
        proc = subprocess.run([str(script)], check=False)
        if proc.returncode != 0:
            logger.warning(
                "[forge] %s extension %s exited %d (ignored)",
                hook_name,
                script.name,
                proc.returncode,
            )
