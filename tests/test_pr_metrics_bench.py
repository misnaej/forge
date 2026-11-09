"""Tests for ``forge-metrics bench`` (:mod:`forge.pr_metrics_bench`).

# MOCKING STRATEGY: every external command (gh, claude, pip, venv, the
# forge clone, the version's own CLIs) goes through the module's ``Runner``
# seam, replaced by ``FakeRunner``. The sandbox is a real bare git repo in
# ``tmp_path``: ``gh repo clone`` is answered with a real ``git clone`` of
# it, so forge's real commit / push / tag / lease-delete seams run against
# it. The metrics database is a real SQLite file.
"""

from __future__ import annotations

import sqlite3
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from forge import pr_metrics, pr_metrics_bench
from forge.agent_profile_tokens import BranchSummary, TokenUsage
from forge.pr_metrics_bench import (
    BenchError,
    BenchOutcome,
    Completed,
    RunOptions,
    RunPlan,
    Sandbox,
    bench_data,
    init_sandbox,
    load_tasks,
    materialise_sample,
    plan_set,
    read_sandbox,
    run_set,
)
from forge.pr_metrics_report import load_bench_rows, render_bench_report
from tests.conftest import GIT_ENV


if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping, Sequence

SLUG = "someone/bench-sandbox"


def _git(cwd: Path, *args: str) -> str:
    """Run real git in *cwd* with the isolated test identity.

    Args:
        cwd: Working directory.
        *args: Git arguments.

    Returns:
        Stripped stdout.
    """
    proc = subprocess.run(
        ["git", *args], cwd=cwd, env=GIT_ENV, capture_output=True, text=True, check=True
    )
    return proc.stdout.strip()


