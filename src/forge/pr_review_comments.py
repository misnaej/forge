"""forge-pr-review-comments — read a PR's review comments in full.

GitHub serves review comments 30 to a page, and a bare ``gh api`` call
returns only the first page without saying so: past the 30th comment a
review looks complete and the rest never get a reply. This CLI reads
every page (``list``) and answers mechanically whether every review
thread has been replied to (``unreplied``), so neither the
``pr-manager`` agent nor the ``/pr-comments`` skill has to re-derive it
from a ``jq`` expression.

Exit codes: ``0`` done (``unreplied``: every thread has a reply), ``1``
(``unreplied`` only) at least one thread has none, its ids printed, ``2``
GitHub could not answer — never read as "none unreplied".
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Final

from forge.gh_comments import list_review_comments
from forge.git_utils import configure_cli_logging, emit


configure_cli_logging()
logger = logging.getLogger(__name__)

EXIT_OK: Final[int] = 0
EXIT_UNREPLIED: Final[int] = 1
EXIT_UNKNOWN: Final[int] = 2


def unreplied_ids(comments: list[dict[str, object]]) -> list[int]:
    """Return the ids of top-level review comments nobody has replied to.

    GitHub threads every reply onto the thread's first comment
    (``in_reply_to_id`` names it, never an intermediate reply), so a
    top-level comment is answered exactly when some comment points at
    its id.

    Args:
        comments: The full listing, as
            :func:`forge.gh_comments.list_review_comments` returns it.

    Returns:
        The unreplied top-level ids, in listing order.
    """
    replied = {
        c["in_reply_to_id"] for c in comments if c.get("in_reply_to_id") is not None
    }
    return [
        int(str(c["id"]))
        for c in comments
        if c.get("in_reply_to_id") is None and c.get("id") not in replied
    ]


def _fetch(pr_number: int) -> list[dict[str, object]] | None:
    """Return the full listing, logging why when there is none.

    Args:
        pr_number: GitHub PR number.

    Returns:
        Every review comment, or ``None`` when GitHub could not answer.
    """
    comments = list_review_comments(pr_number)
    if comments is None:
        logger.error(
            "could not read the review comments of PR #%d — the result is "
            "unknown, not empty",
            pr_number,
        )
    return comments


def _cmd_list(pr_number: int) -> int:
    """Print every review comment, one JSON object per line.

    Args:
        pr_number: GitHub PR number.

    Returns:
        ``EXIT_OK``, or ``EXIT_UNKNOWN`` when GitHub could not answer.
    """
    comments = _fetch(pr_number)
    if comments is None:
        return EXIT_UNKNOWN
    for comment in comments:
        emit(json.dumps(comment))
    return EXIT_OK


def _cmd_unreplied(pr_number: int) -> int:
    """Print the ids of unreplied top-level review comments.

    Args:
        pr_number: GitHub PR number.

    Returns:
        ``EXIT_OK`` when every thread has a reply, ``EXIT_UNREPLIED`` when
        some do not, ``EXIT_UNKNOWN`` when GitHub could not answer.
    """
    comments = _fetch(pr_number)
    if comments is None:
        return EXIT_UNKNOWN
    missing = unreplied_ids(comments)
    for comment_id in missing:
        emit(str(comment_id))
    return EXIT_UNREPLIED if missing else EXIT_OK


def main(argv: list[str] | None = None) -> int:
    """Dispatch ``list`` / ``unreplied``.

    Args:
        argv: Command-line arguments; ``None`` reads ``sys.argv``.

    Returns:
        The subcommand's exit code (see the module docstring).
    """
    parser = argparse.ArgumentParser(
        prog="forge-pr-review-comments",
        description=(
            "Read every review comment on a PR (all pages) and find the "
            "threads nobody has replied to. Exit 2 means GitHub could not "
            "answer — unknown, never 'none unreplied'."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("list", "print every review comment, one JSON object per line"),
        (
            "unreplied",
            (
                "print the ids of top-level comments without a reply "
                "(exit 1 when any, 0 when none)"
            ),
        ),
    ):
        cmd = sub.add_parser(name, help=help_text)
        cmd.add_argument("--pr", type=int, required=True, help="PR number")
    args = parser.parse_args(argv)
    if args.command == "list":
        return _cmd_list(args.pr)
    return _cmd_unreplied(args.pr)


if __name__ == "__main__":
    sys.exit(main())
