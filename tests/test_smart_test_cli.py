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

import fnmatch
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
        was called with, for later assertion. Also carries the ``telemetry``
        kwarg of each call, in call order, in its ``telemetry_flags`` field,
        and the ``label`` kwarg of each call in its ``labels`` field (#376).
    """
    _changed = changed if changed is not None else {"src/myapp/core.py"}
    _results = list(run_results or [])
    captured = CapturedCalls()

    monkeypatch.setattr(cli, "resolve_base_ref", lambda _root, _base: "main")
    monkeypatch.setattr(cli, "changed_python_files", lambda _root, _ref: _changed)
    monkeypatch.setattr(cli, "select_tests", lambda _root, _ch, _depth, **_kw: plan)
    monkeypatch.setattr(cli, "clear_python_cache", lambda _root: None)

    def _fake_run_pytest(
        _root: object,
        paths: list[str],
        *,
        coverage: bool = False,
        telemetry: bool = False,
        label: str = "",
    ) -> tuple[int, str]:
        captured.calls.append(list(paths))
        captured.telemetry_flags.append(telemetry)
        captured.labels.append(label)
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
    """'full' maps to the _FULL sentinel."""
    assert cli._parse_depth("full") == cli._FULL


def test_write_log_creates_code_health_dir_and_writes(tmp_path: Path) -> None:
    """_write_log creates the code_health/ directory and writes the body.

    Two sinks, same content: ``smart_test.log`` (precommit-fixer's input)
    and ``pytest.log`` (``forge-slow-tests-report``'s documented default).
    """
    cli._write_log(tmp_path, "some output\n")
    log = tmp_path / "code_health" / "smart_test.log"
    assert log.exists()
    assert log.read_text(encoding="utf-8") == "some output\n"
    pytest_log = tmp_path / "code_health" / "pytest.log"
    assert pytest_log.exists()
    assert pytest_log.read_text(encoding="utf-8") == "some output\n"


def test_write_log_overwrites_existing_log(tmp_path: Path) -> None:
    """A second _write_log call overwrites the previous content in both sinks."""
    cli._write_log(tmp_path, "first\n")
    cli._write_log(tmp_path, "second\n")
    log = tmp_path / "code_health" / "smart_test.log"
    assert log.read_text(encoding="utf-8") == "second\n"
    pytest_log = tmp_path / "code_health" / "pytest.log"
    assert pytest_log.read_text(encoding="utf-8") == "second\n"


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
    monkeypatch.setattr(cli, "select_tests", lambda _r, _c, _d, **_kw: plan)

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
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``--show-files --depth full`` logs the whole-suite notice and returns 0."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        sys, "argv", ["forge-smart-test", "--show-files", "--depth", "full"]
    )
    with caplog.at_level(logging.INFO, logger="forge.smart_test.cli"):
        code = cli.main()
    assert code == 0
    assert "the entire suite" in caplog.text


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
        _root: object,
        paths: list[str],
        *,
        coverage: bool = False,
        telemetry: bool = False,
        label: str = "",
    ) -> tuple[int, str]:
        del label
        recorded.append({"paths": paths, "coverage": coverage, "telemetry": telemetry})
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
    """Write ``code_health/smart_test.log`` and ``pytest.log`` after run."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["forge-smart-test", "--depth", "1"])
    plan = _make_plan(depth0=["tests/test_x.py"], max_depth=1)
    _stub_cli_deps(monkeypatch, plan=plan, run_results=[(0, "run output")])

    cli.main()

    log_path = tmp_path / "code_health" / "smart_test.log"
    assert log_path.exists()
    assert "run output" in log_path.read_text(encoding="utf-8")
    pytest_log_path = tmp_path / "code_health" / "pytest.log"
    assert pytest_log_path.exists()
    assert "run output" in pytest_log_path.read_text(encoding="utf-8")


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


# ---------------------------------------------------------------------------
# _depth_from_commit
# ---------------------------------------------------------------------------


def test_depth_from_commit_depth_directive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``[depth-2]`` in the HEAD commit message returns ``'2'``."""
    monkeypatch.setattr(
        cli, "head_commit_message", lambda _root: "fix: something [depth-2] here"
    )
    result = cli._depth_from_commit(tmp_path, {})
    assert result == "2"


def test_depth_from_commit_full_directive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``[full]`` in the HEAD commit message returns the ``_FULL`` sentinel."""
    monkeypatch.setattr(
        cli, "head_commit_message", lambda _root: "chore: nightly run [full]"
    )
    result = cli._depth_from_commit(tmp_path, {})
    assert result == cli._FULL


def test_depth_from_commit_no_directive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A commit message without any directive returns ``None``."""
    monkeypatch.setattr(
        cli, "head_commit_message", lambda _root: "fix: regular commit message"
    )
    result = cli._depth_from_commit(tmp_path, {})
    assert result is None


def test_depth_from_commit_custom_regex(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``commit_directive_re`` in cfg overrides the default pattern."""
    monkeypatch.setattr(cli, "head_commit_message", lambda _root: "TIER:2")
    cfg: dict[str, object] = {"commit_directive_re": r"TIER:(?P<n>[0-2])"}
    result = cli._depth_from_commit(tmp_path, cfg)
    assert result == "2"


def test_depth_from_commit_depth_0_directive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``[depth-0]`` in commit message returns ``'0'``."""
    monkeypatch.setattr(
        cli, "head_commit_message", lambda _root: "hotfix: emergency [depth-0]"
    )
    assert cli._depth_from_commit(tmp_path, {}) == "0"


def test_depth_from_commit_depth_1_directive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``[depth-1]`` in commit message returns ``'1'``."""
    monkeypatch.setattr(
        cli, "head_commit_message", lambda _root: "feat: add thing [depth-1]"
    )
    assert cli._depth_from_commit(tmp_path, {}) == "1"


# ---------------------------------------------------------------------------
# --from-commit-message integration
# ---------------------------------------------------------------------------


def test_main_from_commit_message_overrides_depth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--from-commit-message`` with ``[depth-2]`` in HEAD overrides ``--depth 0``.

    SCENARIO: ``--depth 0 --from-commit-message``; HEAD message contains
        ``[depth-2]``.
    MOCK SETUP: cli.head_commit_message returns a directive string; the depth
        passed to cli.select_tests is captured.
    EXPECTED BEHAVIOR: select_tests called with depth=2, not 0.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        sys, "argv", ["forge-smart-test", "--depth", "0", "--from-commit-message"]
    )
    plan = _make_plan(depth0=["tests/test_core.py"], max_depth=2)
    monkeypatch.setattr(cli, "head_commit_message", lambda _root: "nightly [depth-2]")

    depths_used: list[int] = []

    def _capturing_select(
        _root: object, _ch: object, depth: int, **_kw: object
    ) -> SelectionPlan:
        depths_used.append(depth)
        return plan

    monkeypatch.setattr(cli, "resolve_base_ref", lambda _r, _b: "main")
    monkeypatch.setattr(cli, "changed_python_files", lambda _r, _ref: {"src/foo.py"})
    monkeypatch.setattr(cli, "select_tests", _capturing_select)
    monkeypatch.setattr(cli, "clear_python_cache", lambda _root: None)
    monkeypatch.setattr(cli, "run_pytest", lambda _r, _p, **_kw: (0, "ok"))

    code = cli.main()
    assert code == 0
    assert depths_used == [2], (
        f"Expected depth 2 from commit directive, got {depths_used}"
    )


# ---------------------------------------------------------------------------
# Coverage union (--coverage-json)
# ---------------------------------------------------------------------------


def test_main_coverage_union_in_depth0_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Coverage-validated tests from ``--coverage-json`` union into the depth-0 batch.

    SCENARIO: ``--depth 1 --coverage-json cov.json``; coverage stage returns one
        extra test not present in the static plan.
    MOCK SETUP: cli.cov_stage.tests_covering → ``{"tests/test_cov_only.py"}``
        (in the consuming namespace); cli.select_tests → plan with
        ``test_core.py`` at depth 0; run_pytest captured.
    EXPECTED BEHAVIOR: depth-0 batch contains both ``test_core.py`` and
        ``test_cov_only.py``; exit code 0.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        ["forge-smart-test", "--depth", "1", "--coverage-json", "cov.json"],
    )
    plan = _make_plan(depth0=["tests/test_core.py"], max_depth=1)
    monkeypatch.setattr(
        cli.cov_stage,
        "tests_covering",
        lambda _path, _changed: {"tests/test_cov_only.py"},
    )
    captured = _stub_cli_deps(monkeypatch, plan=plan)

    code = cli.main()
    assert code == 0
    assert captured.calls, "run_pytest was not called"
    first_batch = captured.calls[0]
    assert "tests/test_core.py" in first_batch
    assert "tests/test_cov_only.py" in first_batch


def test_main_coverage_validate_config_key_activates_union(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``coverage_validate=true`` activates coverage union without ``--coverage-json``.

    SCENARIO: ``--depth 1`` with no ``--coverage-json`` CLI arg; the
        ``[tool.forge.smart_test]`` table has both ``coverage_validate = true``
        and ``coverage_json = "cov.json"``.
    MOCK SETUP: cli._smart_test_config returns the config dict so no real
        ``pyproject.toml`` is needed; cli.cov_stage.tests_covering returns one
        extra test; cli.select_tests and run_pytest captured via _stub_cli_deps.
    EXPECTED BEHAVIOR: the extra config-driven test appears in the depth-0
        batch even though ``--coverage-json`` was not passed on the CLI.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["forge-smart-test", "--depth", "1"])
    monkeypatch.setattr(
        cli,
        "_smart_test_config",
        lambda _root: {"coverage_validate": True, "coverage_json": "cov.json"},
    )
    plan = _make_plan(depth0=["tests/test_core.py"], max_depth=1)
    monkeypatch.setattr(
        cli.cov_stage,
        "tests_covering",
        lambda _path, _changed: {"tests/test_from_config.py"},
    )
    captured = _stub_cli_deps(monkeypatch, plan=plan)

    code = cli.main()
    assert code == 0
    assert captured.calls, "run_pytest was not called"
    assert "tests/test_from_config.py" in captured.calls[0]


def test_main_follow_mock_patches_config_flows_to_select_tests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``follow_mock_patches=true`` flows to ``select_tests`` as kwarg.

    SCENARIO: ``--depth 0``; ``[tool.forge.smart_test]`` has
        ``follow_mock_patches = true``.
    MOCK SETUP: cli._smart_test_config returns the config dict; a capturing
        replacement for cli.select_tests records the keyword argument.
    EXPECTED BEHAVIOR: ``select_tests`` is called with
        ``follow_mock_patches=True``.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["forge-smart-test", "--depth", "0"])
    monkeypatch.setattr(
        cli,
        "_smart_test_config",
        lambda _root: {"follow_mock_patches": True},
    )
    plan = _make_plan(depth0=["tests/test_core.py"], max_depth=0)
    follow_kwargs_seen: list[bool] = []

    def _capturing(
        _root: object,
        _ch: object,
        _depth: int,
        *,
        follow_mock_patches: bool = False,
        **_kw: object,
    ) -> SelectionPlan:
        follow_kwargs_seen.append(follow_mock_patches)
        return plan

    monkeypatch.setattr(cli, "resolve_base_ref", lambda _r, _b: "main")
    monkeypatch.setattr(cli, "changed_python_files", lambda _r, _ref: {"src/foo.py"})
    monkeypatch.setattr(cli, "select_tests", _capturing)
    monkeypatch.setattr(cli, "clear_python_cache", lambda _root: None)
    monkeypatch.setattr(cli, "run_pytest", lambda _r, _p, **_kw: (0, "ok"))

    cli.main()
    assert follow_kwargs_seen == [True], (
        f"Expected follow_mock_patches=True forwarded; got {follow_kwargs_seen}"
    )


