"""Tests for ``forge-metrics bench`` (:mod:`forge.pr_metrics_bench`).

# MOCKING STRATEGY: every external command (gh, pytest, the forge CLIs run
# in the clone) goes through the module's ``Runner`` seam, replaced by
# ``FakeRunner``. The sandbox is a real bare git repo in ``tmp_path``:
# ``gh repo clone`` is answered with a real ``git clone`` of it, so forge's
# real commit / push / tag / lease-delete seams run against it. The
# metrics database is a real SQLite file.
"""

from __future__ import annotations

import sqlite3
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
    Marker,
    Sandbox,
    Seams,
    bench_data,
    finish_run,
    init_sandbox,
    load_tasks,
    materialise_sample,
    read_marker,
    read_sandbox,
    start_run,
    write_sandbox,
)
from forge.pr_metrics_report import load_bench_rows, render_bench_report
from tests.conftest import GIT_ENV, git_stdout


if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

SLUG = "someone/bench-sandbox"
OWNED = '{"private": true, "permissions": {"admin": true}}'


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
    """No machine git config, data dir in *tmp_path*, no transcripts."""
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
    """The sandbox as ``init`` leaves it, recorded in ``bench.json``."""
    work = tmp_path / "seed"
    work.mkdir()
    _git(work, "init", "--quiet", "-b", "main")
    materialise_sample(data / "sample", work)
    _git(work, "add", "-A")
    _git(work, "commit", "--quiet", "-m", "seed")
    _git(work, "tag", "-a", "-m", "base", "bench-base-1")
    _git(work, "remote", "add", "origin", str(bare_origin))
    _git(work, "push", "--quiet", "origin", "main", "bench-base-1")
    write_sandbox(Sandbox(SLUG))
    return bare_origin


