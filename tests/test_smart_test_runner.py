"""Tests for ``forge.smart_test.runner`` — pytest execution and cache hygiene."""

# MOCKING STRATEGY: ``run_pytest`` tests monkeypatch ``subprocess.run`` on the
# shared ``subprocess`` module object (reachable as ``runner.subprocess.run``
# because runner.py uses ``import subprocess`` + ``subprocess.run(...)``).
# ``make_fake_run`` from conftest records argv lists without spawning a real
# process.  ``_coverage_available`` is monkeypatched (``runner._coverage_available``)
# to control coverage-related branches without requiring ``pytest-cov`` to be
# installed or absent.  ``clear_python_cache`` tests use a real temporary
# directory tree — no mocking needed there.

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

from forge.smart_test import runner
from tests.conftest import CapturedCalls, make_fake_run


if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_clear_python_cache_removes_all_pycache_dirs(tmp_path: Path) -> None:
    """All ``__pycache__`` directories under repo_root are deleted."""
    pkg = tmp_path / "myapp"
    pkg.mkdir()
    cache1 = pkg / "__pycache__"
    cache1.mkdir()
    (cache1 / "mod.cpython-313.pyc").write_bytes(b"")
    nested = pkg / "sub"
    nested.mkdir()
    cache2 = nested / "__pycache__"
    cache2.mkdir()

    runner.clear_python_cache(tmp_path)

    assert not cache1.exists()
    assert not cache2.exists()


def test_clear_python_cache_noop_when_no_pycache(tmp_path: Path) -> None:
    """No error is raised when there are no ``__pycache__`` directories."""
    (tmp_path / "myapp").mkdir()
    runner.clear_python_cache(tmp_path)  # must not raise


def test_run_pytest_empty_paths_no_coverage_returns_noop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty test_paths with coverage=False is a no-op; subprocess is NOT called.

    SCENARIO: no tests selected, coverage off — nothing to run.
    MOCK SETUP: subprocess.run is replaced with a sentinel that would fail the
        test if called.
    EXPECTED BEHAVIOR: returns (0, message), subprocess never invoked.
    """
    called: list[bool] = []

    def _sentinel(*_args: object, **_kwargs: object) -> object:
        called.append(True)
        return None

    monkeypatch.setattr(subprocess, "run", _sentinel)
    code, output = runner.run_pytest(tmp_path, [], coverage=False)
    assert code == 0
    assert "no tests" in output.lower()
    assert not called


def test_run_pytest_paths_sorted_in_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test paths are passed to pytest in sorted order for deterministic runs.

    SCENARIO: two unsorted test paths supplied.
    MOCK SETUP: subprocess.run captured via make_fake_run.
    EXPECTED BEHAVIOR: argv contains paths in sorted order.
    """
    captured = CapturedCalls()
    monkeypatch.setattr(runner, "_coverage_available", lambda: False)
    monkeypatch.setattr(
        subprocess, "run", make_fake_run(stdout="1 passed", captured=captured)
    )
    runner.run_pytest(tmp_path, ["tests/test_z.py", "tests/test_a.py"])
    assert captured.calls, "subprocess.run was not called"
    cmd = captured.calls[0]
    # The sorted paths should appear in this order at the end of argv.
    z_idx = cmd.index("tests/test_z.py")
    a_idx = cmd.index("tests/test_a.py")
    assert a_idx < z_idx


def test_run_pytest_exit5_normalized_to_0(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pytest exit code 5 (no tests collected) is normalized to 0."""
    monkeypatch.setattr(runner, "_coverage_available", lambda: False)
    monkeypatch.setattr(
        subprocess, "run", make_fake_run(stdout="no tests", returncode=5)
    )
    code, _ = runner.run_pytest(tmp_path, ["tests/test_x.py"])
    assert code == 0


def test_run_pytest_nonzero_exit_propagated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-zero pytest exit code (other than 5) is returned unchanged."""
    monkeypatch.setattr(runner, "_coverage_available", lambda: False)
    monkeypatch.setattr(
        subprocess, "run", make_fake_run(stdout="1 failed", returncode=1)
    )
    code, _ = runner.run_pytest(tmp_path, ["tests/test_x.py"])
    assert code == 1


def test_run_pytest_coverage_flag_added_when_available(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--cov`` and ``--cov-report=term-missing`` are added when pytest-cov is present.

    SCENARIO: coverage=True and _coverage_available() returns True.
    MOCK SETUP: runner._coverage_available → True; subprocess.run captured.
    EXPECTED BEHAVIOR: ``--cov`` appears in the argv.
    """
    captured = CapturedCalls()
    monkeypatch.setattr(runner, "_coverage_available", lambda: True)
    monkeypatch.setattr(
        subprocess, "run", make_fake_run(stdout="ok", captured=captured)
    )
    runner.run_pytest(tmp_path, ["tests/test_x.py"], coverage=True)
    assert captured.calls
    assert "--cov" in captured.calls[0]


def test_run_pytest_coverage_notice_when_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A notice is prepended to output when pytest-cov is not installed.

    SCENARIO: coverage=True but _coverage_available() returns False.
    MOCK SETUP: runner._coverage_available → False; subprocess.run mocked.
    EXPECTED BEHAVIOR: ``--cov`` absent from argv; notice in returned output.
    """
    captured = CapturedCalls()
    monkeypatch.setattr(runner, "_coverage_available", lambda: False)
    monkeypatch.setattr(
        subprocess, "run", make_fake_run(stdout="ok", captured=captured)
    )
    _, output = runner.run_pytest(tmp_path, ["tests/test_x.py"], coverage=True)
    assert captured.calls
    assert "--cov" not in captured.calls[0]
    assert "not installed" in output.lower()


def test_run_pytest_empty_paths_with_coverage_runs_full_suite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty paths + coverage=True triggers a full-suite run (subprocess IS called).

    SCENARIO: the ``full`` tier — no specific paths, but coverage is on.
    MOCK SETUP: runner._coverage_available → True; subprocess.run captured.
    EXPECTED BEHAVIOR: subprocess.run is called (at least once).
    """
    captured = CapturedCalls()
    monkeypatch.setattr(runner, "_coverage_available", lambda: True)
    monkeypatch.setattr(
        subprocess, "run", make_fake_run(stdout="all passed", captured=captured)
    )
    code, _ = runner.run_pytest(tmp_path, [], coverage=True)
    assert captured.calls, "subprocess.run was not called for the full-suite run"
    assert code == 0