def test_main_show_files_lists_coverage_additions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``--show-files`` logs coverage-validated additions with ``--coverage-json``.

    SCENARIO: ``--show-files --depth 1 --coverage-json cov.json``; coverage
        stage returns a test not in the static plan.
    MOCK SETUP: cli.cov_stage.tests_covering → ``{"tests/test_cov_extra.py"}``;
        cli.select_tests → plan with test_core.py.
    EXPECTED BEHAVIOR: exit 0; ``test_cov_extra.py`` appears in the log.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "forge-smart-test",
            "--show-files",
            "--depth",
            "1",
            "--coverage-json",
            "cov.json",
        ],
    )
    plan = _make_plan(depth0=["tests/test_core.py"], max_depth=1)
    monkeypatch.setattr(
        cli.cov_stage,
        "tests_covering",
        lambda _path, _changed: {"tests/test_cov_extra.py"},
    )
    monkeypatch.setattr(cli, "resolve_base_ref", lambda _r, _b: "main")
    monkeypatch.setattr(cli, "changed_python_files", lambda _r, _ref: {"src/foo.py"})
    monkeypatch.setattr(cli, "select_tests", lambda _r, _c, _d, **_kw: plan)

    with caplog.at_level(logging.INFO, logger="forge.smart_test.cli"):
        code = cli.main()
    assert code == 0
    assert "test_cov_extra.py" in caplog.text


