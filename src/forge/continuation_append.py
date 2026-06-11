"""forge-continuation-append — append one line to ``.plan/CONTINUATION.md``.

Single source of truth for the activity-log append format used by
``forge:git-commit-push`` and ``forge:pr-manager``. Both agents shell out
to this CLI instead of carrying duplicated Bash blocks — keeps the
format consistent if it ever needs to change.

``.plan/CONTINUATION.md`` is gitignored — appends MUST NOT be committed
(FOUNDATION §10). This CLI only writes the file; the caller is
responsible for not staging it.

Usage:

- ``forge-continuation-append --commit <hash> <subject>`` — record a commit.
- ``forge-continuation-append --pr <number> <subject>`` — record a PR wrap-up.
- ``forge-continuation-append --merge <hash> <subject>`` — record a PR merge.

The CLI ensures both the file and the ``## Recent activity (auto-appended)``
section header exist before appending. Idempotent on the header.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

from forge.git_utils import configure_cli_logging


configure_cli_logging()
logger = logging.getLogger(__name__)


CONTINUATION_PATH = Path(".plan") / "CONTINUATION.md"
RECENT_HEADER = "## Recent activity (auto-appended)"
FILE_HEADER = "# Continuation Log"


def _today_iso() -> str:
    """Return today's date as ``YYYY-MM-DD``.

    Returns:
        ISO-format date string (UTC), no time component.
    """
    return datetime.now(UTC).strftime("%Y-%m-%d")


def _ensure_file_and_section(path: Path) -> None:
    """Create the file with the canonical headers if missing.

    Idempotent: existing files are left alone except for adding the
    ``## Recent activity`` section header if it's not already present.

    Args:
        path: Target file path (typically ``.plan/CONTINUATION.md``).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(f"{FILE_HEADER}\n")
    text = path.read_text()
    if RECENT_HEADER not in text:
        if not text.endswith("\n"):
            text += "\n"
        text += f"\n{RECENT_HEADER}\n\n"
        path.write_text(text)


def _append_line(path: Path, line: str) -> None:
    """Append *line* to *path* with a trailing newline.

    Args:
        path: Target file.
        line: The line to append (newline appended automatically).
    """
    with path.open("a") as fh:
        fh.write(line + "\n")


def main() -> int:
    """Append one activity-log line to ``.plan/CONTINUATION.md``.

    Returns:
        ``0`` on success, ``2`` on argument error.
    """
    parser = argparse.ArgumentParser(
        prog="forge-continuation-append",
        description=(
            "Append one line to .plan/CONTINUATION.md's auto-appended "
            "activity section. Single source of truth for the format "
            "used by forge:git-commit-push and forge:pr-manager."
        ),
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--commit",
        metavar="HASH",
        help="Record a commit. HASH is the short SHA.",
    )
    group.add_argument(
        "--pr",
        metavar="NUMBER",
        help="Record a PR wrap-up. NUMBER is the PR number (no leading #).",
    )
    group.add_argument(
        "--merge",
        metavar="HASH",
        help="Record a PR merge on main. HASH is the short SHA.",
    )
    parser.add_argument(
        "subject",
        help="Subject line — commit subject, PR title, or merge subject.",
    )
    args = parser.parse_args()

    repo_root = Path.cwd()
    path = repo_root / CONTINUATION_PATH
    _ensure_file_and_section(path)

    today = _today_iso()
    if args.commit:
        line = f"- {today} {args.commit} {args.subject}"
    elif args.pr:
        line = f"- {today} PR #{args.pr} wrap-up: {args.subject}"
    else:  # args.merge — required (mutually exclusive group, one is set)
        line = f"- {today} {args.merge} PR merged: {args.subject}"

    _append_line(path, line)
    logger.info("appended to %s: %s", path, line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
