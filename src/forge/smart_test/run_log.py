"""Single-writer lock and incremental log sink for ``forge-smart-test``.

A test log is evidence an agent reads instead of re-running the suite
(FOUNDATION §13), so it must name the code it actually ran against and
must not be half one run, half another:

- :class:`RunLog` stamps the log when the run STARTS — the tree the tests
  were collected from — rather than when it ends, so edits made during a
  long run are never claimed as tested. Each depth tier's output is
  appended as soon as the tier finishes, and a ``# complete:`` line is
  written last; a log without it was interrupted and reads as unknown.
- :func:`acquire_lock` lets only one run write the log directory at a
  time. A second run refuses and names the holder instead of clobbering
  the first run's log; a lock left by a process that no longer exists is
  taken over.

The lock is portable (no ``fcntl``): it is a file created with
``O_CREAT | O_EXCL``, and a stale one is taken over by first renaming it
aside — an atomic step only one process wins — then checking that what
was moved is the dead holder seen a moment earlier; if a live run had
just taken the lock instead, it is put back. A three-way interleaving
(two takers and a third fresh run inside that window) remains possible;
it needs a dead holder and three simultaneous starts.
"""

from __future__ import annotations

import os
import sys
import time
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from forge.git_utils import code_health_dir, produced_at_stamp


if TYPE_CHECKING:
    from pathlib import Path


LOCK_NAME = "smart_test.lock"
# A lock file with no readable holder yet may belong to a run that is
# still writing it; it counts as live for this long after its last change.
_EMPTY_LOCK_GRACE_S = 5.0
COMPLETE_PREFIX = "# complete: "


class LockHeldError(Exception):
    """Another live ``forge-smart-test`` run holds the log lock."""


def _pid_alive(pid: int) -> bool:
    """Return whether a process with *pid* exists.

    Args:
        pid: Process id read from a lock file.

    Returns:
        ``True`` when the process exists (including one owned by another
        user, which signal 0 reports as a permission error).
    """
    if pid <= 0:
        return False
    if sys.platform == "win32":
        # There, os.kill(pid, 0) does not probe — it terminates the
        # process. With no safe probe, a recorded holder counts as live;
        # the refusal message says how to clear a stale lock by hand.
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _read_holder(lock: Path) -> tuple[int, str]:
    """Read the ``<pid> <started>`` line a lock file holds.

    Args:
        lock: The lock file.

    Returns:
        ``(pid, started)``; ``(0, "")`` when the file is unreadable or
        malformed (treated as a stale lock).
    """
    try:
        pid_text, _, started = lock.read_text(encoding="utf-8").strip().partition(" ")
        return int(pid_text), started
    except (OSError, ValueError):
        return 0, ""


def _take_over_stale(lock: Path, dead_pid: int) -> None:
    """Move a stale lock out of the way without clobbering a live one.

    Args:
        lock: The lock path.
        dead_pid: The holder pid just read and found dead.
    """
    aside = lock.with_name(f"{lock.name}.stale-{os.getpid()}")
    try:
        lock.replace(aside)
    except FileNotFoundError:
        return  # another process already moved it
    try:
        moved_pid, _ = _read_holder(aside)
        if moved_pid != dead_pid:
            # A live run took the lock between our read and the rename:
            # put its lock back (link fails rather than overwrite).
            with suppress(OSError):
                os.link(aside, lock)
    finally:
        aside.unlink(missing_ok=True)


def _create_lock(lock: Path, line: str) -> bool:
    """Create *lock* already holding *line*, failing if it exists.

    The holder line is written to a private temporary file first and then
    hard-linked into place, so the lock is never visible while empty — an
    empty lock would read as having no holder. Where hard links are not
    supported, falls back to an exclusive create followed by the write.

    Args:
        lock: The lock path.
        line: The ``<pid> <started>`` holder line.

    Returns:
        ``True`` when this call created the lock, ``False`` when it existed.
    """
    staged = lock.with_name(f"{lock.name}.new-{os.getpid()}")
    staged.write_text(line, encoding="utf-8")
    try:
        os.link(staged, lock)
    except FileExistsError:
        return False
    except OSError:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            return False
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(line)
    finally:
        staged.unlink(missing_ok=True)
    return True