# ---------------------------------------------------------------------------
# --telemetry
# ---------------------------------------------------------------------------


def test_build_parser_telemetry_defaults_false() -> None:
    """``--telemetry`` is off unless the flag is passed."""
    args = cli._build_parser().parse_args(["--depth", "0"])
    assert args.telemetry is False


def test_build_parser_telemetry_flag_sets_true() -> None:
    """``--telemetry`` toggles ``args.telemetry`` to ``True``."""
    args = cli._build_parser().parse_args(["--depth", "0", "--telemetry"])
    assert args.telemetry is True


def test_main_depth0_telemetry_flag_forwarded_to_run_pytest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--depth 0 --telemetry`` forwards ``telemetry=True`` to ``run_pytest``.

    SCENARIO: a single depth-0 batch with ``--telemetry`` on the CLI.
    MOCK SETUP: ``_stub_cli_deps``'s fake records the ``telemetry`` kwarg of
        every ``run_pytest`` call.
    EXPECTED BEHAVIOR: the sole recorded call carries ``telemetry=True``.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        sys, "argv", ["forge-smart-test", "--depth", "0", "--telemetry"]
    )
    plan = _make_plan(depth0=["tests/test_core.py"], max_depth=0)
    captured = _stub_cli_deps(monkeypatch, plan=plan)

    code = cli.main()
    assert code == 0
    assert captured.telemetry_flags == [True]


