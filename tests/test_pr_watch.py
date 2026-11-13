"""Tests for forge.pr_watch — background PR monitor lifecycle CLI.

# MOCKING STRATEGY: registry I/O uses real tmp files; gh calls are faked
# via monkeypatching _gh; process identity via /proc is faked by monkeypatching
# _pid_cmdline; the poll loop is not spawned in tests (start is tested with
# arming-check gating only).
"""

from __future__ import annotations

import json
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

    class FakeProc:
        stdout = json.dumps({"number": 993, "comments": []})
        stderr = "Logged in to github.com"
        returncode = 0

    monkeypatch.setattr(pr_watch, "_gh", lambda *_a, **_k: FakeProc())
    assert pr_watch.arming_check("o/r", 993) == []


def test_arming_check_fails_loudly_on_missing_gh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When gh is missing, arming_check names the signal."""

    def fake_gh(*a: object, **k: object) -> object:
        msg = "gh"
        raise FileNotFoundError(msg)

    monkeypatch.setattr(pr_watch, "_gh", fake_gh)
    failures = pr_watch.arming_check("o/r", 993)
    assert any("gh-present" in f for f in failures)


def test_arming_check_fails_on_unparseable_pr_view(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When gh pr view returns garbage, arming_check names pr-view-json."""

    class FakeProc:
        def __init__(self, stdout: str) -> None:
            self.stdout = stdout
            self.stderr = ""
            self.returncode = 0

    def fake_gh(*args: object, **_kw: object) -> FakeProc:
        joined = " ".join(str(a) for a in args)
        if "--version" in joined:
            return FakeProc("gh version 2.0")
        if "auth" in joined:
            return FakeProc("Logged in to github.com")
        return FakeProc("not json")

    monkeypatch.setattr(pr_watch, "_gh", fake_gh)
    failures = pr_watch.arming_check("o/r", 993)
    assert any("pr-view-json" in f or "comments-json" in f for f in failures)


def test_start_refuses_when_arming_fails(
    watch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Start exits 2 when the arming check finds a blind signal."""

    def fake_gh(*a: object, **k: object) -> object:
        msg = "gh"
        raise FileNotFoundError(msg)

    monkeypatch.setattr(pr_watch, "_gh", fake_gh)
    import argparse

    args = argparse.Namespace(pr=993, repo="o/r", interval=60, bound=1800)
    assert pr_watch.cmd_start(args) == pr_watch.EXIT_ARMING_FAILED


def test_start_self_skips_in_ci(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In CI the command exits 0 without starting anything."""
    monkeypatch.setattr(pr_watch, "repo_root", lambda: tmp_path)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    import argparse

    args = argparse.Namespace(pr=993, repo="o/r", interval=60, bound=1800)
    assert pr_watch.cmd_start(args) == pr_watch.EXIT_OK
    assert not (tmp_path / ".plan" / "pr_monitors.json").exists()


def test_stop_refuses_to_kill_unmatched_pid(
    watch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stop does not signal a pid whose command line is not pr-watch."""
    reg_path = watch_repo / ".plan" / "pr_monitors.json"
    reg = Registry(
        monitors=[
            MonitorRecord(pid=999999, pr=993, repo="o/r", started_at="2026-01-01")
        ]
    )
    reg.save(reg_path)
    monkeypatch.setattr(
        pr_watch, "_pid_cmdline", lambda _pid: "/usr/bin/something-else"
    )
    killed: list[int] = []
    monkeypatch.setattr(pr_watch.os, "kill", lambda pid, _sig: killed.append(pid))
    import argparse

    args = argparse.Namespace(pr=993, all=False)
    pr_watch.cmd_stop(args)
    assert killed == []


def test_stop_kills_matching_pid(
    watch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stop signals a pid whose command line contains pr_watch."""
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
        lambda _pid: "python -c from forge.pr_watch import _monitor_loop",
    )
    killed: list[int] = []
    monkeypatch.setattr(pr_watch.os, "kill", lambda pid, _sig: killed.append(pid))
    import argparse

    args = argparse.Namespace(pr=993, all=False)
    code = pr_watch.cmd_stop(args)
    assert code == pr_watch.EXIT_OK
    assert killed == [999998]
    assert Registry.load(reg_path).monitors == []


def test_list_empty(watch_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """List prints 'no monitors registered' when the registry is empty."""
    import argparse

    pr_watch.cmd_list(argparse.Namespace())
    captured = capsys.readouterr()
    assert "no monitors registered" in captured.out


def test_resolve_repo_from_origin(
    watch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_resolve_repo reads owner/repo from the origin remote URL."""

    class FakeProc:
        returncode = 0

    monkeypatch.setattr(
        pr_watch,
        "run_git",
        lambda *_a, **_k: "git@github.com:misnaej/forge.git\n",
    )
    assert pr_watch._resolve_repo(watch_repo, None) == "misnaej/forge"


def test_resolve_repo_explicit_arg(watch_repo: Path) -> None:
    """An explicit --repo wins over the remote."""
    assert pr_watch._resolve_repo(watch_repo, "foo/bar") == "foo/bar"
