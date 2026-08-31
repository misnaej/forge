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

Every append also rotates the tail (FOUNDATION §10): entries beyond
``[tool.forge.continuation].max_recent_entries`` (default 50) or older
than ``max_recent_age_days`` (default 2 — done work clears fast) move
verbatim, append-only, to ``.plan/CONTINUATION-archive.md`` — never
deleted — and collapse into per-day digest lines under
``## Condensed history (auto-generated)``, so session starts read a
bounded file while the archive keeps full raw history.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

from forge import config as _config
from forge.git_utils import configure_cli_logging


configure_cli_logging()
logger = logging.getLogger(__name__)


CONTINUATION_PATH = Path(".plan") / "CONTINUATION.md"
ARCHIVE_PATH = Path(".plan") / "CONTINUATION-archive.md"
RECENT_HEADER = "## Recent activity (auto-appended)"
CONDENSED_HEADER = "## Condensed history (auto-generated)"
FILE_HEADER = "# Continuation Log"
ARCHIVE_HEADER = "# Continuation Archive (raw rotated entries — never trimmed)"

DEFAULT_MAX_RECENT_ENTRIES = 50
DEFAULT_MAX_RECENT_AGE_DAYS = 2
MIN_RECENT_ENTRIES = 10

_ENTRY_RE = re.compile(r"^- (\d{4}-\d{2}-\d{2}) (.+)$")
_WRAPUP_RE = re.compile(r"^PR #(\d+) wrap-up:")
_MERGE_RE = re.compile(r"PR merged:")
_COMMIT_RE = re.compile(r"^[0-9a-f]{7,40} ")
_PR_NUM_RE = re.compile(r"PR #(\d+)")
_DIGEST_RE = re.compile(
    r"^- (\d{4}-\d{2}-\d{2}) — (\d+) commit\(s\), (\d+) wrap-up\(s\), "
    r"(\d+) merge\(s\), (\d+) other(?:, PRs ([#\d ]+))?$"
)


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


def _split_sections(text: str) -> tuple[str, list[str], list[str]]:
    """Split the file into head, condensed-digest lines, and recent entries.

    Args:
        text: Full CONTINUATION.md content (RECENT_HEADER guaranteed
            present by :func:`_ensure_file_and_section`).

    Returns:
        ``(head, digest_lines, recent_lines)`` — *head* is everything
        before the condensed/recent sections, verbatim; the two lists
        hold existing ``- ``-prefixed lines from each section.
    """
    recent_idx = text.index(RECENT_HEADER)
    cond_idx = text.find(CONDENSED_HEADER)
    head_end = cond_idx if cond_idx != -1 and cond_idx < recent_idx else recent_idx
    head = text[:head_end]
    digest_lines: list[str] = []
    if cond_idx != -1 and cond_idx < recent_idx:
        cond_block = text[cond_idx:recent_idx]
        digest_lines = [ln for ln in cond_block.splitlines() if ln.startswith("- ")]
    recent_block = text[recent_idx + len(RECENT_HEADER) :]
    recent_lines = [ln for ln in recent_block.splitlines() if ln.startswith("- ")]
    return head, digest_lines, recent_lines


def _parse_digests(
    digest_lines: list[str],
) -> dict[str, tuple[int, int, int, int, set[str]]]:
    """Parse existing digest lines into per-day accumulators.

    Args:
        digest_lines: Lines from the condensed-history section.

    Returns:
        Mapping ``date -> (commits, wrapups, merges, other, pr_set)``;
        unparsable lines are dropped (they are regenerated from counts).
    """
    acc: dict[str, tuple[int, int, int, int, set[str]]] = {}
    for ln in digest_lines:
        m = _DIGEST_RE.match(ln)
        if not m:
            continue
        date, c, w, g, o, prs = m.groups()
        pr_set = set((prs or "").split()) - {""}
        acc[date] = (int(c), int(w), int(g), int(o), pr_set)
    return acc