def test_main_depth0_no_telemetry_flag_forwards_false(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without ``--telemetry``, ``run_pytest`` is called with ``telemetry=False``.

    SCENARIO: a single depth-0 batch with no ``--telemetry`` flag on the CLI.
    MOCK SETUP: ``_stub_cli_deps``'s fake records the ``telemetry`` kwarg of
        every ``run_pytest`` call.
    EXPECTED BEHAVIOR: the sole recorded call carries ``telemetry=False``.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["forge-smart-test", "--depth", "0"])
    plan = _make_plan(depth0=["tests/test_core.py"], max_depth=0)
    captured = _stub_cli_deps(monkeypatch, plan=plan)

    code = cli.main()
    assert code == 0
    assert captured.telemetry_flags == [False]


def test_main_depth1_telemetry_flag_forwarded_to_every_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--depth 1 --telemetry`` forwards ``telemetry=True`` to every batch.

    SCENARIO: a plan with tests at both depth 0 and depth 1, ``--telemetry``
        on the CLI; both batches pass, so both run.
    MOCK SETUP: ``_stub_cli_deps``'s fake records the ``telemetry`` kwarg of
        every ``run_pytest`` call, in call order.
    EXPECTED BEHAVIOR: two ``run_pytest`` calls, both carrying
        ``telemetry=True``.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        sys, "argv", ["forge-smart-test", "--depth", "1", "--telemetry"]
    )
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
    assert captured.telemetry_flags == [True, True]


def test_main_full_depth_telemetry_flag_and_coverage_true(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--depth full --telemetry`` calls ``run_pytest`` with both flags set.

    SCENARIO: the ``full`` tier always enables coverage; ``--telemetry`` adds
        resource sampling on top.
    MOCK SETUP: a local fake captures ``coverage`` and ``telemetry`` kwargs.
    EXPECTED BEHAVIOR: the recorded call has ``coverage=True`` and
        ``telemetry=True``.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        sys, "argv", ["forge-smart-test", "--depth", "full", "--telemetry"]
    )

    recorded: list[dict[str, object]] = []

    def _fake(
        _root: object,
        paths: list[str],
        *,
        coverage: bool = False,
        telemetry: bool = False,
        label: str = "",
    ) -> tuple[int, str]:
        del label
        recorded.append({"paths": paths, "coverage": coverage, "telemetry": telemetry})
        return 0, "full suite ok"

    monkeypatch.setattr(cli, "run_pytest", _fake)

    code = cli.main()
    assert code == 0
    assert recorded
    assert recorded[0]["coverage"] is True
    assert recorded[0]["telemetry"] is True


# ---------------------------------------------------------------------------
# telemetry run label — per-tier artifacts (#376)
# ---------------------------------------------------------------------------