def _matches(prefix: tuple[str, ...], argv: list[str]) -> bool:
    """Whether *argv* starts with *prefix*, or names its one word as program/module.

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

    def __call__(
        self, argv: Sequence[str], *, cwd: Path, timeout: float | None = None
    ) -> Completed:
        """Record *argv* and answer it.

        Args:
            argv: The command line to record and match.
            cwd: Directory the command would run in; clones land here.
            timeout: Ignored; present to match the real runner signature.

        Returns:
            The first matching canned answer, else a success.
        """
        del timeout
        argv = list(argv)
        self.calls.append(argv)
        if argv[:3] == ["gh", "repo", "clone"]:
            _git(cwd, "clone", "--quiet", str(self.origin), argv[4])
            return Completed(0)
        for prefix, result in self.answers:
            if _matches(prefix, argv):
                return result
        return Completed(0)

    def ran(self, *prefix: str) -> list[list[str]]:
        """Calls starting with *prefix*.

        Args:
            *prefix: Leading words a recorded call must start with.

        Returns:
            The recorded calls that match.
        """
        return [c for c in self.calls if tuple(c[: len(prefix)]) == prefix]


def _owned(origin: Path, *extra: tuple[tuple[str, ...], Completed]) -> FakeRunner:
    """A runner whose ``gh`` sees an owned private sandbox.

    Args:
        origin: The bare sandbox.
        *extra: Answers checked first.

    Returns:
        The runner.
    """
    access = (("gh", "api", f"repos/{SLUG}"), Completed(0, OWNED))
    return FakeRunner(origin, answers=[*extra, access])


# --- package data ----------------------------------------------------------


def test_package_data_holds_every_file_the_tasks_name(data: Path) -> None:
    """Behavior: five tasks, each with checks, every referenced file shipped."""
    tasks = load_tasks(data)
    assert sorted(tasks) == ["add-command", "docs", "fix-bug", "refactor", "review"]
    assert (data / "workflow.md").is_file()
    for task in tasks.values():
        assert task.checks, task.id
        for rel in task.data_files():
            assert (data / "files" / rel).is_file(), (task.id, rel)


# --- init ------------------------------------------------------------------


@pytest.mark.parametrize(
    "case",
    [
        ('{"private": false, "permissions": {"admin": true}}', "0", "not private"),
        ('{"private": true, "permissions": {"admin": false}}', "0", "not yours"),
        (OWNED, "2", "not empty"),
    ],
    ids=["public", "foreign", "non-empty"],
)
def test_init_refuses_a_public_foreign_or_non_empty_repo(
    bare_origin: Path,
    data: Path,
    tmp_path: Path,
    case: tuple[str, str, str],
) -> None:
    """Behavior: init only ever seeds an empty private owned repo; nothing pushed.

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
    runner = _owned(
        bare_origin, (("gh", "api", f"repos/{SLUG}/branches"), Completed(0, "0"))
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


# --- start -----------------------------------------------------------------


def _start(
    data: Path, runner: FakeRunner, clone: Path, task: str = "fix-bug"
) -> tuple[Marker, str]:
    """Start *task* in *clone* with the sandbox slug check satisfied.

    Args:
        data: The bench data directory.
        runner: Command runner.
        clone: The clone.
        task: Task id.

    Returns:
        ``start_run``'s marker and prompt.
    """
    return start_run(
        load_tasks(data)[task],
        data=data,
        first_n=1,
        clone=clone,
        seams=Seams(runner, lambda _p: SLUG),
    )


def test_start_branches_from_the_base_tag_and_prints_the_prompt(
    seeded_origin: Path, data: Path, tmp_path: Path
) -> None:
    """Behavior: fresh branch at the base tag, setup files written, marker saved."""
    clone = tmp_path / "clone"
    marker, prompt = _start(data, _owned(seeded_origin), clone)
    assert marker.branch == "bench/fix-bug-1"
    assert _git(clone, "branch", "--show-current") == "bench/fix-bug-1"
    assert _git(clone, "rev-parse", "HEAD") == _git(
        clone, "rev-parse", "bench-base-1^{commit}"
    )
    assert (clone / "tests/test_top_words_case.py").is_file()
    assert "bench/fix-bug-1" in prompt
    assert "top_words" in prompt
    assert read_marker() == marker
    with pytest.raises(BenchError, match="finish"):
        _start(data, _owned(seeded_origin), clone)


def test_start_refuses_a_dirty_clone_and_keeps_the_change(
    seeded_origin: Path, data: Path, tmp_path: Path
) -> None:
    """Behavior: uncommitted work is never discarded; no branch, no marker."""
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "--quiet", str(seeded_origin), str(clone))
    (clone / "README.md").write_text("mine\n", encoding="utf-8")
    with pytest.raises(BenchError, match="uncommitted"):
        _start(data, _owned(seeded_origin), clone)
    assert (clone / "README.md").read_text(encoding="utf-8") == "mine\n"
    assert _git(clone, "branch", "--show-current") == "main"
    assert read_marker() is None


def test_start_refuses_a_public_sandbox(
    seeded_origin: Path, data: Path, tmp_path: Path
) -> None:
    """Behavior: access is re-checked at every command, not only at init."""
    runner = FakeRunner(
        seeded_origin,
        answers=[(("gh", "api"), Completed(0, '{"private": false}'))],
    )
    with pytest.raises(BenchError, match="not private"):
        _start(data, runner, tmp_path / "clone")
    assert not runner.ran("gh", "repo", "clone")


# --- finish ----------------------------------------------------------------


