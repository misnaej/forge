"""The alert log and comment cursor a PR wait leaves behind.

Two artefacts, one per watched PR, both under ``code_health/``:

- ``pr_watch_<pr>.log`` — every alert a wait raised, appended. The wait
  delivers what must wake a session by exiting; this file is what
  ``digest`` reads afterwards for the rest, so it is a record and not a
  delivery channel.
- ``pr_watch_<pr>.seen`` — the comment identities already accounted for.
  It outlives the process on purpose: each wait is its own run, and a
  baseline that restarted every time swallowed any comment left between
  two waits.

Separated from the polling and waiting in :mod:`forge.pr_watch` because
they change for different reasons — one follows what GitHub reports, the
other follows how the record is written and read back. The dependency
runs one way only, from the wait to this module.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

from forge.git_utils import code_health_dir


if TYPE_CHECKING:
    from pathlib import Path


logger = logging.getLogger(__name__)

ALERT_PREFIX: Final = "pr-watch: "


#: The ISO stamp :func:`append_alert` writes before an alert, matched so
#: :func:`alert_lines` can drop it before deduplicating.
STAMP_RE: Final = re.compile(r"^\d{4}-\d{2}-\d{2}T[\d:.+\-Z]+\s+")


def flatten(text: str, *, limit: int | None) -> str:
    """Flatten *text* to one printable line, optionally truncating it.

    Args:
        text: The value to flatten.
        limit: Longest result before an ellipsis, or ``None`` to keep the
            whole line. A **field** is truncated — a long check name is
            worth less than a readable log — but a whole alert line is
            not: a per-field cap applied to one cuts the end off the
            timeout line, and its end is the part naming what is left
            unwatched. The field cap itself belongs to the caller that
            renders fields (:mod:`forge.pr_watch`), not here.

    Returns:
        A single-line printable rendering, never empty.
    """
    flat = " ".join(str(text).split())
    printable = "".join(c for c in flat if c.isprintable())
    if limit is not None and len(printable) > limit:
        printable = printable[:limit] + "…"
    return printable or "?"


def monitor_log_path(root: Path, pr: int) -> Path:
    """Return the log a PR's waits append their alerts to.

    One file per PR, because several waits can run at once and a shared
    file interleaves them. This is a runtime log, not step evidence: no
    step produces it and it carries no ``# produced-at:`` stamp, so
    ``forge-precommit``'s freshness check skips it by name rather than
    reporting a stamp it could never have.

    Args:
        root: Git repo root.
        pr: PR number.

    Returns:
        Path to the PR's log file.
    """
    return code_health_dir(root) / f"pr_watch_{pr}.log"


def append_alert(path: Path, line: str) -> None:
    """Append one alert to the PR's log, so ``digest`` can still read it.

    Args:
        path: The PR's alert log.
        line: One alert line.
    """
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            # Flattened here as well as at the source: one call writes one
            # physical line, whatever it was handed, so a newline inside
            # GitHub-supplied text cannot forge a second alert in the log
            # (§14). Callers flatten too; this makes it a local guarantee
            # rather than one that depends on every caller remembering.
            # Without the per-field cap: a field is worth truncating, a
            # whole alert is not — `digest` reads this back, and the tail
            # of a line is where it says what to do about it.
            handle.write(f"{ALERT_PREFIX}{stamp} {flatten(line, limit=None)}\n")
    except OSError:
        # The waiting caller is told on stdout regardless; losing the
        # log costs the catch-up read, not the delivery.
        logger.exception("pr-watch: could not append to %s", path)


def alert_lines(path: Path) -> list[str]:
    """Return a log's alert lines, in order, without repeats.

    Args:
        path: A ``pr_watch_<pr>.log`` path.

    Returns:
        The alert text of each distinct alert line, timestamp stripped so
        a repeated condition collapses; empty when the log is absent or
        holds none.
    """
    try:
        raw = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    seen: set[str] = set()
    lines: list[str] = []
    for line in raw:
        _, marker, rest = line.partition(ALERT_PREFIX)
        # The timestamp is for a human reading the log, and it must not
        # reach the dedup below: keyed on the stamp, a standing condition
        # alerted in two different seconds reads as two events, which is
        # what this function exists to collapse.
        rest = STAMP_RE.sub("", rest.strip(), count=1)
        # Strip control bytes on the way out as well as on the way in: a
        # log written by an older forge holds whatever GitHub supplied,
        # and this text is printed to a terminal and read by an agent.
        text = "".join(c for c in rest.strip() if c.isprintable())
        if not marker or not text or text in seen:
            continue
        seen.add(text)
        lines.append(text)
    return lines


def seen_path(log_path: Path) -> Path:
    """Return the file holding the comment identities already accounted for.

    Args:
        log_path: The PR's alert log.

    Returns:
        A sidecar beside that log.
    """
    return log_path.with_suffix(".seen")


def load_seen(log_path: Path) -> set[str] | None:
    """Return the comment identities an earlier wait already accounted for.

    Each wait is its own process, so without this the baseline restarts
    every time and a comment that arrived between two waits is silently
    swallowed: baselined as history by the next wait's first poll, so
    never alerted and never logged — unrecoverable, including by
    ``digest``. Losing a report that way is the exact failure this
    command exists to fix, so the baseline outlives the process.

    Absence and emptiness are **different answers**, which is why
    ``None`` exists here: a PR with no comments yet saves an empty
    sidecar, so an empty set read as "no wait has run" would make every
    later wait a first poll again and swallow the first comment to
    arrive — the one case the signal exists for, and the common one on a
    fresh PR. :func:`_comment_alerts` documents the same trap for a
    single process; this is its cross-process twin.

    Args:
        log_path: The PR's alert log; the sidecar sits beside it.

    Returns:
        The identities a previous wait recorded, which may legitimately
        be an empty set, or ``None`` when no wait has run for this PR —
        and only that makes the next poll a genuine first poll.
    """
    try:
        text = seen_path(log_path).read_text(encoding="utf-8")
    except OSError:
        return None
    return {line.strip() for line in text.splitlines() if line.strip()}


def save_seen(log_path: Path, seen: set[str]) -> None:
    """Record the comment identities accounted for, for the next wait.

    Args:
        log_path: The PR's alert log; the sidecar sits beside it.
        seen: Identities to persist.
    """
    try:
        path = seen_path(log_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Written whole then renamed, so a wait reading while another
        # writes (a sentinel run and a session, say) never sees a
        # half-written cursor, which reads as a shorter history and
        # re-announces comments.
        tmp = path.with_suffix(".seen.tmp")
        tmp.write_text("\n".join(sorted(seen)) + "\n", encoding="utf-8")
        tmp.replace(path)
    except OSError:
        # Costs the next wait its baseline, which re-announces the
        # conversation once. Never worth failing a wait over.
        logger.exception("pr-watch: could not record seen comments beside %s", log_path)