def test_main_depth_batches_label_each_call_by_tier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each depth batch is labeled ``depth<N>`` so tiers keep separate artifacts.

    SCENARIO: a plan with tests at both depth 0 and depth 1; both batches pass.
    MOCK SETUP: ``_stub_cli_deps``'s fake records the ``label`` kwarg of every
        ``run_pytest`` call, in call order.
    EXPECTED BEHAVIOR: the two calls carry ``label="depth0"`` then
        ``label="depth1"`` — never the same label twice.
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
    assert captured.labels == ["depth0", "depth1"]


def _stub_run_full_deps(
    monkeypatch: pytest.MonkeyPatch,
    *,
    all_tests: set[str],
    dev_marked: set[str],
    skippable: set[str],
    run_result: tuple[int, str] = (0, "ok"),
) -> CapturedCalls:
    """Stub ``_run_full``'s I/O seams: file discovery, lifecycle, and pytest.

    Args:
        monkeypatch: pytest monkeypatch fixture.
        all_tests: Set returned by ``cli.all_test_files``.
        dev_marked: Set returned by ``cli.lifecycle.development_marked_files``.
        skippable: Set returned by ``cli.lifecycle.lifecycle_skippable``.
        run_result: ``(exit_code, output)`` returned by every ``cli.run_pytest``
            call.

    Returns:
        A ``CapturedCalls`` accumulating each ``run_pytest`` invocation's
        paths and label, in call order.
    """
    captured = CapturedCalls()
    monkeypatch.setattr(cli, "all_test_files", lambda _root: all_tests)
    monkeypatch.setattr(
        cli.lifecycle, "development_marked_files", lambda _root, _files: dev_marked
    )
    monkeypatch.setattr(
        cli.lifecycle,
        "lifecycle_skippable",
        lambda _root, _files, _changed, **_kw: skippable,
    )

    def _fake_run_pytest(
        _root: object,
        paths: list[str],
        *,
        coverage: bool = False,
        telemetry: bool = False,
        label: str = "",
    ) -> tuple[int, str]:
        del coverage, telemetry
        captured.calls.append(list(paths))
        captured.labels.append(label)
        return run_result

    monkeypatch.setattr(cli, "run_pytest", _fake_run_pytest)
    return captured


# ---------------------------------------------------------------------------
# Safe-fallback escalation — a non-Python change the selector cannot map
# forces depth=full before the depth-tiered path is ever entered.
# ---------------------------------------------------------------------------


def test_main_depth1_nonpython_change_escalates_to_full(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stubbed unmappable non-Python change escalates ``--depth 1`` to full.

    SCENARIO: ``--depth 1``; ``changed_non_python_files`` reports one
        unmappable change.
    MOCK SETUP: cli.changed_non_python_files → ``{"unmappable.rst"}``;
        cli.changed_python_files → empty; cli.run_pytest captures the call.
    EXPECTED BEHAVIOR: the single run_pytest call carries ``label="full"``
        and ``coverage=True`` — proof the full path ran, not the depth-1 path.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["forge-smart-test", "--depth", "1"])
    monkeypatch.setattr(cli, "resolve_base_ref", lambda _r, _b: "main")
    monkeypatch.setattr(cli, "changed_python_files", lambda _r, _ref: set())
    monkeypatch.setattr(
        cli, "changed_non_python_files", lambda _r, _ref, **_kw: {"unmappable.rst"}
    )
    recorded: list[dict[str, object]] = []

    def _fake(
        _root: object,
        paths: list[str],
        *,
        coverage: bool = False,
        telemetry: bool = False,
        label: str = "",
    ) -> tuple[int, str]:
        del telemetry
        recorded.append({"paths": paths, "coverage": coverage, "label": label})
        return 0, "ok"

    monkeypatch.setattr(cli, "run_pytest", _fake)

    code = cli.main()
    assert code == 0
    assert recorded
    assert recorded[0]["label"] == "full"
    assert recorded[0]["coverage"] is True


