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
in commit messages — the exact bug #266 removed.

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
    1: ``--check`` detected drift, or the hook lacks the managed-block
       markers.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from typing import Final

from forge.git_utils import configure_cli_logging, repo_root
from forge.pr_squash_comment import AI_ATTRIBUTION_PATTERNS


configure_cli_logging()
logger = logging.getLogger(__name__)


HOOK_PATH: Final[str] = "claude-hooks/block_claude_attribution.sh"

_MANAGED_BLOCK_RE: Final[re.Pattern[str]] = re.compile(
    r"(# FORGE_ATTRIBUTION_PATTERNS_BEGIN[^\n]*\n(?:#[^\n]*\n)*)"
    r"^ATTRIBUTION_PATTERNS='[^']*'\s*\n"
    r"(.*?# FORGE_ATTRIBUTION_PATTERNS_END)",
    re.DOTALL | re.MULTILINE,
)


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

    Args:
        content: Current text of ``block_claude_attribution.sh``.

    Returns:
        The text with the ``ATTRIBUTION_PATTERNS`` line inside the
        marker block replaced. Everything outside the block (and the
        marker lines themselves) is byte-preserved.

    Raises:
        ValueError: When the managed-block markers are missing or
            malformed in *content*.
    """
    expected = _expected_line()
    new_content, n = _MANAGED_BLOCK_RE.subn(
        lambda m: f"{m.group(1)}{expected}{m.group(2)}",
        content,
        count=1,
    )
    if n == 0:
        msg = (
            "FORGE_ATTRIBUTION_PATTERNS_BEGIN / END markers not found in "
            f"{HOOK_PATH} — cannot regenerate."
        )
        raise ValueError(msg)
    return new_content


def main() -> int:
    """Entry point for ``forge-gen-attribution-patterns``.

    Returns:
        ``0`` when the hook is written or already in sync; ``1`` when
        ``--check`` detects drift or the hook lacks the managed-block
        markers.
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

    path = repo_root() / HOOK_PATH
    if not path.is_file():
        logger.error("missing %s — nothing to regenerate.", path)
        return 1

    current = path.read_text()
    try:
        expected_content = _rewrite(current)
    except ValueError:
        logger.exception("cannot regenerate %s", HOOK_PATH)
        return 1

    if args.check:
        if current == expected_content:
            logger.info("OK: %s alternation is in sync.", HOOK_PATH)
            return 0
        logger.error(
            "DRIFT: %s alternation does not match the canonical "
            "AI_ATTRIBUTION_PATTERNS tuple. Run "
            "`forge-gen-attribution-patterns` to regenerate.",
            HOOK_PATH,
        )
        return 1

    if current == expected_content:
        logger.info("OK: %s already in sync — no write.", HOOK_PATH)
        return 0
    path.write_text(expected_content)
    logger.info("✓ regenerated %s", HOOK_PATH)
    return 0


if __name__ == "__main__":
    sys.exit(main())
