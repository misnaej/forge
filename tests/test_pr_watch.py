"""Tests for forge.pr_watch — background PR monitor lifecycle CLI.

# MOCKING STRATEGY: registry I/O uses real tmp files; gh calls are faked
# via monkeypatching _gh; process identity via /proc is faked by monkeypatching
# _pid_cmdline. One test (test_start_spawns_monitor_that_polls_and_logs) spawns
# the real detached monitor: it fakes only the `gh` executable on PATH, never
# Popen, so the process boundary is exercised for real. Loop tests patch
# _poll_once (scripted outcomes) and time.sleep; the --pr-less start test fakes
# subprocess.run with FakeGh, which models `gh pr list --head` and rejects any
# gh call it does not know. Digest tests use real log files.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from forge import pr_watch
from forge.pr_watch import MonitorRecord, Registry


if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def watch_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A repo root with repo_root pinned and CI disabled.

    Returns:
        The repo root.
    """
    monkeypatch.setattr(pr_watch, "repo_root", lambda: tmp_path)
    monkeypatch.delenv("CI", raising=False)
    for marker in ("GITHUB_ACTIONS", "BUILD_BUILDID", "JENKINS_URL"):
        monkeypatch.delenv(marker, raising=False)
    return tmp_path


def test_registry_round_trip(tmp_path: Path) -> None:
    """A saved registry loads back with the same records."""
    path = tmp_path / ".plan" / "pr_monitors.json"
    reg = Registry(
        monitors=[
            MonitorRecord(
                pid=123, pr=993, repo="o/r", started_at="2026-01-01T00:00:00+00:00"
            )
        ]
    )
    reg.save(path)
    loaded = Registry.load(path)
    assert len(loaded.monitors) == 1
    assert loaded.monitors[0].pid == 123
    assert loaded.monitors[0].pr == 993


def test_registry_load_missing_or_corrupt(tmp_path: Path) -> None:
    """A missing or corrupt registry loads as empty."""
    assert Registry.load(tmp_path / "nope.json").monitors == []
    bad = tmp_path / "bad.json"
    bad.write_text("not json")
    assert Registry.load(bad).monitors == []


def test_arming_check_all_signals_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    """When gh works, arming_check returns no failures."""
    proc = SimpleNamespace(
        stdout=json.dumps({"number": 993, "comments": []}),
        stderr="Logged in to github.com",
        returncode=0,
    )
    monkeypatch.setattr(pr_watch, "_gh", lambda *_a, **_k: proc)
    assert pr_watch.arming_check("o/r", 993) == []


def test_arming_check_fails_loudly_on_missing_gh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When gh is missing, arming_check names the signal."""

    def fake_gh(*a: object, **k: object) -> object:
        """Simulate a missing gh executable.

        Args:
            *a: Positional gh arguments, ignored.
            **k: Keyword gh arguments, ignored.

        Returns:
            Never returns; always raises FileNotFoundError.
        """
        msg = "gh"
        raise FileNotFoundError(msg)

    monkeypatch.setattr(pr_watch, "_gh", fake_gh)
    failures = pr_watch.arming_check("o/r", 993)
    assert any("gh-present" in f for f in failures)


def test_arming_check_fails_on_unparseable_pr_view(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When gh pr view returns garbage, arming_check names pr-view-json."""

    def fake_proc(stdout: str) -> SimpleNamespace:
        """Build a successful gh result with the given stdout.

        Args:
            stdout: Text the fake gh call prints.

        Returns:
            Namespace mimicking a successful subprocess result.
        """
        return SimpleNamespace(stdout=stdout, stderr="", returncode=0)

    def fake_gh(*args: object, **_kw: object) -> SimpleNamespace:
        """Answer gh version, auth and pr view calls; garbage for pr view.

        Args:
            *args: Positional gh arguments, inspected for the subcommand.
            **_kw: Keyword gh arguments, ignored.

        Returns:
            Namespace with the canned gh output.
        """
        joined = " ".join(str(a) for a in args)
        if "--version" in joined:
            return fake_proc("gh version 2.0")
        if "auth" in joined:
            return fake_proc("Logged in to github.com")
        return fake_proc("not json")

    monkeypatch.setattr(pr_watch, "_gh", fake_gh)
    failures = pr_watch.arming_check("o/r", 993)
    assert any("pr-view-json" in f or "comments-json" in f for f in failures)


