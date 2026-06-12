"""forge-post-merge — runs forge's managed post-merge git-hook logic.

Invoked by the thin ``.githooks/post-merge`` wrapper. CI-aware:
no-ops in non-interactive contexts per FOUNDATION §15. When the
process runs interactively, performs two side effects:

1. Foundation drift check via ``install-forge-claude-md --check
   --quiet`` (shared with post-checkout; see
   :mod:`forge._hook_helpers`).
2. Self-update of managed hook wrappers via a backgrounded
   ``install-forge-githooks --refresh --quiet`` — picks up forge
   upgrades automatically on every ``git pull``. post-checkout does
   not run this step; the installed forge-scripts version only
   changes via ``pip install``, which is most naturally chained
   off a ``git pull``.
"""

from __future__ import annotations

import argparse
import logging
import shutil
import subprocess
import sys

from forge._hook_helpers import run_foundation_drift_check
from forge.git_utils import configure_cli_logging
from forge.run_context import is_non_interactive


configure_cli_logging()
logger = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    """Run the forge-managed post-merge actions. Return an exit code.

    Args:
        argv: Optional argv override (used by tests). When ``None``,
            reads from :data:`sys.argv` (skipping the program name).
            Git invokes ``post-merge`` with one positional — a
            squash-status flag (``1`` for a squash merge, ``0``
            otherwise) — which the thin wrapper forwards via ``"$@"``.
            The parser accepts and ignores it.

    Returns:
        ``0`` in non-interactive contexts (CI / no-TTY) — fast exit
        before any side effect. ``1`` when
        ``install-forge-claude-md`` is not on PATH (forge-scripts not
        installed in this env). ``0`` on normal completion.
    """
    parser = argparse.ArgumentParser(
        prog="forge-post-merge",
        description=(
            "Forge-managed post-merge git-hook entrypoint. Invoked by "
            "the thin .githooks/post-merge wrapper. Runs the foundation "
            "drift check and backgrounds a self-refresh of managed hook "
            "wrappers. No-ops in non-interactive contexts (FOUNDATION §15)."
        ),
    )
    parser.add_argument(
        "squash_flag",
        nargs="?",
        help="squash-merge status flag passed by git (1=squash, 0=otherwise); ignored",
    )
    raw = sys.argv[1:] if argv is None else argv
    parser.parse_args(raw)

    if is_non_interactive():
        return 0

    rc = run_foundation_drift_check("post-merge")
    if rc != 0:
        return rc

    # Self-refresh — backgrounded + output-redirected. The hook
    # process exits first, then ``install-forge-githooks`` rewrites
    # the managed hook files. A mid-execution self-rewrite would
    # risk bash reading stale buffers past the current chunk
    # boundary.
    if shutil.which("install-forge-githooks") is not None:
        subprocess.Popen(
            ["install-forge-githooks", "--refresh", "--quiet"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