def test_main_depth1_ignored_glob_change_no_escalation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A change filtered out by ``ignore_globs`` does not escalate the run.

    SCENARIO: ``--depth 1``; ``changed_non_python_files`` reports no
        unmappable changes (simulating a glob-ignored path already filtered).
    MOCK SETUP: cli.changed_non_python_files → empty set; cli.select_tests →
        a plan with one depth-0 test.
    EXPECTED BEHAVIOR: the depth-tiered path runs — the sole run_pytest call
        carries ``label="depth0"``, never ``"full"``.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["forge-smart-test", "--depth", "1"])
    plan = _make_plan(depth0=["tests/test_core.py"], max_depth=1)
    monkeypatch.setattr(cli, "resolve_base_ref", lambda _r, _b: "main")
    monkeypatch.setattr(cli, "changed_python_files", lambda _r, _ref: set())
    monkeypatch.setattr(cli, "changed_non_python_files", lambda _r, _ref, **_kw: set())
    monkeypatch.setattr(cli, "select_tests", lambda _r, _c, _d, **_kw: plan)
    monkeypatch.setattr(cli, "clear_python_cache", lambda _root: None)
    captured = CapturedCalls()

    def _fake_run_pytest(
        _root: object,
        paths: list[str],
        *,
        coverage: bool = False,
        telemetry: bool = False,
        label: str = "",
    ) -> tuple[int, str]:
        del coverage, telemetry
        captured.calls.append(list(paths))
        captured.labels.append(label)
        return 0, "ok"

    monkeypatch.setattr(cli, "run_pytest", _fake_run_pytest)

    code = cli.main()
    assert code == 0
    assert captured.labels == ["depth0"]


def test_main_reads_nonpython_ignore_from_pyproject_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A configured ``nonpython_ignore`` glob is honored over the built-in default.

    SCENARIO: a real ``pyproject.toml`` sets
        ``[tool.forge.smart_test].nonpython_ignore = ["*.custom"]``; the
        change set contains one ``*.custom`` file, which the configured glob
        (not the built-in default, which knows nothing about ``*.custom``)
        must filter out. The counterpart — an equivalent un-ignored change
        escalating to full — is already covered by
        ``test_main_depth1_nonpython_change_escalates_to_full``.
    MOCK SETUP: ``cli.changed_non_python_files`` is replaced with a fake that
        applies the ``ignore_globs`` kwarg it receives (mirroring the real
        filter) against a fixed ``{"note.custom"}`` change set, so the
        assertion proves the *configured* pattern reached the call — not a
        canned return value.
    EXPECTED BEHAVIOR: the depth-tiered path runs — the sole run_pytest call
        carries ``label="depth0"``, never ``"full"`` — because the
        ``*.custom`` change was filtered by the configured ignore glob.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        '[tool.forge.smart_test]\nnonpython_ignore = ["*.custom"]\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(sys, "argv", ["forge-smart-test", "--depth", "1"])
    plan = _make_plan(depth0=["tests/test_core.py"], max_depth=1)
    monkeypatch.setattr(cli, "resolve_base_ref", lambda _r, _b: "main")
    monkeypatch.setattr(cli, "changed_python_files", lambda _r, _ref: set())

    def _fake_changed_non_python_files(
        _root: object, _ref: object, *, ignore_globs: tuple[str, ...] = ()
    ) -> set[str]:
        changed = {"note.custom"}
        return {
            rel
            for rel in changed
            if not any(fnmatch.fnmatch(rel, pat) for pat in ignore_globs)
        }

    monkeypatch.setattr(cli, "changed_non_python_files", _fake_changed_non_python_files)
    monkeypatch.setattr(cli, "select_tests", lambda _r, _c, _d, **_kw: plan)
    monkeypatch.setattr(cli, "clear_python_cache", lambda _root: None)
    captured = CapturedCalls()

    def _fake_run_pytest(
        _root: object,
        paths: list[str],
        *,
        coverage: bool = False,
        telemetry: bool = False,
        label: str = "",
    ) -> tuple[int, str]:
        del coverage, telemetry
        captured.calls.append(list(paths))
        captured.labels.append(label)
        return 0, "ok"

    monkeypatch.setattr(cli, "run_pytest", _fake_run_pytest)

    code = cli.main()
    assert code == 0
    assert captured.labels == ["depth0"]


def test_main_depth_full_never_calls_changed_non_python_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit ``--depth full`` skips the non-Python escalation probe entirely.

    SCENARIO: ``--depth full`` (already the highest tier, so the safe-fallback
        probe would be redundant work).
    MOCK SETUP: cli.changed_non_python_files replaced with a function that
        raises if called at all.
    EXPECTED BEHAVIOR: main() completes successfully without invoking the
        stubbed probe.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["forge-smart-test", "--depth", "full"])

    def _raise(*_a: object, **_kw: object) -> set[str]:
        msg = "changed_non_python_files must not be called for explicit --depth full"
        raise AssertionError(msg)

    monkeypatch.setattr(cli, "changed_non_python_files", _raise)
    monkeypatch.setattr(cli, "run_pytest", lambda _r, _p, **_kw: (0, "ok"))

    code = cli.main()
    assert code == 0


