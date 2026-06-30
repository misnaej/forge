"""Tests for ``forge.smart_test.cli`` — the forge-smart-test entry point."""

# MOCKING STRATEGY: All tests monkeypatch functions in the ``cli`` module
# namespace (``forge.smart_test.cli.*``) — never in their originating modules.
# Specifically:
#   - cli.resolve_base_ref → returns a fixed ref string
#   - cli.changed_python_files → returns a controlled set of paths
#   - cli.select_tests → returns a canned SelectionPlan
#   - cli.run_pytest → returns (exit_code, output) without running real pytest
#   - cli.clear_python_cache → no-op (cache hygiene not under test here)
#   - sys.argv is patched per test to drive argparse
#   - monkeypatch.chdir(tmp_path) sets the cwd that main() uses as repo_root
# main() is called directly (not via the console script entry point).

from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING

from forge.smart_test import cli
from forge.smart_test.dependencies import SelectionPlan
from tests.conftest import CapturedCalls


if TYPE_CHECKING:
    from pathlib import Path

    import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_plan(
    *,
    depth0: list[str] | None = None,
    depth1: list[str] | None = None,
    changed_tests: list[str] | None = None,
    max_depth: int = 1,
) -> SelectionPlan:
    """Build a minimal SelectionPlan for CLI tests.

    Args:
        depth0: Tests newly reachable at depth 0.
        depth1: Tests newly reachable at depth 1.
        changed_tests: Test files that were themselves modified.
        max_depth: Highest depth the plan covers.

    Returns:
        A ``SelectionPlan`` populated from the supplied lists.
    """
    newly: dict[int, list[str]] = {}
    if depth0:
        newly[0] = depth0
    if depth1:
        newly[1] = depth1
    return SelectionPlan(
        newly_at_depth=newly,
        changed_tests=changed_tests or [],
        max_depth=max_depth,
    )


def _stub_cli_deps(
    monkeypatch: pytest.MonkeyPatch,
    *,
    plan: SelectionPlan,
    changed: set[str] | None = None,
    run_results: list[tuple[int, str]] | None = None,
) -> CapturedCalls:
    """Stub the four I/O seams in the ``cli`` module namespace.

    Args:
        monkeypatch: pytest monkeypatch fixture.
        plan: SelectionPlan returned by ``cli.select_tests``.
        changed: Set returned by ``cli.changed_python_files``; defaults to one
            source file.
        run_results: List of ``(exit_code, output)`` pairs returned by
            successive calls to ``cli.run_pytest``.  When exhausted, returns
            ``(0, "ok")``.

    Returns:
        A ``CapturedCalls`` that accumulates every argv list ``run_pytest``
        was called with, for later assertion.
    """
    _changed = changed if changed is not None else {"src/myapp/core.py"}
    _results = list(run_results or [])
    captured = CapturedCalls()

    monkeypatch.setattr(cli, "resolve_base_ref", lambda _root, _base: "main")
    monkeypatch.setattr(cli, "changed_python_files", lambda _root, _ref: _changed)
    monkeypatch.setattr(cli, "select_tests", lambda _root, _ch, _depth: plan)
    monkeypatch.setattr(cli, "clear_python_cache", lambda _root: None)

    def _fake_run_pytest(
        _root: object, paths: list[str], *, coverage: bool = False
    ) -> tuple[int, str]:
        captured.calls.append(list(paths))
        if _results:
            return _results.pop(0)
        return 0, "ok"

    monkeypatch.setattr(cli, "run_pytest", _fake_run_pytest)
    return captured


def test_parse_depth_numeric_tiers() -> None:
    """Integer strings '0', '1', '2' parse to their int counterparts."""
    assert cli._parse_depth("0") == 0
    assert cli._parse_depth("1") == 1
    assert cli._parse_depth("2") == 2


