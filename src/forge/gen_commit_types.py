"""Generate the conventional-commit alternation in ``check_commit_format.sh``.

The canonical list of conventional-commit type prefixes lives once, as
:data:`forge.pr_squash_comment.CONVENTIONAL_COMMIT_TYPES`. The shell
``PreToolUse`` hook at ``claude-hooks/check_commit_format.sh`` needs the
same list rendered as a regex alternation for its `grep -qE` validator.
This generator reads the Python tuple and rewrites the managed-block
line in the shell file so both copies stay in sync.

The shell file carries a managed-block marker:

.. code-block:: bash

    # FORGE_COMMIT_TYPES_BEGIN — managed by `forge-gen-commit-types`. ...
    CONVENTIONAL_TYPES='feat|fix|refactor|test|docs|chore|...|revert'
    # FORGE_COMMIT_TYPES_END

The generator regex-locates the ``CONVENTIONAL_TYPES='...'`` line
between the two markers and replaces it. Lines outside the block (and
the marker lines themselves) are byte-preserved.

Usage:

    # Regenerate the managed block
    forge-gen-commit-types

    # Verify the managed block is in sync (no write)
    forge-gen-commit-types --check

Exit Codes:
    0: The block was written (default), or is already in sync
       (``--check``).
    1: ``--check`` detected drift between the generated alternation
       and the committed ``check_commit_format.sh``.
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Final

from forge.git_utils import configure_cli_logging, repo_root
from forge.managed_block import BlockSpec, check_or_write, rewrite_block
from forge.pr_squash_comment import CONVENTIONAL_COMMIT_TYPES


configure_cli_logging()
logger = logging.getLogger(__name__)


HOOK_PATH: Final[str] = "claude-hooks/check_commit_format.sh"


def _alternation() -> str:
    """Render ``CONVENTIONAL_COMMIT_TYPES`` as a `|`-joined regex alternation.

    Returns:
        Pipe-joined string of the canonical type prefixes — the exact
        body of the shell variable ``CONVENTIONAL_TYPES``.
    """
    return "|".join(CONVENTIONAL_COMMIT_TYPES)


def _expected_line() -> str:
    """Return the canonical ``CONVENTIONAL_TYPES='...'`` shell line.

    Returns:
        The full line (including trailing newline) the generator
        intends to write into the managed block.
    """
    return f"CONVENTIONAL_TYPES='{_alternation()}'\n"


def _rewrite(content: str) -> str:
    """Return *content* with the managed block updated to the canonical line.

    Thin per-tuple wrapper over
    :func:`forge.managed_block.rewrite_block` — the block grammar lives
    in the shared engine, so it cannot drift from the other generators.

    Args:
        content: Current text of ``check_commit_format.sh``.

    Returns:
        The text with the block's assignment replaced.

    Raises:
        ValueError: When the managed-block markers are missing or
            malformed in *content* (propagated from the engine).
    """
    return rewrite_block(
        content,
        marker="FORGE_COMMIT_TYPES",
        var_name="CONVENTIONAL_TYPES",
        value=_alternation(),
    )


def main() -> int:
    """Entry point for ``forge-gen-commit-types``.

    Returns:
        ``0`` when the hook is written or already in sync; ``1`` when
        ``--check`` detects drift or the hook lacks the managed-block
        markers.
    """
    parser = argparse.ArgumentParser(
        prog="forge-gen-commit-types",
        description=(
            "Regenerate the conventional-commit alternation in "
            "claude-hooks/check_commit_format.sh from the canonical "
            "CONVENTIONAL_COMMIT_TYPES tuple in "
            "forge.pr_squash_comment."
        ),
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help=(
            "Verify the managed block matches the canonical alternation "
            "without writing. Exit 1 on drift."
        ),
    )
    args = parser.parse_args()
    return check_or_write(
        repo_root() / HOOK_PATH,
        spec=BlockSpec(
            marker="FORGE_COMMIT_TYPES",
            var_name="CONVENTIONAL_TYPES",
            value=_alternation(),
            label=HOOK_PATH,
        ),
        check=args.check,
    )


if __name__ == "__main__":
    sys.exit(main())
