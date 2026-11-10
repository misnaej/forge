"""Tests for ``forge-metrics bench`` (:mod:`forge.pr_metrics_bench`).

# MOCKING STRATEGY: every external command (gh, pytest, the forge CLIs run
# in the clone) goes through the module's ``Runner`` seam, replaced by
# ``FakeRunner``. The sandbox is a real bare git repo in ``tmp_path``:
# ``gh repo clone`` is answered with a real ``git clone`` of it, so forge's
# real commit / push / tag / lease-delete seams run against it. The
# metrics database is a real SQLite file.
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from forge import pr_metrics, pr_metrics_bench
from forge.agent_profile_tokens import BranchSummary, TokenUsage
from forge.plugin_sync import TagLookup
from forge.pr_metrics_bench import (
    BenchError,
    BenchOutcome,
    Completed,
    Marker,
    RunRequest,
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
from tests.conftest import git_stdout


if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

pytestmark = pytest.mark.usefixtures("isolated_git_data")
SLUG = "someone/bench-sandbox"
OWNED = '{"private": true, "permissions": {"admin": true}}'
FORGE = "11.2.0"
TAGGED = TagLookup(reached=True, commit="a" * 40)


def _venv_holds(clone: Path, version: str) -> None:
    """Make *clone*'s ``.venv`` look like it holds forge-scripts *version*.

    Args:
        clone: The clone.
        version: Forge release.
    """
    site = clone / ".venv/lib/python3.12/site-packages"
    (site / f"forge_scripts-{version}.dist-info").mkdir(parents=True, exist_ok=True)
    (clone / ".venv/bin").mkdir(exist_ok=True)
    (clone / ".venv/bin/forge-plugin-sync").touch()


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
def no_transcripts(monkeypatch: pytest.MonkeyPatch) -> None:
    """No transcripts: token figures stay out of these tests."""
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
    #: Forge release a successful ``pip install`` leaves in ``<cwd>/.venv``.
    installs: str = FORGE

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
        if argv[1:3] == ["-m", "venv"]:
            shutil.rmtree(argv[-1], ignore_errors=True)
        if argv[1:4] == ["-m", "pip", "install"]:
            _venv_holds(cwd, self.installs)
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
    data: Path,
    runner: FakeRunner,
    clone: Path,
    forge: str = FORGE,
    tag: TagLookup = TAGGED,
) -> tuple[Marker, str]:
    """Start the fix-bug task in *clone* with the sandbox slug check satisfied.

    Args:
        data: The bench data directory.
        runner: Command runner.
        clone: The clone.
        forge: The ``--forge`` value.
        tag: What GitHub says about forge's release tag.

    Returns:
        ``start_run``'s marker and prompt.
    """
    return start_run(
        load_tasks(data)["fix-bug"],
        RunRequest(forge=forge, first_n=1, clone=clone),
        data=data,
        seams=Seams(runner, lambda _p: SLUG, lambda _v: tag),
    )


def _installs(runner: FakeRunner) -> list[list[str]]:
    """The venv and pip commands *runner* saw.

    Args:
        runner: The runner.

    Returns:
        Those calls, in order.
    """
    return [c for c in runner.calls if c[1:3] in (["-m", "venv"], ["-m", "pip"])]


def test_start_branches_from_the_base_tag_and_prints_the_prompt(
    seeded_origin: Path, data: Path, tmp_path: Path
) -> None:
    """Behavior: fresh branch at the base tag, setup files written, marker saved."""
    clone = tmp_path / "clone"
    marker, prompt = _start(data, _owned(seeded_origin), clone)
    assert marker.branch == "bench/fix-bug-1"
    assert _git(clone, "branch", "--show-current") == "bench/fix-bug-1"
    assert _git(clone, "rev-parse", "HEAD^") == _git(
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


def test_start_pins_forge_in_a_setup_commit_on_the_branch(
    seeded_origin: Path, data: Path, tmp_path: Path
) -> None:
    """Behavior: the branch's one new commit pins the forge tag; marker names it."""
    clone = tmp_path / "clone"
    marker, _ = _start(data, _owned(seeded_origin), clone, forge="v11.2.0")
    assert marker.setup_commit == _git(clone, "rev-parse", "HEAD")
    assert marker.forge_version == FORGE
    assert _git(clone, "log", "-1", "--format=%s") == "chore(bench): pin forge v11.2.0"
    assert _git(clone, "diff", "--name-only", "HEAD^", "HEAD") == "pyproject.toml"
    pyproject = (clone / "pyproject.toml").read_text(encoding="utf-8")
    assert '"forge-scripts @ git+https://github.com/' in pyproject
    assert '.git@v11.2.0"' in pyproject
    assert read_marker() == marker


def test_start_builds_the_venv_when_missing(
    seeded_origin: Path, data: Path, tmp_path: Path
) -> None:
    """Behavior: no .venv → venv created, project installed with its dev extra."""
    clone = tmp_path / "clone"
    runner = _owned(seeded_origin)
    _start(data, runner, clone)
    venv = clone.resolve() / ".venv"
    assert _installs(runner) == [
        [sys.executable, "-m", "venv", "--clear", str(venv)],
        [str(venv / "bin/python"), "-m", "pip", "install", "-q", "-e", ".[dev]"],
    ]
    assert runner.ran(str(venv / "bin/forge-plugin-sync"))


def test_start_reuses_a_venv_holding_the_release(
    seeded_origin: Path, data: Path, tmp_path: Path
) -> None:
    """Behavior: .venv already on the release → no venv or pip command runs."""
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "--quiet", str(seeded_origin), str(clone))
    _venv_holds(clone, FORGE)
    runner = _owned(seeded_origin)
    marker, _ = _start(data, runner, clone)
    assert _installs(runner) == []
    assert marker.forge_version == FORGE


