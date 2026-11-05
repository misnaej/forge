"""Tests for forge.commit — guarded commit-and-push CLI."""

# MOCKING STRATEGY: git runs for real in tmp repos (conftest helpers). Faked
# at forge.commit's own import names: `verdict`, `active_state` (pre-commit and
# emergency evidence), `push_branch` (network), `time.sleep` (retry delay),
# `subagent_edits` / `render_edit_receipt` (real ledgers), `repo_root` (main).

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

import pytest

from forge import commit
from forge.commit import CommitRefusedError, Request
from tests.conftest import (
    GIT_ENV,
    commit_all,
    git_stdout,
    init_git_repo,
    make_fake_push_branch,
)


@pytest.fixture
def feature_repo(tmp_path: Path) -> Path:
    """Git repo on branch ``feat/x`` with two tracked files.

    Returns:
        The repo root.
    """
    init_git_repo(tmp_path)
    (tmp_path / "a.txt").write_text("a\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("b\n", encoding="utf-8")
    commit_all(tmp_path, "chore: seed")
    git_stdout(tmp_path, "checkout", "-q", "-b", "feat/x")
    return tmp_path


@pytest.fixture
def _fake_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    """Passing verdict, no emergency, inert edit-ledger."""
    monkeypatch.setattr(commit, "verdict", lambda _root: (True, []))
    monkeypatch.setattr(commit, "active_state", lambda _root: None)
    monkeypatch.setattr(commit, "subagent_edits", lambda *_a, **_k: [])
    monkeypatch.setattr(commit, "render_edit_receipt", lambda _edits: "receipt")


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record of ``time.sleep`` calls (sleeping is disabled)."""
    calls: list[float] = []
    monkeypatch.setattr(commit.time, "sleep", calls.append)
    return calls


def test_current_branch_on_main_raises(tmp_path: Path) -> None:
    """Committing on the protected base branch is refused."""
    init_git_repo(tmp_path)
    with pytest.raises(CommitRefusedError, match="protected"):
        commit.current_branch(tmp_path)


def test_current_branch_detached_raises(tmp_path: Path) -> None:
    """A detached HEAD has no branch to commit to and is refused."""
    init_git_repo(tmp_path)
    git_stdout(tmp_path, "checkout", "-q", "--detach")
    with pytest.raises(CommitRefusedError, match="detached"):
        commit.current_branch(tmp_path)


def test_current_branch_feature_returns_name(feature_repo: Path) -> None:
    """A feature branch is returned by name."""
    assert commit.current_branch(feature_repo) == "feat/x"


@pytest.mark.parametrize(
    "message",
    [
        "not conventional",
        "fix: " + " ".join(["w"] * 55),
        "fix: x\n\nCo-Authored-By: Claude <noreply@anthropic.com>",
    ],
    ids=["bad-title", "56-words", "ai-attribution"],
)
def test_check_message_refuses(message: str) -> None:
    """Non-conventional, over-long or AI-attributed messages are refused.

    Args:
        message: Commit message breaking one rule.
    """
    with pytest.raises(CommitRefusedError):
        commit.check_message(message)


def test_check_message_within_margin_passes_with_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A 52-word message is over the 50 target, under the ceiling: warn, pass."""
    with caplog.at_level(logging.WARNING):
        commit.check_message("fix: " + " ".join(["w"] * 51))
    assert "52 words" in caplog.text


def test_check_message_accepts_valid() -> None:
    """A conventional title with a body passes."""
    commit.check_message("fix(scope): handle empty input\n\nBecause it crashed.")


def test_check_verdict_fail_line_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    """A FAIL line in the verdict refuses the commit."""
    monkeypatch.setattr(commit, "verdict", lambda _r: (False, ["FAIL ruff"]))
    monkeypatch.setattr(commit, "active_state", lambda _r: None)
    with pytest.raises(CommitRefusedError, match="FAIL ruff"):
        commit.check_verdict(Path("unused-root"))


def test_check_verdict_stale_and_armed_passes(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A stale verdict passes with a warning while emergency is armed."""
    monkeypatch.setattr(commit, "verdict", lambda _r: (False, ["STALE ruff"]))
    monkeypatch.setattr(commit, "active_state", lambda _r: object())
    with caplog.at_level(logging.WARNING):
        commit.check_verdict(Path("unused-root"))
    assert "emergency armed" in caplog.text


def test_check_verdict_fail_and_armed_still_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real FAIL refuses even while emergency is armed."""
    monkeypatch.setattr(commit, "verdict", lambda _r: (False, ["FAIL mypy"]))
    monkeypatch.setattr(commit, "active_state", lambda _r: object())
    with pytest.raises(CommitRefusedError):
        commit.check_verdict(Path("unused-root"))


def test_check_verdict_stale_and_unarmed_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale verdict refuses when emergency is not armed."""
    monkeypatch.setattr(commit, "verdict", lambda _r: (False, ["STALE ruff"]))
    monkeypatch.setattr(commit, "active_state", lambda _r: None)
    with pytest.raises(CommitRefusedError):
        commit.check_verdict(Path("unused-root"))


def test_check_verdict_passing_returns(monkeypatch: pytest.MonkeyPatch) -> None:
    """A passing verdict returns without raising."""
    monkeypatch.setattr(commit, "verdict", lambda _r: (True, []))
    commit.check_verdict(Path("unused-root"))


def test_stage_exactly_no_paths_refuses(feature_repo: Path) -> None:
    """Staging with no named files is refused."""
    with pytest.raises(CommitRefusedError, match="name the files"):
        commit.stage_exactly(feature_repo, ())


def test_stage_exactly_prestaged_unrelated_refuses(feature_repo: Path) -> None:
    """Unrelated files already in the index are refused, not swept in."""
    (feature_repo / "a.txt").write_text("changed\n", encoding="utf-8")
    (feature_repo / "b.txt").write_text("changed\n", encoding="utf-8")
    git_stdout(feature_repo, "add", "b.txt")
    with pytest.raises(CommitRefusedError, match="already staged"):
        commit.stage_exactly(feature_repo, ("a.txt",))


def test_stage_exactly_unchanged_path_refuses(feature_repo: Path) -> None:
    """A named path with no change cannot be staged and is refused."""
    with pytest.raises(CommitRefusedError, match="do not match"):
        commit.stage_exactly(feature_repo, ("a.txt",))


def test_stage_exactly_stages_only_named(feature_repo: Path) -> None:
    """Only the named file is staged even when others changed."""
    (feature_repo / "a.txt").write_text("changed\n", encoding="utf-8")
    (feature_repo / "b.txt").write_text("changed\n", encoding="utf-8")
    commit.stage_exactly(feature_repo, ("a.txt",))
    assert git_stdout(feature_repo, "diff", "--cached", "--name-only").split() == [
        "a.txt"
    ]


@pytest.mark.usefixtures("_fake_evidence")
def test_commit_and_push_no_push_commits_only_requested(feature_repo: Path) -> None:
    """Commit-only mode records the named file and leaves others out."""
    (feature_repo / "a.txt").write_text("changed\n", encoding="utf-8")
    (feature_repo / "b.txt").write_text("changed\n", encoding="utf-8")
    request = Request("fix: change a", ("a.txt",), push=False)

    assert commit.commit_and_push(feature_repo, request) == commit.EXIT_OK

    assert git_stdout(
        feature_repo, "show", "--name-only", "--format=", "HEAD"
    ).split() == ["a.txt"]
    assert (
        git_stdout(feature_repo, "log", "-1", "--format=%s").strip() == "fix: change a"
    )


@pytest.mark.usefixtures("_fake_evidence")
def test_commit_and_push_pushes_when_requested(
    feature_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Push mode calls the push once after a successful commit."""
    calls: list[tuple[object, object, dict[str, object]]] = []
    monkeypatch.setattr(commit, "push_branch", make_fake_push_branch(calls=calls))
    (feature_repo / "a.txt").write_text("changed\n", encoding="utf-8")

    code = commit.commit_and_push(
        feature_repo, Request("fix: a", ("a.txt",), push=True)
    )

    assert code == commit.EXIT_OK
    assert len(calls) == 1


@pytest.mark.usefixtures("_fake_evidence")
def test_commit_and_push_committed_paths_mismatch_refuses(
    feature_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SCENARIO: the commit ends up holding a file nobody asked for.

    MOCK SETUP: ``committed_paths`` reports an extra path; push is a fake
    that fails the test if reached.
    EXPECTED BEHAVIOR: refused, and nothing pushed.
    """
    calls: list[tuple[object, object, dict[str, object]]] = []
    monkeypatch.setattr(commit, "push_branch", make_fake_push_branch(calls=calls))
    monkeypatch.setattr(commit, "committed_paths", lambda _r: {"a.txt", "extra.txt"})
    (feature_repo / "a.txt").write_text("changed\n", encoding="utf-8")

    with pytest.raises(CommitRefusedError, match="other files"):
        commit.commit_and_push(feature_repo, Request("fix: a", ("a.txt",), push=True))
    assert calls == []


@pytest.mark.usefixtures("_fake_evidence")
def test_commit_and_push_hook_staged_extra_is_allowed(
    feature_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SCENARIO: the pre-commit hook adds a generated doc to the commit.

    MOCK SETUP: ``committed_paths`` reports the named file plus
    ``docs/api-digest.md`` (a member of ``HOOK_STAGED_PATHS``).
    EXPECTED BEHAVIOR: the commit is accepted and the exit code is OK.
    """
    monkeypatch.setattr(
        commit, "committed_paths", lambda _r: {"a.txt", "docs/api-digest.md"}
    )
    (feature_repo / "a.txt").write_text("changed\n", encoding="utf-8")

    code = commit.commit_and_push(
        feature_repo, Request("fix: a", ("a.txt",), push=False)
    )

    assert code == commit.EXIT_OK


@pytest.mark.usefixtures("_fake_evidence")
def test_commit_and_push_named_file_missing_from_commit_refuses(
    feature_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SCENARIO: a requested file is absent from the resulting commit.

    MOCK SETUP: ``committed_paths`` reports an empty set.
    EXPECTED BEHAVIOR: refused with the "other files" message.
    """
    monkeypatch.setattr(commit, "committed_paths", lambda _r: set())
    (feature_repo / "a.txt").write_text("changed\n", encoding="utf-8")

    with pytest.raises(CommitRefusedError, match="other files"):
        commit.commit_and_push(feature_repo, Request("fix: a", ("a.txt",), push=False))


@pytest.mark.usefixtures("_fake_evidence")
def test_commit_and_push_blocking_hook_refuses(feature_repo: Path) -> None:
    """A failing pre-commit hook refuses the commit with a clear message."""
    hook = feature_repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    (feature_repo / "a.txt").write_text("changed\n", encoding="utf-8")

    with pytest.raises(CommitRefusedError, match="blocked"):
        commit.commit_and_push(feature_repo, Request("fix: a", ("a.txt",), push=False))


def test_push_transient_then_ok_retries(
    feature_repo: Path, monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    """SCENARIO: first push hits a transient GitHub error, second succeeds.

    MOCK SETUP: ``push_branch`` fake returns transient failure then success.
    EXPECTED BEHAVIOR: two attempts, one sleep, exit 0.
    """
    results = iter([("Permission denied (publickey)", False), ("", True)])
    attempts: list[int] = []

    def fake(_root: object, _branch: object, **_kw: object) -> object:
        stderr, ok = next(results)
        attempts.append(1)
        return make_fake_push_branch(ok=ok, stderr=stderr)(_root, _branch)

    monkeypatch.setattr(commit, "push_branch", fake)

    assert commit.push(feature_repo, "feat/x") == commit.EXIT_OK
    assert len(attempts) == 2
    assert sleeps == [commit.PUSH_RETRY_DELAY_S]


def test_push_non_fast_forward_not_retried(
    feature_repo: Path, monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    """A rejected non-fast-forward push fails once, with no retry."""
    calls: list[tuple[object, object, dict[str, object]]] = []
    monkeypatch.setattr(
        commit,
        "push_branch",
        make_fake_push_branch(
            ok=False, stderr="! [rejected] (non-fast-forward)", calls=calls
        ),
    )

    assert commit.push(feature_repo, "feat/x") == commit.EXIT_PUSH_FAILED
    assert len(calls) == 1
    assert sleeps == []


def test_push_all_transient_exhausts_attempts(
    feature_repo: Path, monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    """Persistent transient push errors use every attempt, then fail."""
    calls: list[tuple[object, object, dict[str, object]]] = []
    monkeypatch.setattr(
        commit,
        "push_branch",
        make_fake_push_branch(ok=False, stderr="Internal Server Error", calls=calls),
    )

    assert commit.push(feature_repo, "feat/x") == commit.EXIT_PUSH_FAILED
    assert len(calls) == commit.PUSH_ATTEMPTS
    assert len(sleeps) == commit.PUSH_ATTEMPTS - 1


def test_push_sets_upstream_only_when_missing(
    feature_repo: Path,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SCENARIO: branch without upstream, then the same branch with one.

    MOCK SETUP: local bare repo as ``origin`` supplies a real upstream;
    ``push_branch`` is a recording fake.
    EXPECTED BEHAVIOR: ``set_upstream`` True first, False once tracking.
    """
    origin = tmp_path_factory.mktemp("origin")
    subprocess.run(
        ["git", "init", "-q", "--bare", str(origin)], check=True, env=GIT_ENV
    )
    git_stdout(feature_repo, "remote", "add", "origin", str(origin))
    calls: list[tuple[object, object, dict[str, object]]] = []
    monkeypatch.setattr(commit, "push_branch", make_fake_push_branch(calls=calls))

    commit.push(feature_repo, "feat/x")
    git_stdout(feature_repo, "push", "-q", "-u", "origin", "feat/x")
    commit.push(feature_repo, "feat/x")

    assert [kw["set_upstream"] for _r, _b, kw in calls] == [True, False]


@pytest.fixture
def main_env(feature_repo: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """``feature_repo`` wired as ``main()``'s repo root.

    Returns:
        The repo root.
    """
    monkeypatch.setattr(commit, "repo_root", lambda: feature_repo)
    return feature_repo


@pytest.mark.parametrize(
    "argv",
    [["--push-only", "a.txt"], ["--push-only", "-m", "fix: x"], ["a.txt"]],
    ids=["push-only-with-path", "push-only-with-message", "missing-message"],
)
def test_main_bad_arguments_exit_refused(main_env: Path, argv: list[str]) -> None:
    """Invalid argument combinations exit with the refused code.

    Args:
        argv: Argument list ``main`` must refuse.
    """
    assert commit.main(argv) == commit.EXIT_REFUSED == 1


@pytest.mark.usefixtures("_fake_evidence")
def test_main_guard_refusal_exits_1(main_env: Path) -> None:
    """A guard refusal from main exits with the refused code."""
    assert commit.main(["-m", "not conventional", "a.txt"]) == commit.EXIT_REFUSED


@pytest.mark.usefixtures("_fake_evidence")
def test_main_success_exits_0(main_env: Path) -> None:
    """A clean commit from main exits zero."""
    (main_env / "a.txt").write_text("changed\n", encoding="utf-8")
    assert commit.main(["-m", "fix: a", "--no-push", "a.txt"]) == commit.EXIT_OK


@pytest.mark.usefixtures("_fake_evidence")
def test_main_ignores_inherited_git_index_file(
    main_env: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """An inherited ``GIT_INDEX_FILE`` cannot redirect where main stages.

    A stray index variable (as set inside a git hook) would make staging
    land in another index and the commit miss the named path; ``main``
    must strip it for its run and restore it afterwards.
    """
    foreign_index = str(tmp_path_factory.mktemp("foreign") / "other-index")
    monkeypatch.setenv("GIT_INDEX_FILE", foreign_index)
    (main_env / "a.txt").write_text("changed\n", encoding="utf-8")

    assert commit.main(["-m", "fix: x", "--no-push", "a.txt"]) == commit.EXIT_OK

    committed = git_stdout(main_env, "show", "--name-only", "--format=", "HEAD").split()
    assert committed == ["a.txt"]
    assert os.environ.get("GIT_INDEX_FILE") == foreign_index


@pytest.mark.usefixtures("_fake_evidence")
def test_main_push_failure_exits_2(
    main_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed push from main exits with the push-failed code."""
    monkeypatch.setattr(
        commit, "push_branch", make_fake_push_branch(ok=False, stderr="rejected")
    )
    (main_env / "a.txt").write_text("changed\n", encoding="utf-8")
    assert commit.main(["-m", "fix: a", "a.txt"]) == commit.EXIT_PUSH_FAILED == 2


def test_main_push_only_pushes(main_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Push-only mode pushes the current branch without committing."""
    calls: list[tuple[object, object, dict[str, object]]] = []
    monkeypatch.setattr(commit, "push_branch", make_fake_push_branch(calls=calls))
    assert commit.main(["--push-only"]) == commit.EXIT_OK
    assert len(calls) == 1