def _condense_into(
    acc: dict[str, tuple[int, int, int, int, set[str]]], overflow: list[str]
) -> dict[str, tuple[int, int, int, int, set[str]]]:
    """Fold rotated raw entries into the per-day digest accumulators.

    Args:
        acc: Existing accumulators from :func:`_parse_digests`.
        overflow: Raw ``- YYYY-MM-DD ...`` lines being rotated out.

    Returns:
        The updated accumulator mapping.
    """
    for ln in overflow:
        m = _ENTRY_RE.match(ln)
        if not m:
            continue
        date, rest = m.groups()
        c, w, g, o, prs = acc.get(date, (0, 0, 0, 0, set()))
        if _WRAPUP_RE.match(rest):
            w += 1
        elif _MERGE_RE.search(rest):
            g += 1
        elif _COMMIT_RE.match(rest):
            c += 1
        else:
            o += 1
        prs.update(f"#{n}" for n in _PR_NUM_RE.findall(rest))
        acc[date] = (c, w, g, o, prs)
    return acc


def _render_digest(acc: dict[str, tuple[int, int, int, int, set[str]]]) -> list[str]:
    """Render accumulators back into sorted digest lines.

    Args:
        acc: Per-day accumulators.

    Returns:
        One ``- date — counts[, PRs ...]`` line per day, oldest first.
    """
    lines = []
    for date in sorted(acc):
        c, w, g, o, prs = acc[date]
        line = f"- {date} — {c} commit(s), {w} wrap-up(s), {g} merge(s), {o} other"
        if prs:
            line += ", PRs " + " ".join(sorted(prs, key=lambda s: int(s[1:])))
        lines.append(line)
    return lines


def _partition_recent(
    recent: list[str],
    head: str,
    *,
    max_entries: int,
    cutoff: str,
) -> tuple[list[str], list[str], int]:
    """Partition recent entries into keep/overflow with floor/cap constraints.

    Entries are classified by age and pinning (age threshold and pinned to
    open work), then constrained by a minimum-entries floor and max-entries
    cap that evicts only unpinned entries.

    Args:
        recent: Raw recent-section entries.
        head: File head containing pinned PR/issue references.
        max_entries: Maximum entries to keep.
        cutoff: Cutoff date (YYYY-MM-DD) for aging.

    Returns:
        ``(keep, overflow, pinned_kept)`` — entries to stay in recent,
        entries to rotate to archive, and count of pinned entries in keep.
    """
    # Undone work stays: entries referencing open PRs/issues in head
    # are pinned past the age bound.
    pinned_refs = set(_PR_NUM_RE.findall(head)) | set(re.findall(r"#(\d+)", head))

    # Phase 1: classify by age and pinning
    keep: list[str] = []
    overflow: list[str] = []
    pinned_kept = 0
    for ln in recent:
        m = _ENTRY_RE.match(ln)
        aged = bool(m and m.group(1) < cutoff)
        refs = set(re.findall(r"#(\d+)", ln))
        pinned = bool(refs & pinned_refs)
        if aged and not pinned:
            overflow.append(ln)
        else:
            keep.append(ln)
            pinned_kept += pinned

    # Phase 2: apply minimum-keep floor
    floor = min(MIN_RECENT_ENTRIES, len(recent))
    while len(keep) < floor and overflow:
        keep.append(overflow.pop())

    # Restore original order after floor may have pulled lines back
    kept_set = set(keep)
    keep = [ln for ln in recent if ln in kept_set]

    # Phase 3: apply count cap (evicts oldest unpinned only)
    if len(keep) > max_entries:
        excess = len(keep) - max_entries
        new_keep: list[str] = []
        for ln in keep:
            refs = set(re.findall(r"#(\d+)", ln))
            if excess > 0 and not (refs & pinned_refs):
                overflow.append(ln)
                excess -= 1
            else:
                new_keep.append(ln)
        keep = new_keep

    return keep, overflow, pinned_kept