def test_start_rebuilds_a_venv_on_another_release(
    seeded_origin: Path, data: Path, tmp_path: Path
) -> None:
    """Behavior: .venv on another forge release is rebuilt for the requested one."""
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "--quiet", str(seeded_origin), str(clone))
    _venv_holds(clone, "10.0.0")
    runner = _owned(seeded_origin)
    _start(data, runner, clone)
    assert len(_installs(runner)) == 2
    site = clone / ".venv/lib/python3.12/site-packages"
    assert [p.name for p in site.iterdir()] == [f"forge_scripts-{FORGE}.dist-info"]


def test_start_refuses_a_symlinked_venv(
    seeded_origin: Path, data: Path, tmp_path: Path
) -> None:
    """Behavior: a .venv symlink is refused; its target is never cleared."""
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "--quiet", str(seeded_origin), str(clone))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "keep.txt").write_text("mine\n", encoding="utf-8")
    (clone / ".venv").symlink_to(elsewhere, target_is_directory=True)
    runner = _owned(seeded_origin)
    with pytest.raises(BenchError, match="symlink"):
        _start(data, runner, clone)
    assert _installs(runner) == []
    assert (elsewhere / "keep.txt").read_text(encoding="utf-8") == "mine\n"
    assert read_marker() is None


@pytest.mark.parametrize(
    "case",
    [
        ("11.2", TAGGED, "not a release"),
        ("11.2.0rc1", TAGGED, "not a release"),
        (FORGE, TagLookup(reached=True, commit=None), "no release tag v11.2.0"),
        (FORGE, TagLookup(reached=False, commit=None), "cannot ask GitHub"),
    ],
    ids=["short", "pre-release", "untagged", "unreachable"],
)
def test_start_refuses_a_bad_or_untagged_version(
    seeded_origin: Path,
    data: Path,
    tmp_path: Path,
    case: tuple[str, TagLookup, str],
) -> None:
    """Behavior: --forge must be a tagged release; refused before any clone change.

    Args:
        case: The ``--forge`` value, what GitHub says about the tag, and the
            expected refusal wording.
    """
    forge, tag, reason = case
    runner = _owned(seeded_origin)
    with pytest.raises(BenchError, match=reason):
        _start(data, runner, tmp_path / "clone", forge=forge, tag=tag)
    assert runner.calls == []
    assert read_marker() is None


def test_start_warns_and_continues_when_plugin_sync_fails(
    seeded_origin: Path,
    data: Path,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Behavior: plugin sync failing warns with the command to run; run starts."""
    runner = _owned(
        seeded_origin, (("forge-plugin-sync",), Completed(1, "", "no claude CLI"))
    )
    with caplog.at_level(logging.WARNING, logger=pr_metrics_bench.__name__):
        marker, _ = _start(data, runner, tmp_path / "clone")
    assert read_marker() == marker
    assert marker.plugin_synced is False
    assert "no claude CLI" in caplog.text
    assert "forge-plugin-sync" in caplog.text
    assert f"cd {marker.clone}" in caplog.text


# --- finish ----------------------------------------------------------------


def test_finish_records_closes_the_pr_and_deletes_the_branch(
    seeded_origin: Path, data: Path, tmp_path: Path
) -> None:
    """Behavior: run recorded with facts and version; PR closed; branch gone.

    SCENARIO: the session committed a fix, pushed its branch, opened PR 7.
    MOCK SETUP: gh reports PR 7 with one wrap-up; pre-commit verdict fails;
        tests pass; start installed forge-scripts 11.2.0 in the clone's .venv.
    EXPECTED BEHAVIOR: one row naming 11.2.0, PR opened, verdict 0; lines
        and commits counted after start's pin commit only; PR 7
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
    runner = _owned(
        seeded_origin,
        (("gh", "pr", "list"), Completed(0, "7\n")),
        (("gh", "api", "--paginate"), Completed(0, "1\n")),
        (("forge.precommit",), Completed(1)),
    )
    db = tmp_path / "m.sqlite"
    conn = pr_metrics.open_db(db, "00" * 32)
    shared: list[Path] = []
    outcome = finish_run(
        data=data,
        record=lambda o: pr_metrics.record_bench_run(conn, o),
        seams=Seams(runner, lambda _p: SLUG),
        share=shared.append,
    )
    assert shared == [clone.resolve()]
    assert outcome.version == "11.2.0"
    assert (outcome.pr_opened, outcome.wrapups_posted) == (True, 1)
    assert (outcome.tests_green, outcome.precommit_ok) == (True, False)
    assert outcome.checks_total == 2
    # core.py and the task's test file; the pin's pyproject.toml line swap
    # (one deletion) belongs to the setup commit and is not counted.
    assert (outcome.files, outcome.deletions) == (2, 0)
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


def test_finish_warns_when_start_did_not_sync_the_plugin(
    seeded_origin: Path,
    data: Path,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Behavior: a run started without its plugin is flagged before recording."""
    failing = (("forge-plugin-sync",), Completed(1, "", "no claude CLI"))
    _start(data, _owned(seeded_origin, failing), tmp_path / "clone")
    recorded: list[BenchOutcome] = []
    with caplog.at_level(logging.WARNING, logger=pr_metrics_bench.__name__):
        finish_run(
            data=data,
            record=recorded.append,
            seams=Seams(_owned(seeded_origin), lambda _p: SLUG),
        )
    assert [o.version for o in recorded] == [FORGE]
    assert f"could not install the forge {FORGE} plugin" in caplog.text


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
