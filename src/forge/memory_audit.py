"""forge-memory-audit — count new agent memories since the last audit.

The ``/memory-audit`` skill reconciles an agent's persistent memory with
the repo's rule surface (FOUNDATION §12). Deciding *when* to offer it is
mechanical, so it lives here rather than in skill prose: ``/next`` runs
``status`` and offers the audit once enough new memories have built up,
and the audit's last step runs ``stamp`` so the count restarts.

- ``status --memory-dir <path>`` compares the memory notes (every
  ``*.md`` in the directory except the ``MEMORY.md`` index) against the
  stamp file ``<path>/.memory-audit-stamp`` and prints::

      new memories: <n>
      threshold: <t>
      offer audit: yes | no
      last audit: <YYYY-MM-DD> | never
      lessons: <intake lessons directory>
      promotion candidates: <n>, then one line per candidate

- ``stamp --memory-dir <path>`` rewrites the stamp file: today's date on
  the first line, then one line per note name currently present.

A note counts as new only when its name is absent from the stamp — an
edited note was already seen by the last audit. With no stamp file every
note is new. The threshold (``[tool.forge.memory_audit].threshold``,
default 5) and the intake (``[tool.forge.intake].dir``) come from the
repo the command runs in; outside a git repo the current directory
stands in.

Exit codes:
    0  status printed (the offer is informational) or stamp written
    1  the memory directory does not exist
"""

from __future__ import annotations

import argparse
import datetime as _dt
import logging
import subprocess
import sys
from pathlib import Path

from forge.config import load_config, read_tool_forge_section
from forge.git_utils import configure_cli_logging, emit, run_git
from forge.intake import (
    KIND_DIRS,
    PROMOTION_AT,
    configured_dir,
    discover,
    group_entries,
    occurrence_count,
)


configure_cli_logging()
logger = logging.getLogger(__name__)

STAMP_NAME = ".memory-audit-stamp"
INDEX_NAME = "MEMORY.md"
DEFAULT_THRESHOLD = 5


def note_names(memory_dir: Path) -> set[str]:
    """Return the memory note filenames in *memory_dir*.

    Args:
        memory_dir: The agent's memory directory.

    Returns:
        Every ``*.md`` file name directly in the directory, the
        ``MEMORY.md`` index excluded (it lists notes; it is not one).
    """
    return {
        path.name
        for path in memory_dir.glob("*.md")
        if path.is_file() and path.name != INDEX_NAME
    }


def read_audit_stamp(memory_dir: Path) -> tuple[str, set[str]] | None:
    """Return the last audit's date and the note names it saw.

    Args:
        memory_dir: The agent's memory directory.

    Returns:
        ``(date, names)`` from the stamp file, or ``None`` when it is
        missing or unreadable — both mean no audit has been recorded,
        so every note counts as new.
    """
    try:
        lines = (memory_dir / STAMP_NAME).read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    if not lines:
        return None
    names = {line.strip() for line in lines[1:] if line.strip()}
    return lines[0].strip(), names


def write_audit_stamp(memory_dir: Path) -> Path:
    """Rewrite the stamp with today's date and the current note names.

    Args:
        memory_dir: The agent's memory directory.

    Returns:
        The stamp file's path.
    """
    path = memory_dir / STAMP_NAME
    today = _dt.datetime.now(tz=_dt.UTC).date().isoformat()
    body = "\n".join([today, *sorted(note_names(memory_dir))]) + "\n"
    # Written to a temp file and renamed over the stamp: a symlink planted
    # at the stamp's name is replaced, never written through.
    staging = memory_dir / f".{STAMP_NAME}.tmp"
    staging.unlink(missing_ok=True)
    staging.write_text(body, encoding="utf-8")
    staging.replace(path)
    return path


def new_notes(memory_dir: Path) -> set[str]:
    """Return the notes the last audit did not see.

    Args:
        memory_dir: The agent's memory directory.

    Returns:
        Note names absent from the stamp; every note when there is no
        stamp. Names only — an edited note is not new.
    """
    current = note_names(memory_dir)
    stamp = read_audit_stamp(memory_dir)
    if stamp is None:
        return current
    return current - stamp[1]


def _repo_root() -> Path:
    """Return the git toplevel of the cwd, or the cwd outside a repo.

    Not ``git_utils.repo_root``: that one exits outside a repo, and the
    memory directory this CLI reads lives outside any repo.
    """
    try:
        top = run_git("rev-parse", "--show-toplevel", check=False, log_errors=False)
    except (OSError, subprocess.SubprocessError):
        top = ""
    return Path(top) if top else Path.cwd()