# ---------------------------------------------------------------------------
# _run_full — lifecycle deselection, --all-tests, and the differential check
# ---------------------------------------------------------------------------


def test_run_full_deselects_stale_dev_file_and_reports_line(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A skippable stale dev file is excluded from the batch and reported.

    SCENARIO: two test files total; one is lifecycle-skippable.
    MOCK SETUP: ``_stub_run_full_deps`` fixes ``all_test_files`` and
        ``lifecycle_skippable``'s return values.
    EXPECTED BEHAVIOR: run_pytest's batch omits the skippable file; the
        output carries a ``lifecycle-skipped: 1`` line.
    """
    monkeypatch.chdir(tmp_path)
    captured = _stub_run_full_deps(
        monkeypatch,
        all_tests={"tests/test_a.py", "tests/test_stale.py"},
        dev_marked={"tests/test_stale.py"},
        skippable={"tests/test_stale.py"},
    )
    code, out = cli._run_full(tmp_path, {}, set())
    assert code == 0
    assert captured.calls == [["tests/test_a.py"]]
    assert "lifecycle-skipped: 1" in out


def test_run_full_all_tests_flag_includes_everything_no_skip_line(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``all_tests=True`` disables lifecycle deselection — no skip reporting.

    SCENARIO: same file set as the deselection test, but ``all_tests=True``.
    MOCK SETUP: ``_stub_run_full_deps``'s ``lifecycle_skippable`` stub is never
        even called (guarded by ``if not all_tests`` in ``_run_full``), so its
        configured non-empty return value has no effect.
    EXPECTED BEHAVIOR: the batch degenerates to ``[]`` (pytest runs
        everything); no ``lifecycle-skipped`` line.
    """
    monkeypatch.chdir(tmp_path)
    captured = _stub_run_full_deps(
        monkeypatch,
        all_tests={"tests/test_a.py", "tests/test_stale.py"},
        dev_marked={"tests/test_stale.py"},
        skippable={"tests/test_stale.py"},
    )
    code, out = cli._run_full(tmp_path, {}, set(), all_tests=True)
    assert code == 0
    assert captured.calls == [[]]
    assert "lifecycle-skipped" not in out


def test_run_full_threads_lifecycle_skip_days_from_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``cfg["lifecycle_skip_days"]`` threads through to ``lifecycle_skippable``.

    SCENARIO: ``cfg={"lifecycle_skip_days": 7}`` — a config override of the
        30-day default.
    MOCK SETUP: ``cli.lifecycle.lifecycle_skippable`` is stubbed to record
        its keyword arguments (instead of swallowing them via ``**_kw``), so
        the threaded value can be asserted directly.
    EXPECTED BEHAVIOR: ``lifecycle_skippable`` is called with
        ``skip_days=7.0`` — the configured value coerced to float, not the
        30-day default.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "all_test_files", lambda _root: {"tests/test_a.py"})
    monkeypatch.setattr(
        cli.lifecycle, "development_marked_files", lambda _root, _files: set()
    )
    recorded_kwargs: dict[str, object] = {}

    def _fake_skippable(
        _root: object, _files: set[str], _changed: set[str], **kwargs: object
    ) -> set[str]:
        recorded_kwargs.update(kwargs)
        return set()

    monkeypatch.setattr(cli.lifecycle, "lifecycle_skippable", _fake_skippable)
    monkeypatch.setattr(cli, "run_pytest", lambda _r, _p, **_kw: (0, "ok"))

    cli._run_full(tmp_path, {"lifecycle_skip_days": 7}, set())

    assert recorded_kwargs == {"skip_days": 7.0}


