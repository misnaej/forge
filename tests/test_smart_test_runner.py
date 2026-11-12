"""Tests for ``forge.smart_test.runner`` — pytest execution and cache hygiene."""

# MOCKING STRATEGY: ``run_pytest`` tests monkeypatch ``subprocess.run`` on the
# shared ``subprocess`` module object (reachable as ``runner.subprocess.run``
# because runner.py uses ``import subprocess`` + ``subprocess.run(...)``).
# ``make_fake_run`` from conftest records argv lists without spawning a real
# process.  ``_coverage_available`` is monkeypatched (``runner._coverage_available``)
# to control coverage-related branches without requiring ``pytest-cov`` to be
# installed or absent.  ``clear_python_cache`` tests use a real temporary
# directory tree — no mocking needed there.  Telemetry-delegate tests
# monkeypatch ``runner.telemetry_mod.*`` — runner.py does
# ``from forge import telemetry as telemetry_mod``, so ``telemetry_mod`` is
# the namespace ``patch`` must target, per CLAUDE.md's patch-target rule.

from __future__ import annotations

import subprocess
import sys
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


def test_run_pytest_exit5_with_paths_stays_5_and_explains(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exit 5 for a named selection is a failure with an explanation appended."""
    monkeypatch.setattr(runner, "_coverage_available", lambda: False)
    monkeypatch.setattr(
        subprocess, "run", make_fake_run(stdout="no tests", returncode=5)
    )
    code, output = runner.run_pytest(tmp_path, ["tests/test_x.py"])
    assert code == 5
    assert "collected no tests" in output


def test_run_pytest_exit5_whole_suite_normalized_to_0(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exit 5 with nothing named (whole-suite coverage run) is still benign."""
    monkeypatch.setattr(runner, "_coverage_available", lambda: False)
    monkeypatch.setattr(
        subprocess, "run", make_fake_run(stdout="no tests", returncode=5)
    )
    code, output = runner.run_pytest(tmp_path, [], coverage=True)
    assert code == 0
    assert "collected no tests" not in output


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


# ---------------------------------------------------------------------------
# telemetry=True — delegate to forge.telemetry.run_command
# ---------------------------------------------------------------------------


def test_run_pytest_telemetry_delegates_to_run_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``telemetry_label`` with psutil available delegates to ``run_command``.

    SCENARIO: ``telemetry_label="depth0"`` plus an ``on_output`` recorder;
        ``telemetry_mod.telemetry_available()`` is True.
    MOCK SETUP: ``telemetry_mod.run_command`` is stubbed to capture its
        ``(cmd, root, capture, cwd, label)`` arguments and return canned
        output; ``subprocess.run`` is a sentinel that fails the test if
        invoked — the delegate path must never fall through to the plain
        subprocess run.
    EXPECTED BEHAVIOR: ``run_command`` is called with ``capture=True``,
        ``cwd=repo_root``, and the ``label`` forwarded unchanged; the
        returned output is passed through unchanged; the ``on_output``
        recorder is never called.
    """
    captured: dict[str, object] = {}

    def _fake_run_command(
        cmd: list[str],
        root: Path,
        *,
        capture: bool = False,
        cwd: Path | None = None,
        label: str = "",
    ) -> tuple[int, str]:
        captured["cmd"] = cmd
        captured["root"] = root
        captured["capture"] = capture
        captured["cwd"] = cwd
        captured["label"] = label
        return 0, "child output"

    def _sentinel_subprocess_run(*_args: object, **_kwargs: object) -> object:
        msg = "subprocess.run must not be called on the delegate path"
        raise AssertionError(msg)

    monkeypatch.setattr(runner.telemetry_mod, "telemetry_available", lambda: True)
    monkeypatch.setattr(runner.telemetry_mod, "run_command", _fake_run_command)
    monkeypatch.setattr(subprocess, "run", _sentinel_subprocess_run)

    streamed: list[str] = []
    code, output = runner.run_pytest(
        tmp_path,
        ["tests/test_x.py"],
        telemetry_label="depth0",
        on_output=streamed.append,
    )

    assert code == 0
    assert "child output" in output
    # The delegate captures through a spool file, so it never streams.
    assert streamed == []
    assert captured["capture"] is True
    assert captured["cwd"] == tmp_path
    assert captured["root"] == tmp_path
    assert captured["label"] == "depth0"


def test_run_pytest_telemetry_delegate_exit5_with_paths_stays_5(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exit code 5 from the telemetry delegate stays 5 for a named selection.

    SCENARIO: a ``telemetry_label`` with test paths; the delegate reports
        pytest's "no tests collected" exit code (5).
    MOCK SETUP: ``telemetry_mod.telemetry_available`` -> True;
        ``telemetry_mod.run_command`` stubbed to return ``(5, "")``.
    EXPECTED BEHAVIOR: code stays 5 and the explanation is appended, same as
        the plain subprocess path.
    """
    monkeypatch.setattr(runner.telemetry_mod, "telemetry_available", lambda: True)
    monkeypatch.setattr(runner.telemetry_mod, "run_command", lambda *_a, **_kw: (5, ""))
    code, output = runner.run_pytest(
        tmp_path, ["tests/test_x.py"], telemetry_label="x"
    )
    assert code == 5
    assert "collected no tests" in output


def test_run_pytest_telemetry_unavailable_falls_back_with_notice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing ``psutil`` degrades to a plain (unprofiled) run with a notice.

    SCENARIO: a ``telemetry_label`` but ``telemetry_mod.telemetry_available()``
        is False.
    MOCK SETUP: ``telemetry_mod.telemetry_available`` → False;
        ``subprocess.run`` stubbed via ``make_fake_run`` — the fallback path
        must still run pytest normally.
    EXPECTED BEHAVIOR: output is prefixed with a notice naming ``psutil`` and
        stating the run proceeds without telemetry; the plain subprocess run
        still happens.
    """
    captured = CapturedCalls()
    monkeypatch.setattr(runner.telemetry_mod, "telemetry_available", lambda: False)
    monkeypatch.setattr(runner, "_coverage_available", lambda: False)
    monkeypatch.setattr(
        subprocess, "run", make_fake_run(stdout="1 passed", captured=captured)
    )

    _, output = runner.run_pytest(
        tmp_path, ["tests/test_x.py"], telemetry_label="x"
    )

    assert captured.calls, "the fallback subprocess.run was not called"
    assert "psutil" in output
    assert "running without telemetry" in output


def test_run_pytest_telemetry_false_never_touches_telemetry_mod(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ``telemetry_label`` (the default) never consults ``telemetry_mod``.

    SCENARIO: ``run_pytest`` called without the ``telemetry_label`` kwarg.
    MOCK SETUP: both ``telemetry_mod.telemetry_available`` and
        ``telemetry_mod.run_command`` raise if called; ``subprocess.run`` is
        stubbed normally.
    EXPECTED BEHAVIOR: the plain subprocess path runs; no assertion error.
    """

    def _fail(*_a: object, **_kw: object) -> object:
        msg = "telemetry_mod must not be touched without a telemetry_label"
        raise AssertionError(msg)

    monkeypatch.setattr(runner.telemetry_mod, "telemetry_available", _fail)
    monkeypatch.setattr(runner.telemetry_mod, "run_command", _fail)
    monkeypatch.setattr(runner, "_coverage_available", lambda: False)
    monkeypatch.setattr(subprocess, "run", make_fake_run(stdout="1 passed"))

    code, _ = runner.run_pytest(tmp_path, ["tests/test_x.py"])
    assert code == 0


# ---------------------------------------------------------------------------
# _run_streamed — lines reach the callback while the child is still running
# ---------------------------------------------------------------------------

# Prints "first" WITHOUT flushing, then waits for the parent to create the
# marker file in answer to it. The wait is bounded and reports TIMEOUT, so a
# parent that only delivers after exit fails here without a timing assertion.
# Not flushing is deliberate: the line can only arrive early if the child
# really runs unbuffered, which is what PYTHONUNBUFFERED=1 is for.
_HANDSHAKE_SCRIPT = """
import os, sys, time
print("PYTHONUNBUFFERED=" + os.environ.get("PYTHONUNBUFFERED", ""))
print("first")
for _ in range(200):
    if os.path.exists(sys.argv[1]):
        break
    time.sleep(0.05)
else:
    print("TIMEOUT")
    sys.exit(99)
print("second")
sys.exit(3)
"""


def test_run_streamed_delivers_lines_while_the_child_runs(tmp_path: Path) -> None:
    """Each line reaches ``on_output`` before the child exits, newline included.

    The child waits for a file the callback creates on seeing ``first``; if
    delivery were deferred to the end it would print ``TIMEOUT`` and exit 99.
    The same run shows the child is unbuffered and the exit code comes back.
    """
    marker = tmp_path / "seen-first"
    delivered: list[str] = []

    def _on_output(line: str) -> None:
        delivered.append(line)
        if line == "first\n":
            marker.write_text("x", encoding="utf-8")

    code, output = runner._run_streamed(
        [sys.executable, "-c", _HANDSHAKE_SCRIPT, str(marker)],
        tmp_path,
        "notice\n",
        _on_output,
    )

    assert code == 3
    assert "TIMEOUT\n" not in delivered
    assert delivered == [
        "notice\n",
        "PYTHONUNBUFFERED=1\n",
        "first\n",
        "second\n",
    ]
    assert output == "".join(delivered)


def test_run_streamed_returns_the_same_text_as_the_captured_path(
    tmp_path: Path,
) -> None:
    """For a stdout-only child, streaming and capturing return identical text.

    Callers parse this text (run log, exit-5 rule), so streaming must not
    change it. Stdout only: with both streams the two paths order stderr
    differently, which the docstring states.
    """
    cmd = [sys.executable, "-c", "print('one'); print('two'); raise SystemExit(4)"]
    captured = subprocess.run(
        cmd, cwd=tmp_path, capture_output=True, text=True, check=False
    )
    delivered: list[str] = []

    code, output = runner._run_streamed(cmd, tmp_path, "", delivered.append)

    assert code == captured.returncode == 4
    assert output == captured.stdout
    assert "".join(delivered) == captured.stdout
