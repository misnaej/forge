"""Tests for ``forge.smart_test.run_log`` — the run lock and incremental log."""

from __future__ import annotations

import os
import subprocess
import sys
from typing import TYPE_CHECKING

import pytest

from forge.git_utils import code_health_dir
from forge.smart_test import run_log
from forge.smart_test.run_log import (
    COMPLETE_PREFIX,
    LOCK_NAME,
    LockHeldError,
    RunLog,
    acquire_lock,
    release_lock,
)
from tests.conftest import PRODUCED_AT_RE


if TYPE_CHECKING:
    from pathlib import Path


def _dead_pid() -> int:
    """Return the pid of a child process that has already exited."""
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


# ---------------------------------------------------------------------------
# RunLog
# ---------------------------------------------------------------------------


def test_start_writes_stamp_line_first_then_header(tmp_path: Path) -> None:
    """Line 1 of every sink is the provenance stamp, the header follows."""
    log = RunLog.for_repo(tmp_path, ("a.log", "b.log"))

    log.start(tmp_path, "HEADER\n")

    for sink in log.paths:
        lines = sink.read_text(encoding="utf-8").splitlines()
        assert PRODUCED_AT_RE.fullmatch(lines[0])
        assert lines[1:] == ["HEADER"]


def test_start_truncates_what_an_earlier_run_left(tmp_path: Path) -> None:
    """A new run never inherits the previous run's output."""
    log = RunLog.for_repo(tmp_path, ("a.log",))
    log.start(tmp_path)
    log.append("old output\n")

    log.start(tmp_path)

    assert "old output" not in log.paths[0].read_text(encoding="utf-8")


def test_for_repo_follows_the_code_health_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sinks land in the relocated log directory, created on demand."""
    monkeypatch.setenv("FORGE_CODE_HEALTH_DIR", str(tmp_path / "elsewhere" / "logs"))
    log = RunLog.for_repo(tmp_path, ("a.log",))

    log.start(tmp_path)

    assert log.paths[0] == tmp_path / "elsewhere" / "logs" / "a.log"
    assert log.paths[0].is_file()


def test_append_adds_to_every_sink_in_order(tmp_path: Path) -> None:
    """Both sinks receive each appended section, in call order."""
    log = RunLog.for_repo(tmp_path, ("a.log", "b.log"))
    log.start(tmp_path)

    log.append("one\n")
    log.append("two\n")

    bodies = [sink.read_text(encoding="utf-8") for sink in log.paths]
    assert bodies[0] == bodies[1]
    assert bodies[0].endswith("one\ntwo\n")


@pytest.mark.parametrize("verdict", ["passed", "failed (exit 2)"])
def test_complete_writes_the_verdict_line_last(tmp_path: Path, verdict: str) -> None:
    """The ``# complete:`` line is the final line of every sink.

    Args:
        verdict: The outcome text written into the completion line.
    """
    log = RunLog.for_repo(tmp_path, ("a.log", "b.log"))
    log.start(tmp_path)
    log.append("body\n")

    log.complete(verdict)

    for sink in log.paths:
        assert sink.read_text(encoding="utf-8").splitlines()[-1] == (
            f"{COMPLETE_PREFIX}{verdict}"
        )


# ---------------------------------------------------------------------------
# acquire_lock / release_lock
# ---------------------------------------------------------------------------


def test_acquire_lock_creates_lock_naming_our_pid(tmp_path: Path) -> None:
    """The lock file holds ``<pid> <started>`` for this process."""
    lock = acquire_lock(tmp_path)

    assert lock == code_health_dir(tmp_path) / LOCK_NAME
    pid, _, started = lock.read_text(encoding="utf-8").strip().partition(" ")
    assert int(pid) == os.getpid()
    assert started


def test_second_acquire_while_holder_alive_raises_naming_pid(tmp_path: Path) -> None:
    """A live holder (this very process) makes the next acquire refuse."""
    lock = acquire_lock(tmp_path)

    with pytest.raises(LockHeldError, match=str(os.getpid())):
        acquire_lock(tmp_path)

    assert lock.exists()  # the refusal left the holder's lock alone


def test_acquire_lock_takes_over_a_dead_holders_lock(tmp_path: Path) -> None:
    """A lock left by a finished process is replaced by ours."""
    lock = code_health_dir(tmp_path) / LOCK_NAME
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(f"{_dead_pid()} 2020-01-01T00:00:00+00:00\n", encoding="utf-8")

    taken = acquire_lock(tmp_path)

    assert int(taken.read_text(encoding="utf-8").split()[0]) == os.getpid()


@pytest.mark.parametrize("content", ["", "garbage\n", "not-a-pid 2020\n", "0 x\n"])
def test_acquire_lock_treats_a_malformed_lock_as_stale(
    tmp_path: Path, content: str
) -> None:
    """An unreadable holder line cannot name a live process, so it is replaced.

    Args:
        content: The malformed lock-file content.
    """
    lock = code_health_dir(tmp_path) / LOCK_NAME
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(content, encoding="utf-8")

    taken = acquire_lock(tmp_path)

    assert int(taken.read_text(encoding="utf-8").split()[0]) == os.getpid()


def test_release_lock_removes_our_own_lock(tmp_path: Path) -> None:
    """Releasing a lock we hold deletes it, so the next run can acquire."""
    lock = acquire_lock(tmp_path)

    release_lock(lock)

    assert not lock.exists()
    assert acquire_lock(tmp_path) == lock


def test_release_lock_leaves_a_foreign_pid_lock(tmp_path: Path) -> None:
    """A lock now owned by another process is not ours to delete."""
    lock = code_health_dir(tmp_path) / LOCK_NAME
    lock.parent.mkdir(parents=True, exist_ok=True)
    foreign = f"{_dead_pid()} 2020-01-01T00:00:00+00:00\n"
    lock.write_text(foreign, encoding="utf-8")

    release_lock(lock)

    assert lock.read_text(encoding="utf-8") == foreign


def test_release_lock_missing_file_is_a_no_op(tmp_path: Path) -> None:
    """Releasing a lock that is already gone does not raise."""
    release_lock(tmp_path / "never-created.lock")


def test_pid_alive_rejects_non_positive_pids() -> None:
    """Pid 0 and negatives never count as a live holder."""
    assert not run_log._pid_alive(0)
    assert not run_log._pid_alive(-5)
    assert run_log._pid_alive(os.getpid())