def test_finish_records_closes_the_pr_and_deletes_the_branch(
    seeded_origin: Path, data: Path, tmp_path: Path
) -> None:
    """Behavior: run recorded with facts and version; PR closed; branch gone.

    SCENARIO: the session committed a fix, pushed its branch, opened PR 7.
    MOCK SETUP: gh reports PR 7 with one wrap-up; pre-commit verdict fails;
        tests pass; the clone's .venv holds forge-scripts 11.2.0.
    EXPECTED BEHAVIOR: one row naming 11.2.0, PR opened, verdict 0; PR 7
        closed; remote branch deleted; hidden check files removed; marker gone;
        nothing in the database names the sandbox, branch or path.
    """
    clone = tmp_path / "clone"
    marker, _ = _start(data, _owned(seeded_origin), clone)
    core = clone / "src/tally/core.py"
    core.write_text(core.read_text(encoding="utf-8") + "\n# fixed\n", encoding="utf-8")
    _git(clone, "add", "-A")
    _git(clone, "commit", "--quiet", "-m", "fix")
    _git(clone, "push", "--quiet", "origin", f"HEAD:refs/heads/{marker.branch}")
    site = clone / ".venv/lib/python3.12/site-packages/forge_scripts-11.2.0.dist-info"
    site.mkdir(parents=True)
    runner = _owned(
        seeded_origin,
        (("gh", "pr", "list"), Completed(0, "7\n")),
        (("gh", "api", "--paginate"), Completed(0, "1\n")),
        (("forge.precommit",), Completed(1)),
    )
    db = tmp_path / "m.sqlite"
    conn = pr_metrics.open_db(db, "00" * 32)
    outcome = finish_run(
        data=data,
        record=lambda o: pr_metrics.record_bench_run(conn, o),
        seams=Seams(runner, lambda _p: SLUG),
    )
    assert outcome.version == "11.2.0"
    assert (outcome.pr_opened, outcome.wrapups_posted) == (True, 1)
    assert (outcome.tests_green, outcome.precommit_ok) == (True, False)
    assert outcome.checks_total == 2
    assert outcome.commits == 1
    assert runner.ran("gh", "pr", "close", "7")
    assert not _git(seeded_origin, "branch", "--list", marker.branch)
    assert not list((clone / "tests").glob("test_bench_hidden_*"))
    assert read_marker() is None
    row = conn.execute(
        "SELECT forge_version, pr_opened, precommit_ok FROM bench_runs"
    ).fetchone()
    assert row == ("11.2.0", 1, 0)
    dump = "\n".join(conn.iterdump())
    for name in (SLUG, "someone", "bench-sandbox", marker.branch, str(tmp_path)):
        assert name not in dump


def test_finish_refuses_a_clone_of_another_repo(
    seeded_origin: Path, data: Path, tmp_path: Path
) -> None:
    """Behavior: --clone pointing elsewhere records, closes and deletes nothing."""
    _start(data, _owned(seeded_origin), tmp_path / "clone")
    other = tmp_path / "other"
    other.mkdir()
    runner = _owned(seeded_origin, (("gh", "pr", "list"), Completed(0, "7\n")))
    recorded: list[BenchOutcome] = []
    with pytest.raises(BenchError, match="not a clone of the sandbox"):
        finish_run(
            data=data,
            record=recorded.append,
            clone=other,
            seams=Seams(runner, lambda _p: "someone/elsewhere"),
        )
    assert recorded == []
    assert runner.calls == []
    assert read_marker() is not None


def test_finish_without_a_started_run_is_refused(data: Path) -> None:
    """Behavior: finish needs start's marker; nothing is recorded."""
    recorded: list[BenchOutcome] = []
    with pytest.raises(BenchError, match="no run started"):
        finish_run(data=data, record=recorded.append)
    assert recorded == []


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
            base_tag="bench-base-1",
            started_at=f"2026-01-0{run_n}T00:00:00+00:00",
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
    for version, values in (("1.10.0", (100, 300)), ("1.2.0", (40, 60))):
        for n, new in enumerate(values, 1):
            _record(conn, version, n, new)
    text = render_bench_report(load_bench_rows(conn))
    lines = text.splitlines()
    assert lines[0] == "task docs"
    first, second = lines[2].split(), lines[3].split()
    assert first[:3] == ["1.2.0", "2", "50"]
    assert second[:4] == ["1.10.0", "2", "200", "50%"]
    assert "1/2" in second
    assert "6/6" in second
    assert "pass" not in text.lower()
    assert "fail" not in text.lower()
