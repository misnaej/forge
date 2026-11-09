"""Tests for sandbox-kept benchmark results (:mod:`forge.pr_metrics_bench_results`).

# MOCKING STRATEGY: the sandbox is a real bare repo; each "machine" is a
# real clone of it sitting on its bench branch. Commits, merges and pushes
# run through forge's real git seams. Only ``gh`` (the access check of
# ``sandbox_rows``) is a fake runner, and one test wraps ``push_branch`` to
# make another machine push first or to reject every push.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from forge import pr_metrics_bench_results as results
from forge.agent_profile_tokens import TokenUsage
from forge.git_utils import PushResult
from forge.pr_metrics_bench import BenchError, Completed, Sandbox, Seams, write_sandbox
from forge.pr_metrics_report import BenchRow, render_bench_report
from tests.conftest import GIT_ENV, git_stdout


if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

SLUG = "someone/bench-sandbox"
BENCH_BRANCH = "bench/docs-1"


def _git(cwd: Path, *args: str) -> str:
    """Shared test git runner, stripped.

    Args:
        cwd: Working directory.
        *args: Git arguments.

    Returns:
        Stripped stdout.
    """
    return git_stdout(cwd, *args).strip()


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No machine git config; data dir in *tmp_path*."""
    for name, value in GIT_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    """The sandbox: a bare repo whose main holds one seed commit."""
    bare = tmp_path / "origin.git"
    _git(tmp_path, "init", "--quiet", "--bare", "-b", "main", str(bare))
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "--quiet", "-b", "main")
    (seed / "README.md").write_text("sandbox\n", encoding="utf-8")
    _git(seed, "add", "-A")
    _git(seed, "commit", "--quiet", "-m", "seed")
    _git(seed, "remote", "add", "origin", str(bare))
    _git(seed, "push", "--quiet", "origin", "main")
    return bare


def _machine(origin: Path, tmp_path: Path, name: str) -> Path:
    """A clone of the sandbox on its bench branch with one committed change.

    Args:
        origin: The bare sandbox.
        tmp_path: Test directory.
        name: Clone directory name.

    Returns:
        The clone.
    """
    clone = tmp_path / name
    _git(tmp_path, "clone", "--quiet", str(origin), str(clone))
    _git(clone, "checkout", "--quiet", "-b", BENCH_BRANCH)
    (clone / "work.txt").write_text("bench work\n", encoding="utf-8")
    _git(clone, "add", "-A")
    _git(clone, "commit", "--quiet", "-m", "bench work")
    return clone


def _row(run_id: str, version: str = "1.0.0", new: int = 100) -> BenchRow:
    """One docs run.

    Args:
        run_id: Its id.
        version: Forge version.
        new: Fresh input tokens.

    Returns:
        The row.
    """
    return BenchRow(
        task_id="docs",
        forge_version=version,
        run_n=1,
        pr_opened=True,
        tests_green=True,
        precommit_ok=False,
        checks_passed=3,
        checks_total=3,
        review_rounds=1,
        additions=12,
        deletions=2,
        active_s=600.0,
        wall_s=900.0,
        tokens={"main": TokenUsage(input_tokens=new, cache_read_tokens=new)},
        run_id=run_id,
        model="claude-test",
        base_tag="bench-base-1",
        recorded_on="2026-11-09",
    )


def _remote_runs(origin: Path) -> dict[str, dict]:
    """The run entries on the sandbox's main.

    Args:
        origin: The bare sandbox.

    Returns:
        Run id → entry.
    """
    text = _git(origin, "show", f"main:{results.RESULTS_JSON}")
    return json.loads(text)["runs"]


def test_first_run_creates_the_results_files(origin: Path, tmp_path: Path) -> None:
    """Behavior: JSON and page committed on main in the clone; bench branch kept."""
    clone = _machine(origin, tmp_path, "a")
    head = _git(clone, "rev-parse", BENCH_BRANCH)
    assert results.share_results(clone, [_row("r1")])
    runs = _remote_runs(origin)
    assert list(runs) == ["r1"]
    assert runs["r1"]["forge_version"] == "1.0.0"
    assert runs["r1"]["tokens"]["main"]["input"] == 100
    page = _git(origin, "show", f"main:{results.RESULTS_HTML}")
    assert "task docs" in page
    assert "<script" not in page
    assert _git(clone, "branch", "--show-current") == "main"
    assert _git(clone, "rev-parse", "HEAD") == _git(origin, "rev-parse", "main")
    assert (clone / results.RESULTS_JSON).is_file()
    assert _git(clone, "rev-parse", BENCH_BRANCH) == head
    assert not _git(clone, "status", "--porcelain")
    assert _git(clone, "worktree", "list").count("\n") == 0