@pytest.fixture(autouse=True)
def isolated_git(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point git at no machine config and the data directory at *tmp_path*."""
    for name, value in GIT_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setattr(pr_metrics_bench, "default_transcripts_dir", lambda _p: None)


@pytest.fixture
def data() -> Iterator[Path]:
    """The shipped bench data directory."""
    with bench_data() as path:
        yield path


@pytest.fixture
def bare_origin(tmp_path: Path) -> Path:
    """An empty bare repo standing in for the GitHub sandbox."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "--quiet", "--bare", "-b", "main", str(origin))
    return origin


@pytest.fixture
def seeded_origin(bare_origin: Path, tmp_path: Path, data: Path) -> Path:
    """The sandbox as ``init`` leaves it: the sample on ``main``, tagged."""
    work = tmp_path / "seed"
    work.mkdir()
    _git(work, "init", "--quiet", "-b", "main")
    materialise_sample(data / "sample", work)
    _git(work, "add", "-A")
    _git(work, "commit", "--quiet", "-m", "seed")
    _git(work, "tag", "-a", "-m", "base", "bench-base-1")
    _git(work, "remote", "add", "origin", str(bare_origin))
    _git(work, "push", "--quiet", "origin", "main", "bench-base-1")
    return bare_origin


def _matches(prefix: tuple[str, ...], argv: list[str]) -> bool:
    """Whether *argv* starts with *prefix*, or names its one word as program or module.

    Args:
        prefix: Leading words, or one program / ``-m`` module name.
        argv: The command.

    Returns:
        ``True`` on a match.
    """
    if tuple(argv[: len(prefix)]) == prefix:
        return True
    word = prefix[0]
    return len(prefix) == 1 and (Path(argv[0]).name == word or word in argv[1:3])


@dataclass
class FakeRunner:
    """Answers each command by its leading words; clones become real clones."""

    origin: Path
    #: ``(prefix, result)`` pairs, first match wins; unmatched commands succeed.
    answers: list[tuple[tuple[str, ...], Completed]] = field(default_factory=list)
    calls: list[list[str]] = field(default_factory=list)
    #: Run when ``claude`` is called (the agent's side effects).
    on_claude: Callable[[Path], None] | None = None

    def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> Completed:
        """Record *argv* and answer it.

        Args:
            argv: The command to run.
            cwd: Directory the command runs in.
            env: Ignored; accepted to match the runner signature.
            timeout: Ignored; accepted to match the runner signature.

        Returns:
            The first matching canned answer, else a successful result.
        """
        del env, timeout
        argv = list(argv)
        self.calls.append(argv)
        if argv[:3] == ["gh", "repo", "clone"]:
            dest = argv[4]
            _git(cwd, "clone", "--quiet", str(self.origin), dest)
            return Completed(0)
        if argv[:2] == ["git", "clone"]:
            Path(argv[-1]).mkdir(parents=True)
            return Completed(0)
        if argv[0] == "claude" and self.on_claude is not None:
            self.on_claude(cwd)
        for prefix, result in self.answers:
            if _matches(prefix, argv):
                return result
        return Completed(0)

    def ran(self, *prefix: str) -> list[list[str]]:
        """Calls starting with *prefix*.

        Args:
            *prefix: Leading words the recorded command must start with.

        Returns:
            The recorded commands that match.
        """
        return [c for c in self.calls if tuple(c[: len(prefix)]) == prefix]


# --- init ------------------------------------------------------------------


@pytest.mark.parametrize(
    "case",
    [
        ('{"private": false}', "0", "not private"),
        ('{"private": true}', "2", "not empty"),
    ],
    ids=["public", "non-empty"],
)
def test_init_refuses_a_public_or_non_empty_repo(
    bare_origin: Path,
    data: Path,
    tmp_path: Path,
    case: tuple[str, str, str],
) -> None:
    """Behavior: init only ever seeds an empty private repo; nothing is pushed.

    Args:
        case: What ``gh api repos/<slug>`` returns, the branch count ``gh``
            reports, and the expected refusal wording.
    """
    repo_json, branches, reason = case
    runner = FakeRunner(
        bare_origin,
        answers=[
            (("gh", "api", f"repos/{SLUG}/branches"), Completed(0, branches)),
            (("gh", "api", f"repos/{SLUG}"), Completed(0, repo_json)),
        ],
    )
    with pytest.raises(BenchError, match=reason):
        init_sandbox(SLUG, data=data, runner=runner, workdir=tmp_path)
    assert not runner.ran("gh", "repo", "clone")
    assert read_sandbox() is None


def test_init_seeds_main_and_the_base_tag(
    bare_origin: Path, data: Path, tmp_path: Path
) -> None:
    """Behavior: the sample lands on main, tagged bench-base-1, slug saved locally."""
    runner = FakeRunner(
        bare_origin,
        answers=[
            (("gh", "api", f"repos/{SLUG}/branches"), Completed(0, "0")),
            (("gh", "api", f"repos/{SLUG}"), Completed(0, '{"private": true}')),
        ],
    )
    sandbox = init_sandbox(SLUG, data=data, runner=runner, workdir=tmp_path)
    assert sandbox == Sandbox(SLUG, "bench-base-1")
    assert read_sandbox() == sandbox
    files = _git(bare_origin, "ls-tree", "-r", "--name-only", "bench-base-1")
    assert "src/tally/core.py" in files.splitlines()
    assert _git(bare_origin, "rev-parse", "main") == _git(
        bare_origin, "rev-parse", "bench-base-1^{commit}"
    )
    assert not list(tmp_path.glob("forge-bench-init-*"))


# --- run -------------------------------------------------------------------


def _plans(data: Path, *task_ids: str, runs: int = 1) -> list[RunPlan]:
    """Plans of *task_ids* against a fixed version.

    Args:
        data: The bench data directory.
        *task_ids: Tasks to plan.
        runs: Runs per task.

    Returns:
        The plans.
    """
    tasks = load_tasks(data)
    return plan_set(
        Sandbox(SLUG),
        [tasks[t] for t in task_ids],
        version="v9.9.9",
        runs=runs,
        run_options=RunOptions(None, 60),
    )


def test_timed_out_run_is_recorded_and_cleaned_up(
    seeded_origin: Path, data: Path, tmp_path: Path
) -> None:
    """Behavior: a claude timeout is a fact; PR closed, branch deleted, dir removed.

    SCENARIO: the agent pushes its branch and opens a PR, then hits the bound.
    MOCK SETUP: claude times out after pushing; gh reports PR 7 with one wrap-up;
        pre-commit verdict fails; tests pass.
    EXPECTED BEHAVIOR: one row with timed_out, pr_opened, setup_ok set; PR 7
        closed; the remote branch gone; no run directory left.
    """
    plan = _plans(data, "fix-bug")[0]

    def agent(clone: Path) -> None:
        _git(clone, "push", "--quiet", "origin", f"HEAD:refs/heads/{plan.branch}")

    runner = FakeRunner(
        seeded_origin,
        answers=[
            (("claude",), Completed(None, timed_out=True)),
            (("gh", "pr", "list"), Completed(0, "7\n")),
            (("gh", "api", "--paginate"), Completed(0, "1\n")),
            (("forge-precommit",), Completed(1)),
        ],
        on_claude=agent,
    )
    work = tmp_path / "runs"
    work.mkdir()
    conn = pr_metrics.open_db(tmp_path / "m.sqlite", "00" * 32)
    [outcome] = run_set(
        [plan],
        data=data,
        record=lambda o: pr_metrics.record_bench_run(conn, o),
        runner=runner,
        workdir=work,
    )
    assert outcome.setup_ok
    assert outcome.timed_out
    assert outcome.claude_exit is None
    assert outcome.pr_opened
    assert outcome.wrapups_posted == 1
    assert outcome.precommit_ok is False
    assert outcome.tests_green is True
    assert runner.ran("gh", "pr", "close", "7")
    assert not _git(seeded_origin, "branch", "--list", plan.branch)
    assert list(work.iterdir()) == []
    row = conn.execute(
        "SELECT timed_out, pr_opened, setup_ok FROM bench_runs"
    ).fetchone()
    assert row == (1, 1, 1)


def test_failed_setup_is_recorded_and_the_set_continues(
    seeded_origin: Path, data: Path, tmp_path: Path
) -> None:
    """Behavior: a run whose setup fails still records; the next run happens."""
    runner = FakeRunner(seeded_origin, answers=[(("venv",), Completed(1, "", "boom"))])
    recorded: list[BenchOutcome] = []
    outcomes = run_set(
        _plans(data, "docs", runs=2),
        data=data,
        record=recorded.append,
        runner=runner,
        workdir=tmp_path,
    )
    assert [o.setup_ok for o in outcomes] == [False, False]
    assert recorded == outcomes
    assert not runner.ran("claude")


def test_database_holds_no_sandbox_or_branch_name(tmp_path: Path, data: Path) -> None:
    """Behavior: nothing stored names the sandbox, its owner or the run branch."""
    plan = _plans(data, "review")[0]
    outcome = BenchOutcome(
        run_id="r1",
        version=plan.version,
        task_id=plan.task.id,
        run_n=1,
        model="claude-test",
        base_tag="bench-base-1",
        started_at="2026-01-01T00:00:00+00:00",
        setup_ok=True,
        summary=BranchSummary(tokens={"main": TokenUsage(input_tokens=5)}),
    )
    db = tmp_path / "m.sqlite"
    conn = pr_metrics.open_db(db, "00" * 32)
    pr_metrics.record_bench_run(conn, outcome)
    dump = "\n".join(conn.iterdump())
    conn.close()
    for name in (SLUG, "someone", "bench-sandbox", plan.branch, str(tmp_path)):
        assert name not in dump


# --- schema and report -----------------------------------------------------


def test_schema_3_database_gains_the_bench_tables(tmp_path: Path) -> None:
    """Behavior: opening a schema-3 database adds bench tables, keeps its rows."""
    db = tmp_path / "m.sqlite"
    conn = sqlite3.connect(db)
    conn.executescript(pr_metrics._SCHEMA.split("CREATE TABLE IF NOT EXISTS bench")[0])
    conn.execute("INSERT INTO prs (pr_key, repo_id) VALUES ('k', 'r')")
    conn.execute("PRAGMA user_version = 3")
    conn.commit()
    conn.close()
    conn = pr_metrics.open_db(db, "00" * 32)
    assert conn.execute("PRAGMA user_version").fetchone() == (
        pr_metrics.SCHEMA_VERSION,
    )
    assert conn.execute("SELECT COUNT(*) FROM prs").fetchone() == (1,)
    assert conn.execute("SELECT COUNT(*) FROM bench_runs").fetchone() == (0,)


def _record(conn: sqlite3.Connection, version: str, run_n: int, new: int) -> None:
    """Store one docs run of *version* with *new* fresh input tokens.

    Args:
        conn: The database.
        version: Forge version.
        run_n: Run number.
        new: Fresh input tokens.
    """
    pr_metrics.record_bench_run(
        conn,
        BenchOutcome(
            run_id=f"{version}-{run_n}",
            version=version,
            task_id="docs",
            run_n=run_n,
            model=None,
            base_tag="bench-base-1",
            started_at=f"2026-01-0{run_n}T00:00:00+00:00",
            setup_ok=True,
            pr_opened=run_n == 1,
            checks_passed=3,
            checks_total=3,
            additions=10 * run_n,
            summary=BranchSummary(
                tokens={"main": TokenUsage(input_tokens=new, cache_read_tokens=new)}
            ),
        ),
    )


def test_report_shows_versions_side_by_side_with_medians(tmp_path: Path) -> None:
    """Behavior: one line per version, release order, median tokens, fact counts."""
    conn = pr_metrics.open_db(tmp_path / "m.sqlite", "00" * 32)
    for version, values in (("v1.10.0", (100, 300)), ("v1.2.0", (40, 60))):
        for n, new in enumerate(values, 1):
            _record(conn, version, n, new)
    text = render_bench_report(load_bench_rows(conn))
    lines = text.splitlines()
    assert lines[0] == "task docs"
    first, second = lines[2].split(), lines[3].split()
    assert first[:3] == ["v1.2.0", "2", "50"]
    assert second[:4] == ["v1.10.0", "2", "200", "50%"]
    assert "1/2" in second
    assert "6/6" in second
    assert "pass" not in text.lower()
    assert "fail" not in text.lower()