def test_start_refuses_when_arming_fails(
    watch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Start exits 2 when the arming check finds a blind signal."""

    def fake_gh(*a: object, **k: object) -> object:
        """Simulate a missing gh executable.

        Args:
            *a: Positional gh arguments, ignored.
            **k: Keyword gh arguments, ignored.

        Returns:
            Never returns; always raises FileNotFoundError.
        """
        msg = "gh"
        raise FileNotFoundError(msg)

    monkeypatch.setattr(pr_watch, "_gh", fake_gh)
    args = argparse.Namespace(pr=993, repo="o/r", interval=60, bound=1800)
    assert pr_watch.cmd_start(args) == pr_watch.EXIT_ARMING_FAILED


def test_start_self_skips_in_ci(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In CI the command exits 0 without starting anything."""
    monkeypatch.setattr(pr_watch, "repo_root", lambda: tmp_path)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    args = argparse.Namespace(pr=993, repo="o/r", interval=60, bound=1800)
    assert pr_watch.cmd_start(args) == pr_watch.EXIT_OK
    assert not (tmp_path / ".plan" / "pr_monitors.json").exists()


@pytest.mark.parametrize(
    "cmdline",
    [
        "/usr/bin/something-else",
        # The false positive the old bare "pr_watch" match allowed: with pid
        # reuse it could SIGTERM an unrelated test run.
        "python -m pytest tests/test_pr_watch.py",
    ],
)
def test_stop_refuses_to_kill_unmatched_pid(
    watch_repo: Path, monkeypatch: pytest.MonkeyPatch, cmdline: str
) -> None:
    """Stop does not signal a pid whose command line is not a pr-watch monitor.

    Args:
        watch_repo: Temporary repo holding the monitor registry.
        monkeypatch: Pytest fixture used to stub the pid lookup and kill.
        cmdline: Command line reported for the recorded pid.
    """
    reg_path = watch_repo / ".plan" / "pr_monitors.json"
    reg = Registry(
        monitors=[
            MonitorRecord(pid=999999, pr=993, repo="o/r", started_at="2026-01-01")
        ]
    )
    reg.save(reg_path)
    monkeypatch.setattr(pr_watch, "_pid_cmdline", lambda _pid: cmdline)
    killed: list[int] = []
    monkeypatch.setattr(pr_watch.os, "kill", lambda pid, _sig: killed.append(pid))
    args = argparse.Namespace(pr=993, all=False)
    pr_watch.cmd_stop(args)
    assert killed == []


def test_stop_kills_matching_pid(
    watch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stop signals a pid whose command line is a pr-watch monitor."""
    reg_path = watch_repo / ".plan" / "pr_monitors.json"
    reg = Registry(
        monitors=[
            MonitorRecord(pid=999998, pr=993, repo="o/r", started_at="2026-01-01")
        ]
    )
    reg.save(reg_path)
    monkeypatch.setattr(
        pr_watch,
        "_pid_cmdline",
        lambda _pid: "python -m forge.pr_watch _monitor --pr 993",
    )
    killed: list[int] = []
    monkeypatch.setattr(pr_watch.os, "kill", lambda pid, _sig: killed.append(pid))
    args = argparse.Namespace(pr=993, all=False)
    code = pr_watch.cmd_stop(args)
    assert code == pr_watch.EXIT_OK
    assert killed == [999998]
    assert Registry.load(reg_path).monitors == []


def test_stop_keeps_record_it_could_not_signal(
    watch_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A live monitor refusing the signal stays registered and is reported kept.

    BEHAVIOR: dropping the record would leave a running process nothing
    tracks, and the message would read "0 stopped, 0 removed".
    """
    reg_path = watch_repo / ".plan" / "pr_monitors.json"
    Registry(
        monitors=[
            MonitorRecord(pid=999996, pr=993, repo="o/r", started_at="2026-01-01")
        ]
    ).save(reg_path)
    monkeypatch.setattr(
        pr_watch,
        "_pid_cmdline",
        lambda _pid: "python -m forge.pr_watch _monitor --pr 993",
    )

    def deny(_pid: int, _sig: int) -> None:
        raise PermissionError

    monkeypatch.setattr(pr_watch.os, "kill", deny)
    with caplog.at_level(logging.INFO, logger=pr_watch.logger.name):
        code = pr_watch.cmd_stop(argparse.Namespace(pr=993, all=False))
    assert code == pr_watch.EXIT_REFUSED
    assert [m.pid for m in Registry.load(reg_path).monitors] == [999996]
    assert "kept (could not signal)" in caplog.text


def test_stop_mixed_outcome_fails_when_one_cannot_be_signalled(
    watch_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One target stopping must not mask another that could not be signalled.

    BEHAVIOR: the all-kept case (test above) cannot reach this branch. Only
    a mixed run detects an exit code computed from "something stopped"
    returning OK while the message says "1 kept": a script reading only the
    exit code was told every monitor stopped.
    """
    reg_path = watch_repo / ".plan" / "pr_monitors.json"
    Registry(
        monitors=[
            MonitorRecord(pid=999991, pr=991, repo="o/r", started_at="2026-01-01"),
            MonitorRecord(pid=999992, pr=992, repo="o/r", started_at="2026-01-01"),
        ]
    ).save(reg_path)
    monkeypatch.setattr(
        pr_watch,
        "_pid_cmdline",
        lambda _pid: "python -m forge.pr_watch _monitor --pr 991",
    )

    def kill_first_only(pid: int, _sig: int) -> None:
        if pid == 999992:
            raise PermissionError

    monkeypatch.setattr(pr_watch.os, "kill", kill_first_only)
    with caplog.at_level(logging.INFO, logger=pr_watch.logger.name):
        code = pr_watch.cmd_stop(argparse.Namespace(pr=None, all=True))
    assert code == pr_watch.EXIT_REFUSED
    assert [m.pid for m in Registry.load(reg_path).monitors] == [999992]
    assert "1 monitor(s) stopped, 0 stale record(s) removed, 1 kept" in caplog.text


@pytest.mark.parametrize(
    ("cmdline", "expected"),
    [
        ("python -m forge.pr_watch _monitor --pr 993", True),
        ("python -m pytest tests/test_pr_watch.py", False),
        ("", False),
    ],
    ids=["real-monitor", "reused-pid-unrelated-process", "no-such-process"],
)
def test_is_monitor_requires_monitor_subcommand_and_module(
    monkeypatch: pytest.MonkeyPatch, cmdline: str, *, expected: bool
) -> None:
    """Only a real monitor cmdline counts; a reused pid does not read as alive.

    Args:
        cmdline: Command line the faked process lookup returns.
        expected: Whether the pid should be treated as a monitor.
    """
    monkeypatch.setattr(pr_watch, "_pid_cmdline", lambda _pid: cmdline)
    assert pr_watch._is_monitor(4242) is expected


def test_stop_stale_only_record_is_removed_and_ok(
    watch_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A record whose process is gone is pruned, exit OK, reported as stale."""
    reg_path = watch_repo / ".plan" / "pr_monitors.json"
    Registry(
        monitors=[
            MonitorRecord(pid=999997, pr=993, repo="o/r", started_at="2026-01-01")
        ]
    ).save(reg_path)
    monkeypatch.setattr(pr_watch, "_pid_cmdline", lambda _pid: "")
    with caplog.at_level(logging.INFO, logger=pr_watch.logger.name):
        code = pr_watch.cmd_stop(argparse.Namespace(pr=993, all=False))
    assert code == pr_watch.EXIT_OK
    assert Registry.load(reg_path).monitors == []
    assert "0 monitor(s) stopped, 1 stale record(s) removed." in caplog.text


@pytest.mark.parametrize(
    ("alive", "last_poll", "expected"),
    [
        (True, "", "alive"),
        (True, "2026-01-01T00:00:00+00:00", "alive"),
        (False, "2026-01-01T00:00:00+00:00", "dead"),
        (False, "", "died-before-first-poll"),
    ],
)
def test_monitor_state_reading(*, alive: bool, last_poll: str, expected: str) -> None:
    """Liveness is tested first; an empty last_poll only damns a dead monitor.

    Args:
        alive: Whether the monitor process is running.
        last_poll: ISO timestamp of the last poll, or empty if none happened.
        expected: Expected state label from ``_monitor_state``.
    """
    record = MonitorRecord(
        pid=1, pr=1, repo="o/r", started_at="2026-01-01", last_poll=last_poll
    )
    assert pr_watch._monitor_state(record, alive=alive) == expected


def test_list_reports_monitor_that_never_polled(
    watch_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """List names the log to read and the restart command for an unpolled monitor."""
    monkeypatch.setenv("FORGE_CODE_HEALTH_DIR", str(watch_repo / "health"))
    Registry(
        monitors=[
            MonitorRecord(pid=999996, pr=993, repo="o/r", started_at="2026-01-01")
        ]
    ).save(watch_repo / ".plan" / "pr_monitors.json")
    monkeypatch.setattr(pr_watch, "_pid_cmdline", lambda _pid: "")
    pr_watch.cmd_list(argparse.Namespace())
    out = capsys.readouterr().out
    assert "died-before-first-poll" in out
    assert str(pr_watch.monitor_log_path(watch_repo, 993)) in out
    assert "forge-pr-watch start --pr 993" in out


_FAKE_GH = """#!/bin/sh
# Answers every gh call _poll_once makes; reports the PR MERGED.
case "$1 $2" in
  "pr view") echo '{"number": 993, "state": "MERGED", "mergeable": "MERGEABLE", \
"headRefOid": "abc", "baseRefName": "main", "comments": []}' ;;
  "pr list") echo '[]' ;;
  "pr checks") echo '[]' ;;
  "auth status") echo 'Logged in to github.com' ;;
  *) echo 'gh version 2.0' ;;
esac
"""


def test_start_spawns_monitor_that_polls_and_logs(
    watch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The detached monitor really starts, completes a poll, and logs its alert.

    Regression test for a real bug: every monitor died before its first poll
    (a str reached a Path parameter) and its stderr went to DEVNULL, so
    nothing showed it. This is the only test crossing the process boundary;
    ``cmd_start`` and ``Popen`` are real, only the ``gh`` executable is fake.
    """
    bin_dir = watch_repo / "bin"
    bin_dir.mkdir()
    fake_gh = bin_dir / "gh"
    fake_gh.write_text(_FAKE_GH)
    fake_gh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FORGE_CODE_HEALTH_DIR", str(watch_repo / "health"))
    monkeypatch.setattr(pr_watch, "arming_check", lambda *_a, **_k: [])
    reg_path = watch_repo / ".plan" / "pr_monitors.json"
    log_path = pr_watch.monitor_log_path(watch_repo, 993)
    args = argparse.Namespace(pr=993, repo="o/r", interval=1, bound=3)
    assert pr_watch.cmd_start(args) == pr_watch.EXIT_OK
    pid = Registry.load(reg_path).monitors[0].pid
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if Registry.load(reg_path).monitors[0].last_poll:
                break
            # A monitor that died reports now rather than at the deadline:
            # that is the regression this test exists for, and waiting ten
            # seconds to say so buys nothing. Read /proc rather than signal
            # 0, which succeeds on a zombie.
            if pr_watch._pid_cmdline(pid) == "":
                break
            time.sleep(0.1)
        log_text = log_path.read_text() if log_path.exists() else "<no log file>"
        assert Registry.load(reg_path).monitors[0].last_poll != "", log_text
        assert "Traceback" not in log_text, log_text
        assert "PR #993 MERGED" in log_text, log_text
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)


def test_list_empty(watch_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """List prints 'no monitors registered' when the registry is empty."""
    pr_watch.cmd_list(argparse.Namespace())
    captured = capsys.readouterr()
    assert "no monitors registered" in captured.out


def test_resolve_repo_from_origin(
    watch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_resolve_repo reads owner/repo from the origin remote URL."""
    monkeypatch.setattr(
        pr_watch,
        "run_git",
        lambda *_a, **_k: "git@github.com:misnaej/forge.git\n",
    )
    assert pr_watch._resolve_repo(watch_repo, None) == "misnaej/forge"


def test_resolve_repo_explicit_arg(watch_repo: Path) -> None:
    """An explicit --repo wins over the remote."""
    assert pr_watch._resolve_repo(watch_repo, "foo/bar") == "foo/bar"


def _completed(stdout: str) -> subprocess.CompletedProcess[str]:
    """A successful gh call carrying *stdout*.

    Args:
        stdout: The decoded payload gh printed.

    Returns:
        A CompletedProcess with returncode 0.
    """
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout)


# ---------------------------------------------------------------------------
# Stack-aware signals: _state_alerts / _ci_alerts / _stack_parent / stack / cmd_stack
# ---------------------------------------------------------------------------


def test_state_alerts_suppresses_conflicting_for_stack_child() -> None:
    """CONFLICTING is expected in a stack: no alert when stack_child is True."""
    view = {"state": "OPEN", "mergeable": "CONFLICTING"}
    alerts = pr_watch._state_alerts(1, view, stack_child=True)
    assert alerts == []
    alerts = pr_watch._state_alerts(1, view, stack_child=False)
    assert any("CONFLICTING" in a for a in alerts)
    merged = pr_watch._state_alerts(
        1, {"state": "MERGED", "mergeable": ""}, stack_child=True
    )
    assert any("MERGED" in a for a in merged)


def test_ci_alerts_marks_unmerged_parent_for_stack_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stack child's CI line names the unmerged parent base."""
    checks = json.dumps([{"name": "ci", "bucket": "fail"}])
    monkeypatch.setattr(pr_watch, "_gh", lambda *_a, **_k: _completed(checks))
    alerts = pr_watch._ci_alerts("o/r", 1, stack_child=True, base_ref="feat/parent")
    assert alerts == [
        "PR #1 CI FAILED: ci (CI run against unmerged parent base feat/parent)"
    ]
    plain = pr_watch._ci_alerts("o/r", 1, stack_child=False)
    assert plain == ["PR #1 CI FAILED: ci"]


def test_stack_parent_matches_open_pr_head(monkeypatch: pytest.MonkeyPatch) -> None:
    """_stack_parent is True exactly when base_ref is an open PR's headRefName."""
    listing = json.dumps([{"headRefName": "feat/parent"}, {"headRefName": "other"}])
    monkeypatch.setattr(pr_watch, "_gh", lambda *_a, **_k: _completed(listing))
    assert pr_watch._stack_parent("o/r", "feat/parent") is True
    assert pr_watch._stack_parent("o/r", "main") is False


def test_stack_order_parents_before_children() -> None:
    """A chain a <- b <- c orders as [a, b, c]; a cycle terminates in number order."""
    chain = [
        {"number": 3, "headRefName": "c", "baseRefName": "b"},
        {"number": 1, "headRefName": "a", "baseRefName": "main"},
        {"number": 2, "headRefName": "b", "baseRefName": "a"},
    ]
    assert [p["number"] for p in pr_watch._stack_order(chain)] == [1, 2, 3]
    cycle = [
        {"number": 2, "headRefName": "b", "baseRefName": "a"},
        {"number": 1, "headRefName": "a", "baseRefName": "b"},
    ]
    assert [p["number"] for p in pr_watch._stack_order(cycle)] == [1, 2]


def test_cmd_stack_prints_prs_in_dependency_order(
    watch_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """cmd_stack lists open PRs in dependency order with per-PR state."""
    listing = json.dumps(
        [
            {
                "number": 3,
                "title": "child",
                "headRefName": "c",
                "baseRefName": "b",
                "mergeable": "CONFLICTING",
            },
            {
                "number": 1,
                "title": "root",
                "headRefName": "a",
                "baseRefName": "main",
                "mergeable": "MERGEABLE",
            },
            {
                "number": 2,
                "title": "mid",
                "headRefName": "b",
                "baseRefName": "a",
                "mergeable": "MERGEABLE",
            },
        ]
    )
    monkeypatch.setattr(pr_watch, "_gh", lambda *_a, **_k: _completed(listing))
    monkeypatch.setattr(
        pr_watch,
        "wrapup_freshness",
        lambda _n, **_k: SimpleNamespace(fresh=True),
    )
    code = pr_watch.cmd_stack(argparse.Namespace(repo="o/r"))
    assert code == pr_watch.EXIT_OK
    lines = capsys.readouterr().out.splitlines()
    assert [line.split()[0] for line in lines] == ["#1", "#2", "#3"]
    assert "base=main" in lines[0]
    assert "fresh=fresh" in lines[0]
    assert "base=a" in lines[1]
    assert "base=b" in lines[2]
    assert "monitor=no-monitor" in lines[0]


def test_cmd_stack_empty_list_prints_no_open_prs(
    watch_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """With no open PRs, cmd_stack prints the empty marker."""
    monkeypatch.setattr(pr_watch, "_gh", lambda *_a, **_k: _completed("[]"))
    code = pr_watch.cmd_stack(argparse.Namespace(repo="o/r"))
    assert code == pr_watch.EXIT_OK
    assert "no open PRs" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Poll loop, auto-detect, comment baseline, poll producer, digest
# ---------------------------------------------------------------------------


class FakeGh:
    """Stand-in for ``subprocess.run`` that answers only known gh calls.

    Attributes:
        calls: Every argv seen, in order.
        branch_pr_list: Stdout returned for ``gh pr list --head``.
    """

    def __init__(self, branch_pr_list: str) -> None:
        """Record the canned ``pr list`` payload.

        Args:
            branch_pr_list: Stdout for ``gh pr list --head <branch>``.
        """
        self.calls: list[list[str]] = []
        self.branch_pr_list = branch_pr_list

    def __call__(self, argv: list[str], **_kw: object) -> subprocess.CompletedProcess:
        """Dispatch one gh call, rejecting any invocation not modelled.

        Args:
            argv: The command line.
            **_kw: Keyword arguments to ``subprocess.run``, ignored.

        Returns:
            The canned result.

        Raises:
            AssertionError: The invocation is not one gh accepts.
        """
        self.calls.append(argv)
        if argv[:3] == ["gh", "pr", "view"] and "--head" in argv:
            return subprocess.CompletedProcess(
                argv, 1, stdout="", stderr="unknown flag: --head"
            )
        if argv[:3] == ["gh", "pr", "list"] and "--head" in argv:
            return subprocess.CompletedProcess(
                argv, 0, stdout=self.branch_pr_list, stderr=""
            )
        msg = f"unmodelled gh invocation: {argv}"
        raise AssertionError(msg)


class _FakePopen:
    """A spawned monitor that never ran; carries only a pid."""

    pid = 4321

    def __init__(self, *_a: object, **_k: object) -> None:
        """Accept and ignore the real ``Popen`` arguments.

        Args:
            *_a: Positional ``Popen`` arguments, ignored.
            **_k: Keyword ``Popen`` arguments, ignored.
        """


@dataclass(frozen=True)
class _LoopRun:
    """What one scripted ``_monitor_loop`` run observed.

    Attributes:
        registry: The registry file the loop stamped.
        sleeps: Every sleep the loop asked for, so a test can prove it
            stopped without waiting.
        first_polls: The ``first_poll`` keyword the loop passed on each
            poll, in order — the comment baseline's wiring.
    """

    registry: Path
    sleeps: list[float]
    first_polls: list[bool]


def _run_loop(
    outcomes: list[pr_watch.PollOutcome],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    bound: int = 1000,
    interval: int = 0,
) -> _LoopRun:
    """Run ``_monitor_loop`` against scripted polls with a registered PR 9.

    Args:
        outcomes: What each successive ``_poll_once`` returns; polling past
            the end raises, so a loop that fails to stop is caught.
        tmp_path: Directory holding the registry.
        monkeypatch: Used to script ``_poll_once`` and ``time.sleep``.
        bound: Loop bound in seconds.
        interval: Poll interval in seconds.

    Returns:
        What the run observed — see :class:`_LoopRun`.
    """
    reg_path = tmp_path / ".plan" / "pr_monitors.json"
    Registry(
        monitors=[MonitorRecord(pid=1, pr=9, repo="o/r", started_at="2026-01-01")]
    ).save(reg_path)
    scripted = iter(outcomes)
    sleeps: list[float] = []
    first_polls: list[bool] = []

    def scripted_poll(*_a: object, **kwargs: object) -> pr_watch.PollOutcome:
        first_polls.append(bool(kwargs["first_poll"]))
        return next(scripted)

    monkeypatch.setattr(pr_watch, "_poll_once", scripted_poll)
    monkeypatch.setattr(pr_watch.time, "sleep", sleeps.append)
    pr_watch._monitor_loop("o/r", 9, interval, bound, reg_path)
    return _LoopRun(registry=reg_path, sleeps=sleeps, first_polls=first_polls)


def test_monitor_loop_logs_a_standing_alert_once_per_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A standing alert is logged once per head; a new head alerts again.

    SCENARIO: a PR stays CONFLICTING over three polls on head h1, then the
    head moves to h2 and stays conflicting. Regression: one real log held 19
    copies of one line, so a reader could not tell news from a standing fact.

    MOCK SETUP: ``_poll_once`` scripted to five outcomes; ``time.sleep``
    recorded, not slept; real registry file in tmp.

    EXPECTED BEHAVIOR: the alert line is logged exactly twice (once per head)
    and the registry's ``last_alerted_head`` is h2.
    """
    alert = "PR #9 CONFLICTING"
    outcomes = [pr_watch.PollOutcome(alerts=[alert], head="h1") for _ in range(3)]
    outcomes += [
        pr_watch.PollOutcome(alerts=[alert], head="h2"),
        pr_watch.PollOutcome(alerts=[], head="h2", terminal=True),
    ]
    with caplog.at_level(logging.WARNING, logger=pr_watch.logger.name):
        reg_path = _run_loop(outcomes, tmp_path, monkeypatch).registry
    assert caplog.messages.count(f"pr-watch: {alert}") == 2
    assert Registry.load(reg_path).monitors[0].last_alerted_head == "h2"


def test_monitor_loop_marks_only_the_first_successful_poll_as_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The loop passes ``first_poll=True`` to poll 1 only.

    SCENARIO: the comment baseline is whatever the first poll reads; every
    later poll must treat new comments as news. A loop passing a constant, or
    deriving the flag wrongly, silently swallows or floods comments.

    MOCK SETUP: ``_poll_once`` scripted to three successful outcomes, the
    last terminal, recording the ``first_poll`` keyword of each call.

    EXPECTED BEHAVIOR: recorded flags are ``[True, False, False]``.
    """
    outcomes = [
        pr_watch.PollOutcome(alerts=[], head="h1"),
        pr_watch.PollOutcome(alerts=[], head="h1"),
        pr_watch.PollOutcome(alerts=[], head="h1", terminal=True),
    ]
    first_polls = _run_loop(outcomes, tmp_path, monkeypatch).first_polls
    assert first_polls == [True, False, False]


def test_monitor_loop_does_not_spend_the_comment_baseline_on_a_failed_poll(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed first poll leaves the baseline owed to the next successful one.

    SCENARIO: poll 1 fails (gh error) and reads no comments; poll 2 succeeds.
    Regression: the poll counter advanced on failure, so poll 2 got
    ``first_poll=False`` and announced every comment the PR already had.

    MOCK SETUP: ``_poll_once`` scripted to a failed outcome, a successful
    one, then a terminal one, recording each call's ``first_poll``.

    EXPECTED BEHAVIOR: recorded flags are ``[True, True, False]``.
    """
    failed = pr_watch.PollOutcome(alerts=["PR #9: poll failed (boom)"], ok=False)
    outcomes = [
        failed,
        pr_watch.PollOutcome(alerts=[], head="h1"),
        pr_watch.PollOutcome(alerts=[], head="h1", terminal=True),
    ]
    first_polls = _run_loop(outcomes, tmp_path, monkeypatch).first_polls
    assert first_polls == [True, True, False]


def test_monitor_loop_logs_a_repeating_headless_failure_once_and_never_stamps_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An identical ``poll failed`` line is logged once and stamps no head.

    SCENARIO: gh keeps failing, so every poll yields the same line with an
    empty head. Dedup is keyed on ``(head, line)``, so the standing failure
    appears once rather than once per poll; with no head there is nothing
    to record as the last alerted head. Intended: the log shows a standing
    failure once, and the bound-reached line still reports the poll count.

    MOCK SETUP: ``_poll_once`` scripted to three identical failed outcomes
    then a quiet terminal one; real registry in tmp.

    EXPECTED BEHAVIOR: the failure line is logged exactly once and
    ``last_alerted_head`` stays empty.
    """
    line = "PR #9: poll failed (boom)"
    outcomes = [pr_watch.PollOutcome(alerts=[line], ok=False) for _ in range(3)]
    outcomes.append(pr_watch.PollOutcome(alerts=[], terminal=True))
    with caplog.at_level(logging.WARNING, logger=pr_watch.logger.name):
        reg_path = _run_loop(outcomes, tmp_path, monkeypatch).registry
    assert caplog.messages.count(f"pr-watch: {line}") == 1
    assert Registry.load(reg_path).monitors[0].last_alerted_head == ""


def test_monitor_loop_does_not_stamp_a_head_for_a_quiet_poll(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A poll with no alerts leaves ``last_alerted_head`` unset.

    SCENARIO: the PR is healthy on head h1; nothing was alerted, so no head
    is recorded as alerted. Intended behavior, not an omission.

    MOCK SETUP: ``_poll_once`` scripted to two quiet outcomes on h1, the
    last terminal; real registry in tmp.

    EXPECTED BEHAVIOR: ``last_alerted_head`` is empty, ``last_poll`` is set.
    """
    outcomes = [
        pr_watch.PollOutcome(alerts=[], head="h1"),
        pr_watch.PollOutcome(alerts=[], head="h1", terminal=True),
    ]
    reg_path = _run_loop(outcomes, tmp_path, monkeypatch).registry
    record = Registry.load(reg_path).monitors[0]
    assert record.last_alerted_head == ""
    assert record.last_poll != ""


@pytest.mark.parametrize(
    ("pr_list_stdout", "expected_code", "expected_pr"),
    [
        ('[{"number": 77}]', pr_watch.EXIT_OK, 77),
        ("[]", pr_watch.EXIT_REFUSED, None),
        ("not json", pr_watch.EXIT_REFUSED, None),
        ("[1]", pr_watch.EXIT_REFUSED, None),
    ],
    ids=["found", "no-open-pr", "malformed-json", "wrong-element-type"],
)
def test_start_without_pr_detects_it_from_the_branch_via_pr_list(
    watch_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    pr_list_stdout: str,
    expected_code: int,
    expected_pr: int | None,
) -> None:
    """Start without --pr finds the PR with ``gh pr list --head``, never ``pr view``.

    SCENARIO: ``gh pr view`` has no ``--head`` flag, so the old form made
    every start without ``--pr`` refuse. The branch is resolved to a PR
    through ``pr list``; an empty, malformed or oddly-shaped answer refuses.

    MOCK SETUP: ``subprocess.run`` replaced by ``FakeGh`` (exits 1 for
    ``view --head``, serves the parametrized ``list`` payload, rejects
    anything else); ``Popen`` faked; arming check stubbed to pass; git
    branch name stubbed.

    EXPECTED BEHAVIOR: the exit code and registered PR match the row, and
    no ``view`` call carried ``--head``.

    Args:
        pr_list_stdout: What ``gh pr list --head`` prints.
        expected_code: Exit code ``cmd_start`` must return.
        expected_pr: PR registered, or ``None`` when nothing may register.
    """
    monkeypatch.setenv("FORGE_CODE_HEALTH_DIR", str(watch_repo / "health"))
    fake = FakeGh(pr_list_stdout)
    monkeypatch.setattr(pr_watch.subprocess, "run", fake)
    monkeypatch.setattr(pr_watch.subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(pr_watch, "run_git", lambda *_a, **_k: "feat/x\n")
    monkeypatch.setattr(pr_watch, "arming_check", lambda *_a, **_k: [])
    args = argparse.Namespace(pr=None, repo="o/r", interval=60, bound=1800)
    assert pr_watch.cmd_start(args) == expected_code
    registered = Registry.load(watch_repo / ".plan" / "pr_monitors.json").monitors
    assert [m.pr for m in registered] == ([] if expected_pr is None else [expected_pr])
    assert not any(c[2:3] == ["view"] and "--head" in c for c in fake.calls)


def test_monitor_loop_exits_on_terminal_and_names_what_is_owed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A merged or closed PR ends the loop at once and says what is owed.

    SCENARIO: the first poll reports the PR terminal while the bound is a
    full minute away. Without the exit the loop polls a merged PR to its
    bound and then reports that, not the owed merge actions.

    MOCK SETUP: ``_poll_once`` returns one terminal outcome (a second poll
    would raise); ``time.sleep`` is recorded.

    EXPECTED BEHAVIOR: loop returns with no sleep requested; the log names
    ``/forge-next`` and ``forge-issue-triage post-pr``, not the bound line.
    """
    outcomes = [pr_watch.PollOutcome(alerts=[], head="h1", terminal=True)]
    with caplog.at_level(logging.WARNING, logger=pr_watch.logger.name):
        sleeps = _run_loop(
            outcomes, tmp_path, monkeypatch, bound=60, interval=60
        ).sleeps
    assert sleeps == []
    assert "/forge-next" in caplog.text
    assert "forge-issue-triage post-pr" in caplog.text
    assert "bound of" not in caplog.text


def test_comment_alerts_baseline_then_news() -> None:
    """The first poll baselines existing comments; later arrivals alert once."""
    two = [{"id": "a"}, {"id": "b"}]
    seen: set[str] = set()
    assert pr_watch._comment_alerts(5, {"comments": two}, seen, first_poll=True) == []
    three = [*two, {"id": "c"}]
    alerts = pr_watch._comment_alerts(5, {"comments": three}, seen, first_poll=False)
    assert len(alerts) == 1
    assert "1 new comment" in alerts[0]
    assert (
        pr_watch._comment_alerts(5, {"comments": three}, seen, first_poll=False) == []
    )


def test_comment_alerts_identifies_an_idless_comment_by_date_and_author() -> None:
    """A comment with no id is keyed ``createdAt:author`` and alerts once."""
    anonymous = {"createdAt": "2026-01-01", "author": "bob"}
    seen_anonymous: set[str] = {"x"}
    assert (
        len(
            pr_watch._comment_alerts(
                5, {"comments": [anonymous]}, seen_anonymous, first_poll=False
            )
        )
        == 1
    )
    assert "2026-01-01:bob" in seen_anonymous
    assert (
        pr_watch._comment_alerts(
            5, {"comments": [anonymous]}, seen_anonymous, first_poll=False
        )
        == []
    )


@pytest.mark.parametrize(
    "view", [{}, {"comments": None}], ids=["missing-key", "none-value"]
)
def test_comment_alerts_ignores_a_view_without_a_comment_list(
    view: dict[str, object],
) -> None:
    """A view lacking a usable ``comments`` list yields no alerts.

    Args:
        view: A ``gh pr view`` payload with no comment list.
    """
    assert pr_watch._comment_alerts(5, view, set(), first_poll=False) == []


def test_comment_alerts_first_comment_on_an_empty_pr_is_news() -> None:
    """A PR with no comments at arming must alert when its first one arrives.

    The regression this pins: the baseline used to be inferred from *seen*
    being empty, which a PR with no comments at arming leaves empty — so
    the first comment to arrive was read as the baseline and swallowed.
    The caller now says which poll is the first.
    """
    seen: set[str] = set()
    assert pr_watch._comment_alerts(5, {"comments": []}, seen, first_poll=True) == []
    assert (
        len(
            pr_watch._comment_alerts(
                5, {"comments": [{"id": "n"}]}, seen, first_poll=False
            )
        )
        == 1
    )


@pytest.mark.parametrize(
    ("state", "terminal"),
    [("MERGED", True), ("CLOSED", True), ("OPEN", False)],
)
def test_poll_once_reports_head_and_terminal_state(
    monkeypatch: pytest.MonkeyPatch, state: str, *, terminal: bool
) -> None:
    """``_poll_once`` returns the head it read and whether the PR is finished.

    SCENARIO: the loop decides re-alerting and exit from the outcome, so the
    producer must report head and terminal state faithfully, and a transient
    gh failure must neither end the loop nor stamp a head.

    MOCK SETUP: ``_gh`` returns a canned ``pr view``; the stack, CI and
    staleness helpers are stubbed to report nothing.

    EXPECTED BEHAVIOR: ``head`` equals ``headRefOid``; ``terminal`` is true
    only for MERGED/CLOSED; a failing gh gives one "poll failed" alert,
    empty head, not terminal.

    Args:
        state: The PR state gh reports.
        terminal: Whether that state ends the watch.
    """
    view = {"number": 9, "state": state, "mergeable": "MERGEABLE"}
    view |= {"headRefOid": "abc123", "baseRefName": "main", "comments": []}
    monkeypatch.setattr(pr_watch, "_gh", lambda *_a, **_k: _completed(json.dumps(view)))
    monkeypatch.setattr(pr_watch, "_stack_parent", lambda *_a, **_k: False)
    monkeypatch.setattr(pr_watch, "_ci_alerts", lambda *_a, **_k: [])
    monkeypatch.setattr(pr_watch, "_staleness_alerts", lambda *_a, **_k: [])
    outcome = pr_watch._poll_once("o/r", 9, set(), first_poll=False)
    assert outcome.head == "abc123"
    assert outcome.terminal is terminal

    def failing_gh(*_a: object, **_k: object) -> object:
        raise subprocess.CalledProcessError(1, "gh")

    monkeypatch.setattr(pr_watch, "_gh", failing_gh)
    failed = pr_watch._poll_once("o/r", 9, set(), first_poll=False)
    assert len(failed.alerts) == 1
    assert "poll failed" in failed.alerts[0]
    assert failed.head == ""
    assert failed.terminal is False
    assert failed.ok is False


def test_poll_once_feeds_the_comments_it_reads_to_the_comment_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_poll_once`` requests comments and forwards them with ``first_poll``.

    SCENARIO: the other signals are tested apart, so nothing else catches
    ``_poll_once`` dropping the comment signal, not forwarding ``first_poll``,
    or omitting ``comments`` from the fields it asks gh for.

    MOCK SETUP: ``_gh`` records its argv and returns a view whose comments
    are set per call; stack, CI and staleness helpers report nothing.

    EXPECTED BEHAVIOR: ``comments`` is in the ``--json`` field list; poll one
    (one comment, ``first_poll=True``) is quiet; poll two (two comments,
    ``first_poll=False``, same ``seen``) raises exactly one "1 new comment".
    """
    calls: list[tuple[str, ...]] = []
    comments: list[dict[str, str]] = [{"id": "a"}]

    def fake_gh(*argv: str, **_k: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        view = {"number": 9, "state": "OPEN", "mergeable": "MERGEABLE"}
        view |= {"headRefOid": "abc", "baseRefName": "main", "comments": comments}
        return _completed(json.dumps(view))

    monkeypatch.setattr(pr_watch, "_gh", fake_gh)
    monkeypatch.setattr(pr_watch, "_stack_parent", lambda *_a, **_k: False)
    monkeypatch.setattr(pr_watch, "_ci_alerts", lambda *_a, **_k: [])
    monkeypatch.setattr(pr_watch, "_staleness_alerts", lambda *_a, **_k: [])
    seen: set[str] = set()
    first = pr_watch._poll_once("o/r", 9, seen, first_poll=True)
    assert first.alerts == []
    assert "comments" in calls[0][calls[0].index("--json") + 1].split(",")
    comments.append({"id": "b"})
    second = pr_watch._poll_once("o/r", 9, seen, first_poll=False)
    assert len(second.alerts) == 1
    assert "1 new comment" in second.alerts[0]


def test_digest_reports_distinct_alerts_from_every_log_and_exits_zero(
    watch_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Digest lists each distinct alert once per log, reading unregistered logs.

    Regression: monitor alerts sat in a gitignored log nothing opened, so a
    PR conflicted and merged with nobody told.
    """
    health = watch_repo / "health"
    monkeypatch.setenv("FORGE_CODE_HEALTH_DIR", str(health))
    health.mkdir()
    conflict = "pr-watch: PR #1043 CONFLICTING — run forge-update-branch"
    (health / "pr_watch_1043.log").write_text(
        f"{conflict}\n"
        f"2026-01-01 10:00:00,123 WARNING {conflict}\n"
        f"{conflict}\n"
        "pr-watch: PR #1043 MERGED\n"
        "Traceback-free noise without the prefix\n"
    )
    (health / "pr_watch_7.log").write_text("noise only\n")
    assert pr_watch.main(["digest"]) == pr_watch.EXIT_OK
    out = capsys.readouterr().out
    assert out.count("PR #1043 CONFLICTING") == 1
    assert out.index("CONFLICTING") < out.index("MERGED")
    assert "pr_watch_7" not in out
    assert "noise" not in out


@pytest.mark.parametrize(
    ("log_text", "wording"),
    [
        (None, "no monitor alerts to report (no monitor logs present)"),
        ("", "no monitor alerts to report\n"),
    ],
    ids=["no-logs", "empty-log"],
)
def test_digest_says_nothing_to_report_and_exits_zero(
    watch_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    log_text: str | None,
    wording: str,
) -> None:
    """Digest exits 0 with distinct wording for absent logs and empty logs.

    Args:
        log_text: Content of a written log, or ``None`` to write none.
        wording: Text the output must contain.
    """
    health = watch_repo / "health"
    monkeypatch.setenv("FORGE_CODE_HEALTH_DIR", str(health))
    health.mkdir()
    if log_text is not None:
        (health / "pr_watch_5.log").write_text(log_text)
    assert pr_watch.main(["digest"]) == pr_watch.EXIT_OK
    assert wording in capsys.readouterr().out