def _rotate(path: Path, archive: Path, *, max_entries: int, max_age_days: int) -> None:
    """Rotate aged/overflowing recent entries into digest + archive.

    An entry rotates when its date is older than *max_age_days* (done
    work clears the recent tail after a week by default) or when it
    falls outside the newest *max_entries* (flood guard). Rotated lines
    are appended verbatim to *archive* — never deleted — and folded into
    the per-day condensed-history digest.

    Args:
        path: The CONTINUATION.md path.
        archive: The raw-archive path.
        max_entries: Count bound for the recent section.
        max_age_days: Age bound in days for the recent section.
    """
    text = path.read_text()
    head, digest_lines, recent = _split_sections(text)
    cutoff_dt = datetime.now(UTC).timestamp() - max_age_days * 86400
    cutoff = datetime.fromtimestamp(cutoff_dt, tz=UTC).strftime("%Y-%m-%d")

    keep, overflow, pinned_kept = _partition_recent(
        recent, head, max_entries=max_entries, cutoff=cutoff
    )

    if pinned_kept and pinned_kept > max_entries // 2:
        logger.info(
            "advisory: %d of %d kept entries are pinned to open work — "
            "consider finishing before starting more (WIP signal).",
            pinned_kept,
            len(keep),
        )
    if not overflow:
        return

    if not archive.exists():
        archive.write_text(f"{ARCHIVE_HEADER}\n\n")
    with archive.open("a") as fh:
        fh.write("\n".join(overflow) + "\n")

    acc = _condense_into(_parse_digests(digest_lines), overflow)
    digest = _render_digest(acc)
    if not head.endswith("\n"):
        head += "\n"
    new_text = (
        head
        + f"{CONDENSED_HEADER}\n\n"
        + "\n".join(digest)
        + ("\n" if digest else "")
        + f"\n{RECENT_HEADER}\n\n"
        + "\n".join(keep)
        + ("\n" if keep else "")
    )
    path.write_text(new_text)
    logger.info(
        "rotated %d entr(ies) to %s (digest: %d day(s))",
        len(overflow),
        archive,
        len(digest),
    )


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
        "--rotate",
        action="store_true",
        help="Run rotation/condensation only, without appending — the "
        "continuation-hygiene entry point for the /next skill.",
    )
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
        nargs="?",
        default=None,
        help="Subject line — commit subject, PR title, or merge subject "
        "(omitted with --rotate).",
    )
    args = parser.parse_args()
    if not args.rotate and args.subject is None:
        parser.error("subject is required unless --rotate is given")

    repo_root = Path.cwd()
    path = repo_root / CONTINUATION_PATH
    _ensure_file_and_section(path)

    today = _today_iso()
    if args.rotate:
        line = None
    elif args.commit:
        line = f"- {today} {args.commit} {args.subject}"
    elif args.pr:
        line = f"- {today} PR #{args.pr} wrap-up: {args.subject}"
    else:  # args.merge — required (mutually exclusive group, one is set)
        line = f"- {today} {args.merge} PR merged: {args.subject}"

    if line is not None:
        _append_line(path, line)
        logger.info("appended to %s: %s", path, line)

    cont_cfg = _config.read_tool_forge_section(repo_root, "continuation")
    max_entries_raw = cont_cfg.get("max_recent_entries", DEFAULT_MAX_RECENT_ENTRIES)
    max_age_raw = cont_cfg.get("max_recent_age_days", DEFAULT_MAX_RECENT_AGE_DAYS)
    max_entries = (
        int(max_entries_raw)
        if isinstance(max_entries_raw, int)
        else DEFAULT_MAX_RECENT_ENTRIES
    )
    max_age = (
        int(max_age_raw)
        if isinstance(max_age_raw, int)
        else DEFAULT_MAX_RECENT_AGE_DAYS
    )
    _rotate(
        path,
        repo_root / ARCHIVE_PATH,
        max_entries=max_entries,
        max_age_days=max_age,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