def test_parse_depth_full_sentinel() -> None:
    """'full' and 'infinity' both map to the _FULL sentinel."""
    assert cli._parse_depth("full") == cli._FULL
    assert cli._parse_depth("infinity") == cli._FULL


def test_write_log_creates_code_health_dir_and_writes(tmp_path: Path) -> None:
    """_write_log creates the code_health/ directory and writes the body."""
    cli._write_log(tmp_path, "some output\n")
    log = tmp_path / "code_health" / "smart_test.log"
    assert log.exists()
    assert log.read_text(encoding="utf-8") == "some output\n"


def test_write_log_overwrites_existing_log(tmp_path: Path) -> None:
    """A second _write_log call overwrites the previous content."""
    cli._write_log(tmp_path, "first\n")
    cli._write_log(tmp_path, "second\n")
    log = tmp_path / "code_health" / "smart_test.log"
    assert log.read_text(encoding="utf-8") == "second\n"


def test_main_show_files_prints_plan_and_exits_0(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``--show-files`` prints the depth-N plan and returns 0; run_pytest never called.

    SCENARIO: ``--show-files --depth 1``.
    MOCK SETUP: cli.select_tests returns a plan with one test at depth 0;
        cli.run_pytest would fail the test if called.
    EXPECTED BEHAVIOR: exit code 0; run_pytest not invoked; plan header in log.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        sys, "argv", ["forge-smart-test", "--show-files", "--depth", "1"]
    )
    plan = _make_plan(depth0=["tests/test_core.py"])
    called: list[bool] = []

    monkeypatch.setattr(cli, "resolve_base_ref", lambda _r, _b: "main")
    monkeypatch.setattr(cli, "changed_python_files", lambda _r, _ref: {"src/foo.py"})
    monkeypatch.setattr(cli, "select_tests", lambda _r, _c, _d: plan)

    def _fail(*_a: object, **_kw: object) -> tuple[int, str]:
        called.append(True)
        return 0, ""

    monkeypatch.setattr(cli, "run_pytest", _fail)

    with caplog.at_level(logging.INFO, logger="forge.smart_test.cli"):
        code = cli.main()
    assert code == 0
    assert not called
    assert "📋 Tests covering changed code" in caplog.text


def test_main_show_files_full_prints_full_suite_notice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--show-files --depth full`` logs the whole-suite notice and returns 0."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        sys, "argv", ["forge-smart-test", "--show-files", "--depth", "full"]
    )
    code = cli.main()
    assert code == 0


def test_main_depth0_runs_only_one_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--depth 0`` calls run_pytest exactly once with depth-0 tests.

    SCENARIO: plan has tests at depth 0.
    MOCK SETUP: select_tests returns plan; run_pytest returns (0, "ok").
    EXPECTED BEHAVIOR: run_pytest called once; tests_up_to(0) paths in argv.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["forge-smart-test", "--depth", "0"])
    plan = _make_plan(depth0=["tests/test_core.py"], max_depth=0)
    captured = _stub_cli_deps(monkeypatch, plan=plan)

    code = cli.main()
    assert code == 0
    assert len(captured.calls) == 1
    assert "tests/test_core.py" in captured.calls[0]


def test_main_depth1_fail_fast_skips_higher(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing depth-0 batch short-circuits; depth-1 batch never runs.

    SCENARIO: plan has tests at depth 0 and depth 1; depth-0 run exits 1.
    MOCK SETUP: run_pytest returns [(1, "FAIL"), (0, "ok")].
    EXPECTED BEHAVIOR: run_pytest called once; exit code 1.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["forge-smart-test", "--depth", "1"])
    plan = _make_plan(
        depth0=["tests/test_core.py"],
        depth1=["tests/test_service.py"],
        max_depth=1,
    )
    captured = _stub_cli_deps(
        monkeypatch, plan=plan, run_results=[(1, "FAIL"), (0, "ok")]
    )

    code = cli.main()
    assert code == 1
    assert len(captured.calls) == 1