def configured_threshold(repo_root: Path) -> int:
    """Return ``[tool.forge.memory_audit].threshold``.

    Args:
        repo_root: Repository whose ``pyproject.toml`` is read.

    Returns:
        The configured positive integer, else :data:`DEFAULT_THRESHOLD`
        (an invalid value is logged and replaced, never fatal — the
        command only informs).
    """
    raw = read_tool_forge_section(repo_root, "memory_audit").get("threshold")
    if raw is None:
        return DEFAULT_THRESHOLD
    # bool is an int subclass; `threshold = true` is a typo, not 1.
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        logger.warning(
            "forge-memory-audit: invalid [tool.forge.memory_audit].threshold "
            "%r — using %d",
            raw,
            DEFAULT_THRESHOLD,
        )
        return DEFAULT_THRESHOLD
    return raw


def _check_dir(memory_dir: Path) -> bool:
    """Return whether *memory_dir* exists, explaining on stderr if not.

    Args:
        memory_dir: The directory to check.

    Returns:
        ``True`` if the directory exists, ``False`` otherwise (with an
        explanation written to stderr).
    """
    if memory_dir.is_dir():
        return True
    sys.stderr.write(
        f"forge-memory-audit: memory directory not found: {memory_dir}\n"
        "Pass the agent's memory directory (the harness names it in the "
        "session context).\n"
    )
    return False


def promotion_candidates(repo_root: Path) -> list[str]:
    """Return the lessons that have come up often enough to promote.

    The threshold is FOUNDATION §12's second occurrence, counted from the
    intake's lesson files (one per sighting) rather than re-read by the
    agent each audit.

    Args:
        repo_root: Repository whose intake is read.

    Returns:
        ``<title> (<count>)`` per lesson group reaching
        :data:`forge.intake.PROMOTION_AT` occurrences, most-seen first;
        empty without an intake.
    """
    base = load_config(repo_root).base_branch
    entries = discover(configured_dir(repo_root), base_branch=base)
    lessons = [e for e in entries if e.kind == "lesson"]
    return [
        f"{group[0].title} ({occurrence_count(group)})"
        for group in group_entries(lessons)
        if occurrence_count(group) >= PROMOTION_AT
    ]


def status(memory_dir: Path, repo_root: Path) -> int:
    """Print the new-memory count and whether to offer the audit.

    Args:
        memory_dir: The agent's memory directory.
        repo_root: Repository whose config sets the threshold and intake.

    Returns:
        ``0`` when printed, ``1`` when the memory directory is missing.
    """
    if not _check_dir(memory_dir):
        return 1
    count = len(new_notes(memory_dir))
    threshold = configured_threshold(repo_root)
    last = read_audit_stamp(memory_dir)
    emit(f"new memories: {count}")
    emit(f"threshold: {threshold}")
    emit(f"offer audit: {'yes' if count >= threshold else 'no'}")
    emit(f"last audit: {last[0] if last else 'never'}")
    emit(f"lessons: {configured_dir(repo_root) / KIND_DIRS['lesson']}")
    promote = promotion_candidates(repo_root)
    emit(f"promotion candidates: {len(promote)}")
    for title in promote:
        emit(f"  - {title}")
    return 0


def stamp(memory_dir: Path) -> int:
    """Record the current notes as audited.

    Args:
        memory_dir: The agent's memory directory.

    Returns:
        ``0`` when the stamp was written, ``1`` when the directory is
        missing.
    """
    if not _check_dir(memory_dir):
        return 1
    path = write_audit_stamp(memory_dir)
    emit(f"stamped {len(note_names(memory_dir))} memories: {path}")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    """Build the ``forge-memory-audit`` argument parser."""
    parser = argparse.ArgumentParser(
        prog="forge-memory-audit",
        description="Count agent memories added since the last /memory-audit.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("status", "print the new-memory count and whether to offer the audit"),
        ("stamp", "record the current memories as audited"),
    ):
        cmd = sub.add_parser(name, help=help_text)
        cmd.add_argument(
            "--memory-dir",
            type=Path,
            required=True,
            help="the agent's memory directory (holds MEMORY.md)",
        )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run ``forge-memory-audit``.

    Args:
        argv: Argument vector; ``None`` reads ``sys.argv``.

    Returns:
        The subcommand's exit code.
    """
    args = _build_parser().parse_args(argv)
    memory_dir = args.memory_dir.expanduser()
    if args.command == "stamp":
        return stamp(memory_dir)
    return status(memory_dir, _repo_root())


if __name__ == "__main__":
    sys.exit(main())