def _holder_live(lock: Path, pid: int) -> bool:
    """Return whether the lock's recorded holder must be respected.

    Args:
        lock: The lock path.
        pid: The pid read from it (``0`` when unreadable or empty).

    Returns:
        ``True`` for a live pid, and for an unreadable lock changed within
        the grace period (a run may be mid-way through writing it).
    """
    if pid <= 0:
        try:
            age = time.time() - lock.stat().st_mtime
        except OSError:
            return False
        return age < _EMPTY_LOCK_GRACE_S
    return _pid_alive(pid)


def acquire_lock(repo_root: Path) -> Path:
    """Take the log-directory lock for this process.

    Created atomically with its holder line already in it
    (:func:`_create_lock`). An existing lock whose process is gone is
    taken over (:func:`_take_over_stale`) and the creation retried once.

    Args:
        repo_root: Git repo root.

    Returns:
        The lock file path, to pass to :func:`release_lock`.

    Raises:
        LockHeldError: When a live process holds the lock; the message
            names its pid and start time.
    """
    log_dir = code_health_dir(repo_root)
    log_dir.mkdir(parents=True, exist_ok=True)
    lock = log_dir / LOCK_NAME
    line = f"{os.getpid()} {datetime.now(UTC).isoformat(timespec='seconds')}\n"
    for _ in range(2):
        if _create_lock(lock, line):
            return lock
        pid, started = _read_holder(lock)
        if _holder_live(lock, pid):
            msg = (
                f"another forge-smart-test run (pid {pid or '?'}, started "
                f"{started or '?'}) is writing {log_dir}; wait for it to finish, "
                f"or read its log when it does. If no run is active, delete {lock}"
            )
            raise LockHeldError(msg)
        _take_over_stale(lock, pid)
    msg = f"could not take {lock}: it reappeared while being replaced"
    raise LockHeldError(msg)


def release_lock(lock: Path) -> None:
    """Remove the lock if this process still holds it.

    Args:
        lock: The path :func:`acquire_lock` returned.
    """
    pid, _ = _read_holder(lock)
    if pid == os.getpid():
        lock.unlink(missing_ok=True)


# Refuse to write through a symlink planted at a sink path (a log
# directory relocated somewhere shared). Not every platform has the flag.
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def _write_sink(path: Path, text: str, *, append: bool) -> None:
    """Write *text* to *path* without following a symlink there.

    Args:
        path: Sink file.
        text: Content to write.
        append: Append instead of truncating.
    """
    flags = (
        os.O_WRONLY | os.O_CREAT | _NOFOLLOW | (os.O_APPEND if append else os.O_TRUNC)
    )
    fd = os.open(path, flags, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)


@dataclass
class RunLog:
    """The two log sinks of one run, written incrementally.

    ``smart_test.log`` is what ``forge:precommit-fixer`` reads;
    ``pytest.log`` is ``forge-slow-tests-report``'s default input. Both
    get the same bytes.
    """

    paths: tuple[Path, ...]
    """The sink files, rewritten at :meth:`start` and appended after."""

    @classmethod
    def for_repo(cls, repo_root: Path, names: tuple[str, ...]) -> RunLog:
        """Build the sinks for *repo_root*'s log directory.

        Args:
            repo_root: Git repo root.
            names: Sink file names inside the log directory.

        Returns:
            A log over ``code_health_dir(repo_root) / name`` for each name.
        """
        log_dir = code_health_dir(repo_root)
        return cls(tuple(log_dir / name for name in names))

    def start(self, repo_root: Path, header: str = "") -> None:
        """Truncate the sinks and write the start-of-run stamp and header.

        Args:
            repo_root: Git repo root — the tree stamped is the one the
                tests are about to run against.
            header: Optional first body text.
        """
        text = f"{produced_at_stamp(repo_root)}\n{header}"
        for path in self.paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            _write_sink(path, text, append=False)

    def append(self, text: str) -> None:
        """Append *text* to every sink.

        Args:
            text: Output to add (one tier's section).
        """
        for path in self.paths:
            _write_sink(path, text, append=True)

    def complete(self, verdict: str) -> None:
        """Write the completion line that marks the log whole.

        Args:
            verdict: Short outcome, e.g. ``passed`` or ``failed (exit 1)``.
        """
        self.append(f"\n{COMPLETE_PREFIX}{verdict}\n")
