"""Tests for ``forge.continuation`` — the ``forge-continuation`` CLI.

# MOCKING STRATEGY: the panel itself is real (``tmp_path`` git repo with a
# gitignored ``.plan/``). Only ``read_pr_state`` — the GitHub seam — is
# stubbed where a test drives ``--with-pr``; ``gh`` is faked for the reader's
# own tests via ``forge.continuation.subprocess.run``.
"""

from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING

import pytest

from forge import continuation as cont
from forge import continuation_state as cs
from forge.run_context import _CI_MARKERS
from tests.conftest import commit_all, init_git_repo


if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def _not_in_ci(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear every CI marker so the CI guard is off unless a test sets it."""
    for name in _CI_MARKERS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def ignored_repo(tmp_path: Path) -> Path:
    """A git repo with one commit and ``.plan/`` gitignored.

    Returns:
        The repo root.
    """
    init_git_repo(tmp_path)
    (tmp_path / ".gitignore").write_text(".plan/\n", encoding="utf-8")
    commit_all(tmp_path, "ignore plan")
    return tmp_path


def _text(root: Path) -> str:
    """Return the handoff note under *root*.

    Args:
        root: The repository root path.

    Returns:
        The text content of the continuation note.
    """
    return (root / cs.CONTINUATION_PATH).read_text(encoding="utf-8")


def _stub_pr(monkeypatch: pytest.MonkeyPatch, state: cs.PrState | None) -> list[Path]:
    """Make the PR read return *state*; return the roots it was asked about.

    Args:
        monkeypatch: Pytest fixture for patching.
        state: The PR state to return from the stubbed read.

    Returns:
        A list that accumulates the root paths passed to read_pr_state.
    """
    calls: list[Path] = []

    def _read(root: Path) -> cs.PrState | None:
        calls.append(root)
        return state

    monkeypatch.setattr(cont, "read_pr_state", _read)
    return calls


def test_state_without_with_pr_never_reads_github(
    ignored_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The commit-path write is offline: no PR read, attempt recorded."""
    calls = _stub_pr(monkeypatch, None)
    rc = cont.main(["state", "--attempt", "blocked:ruff"], repo_root=ignored_repo)
    assert rc == 0
    assert calls == []
    assert "- last-attempt: blocked:ruff" in _text(ignored_repo)


def test_with_pr_refreshes_and_a_failed_read_keeps_the_last_fields(
    ignored_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--with-pr`` writes fresh fields; an unreadable PR leaves the old ones."""
    good = cs.PrState("61", "clean", "passed", "fresh", "abc1234")
    _stub_pr(monkeypatch, good)
    assert cont.main(["state", "--with-pr"], repo_root=ignored_repo) == 0
    assert "- pr: 61 (as of abc1234)" in _text(ignored_repo)
    _stub_pr(monkeypatch, None)
    assert cont.main(["state", "--with-pr"], repo_root=ignored_repo) == 0
    assert "- pr: 61 (as of abc1234)" in _text(ignored_repo)


def test_state_exits_one_when_markers_refuse_the_write(ignored_repo: Path) -> None:
    """A refused write is a failing exit, not a silent skip."""
    path = ignored_repo / cs.CONTINUATION_PATH
    path.parent.mkdir()
    path.write_text(f"{cs.BEGIN_MARKER}\n{cs.BEGIN_MARKER}\n", encoding="utf-8")
    assert cont.main(["state"], repo_root=ignored_repo) == 1


def test_state_in_ci_exits_zero_and_writes_nothing(
    ignored_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CI is a deliberate skip: success exit, no file, no PR read."""
    monkeypatch.setenv("CI", "1")
    calls = _stub_pr(monkeypatch, None)
    assert cont.main(["state", "--with-pr"], repo_root=ignored_repo) == 0
    assert calls == []
    assert not (ignored_repo / cs.CONTINUATION_PATH).exists()


def test_check_reports_usage_within_and_over_budget(
    ignored_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``check`` prints used/budget and the verdict, exiting 0."""
    (ignored_repo / "pyproject.toml").write_text(
        "[tool.forge.continuation]\njudgment_max_lines = 3\n", encoding="utf-8"
    )
    cont.main(["state"], repo_root=ignored_repo)
    assert cont.main(["check"], repo_root=ignored_repo) == 0
    assert "1/3 lines (within budget)" in capsys.readouterr().out
    path = ignored_repo / cs.CONTINUATION_PATH
    path.write_text(_text(ignored_repo) + "\na\nb\nc\nd\n", encoding="utf-8")
    assert cont.main(["check"], repo_root=ignored_repo) == 0
    assert "over budget" in capsys.readouterr().out


def test_check_exits_one_on_duplicate_markers(
    ignored_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Duplicate or mismatched markers fail ``check`` and are named."""
    path = ignored_repo / cs.CONTINUATION_PATH
    path.parent.mkdir()
    path.write_text(f"{cs.BEGIN_MARKER}\n{cs.BEGIN_MARKER}\n{cs.END_MARKER}\n")
    assert cont.main(["check"], repo_root=ignored_repo) == 1
    assert "expected one" in capsys.readouterr().out


def test_check_with_no_note_is_a_no_op(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An absent note is nothing to check, not an error."""
    assert cont.main(["check"], repo_root=tmp_path) == 0
    assert "absent" in capsys.readouterr().out


def _fake_gh(
    monkeypatch: pytest.MonkeyPatch, *, returncode: int, stdout: str, stderr: str = ""
) -> None:
    """Replace every subprocess call with one fixed ``gh`` result.

    Args:
        monkeypatch: Pytest fixture for patching.
        returncode: The return code to always return.
        stdout: The stdout to always return.
        stderr: The stderr to always return (defaults to empty string).
    """

    def _run(*_a: object, **_kw: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], returncode, stdout, stderr)

    monkeypatch.setattr(cont.subprocess, "run", _run)


def test_read_pr_state_reads_an_open_pr(
    ignored_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An open PR yields sanitized fields labelled with its head SHA."""
    view = {
        "number": 61,
        "state": "OPEN",
        "headRefOid": "abc1234deadbeef",
        "baseRefName": "main",
        "mergeable": "MERGEABLE",
        "statusCheckRollup": [],
    }
    _fake_gh(monkeypatch, returncode=0, stdout=json.dumps(view))
    monkeypatch.setattr(
        cont, "wrapup_freshness", lambda _n: type("F", (), {"fresh": True})()
    )
    state = cont.read_pr_state(ignored_repo)
    assert state is not None
    assert (state.number, state.as_of, state.wrapup) == ("61", "abc1234", "fresh")


def test_read_pr_state_skipped_draft_ci_reads_not_run(
    ignored_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A draft whose checks were all skipped never reads as passed in the panel."""
    view = {
        "number": 61,
        "state": "OPEN",
        "headRefOid": "abc1234deadbeef",
        "baseRefName": "main",
        "mergeable": "MERGEABLE",
        "isDraft": True,
        "statusCheckRollup": [
            {"name": "ci", "status": "COMPLETED", "conclusion": "SKIPPED"}
        ],
    }
    _fake_gh(monkeypatch, returncode=0, stdout=json.dumps(view))
    monkeypatch.setattr(
        cont, "wrapup_freshness", lambda _n: type("F", (), {"fresh": True})()
    )
    state = cont.read_pr_state(ignored_repo)
    assert state is not None
    assert state.ci == "CI-not-run-1-skipped-draft-PR"


def test_read_pr_state_no_pr_is_none_fields_and_other_errors_are_unreadable(
    ignored_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``no pull requests found`` means no PR; any other failure means unknown."""
    _fake_gh(
        monkeypatch,
        returncode=1,
        stdout="",
        stderr='no pull requests found for branch "x"',
    )
    state = cont.read_pr_state(ignored_repo)
    assert state is not None
    assert state.number == "none"
    _fake_gh(monkeypatch, returncode=1, stdout="", stderr="HTTP 502")
    assert cont.read_pr_state(ignored_repo) is None
