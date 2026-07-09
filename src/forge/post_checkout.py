"""forge-post-checkout — runs forge's managed post-checkout git-hook logic.

Invoked by the thin ``.githooks/post-checkout`` wrapper. CI-aware:
no-ops in non-interactive contexts per FOUNDATION §15.

Git invokes ``post-checkout`` with three positional args:
``<prev_head> <new_head> <branch_flag>``. The CLI honors the third
arg: it fires the foundation drift check only when the HEAD actually
moved (``branch_flag == "1"``) to avoid running on every file-level
``git checkout <path>``.

Shares the drift-check sequence with ``post_merge`` via
:mod:`forge._hook_helpers`, and runs consumer extension scripts in
``.githooks/post-checkout.d/`` through the same shared helper. The
asymmetry between the two entrypoints is intentional: ``post-merge``
additionally backgrounds ``install-forge-githooks --refresh`` because
a ``git pull`` is the common trigger for a forge-scripts upgrade. A
branch checkout does not change the installed forge-scripts version,
so no self-refresh is needed here.
"""

from __future__ import annotations

import argparse
import logging
import sys

from forge._hook_helpers import run_foundation_drift_check, run_hook_extensions
from forge.git_utils import configure_cli_logging
from forge.run_context import is_ci, is_non_interactive


configure_cli_logging()
logger = logging.getLogger(__name__)


# Git invokes post-checkout with three positional args: the prior HEAD,
# the new HEAD, and a flag that is ``1`` for branch-changing checkouts
# and ``0`` for file-level checkouts. We act only on branch-changing
# checkouts to avoid spurious foundation-drift checks during normal
# work-tree operations like ``git checkout -- <path>``.
_GIT_BRANCH_CHECKOUT_FLAG = "1"


def main(argv: list[str] | None = None) -> int:
    """Run the forge-managed post-checkout actions. Return an exit code.

    Args:
        argv: Optional argv override (used by tests). When ``None``,
            reads from :data:`sys.argv` (skipping the program name).
            Expected positional args from git: ``<prev_head>
            <new_head> <branch_flag>``.

    Returns:
        ``0`` when ``branch_flag != "1"`` (file-level checkout, no
        HEAD move) — fast exit. ``0`` in CI — fast exit before any side
        effect. In a non-interactive-but-non-CI context (a no-tty local
        checkout), the drift check is skipped but consumer ``.d``
        extensions still run. ``1`` when ``install-forge-claude-md`` is
        not on PATH. ``0`` on normal completion.
    """
    parser = argparse.ArgumentParser(
        prog="forge-post-checkout",
        description=(
            "Forge-managed post-checkout git-hook entrypoint. Invoked by "
            "the thin .githooks/post-checkout wrapper. Runs the foundation "
            "drift check only when the HEAD actually moved (branch_flag == "
            "'1'). No-ops in non-interactive contexts (FOUNDATION §15)."
        ),
    )
    parser.add_argument("prev_head", nargs="?", help="prior HEAD (passed by git)")
    parser.add_argument("new_head", nargs="?", help="new HEAD (passed by git)")
    parser.add_argument(
        "branch_flag",
        nargs="?",
        default="0",
        help="'1' for branch-changing checkouts; '0' for file-level checkouts",
    )
    raw = sys.argv[1:] if argv is None else argv
    parsed = parser.parse_args(raw)
    if parsed.branch_flag != _GIT_BRANCH_CHECKOUT_FLAG:
        return 0

    # forge's own drift check is an interactive-only dev-loop aid
    # (FOUNDATION §15) — skipped in any non-interactive context.
    rc = 0
    if not is_non_interactive():
        rc = run_foundation_drift_check("post-checkout")

    # Consumer extensions are the consumer's own logic — they must run
    # wherever a human is present, including a no-tty local checkout.
    # Only genuine CI suppresses them; gating on the tty-inclusive
    # is_non_interactive() silently skipped them for many local terminals
    # (#177). Symmetric with post-merge.
    if not is_ci():
        run_hook_extensions("post-checkout")
    return rc


if __name__ == "__main__":
    sys.exit(main())
