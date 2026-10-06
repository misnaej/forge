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
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from forge.git_utils import code_health_dir, produced_at_stamp


if TYPE_CHECKING:
    from pathlib import Path


LOCK_NAME = "smart_test.lock"
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


def acquire_lock(repo_root: Path) -> Path:
    """Take the log-directory lock for this process.

    Created atomically (``O_CREAT | O_EXCL``). An existing lock whose
    process is gone is removed and the creation retried once.

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
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            pid, started = _read_holder(lock)
            if _pid_alive(pid):
                msg = (
                    f"another forge-smart-test run (pid {pid}, started {started}) "
                    f"is writing {log_dir}; wait for it to finish, or read its "
                    "log when it does"
                )
                raise LockHeldError(msg) from None
            lock.unlink(missing_ok=True)
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(line)
        return lock
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
            path.write_text(text, encoding="utf-8")

    def append(self, text: str) -> None:
        """Append *text* to every sink.

        Args:
            text: Output to add (one tier's section).
        """
        for path in self.paths:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(text)

    def complete(self, verdict: str) -> None:
        """Write the completion line that marks the log whole.

        Args:
            verdict: Short outcome, e.g. ``passed`` or ``failed (exit 1)``.
        """
        self.append(f"\n{COMPLETE_PREFIX}{verdict}\n")