def test_dirty_clone_is_refused_and_kept(origin: Path, tmp_path: Path) -> None:
    """Behavior: uncommitted work is never discarded; results stay local."""
    clone = _machine(origin, tmp_path, "a")
    before = _git(origin, "rev-parse", "main")
    (clone / "draft.txt").write_text("uncommitted\n", encoding="utf-8")
    assert not results.share_results(clone, [_row("r1")])
    assert (clone / "draft.txt").read_text(encoding="utf-8") == "uncommitted\n"
    assert _git(clone, "branch", "--show-current") == BENCH_BRANCH
    assert _git(origin, "rev-parse", "main") == before


def test_entries_hold_no_machine_identifiers(origin: Path, tmp_path: Path) -> None:
    """Behavior: no path, branch or host-like field reaches the shared file."""
    clone = _machine(origin, tmp_path, "a")
    results.share_results(clone, [_row("r1")])
    text = _git(origin, "show", f"main:{results.RESULTS_JSON}")
    for name in (str(tmp_path), BENCH_BRANCH, "host", "user"):
        assert name not in text


def test_second_machine_appends_and_same_run_id_replaces(
    origin: Path, tmp_path: Path
) -> None:
    """Behavior: other machines' runs kept; re-sharing a run never duplicates it."""
    first, second = _machine(origin, tmp_path, "a"), _machine(origin, tmp_path, "b")
    assert results.share_results(first, [_row("r1")])
    assert results.share_results(second, [_row("r2", "1.1.0")])
    assert sorted(_remote_runs(origin)) == ["r1", "r2"]
    assert results.share_results(first, [_row("r1", new=500)])
    runs = _remote_runs(origin)
    assert sorted(runs) == ["r1", "r2"]
    assert runs["r1"]["tokens"]["main"]["input"] == 500


def test_rejected_push_is_retried_after_merging(
    origin: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Behavior: a push lost to another machine merges its runs and retries.

    SCENARIO: machine B pushes its results between A's commit and A's push.
    MOCK SETUP: push_branch wrapped so its first call lets B share first.
    EXPECTED BEHAVIOR: A's first push is rejected, the retry carries both runs.
    """
    first, second = _machine(origin, tmp_path, "a"), _machine(origin, tmp_path, "b")
    real_push = results.push_branch
    calls: list[bool] = []

    def racing_push(tree: Path, branch: str, **kwargs: object) -> PushResult:
        if not calls:
            monkeypatch.setattr(results, "push_branch", real_push)
            assert results.share_results(second, [_row("r2")])
            monkeypatch.setattr(results, "push_branch", racing_push)
        outcome = real_push(tree, branch, **kwargs)
        calls.append(outcome.ok)
        return outcome

    monkeypatch.setattr(results, "push_branch", racing_push)
    assert results.share_results(first, [_row("r1")])
    assert calls == [False, True]
    assert sorted(_remote_runs(origin)) == ["r1", "r2"]


def test_every_push_rejected_leaves_results_local(
    origin: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Behavior: after the retries the sandbox is unchanged and the caller told."""
    clone = _machine(origin, tmp_path, "a")
    before = _git(origin, "rev-parse", "main")
    attempts: list[str] = []

    def rejected(tree: Path, branch: str, **_kwargs: object) -> PushResult:
        attempts.append(branch)
        return PushResult(ok=False, returncode=1, stderr="rejected")

    monkeypatch.setattr(results, "push_branch", rejected)
    assert not results.share_results(clone, [_row("r1")])
    assert len(attempts) == results.PUSH_ATTEMPTS
    assert _git(origin, "rev-parse", "main") == before
    assert _git(clone, "branch", "--show-current") == "main"


def test_report_from_sandbox_shows_every_machine(origin: Path, tmp_path: Path) -> None:
    """Behavior: the sandbox's file is read back with both machines' runs."""
    first, second = _machine(origin, tmp_path, "a"), _machine(origin, tmp_path, "b")
    results.share_results(first, [_row("r1", "1.0.0")])
    results.share_results(second, [_row("r2", "1.1.0")])
    write_sandbox(Sandbox(SLUG))

    def gh(
        argv: Sequence[str], *, cwd: Path, timeout: float | None = None
    ) -> Completed:
        del argv, cwd, timeout
        return Completed(0, '{"private": true, "permissions": {"admin": true}}')

    rows = results.sandbox_rows(first, seams=Seams(gh, lambda _p: SLUG))
    assert sorted(r.run_id for r in rows) == ["r1", "r2"]
    text = render_bench_report(rows)
    assert "1.0.0" in text
    assert "1.1.0" in text


def test_page_escapes_shared_values() -> None:
    """Behavior: values from the shared file never become markup."""
    entry = results.merge_results({}, [_row("r1")])["r1"]
    entry["task_id"] = "<script>x</script>"
    page = results.results_files({"r1": entry})[results.RESULTS_HTML]
    assert "<script>" not in page
    assert "&lt;script&gt;" in page


def test_newer_schema_is_refused() -> None:
    """Behavior: a results file from a newer forge is not rewritten."""
    with pytest.raises(BenchError, match="schema"):
        results.parse_results(json.dumps({"schema": 99, "runs": {}}))