def test_run_full_no_skippable_degenerates_to_empty_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When nothing is skippable, the batch is ``[]`` (a full pytest run).

    Distinct from the existing full-depth ``main()`` test above: this stubs
    ``_run_full``'s dependencies directly instead of exercising main()'s
    unstubbed real-filesystem path.

    SCENARIO: two test files total; ``lifecycle_skippable`` reports none of
        them as skippable.
    MOCK SETUP: ``_stub_run_full_deps`` fixes ``all_test_files`` and
        ``lifecycle_skippable``'s return values (an empty set).
    EXPECTED BEHAVIOR: ``run_pytest``'s batch degenerates to ``[]`` (pytest
        runs everything on an empty argv list); no ``lifecycle-skipped``
        line in the output.
    """
    monkeypatch.chdir(tmp_path)
    captured = _stub_run_full_deps(
        monkeypatch,
        all_tests={"tests/test_a.py", "tests/test_b.py"},
        dev_marked=set(),
        skippable=set(),
    )
    code, out = cli._run_full(tmp_path, {}, set())
    assert code == 0
    assert captured.calls == [[]]
    assert "lifecycle-skipped" not in out


def test_run_full_differential_line_for_failure_outside_depth2_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A FAILED file outside the depth-2 selection is reported as a differential.

    SCENARIO: ``changed`` is non-empty; the pytest run fails a file the
        depth-2 static plan would not have selected.
    MOCK SETUP: ``_stub_run_full_deps``'s ``run_pytest`` returns a FAILED line
        for ``tests/test_outside.py``; ``cli.select_tests`` returns a plan
        that selects only ``tests/test_a.py`` at depth 2.
    EXPECTED BEHAVIOR: the output carries a ``differential: 1 failing
        file(s)`` line naming ``tests/test_outside.py``.
    """
    monkeypatch.chdir(tmp_path)
    _stub_run_full_deps(
        monkeypatch,
        all_tests={"tests/test_a.py"},
        dev_marked=set(),
        skippable=set(),
        run_result=(1, "FAILED tests/test_outside.py::test_x - AssertionError\n"),
    )
    plan = _make_plan(depth0=["tests/test_a.py"], max_depth=2)
    monkeypatch.setattr(cli, "select_tests", lambda _r, _c, _d, **_kw: plan)

    code, out = cli._run_full(tmp_path, {}, changed={"src/foo.py"})
    assert code == 1
    assert "differential: 1 failing file(s)" in out
    assert "tests/test_outside.py" in out


def test_run_full_no_differential_line_when_changed_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With an empty change set, the differential check is skipped entirely.

    SCENARIO: ``changed=set()`` — no changed files to diff against.
    MOCK SETUP: ``_stub_run_full_deps``'s run returns a passing result;
        ``cli.select_tests`` is never called (the ``if changed:`` guard short-
        circuits).
    EXPECTED BEHAVIOR: no ``differential`` text in the output.
    """
    monkeypatch.chdir(tmp_path)
    _stub_run_full_deps(
        monkeypatch,
        all_tests={"tests/test_a.py"},
        dev_marked=set(),
        skippable=set(),
        run_result=(0, "1 passed\n"),
    )
    code, out = cli._run_full(tmp_path, {}, changed=set())
    assert code == 0
    assert "differential" not in out


def test_run_full_appends_exactly_one_history_line(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One ``_run_full`` invocation appends exactly one history record.

    Uses the real (unstubbed) ``lifecycle.append_history`` so the on-disk
    line content is asserted directly, not just that a stub was called.

    SCENARIO: two test files, one development-marked; nothing skippable.
    MOCK SETUP: ``_stub_run_full_deps`` fixes ``all_test_files`` and
        ``development_marked_files``'s return values; ``run_pytest`` and
        ``lifecycle_skippable`` are stubbed, but ``append_history`` runs for
        real against ``tmp_path``.
    EXPECTED BEHAVIOR: the history log has exactly one line, recording
        ``label=full``, ``files=2``, and ``dev_files=1``.
    """
    monkeypatch.chdir(tmp_path)
    _stub_run_full_deps(
        monkeypatch,
        all_tests={"tests/test_a.py", "tests/test_b.py"},
        dev_marked={"tests/test_b.py"},
        skippable=set(),
    )
    cli._run_full(tmp_path, {}, changed=set())

    log = tmp_path / "code_health" / "smart_test_history.log"
    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert "label=full" in lines[0]
    assert "files=2" in lines[0]
    assert "dev_files=1" in lines[0]


def test_main_full_depth_labels_run_full(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--depth full`` labels its single ``run_pytest`` call ``"full"``."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["forge-smart-test", "--depth", "full"])

    recorded: list[dict[str, object]] = []

    def _fake(
        _root: object,
        paths: list[str],
        *,
        coverage: bool = False,
        telemetry: bool = False,
        label: str = "",
    ) -> tuple[int, str]:
        recorded.append({"paths": paths, "coverage": coverage, "label": label})
        del telemetry
        return 0, "full suite ok"

    monkeypatch.setattr(cli, "run_pytest", _fake)

    code = cli.main()
    assert code == 0
    assert recorded
    assert recorded[0]["label"] == "full"