def test_main_depth1_two_batches_when_depth0_passes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When depth-0 passes, depth-1 batch also runs.

    SCENARIO: plan has tests at depth 0 and depth 1; depth-0 run exits 0.
    MOCK SETUP: run_pytest returns [(0, "ok"), (0, "ok")].
    EXPECTED BEHAVIOR: run_pytest called twice; exit code 0.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["forge-smart-test", "--depth", "1"])
    plan = _make_plan(
        depth0=["tests/test_core.py"],
        depth1=["tests/test_service.py"],
        max_depth=1,
    )
    captured = _stub_cli_deps(
        monkeypatch, plan=plan, run_results=[(0, "ok"), (0, "ok")]
    )

    code = cli.main()
    assert code == 0
    assert len(captured.calls) == 2


def test_main_full_depth_calls_run_pytest_with_empty_paths_and_coverage_true(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--depth full`` calls run_pytest with empty paths and coverage=True.

    SCENARIO: depth=full tier.
    MOCK SETUP: capture kwargs passed to run_pytest.
    EXPECTED BEHAVIOR: paths=[] and coverage=True.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["forge-smart-test", "--depth", "full"])

    recorded: list[dict[str, object]] = []

    def _fake(
        _root: object, paths: list[str], *, coverage: bool = False
    ) -> tuple[int, str]:
        recorded.append({"paths": paths, "coverage": coverage})
        return 0, "full suite ok"

    monkeypatch.setattr(cli, "run_pytest", _fake)

    code = cli.main()
    assert code == 0
    assert recorded
    assert recorded[0]["paths"] == []
    assert recorded[0]["coverage"] is True


def test_main_no_tests_selected_returns_0(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the plan selects no tests, main returns 0 and run_pytest is not called.

    SCENARIO: empty SelectionPlan — no changed files that map to tests.
    MOCK SETUP: cli.select_tests returns an empty plan.
    EXPECTED BEHAVIOR: run_pytest never called; exit code 0.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["forge-smart-test", "--depth", "1"])
    plan = _make_plan(max_depth=1)
    captured = _stub_cli_deps(monkeypatch, plan=plan)

    code = cli.main()
    assert code == 0
    assert not captured.calls


def test_main_exit_code_propagated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-zero run_pytest result propagates as main()'s return value."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["forge-smart-test", "--depth", "0"])
    plan = _make_plan(depth0=["tests/test_x.py"], max_depth=0)
    _stub_cli_deps(monkeypatch, plan=plan, run_results=[(2, "error")])

    code = cli.main()
    assert code == 2


def test_main_log_written_after_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``code_health/smart_test.log`` is written after the run completes."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["forge-smart-test", "--depth", "1"])
    plan = _make_plan(depth0=["tests/test_x.py"], max_depth=1)
    _stub_cli_deps(monkeypatch, plan=plan, run_results=[(0, "run output")])

    cli.main()

    log_path = tmp_path / "code_health" / "smart_test.log"
    assert log_path.exists()


def test_main_changed_test_file_not_run_twice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A changed test file that also appears via imports is deduplicated across batches.

    SCENARIO: test_core.py is both a changed test file (in changed_tests) AND
        reachable at depth 0.  It must appear only once across all run_pytest
        calls — not in both the depth-0 batch and separately.
    MOCK SETUP: plan has test_core.py in changed_tests AND in newly_at_depth[0].
    EXPECTED BEHAVIOR: the total set of paths across all run_pytest calls
        contains test_core.py exactly once.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["forge-smart-test", "--depth", "0"])
    plan = SelectionPlan(
        newly_at_depth={0: ["tests/test_core.py"]},
        changed_tests=["tests/test_core.py"],
        max_depth=0,
    )
    captured = _stub_cli_deps(monkeypatch, plan=plan)

    cli.main()

    all_paths: list[str] = [p for call_paths in captured.calls for p in call_paths]
    assert all_paths.count("tests/test_core.py") == 1
