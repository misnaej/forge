"""Tests for ``forge.smart_test.run_log`` — the run lock and incremental log."""

from __future__ import annotations

import os
import subprocess
import sys
import time
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
    """An unreadable holder line, past the grace period, is stale and replaced.

    Args:
        content: The malformed lock-file content.
    """
    lock = code_health_dir(tmp_path) / LOCK_NAME
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(content, encoding="utf-8")
    aged = time.time() - 60
    os.utime(lock, (aged, aged))

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


# ---------------------------------------------------------------------------
# _take_over_stale
# ---------------------------------------------------------------------------


def _stale_leftovers(lock: Path) -> list[Path]:
    """Return any ``<lock>.stale-*`` files beside *lock*.

    Args:
        lock: Lock file path whose stale siblings are listed.

    Returns:
        The matching leftover paths, empty when none remain.
    """
    return list(lock.parent.glob(f"{lock.name}.stale-*"))


def test_take_over_stale_removes_the_dead_holders_lock(tmp_path: Path) -> None:
    """A lock still naming the dead pid is deleted, leaving nothing behind."""
    dead = _dead_pid()
    lock = tmp_path / LOCK_NAME
    lock.write_text(f"{dead} 2020-01-01T00:00:00+00:00\n", encoding="utf-8")

    run_log._take_over_stale(lock, dead)

    assert not lock.exists()
    assert _stale_leftovers(lock) == []


def test_take_over_stale_restores_a_lock_a_live_run_took_meanwhile(
    tmp_path: Path,
) -> None:
    """If the lock now names another pid, it is put back untouched."""
    lock = tmp_path / LOCK_NAME
    live = f"{os.getpid()} 2026-01-01T00:00:00+00:00\n"
    lock.write_text(live, encoding="utf-8")

    run_log._take_over_stale(lock, _dead_pid())

    assert lock.read_text(encoding="utf-8") == live
    assert _stale_leftovers(lock) == []


def test_take_over_stale_missing_lock_is_a_no_op(tmp_path: Path) -> None:
    """Another process already moved the lock: nothing to do, no error."""
    lock = tmp_path / LOCK_NAME

    run_log._take_over_stale(lock, _dead_pid())

    assert not lock.exists()
    assert _stale_leftovers(lock) == []


# ---------------------------------------------------------------------------
# sinks never follow a symlink
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="needs O_NOFOLLOW")
def test_start_refuses_to_write_through_a_symlinked_sink(tmp_path: Path) -> None:
    """A symlink planted at a sink path is an error, not a write elsewhere."""
    target = tmp_path / "victim.txt"
    target.write_text("precious\n", encoding="utf-8")
    log = RunLog.for_repo(tmp_path, ("a.log",))
    log.paths[0].parent.mkdir(parents=True, exist_ok=True)
    log.paths[0].symlink_to(target)

    with pytest.raises(OSError, match="symbolic link"):
        log.start(tmp_path)

    assert target.read_text(encoding="utf-8") == "precious\n"


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="needs O_NOFOLLOW")
def test_append_refuses_to_write_through_a_symlinked_sink(tmp_path: Path) -> None:
    """A sink swapped for a symlink mid-run is not appended through."""
    target = tmp_path / "victim.txt"
    target.write_text("precious\n", encoding="utf-8")
    log = RunLog.for_repo(tmp_path, ("a.log",))
    log.start(tmp_path)
    log.paths[0].unlink()
    log.paths[0].symlink_to(target)

    with pytest.raises(OSError, match="symbolic link"):
        log.append("injected\n")

    assert target.read_text(encoding="utf-8") == "precious\n"


# ---------------------------------------------------------------------------
# lock creation, grace period, platform guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("content", ["", "garbage\n", "0 x\n"])
def test_acquire_lock_fresh_malformed_lock_is_treated_as_live(
    tmp_path: Path, content: str
) -> None:
    """A just-written unreadable lock may be mid-write by a live run: refuse.

    Args:
        content: The malformed lock-file content.
    """
    lock = code_health_dir(tmp_path) / LOCK_NAME
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(content, encoding="utf-8")

    with pytest.raises(LockHeldError):
        acquire_lock(tmp_path)

    assert lock.read_text(encoding="utf-8") == content


def test_held_lock_refusal_tells_the_user_how_to_clear_it(tmp_path: Path) -> None:
    """The refusal names the lock file to delete when no run is active."""
    lock = acquire_lock(tmp_path)

    with pytest.raises(LockHeldError, match=f"delete {lock}"):
        acquire_lock(tmp_path)


def test_acquire_lock_leaves_no_staged_file_behind(tmp_path: Path) -> None:
    """The temporary file the lock is linked from never outlives the call."""
    lock = acquire_lock(tmp_path)

    assert list(lock.parent.glob(f"{LOCK_NAME}.new-*")) == []


def test_create_lock_falls_back_to_exclusive_create_without_hard_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A filesystem refusing ``link`` still yields a complete lock file."""

    def no_link(*_args: object, **_kwargs: object) -> None:
        raise PermissionError

    monkeypatch.setattr(run_log.os, "link", no_link)
    lock = tmp_path / LOCK_NAME

    assert run_log._create_lock(lock, "123 now\n") is True

    assert lock.read_text(encoding="utf-8") == "123 now\n"
    assert list(tmp_path.glob(f"{LOCK_NAME}.new-*")) == []


def test_create_lock_existing_lock_returns_false_and_stays_untouched(
    tmp_path: Path,
) -> None:
    """Losing the creation race reports ``False`` and cleans its staged file."""
    lock = tmp_path / LOCK_NAME
    lock.write_text("1 theirs\n", encoding="utf-8")

    assert run_log._create_lock(lock, "2 ours\n") is False

    assert lock.read_text(encoding="utf-8") == "1 theirs\n"
    assert list(tmp_path.glob(f"{LOCK_NAME}.new-*")) == []


def test_pid_alive_on_windows_never_calls_os_kill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``os.kill`` terminates a process on win32, so liveness must not use it."""

    def forbidden(*_args: object) -> None:
        pytest.fail("os.kill must not be called on win32")

    monkeypatch.setattr(run_log.sys, "platform", "win32")
    monkeypatch.setattr(run_log.os, "kill", forbidden)

    assert run_log._pid_alive(os.getpid() + 1) is True
