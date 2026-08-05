"""Generate the AI-attribution alternation in ``block_claude_attribution.sh``.

The canonical list of attribution phrases lives once, as
:data:`forge.pr_squash_comment.AI_ATTRIBUTION_PATTERNS`. The shell
``PreToolUse`` hook at ``claude-hooks/block_claude_attribution.sh``
needs the same list rendered as a ``grep -qiE`` alternation. This
generator reads the Python tuple and rewrites the managed-block line in
the shell file so both copies stay in sync (FOUNDATION §12) — the same
mechanism ``forge-gen-commit-types`` uses for the commit-type list.

Only the **phrase list** ports. The Python validator's bare-vendor-token
backstop (``_VENDOR_TOKENS`` + the citable-path exemption in
``_cites_repo_file``) stays Python-only: the hook has no equivalent of
the path-shape exemption, and porting the bare tokens verbatim would
false-positive on legitimate ``CLAUDE.md`` / ``.claude/`` path mentions
in commit messages.

The shell file carries a managed-block marker:

.. code-block:: bash

    # FORGE_ATTRIBUTION_PATTERNS_BEGIN — managed by
    # `forge-gen-attribution-patterns`. ...
    ATTRIBUTION_PATTERNS='co-authored-by:|...|authored by claude'
    # FORGE_ATTRIBUTION_PATTERNS_END

Usage:

    # Regenerate the managed block
    forge-gen-attribution-patterns

    # Verify the managed block is in sync (no write)
    forge-gen-attribution-patterns --check

Exit Codes:
    0: The block was written (default), or is already in sync
       (``--check``).
    1: The hook file is missing, ``--check`` detected drift, or the
       block can't be regenerated (missing managed-block markers, or a
       canonical phrase containing a single quote).
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from typing import Final

from forge.git_utils import configure_cli_logging, repo_root
from forge.managed_block import BlockSpec, check_or_write, rewrite_block
from forge.pr_squash_comment import AI_ATTRIBUTION_PATTERNS


configure_cli_logging()
logger = logging.getLogger(__name__)


HOOK_PATH: Final[str] = "claude-hooks/block_claude_attribution.sh"


def _alternation() -> str:
    """Render ``AI_ATTRIBUTION_PATTERNS`` as a ``|``-joined regex alternation.

    Each phrase is ``re.escape``d — the phrases are prose, and escaping
    keeps a future phrase containing ``.`` or ``(`` from silently
    widening the hook's match (today it only escapes hyphens and
    inter-word spaces, both no-ops for matching).

    Returns:
        Pipe-joined string of escaped phrases — the exact body of the
        shell variable ``ATTRIBUTION_PATTERNS``.

    """
    return "|".join(re.escape(p) for p in AI_ATTRIBUTION_PATTERNS)


def _expected_line() -> str:
    """Return the canonical ``ATTRIBUTION_PATTERNS='...'`` shell line.

    Returns:
        The full line (including trailing newline) the generator
        intends to write into the managed block.
    """
    return f"ATTRIBUTION_PATTERNS='{_alternation()}'\n"


def _rewrite(content: str) -> str:
    """Return *content* with the managed block updated to the canonical line.

    Thin per-tuple wrapper over
    :func:`forge.managed_block.rewrite_block` — the block grammar and
    the single-quote guard live in the shared engine.

    Args:
        content: Current text of ``block_claude_attribution.sh``.

    Returns:
        The text with the block's assignment replaced.

    Raises:
        ValueError: When the markers are missing or a phrase would break
            the shell quoting (propagated from the engine).
    """
    return rewrite_block(
        content,
        marker="FORGE_ATTRIBUTION_PATTERNS",
        var_name="ATTRIBUTION_PATTERNS",
        value=_alternation(),
    )


def main() -> int:
    """Entry point for ``forge-gen-attribution-patterns``.

    Returns:
        ``0`` when the hook is written or already in sync; ``1`` when the
        hook file is missing, ``--check`` detects drift, or the block
        can't be regenerated (missing managed-block markers, or a
        canonical phrase containing a single quote).
    """
    parser = argparse.ArgumentParser(
        prog="forge-gen-attribution-patterns",
        description=(
            "Regenerate the AI-attribution alternation in "
            "claude-hooks/block_claude_attribution.sh from the canonical "
            "AI_ATTRIBUTION_PATTERNS tuple in forge.pr_squash_comment."
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
            marker="FORGE_ATTRIBUTION_PATTERNS",
            var_name="ATTRIBUTION_PATTERNS",
            value=_alternation(),
            label=HOOK_PATH,
        ),
        check=args.check,
    )


if __name__ == "__main__":
    sys.exit(main())
