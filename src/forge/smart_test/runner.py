"""Pytest execution for smart-test.

Owns the mechanics of actually running a selected batch of tests: clear
the import cache so a stale ``__pycache__`` from an earlier tree can't
mask a real failure, then invoke ``pytest`` **once** over the batch with
a deterministic (sorted) file order. Coverage instrumentation — which
slows pytest ~3-5x — is reserved for the ``full`` tier and self-disables
when ``pytest-cov`` is not installed rather than erroring.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from typing import TYPE_CHECKING

from forge import telemetry as telemetry_mod
from forge.git_utils import missing_dependency_hint


if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path


# pytest's "no tests collected" exit code — `_finalize` treats it as success
# only for a whole-suite run; named test files that collect nothing fail.
_PYTEST_NO_TESTS = 5


def clear_python_cache(repo_root: Path) -> None:
    """Delete every ``__pycache__`` directory under *repo_root*.

    Run between depth batches so a ``.pyc`` compiled against a stale source
    tree cannot satisfy an import and hide a real failure.

    Args:
        repo_root: Git repo root.
    """
    for cache in repo_root.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)


def _coverage_available() -> bool:
    """Return whether the ``pytest-cov`` plugin is importable."""
    return importlib.util.find_spec("pytest_cov") is not None


def run_pytest(
    repo_root: Path,
    test_paths: Sequence[str],
    *,
    coverage: bool = False,
    telemetry_label: str | None = None,
    on_output: Callable[[str], None] | None = None,
) -> tuple[int, str]:
    """Run ``pytest`` once over *test_paths* and return ``(exit_code, output)``.

    A deterministic sorted path order is passed so collection is
    reproducible across runs (deterministic by design). An empty
    *test_paths* with ``coverage`` runs the whole suite (the ``full``
    tier); an empty *test_paths* without coverage is a no-op success.

    Args:
        repo_root: Git repo root (pytest's working directory).
        test_paths: Repo-relative test file paths; empty means "whole suite".
        coverage: Enable ``--cov`` (ignored with a notice when ``pytest-cov``
            is absent).
        telemetry_label: Sample resource usage during the run via
            ``forge.telemetry``, labelling its artifacts with this name so
            each depth tier of a multi-tier run keeps its own instead of
            clobbering the previous tier's. None means no sampling. One
            parameter rather than a flag plus a name, because a label only
            ever meant anything with sampling on and the pair could
            express a combination that did nothing. Sampling degrades to
            an unprofiled run with a notice when ``psutil`` is absent — a
            missing profiler must never fail the test run.
        on_output: Called with each line as pytest produces it, so a long
            run shows progress instead of looking like a hang. The return
            value is unchanged either way — the whole output is still
            returned, because the run log, the differential check and the
            exit-5 rule all read it. Ignored under *telemetry*, whose
            capture goes through a spool file to avoid a pipe deadlock;
            that path stays unstreamed.

    Returns:
        ``(exit_code, combined_output)``. Exit code 5 ("no tests collected")
        is normalized to 0 only for a whole-suite run; for named
        *test_paths* it stays a failure.
    """
    if not test_paths and not coverage:
        return 0, "(no tests selected — nothing to run)\n"

    cmd = [sys.executable, "-m", "pytest", "-q"]
    notice = ""
    if coverage:
        if _coverage_available():
            cmd += ["--cov", "--cov-report=term-missing"]
        else:
            notice = "(pytest-cov not installed — running without coverage)\n"
    cmd += sorted(test_paths)

    if telemetry_label is not None:
        if telemetry_mod.telemetry_available():
            code, output = telemetry_mod.run_command(
                cmd, repo_root, capture=True, cwd=repo_root, label=telemetry_label
            )
            code, output = _finalize(code, notice + output, selected=bool(test_paths))
            if on_output is not None:
                # This path spools rather than streams, so the reader
                # gets the whole output here instead of line by line.
                # Showing nothing would hide a failing run completely.
                on_output(output)
            return code, output
        notice += (
            f"({missing_dependency_hint('psutil', extra='telemetry')} "
            "— running without telemetry)\n"
        )

    if on_output is None:
        proc = subprocess.run(
            cmd, cwd=repo_root, capture_output=True, text=True, check=False
        )
        return _finalize(
            proc.returncode,
            notice + proc.stdout + proc.stderr,
            selected=bool(test_paths),
        )
    code, streamed = _run_streamed(cmd, repo_root, notice, on_output)
    code, output = _finalize(code, streamed, selected=bool(test_paths))
    if output != streamed:
        # `_finalize` explains a selection that collected nothing, and it
        # does so after the child has exited — past everything streaming
        # could carry. Show the tail, or the console reports a failure
        # with its reason only in the log.
        on_output(output[len(streamed) :])
    return code, output


def _run_streamed(
    cmd: list[str],
    repo_root: Path,
    notice: str,
    on_output: Callable[[str], None],
) -> tuple[int, str]:
    """Run *cmd*, passing each output line on as it arrives.

    stderr is merged into stdout rather than read separately: two pipes
    read in sequence deadlock as soon as one fills, and pytest's own
    output interleaves the two anyway. ``PYTHONUNBUFFERED`` is set in the
    child because an unbuffered pipe is what makes the lines arrive while
    the run is happening rather than in one block at the end.

    Args:
        cmd: The pytest argv.
        repo_root: Working directory for the child.
        notice: Text to prepend to the captured output (and to pass on
            first, so the reader sees it in the same order).
        on_output: Receives each line, newline included.

    Returns:
        ``(exit_code, combined_output)`` — the whole output, so callers
        that parse it are unaffected by streaming. It is not byte-equal
        to the captured path when the child writes to both streams: here
        stderr is interleaved in the order it was written, where the
        captured path appends it after stdout. pytest writes to stdout.
    """
    if notice:
        on_output(notice)
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    collected = [notice]
    with subprocess.Popen(
        cmd,
        cwd=repo_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=env,
    ) as proc:
        if proc.stdout is None:
            msg = "pytest stdout pipe was not created"
            raise RuntimeError(msg)
        try:
            for line in proc.stdout:
                collected.append(line)
                on_output(line)
        except BaseException:
            # Leaving the loop early — an interrupt, or a callback that
            # raised — must not leave a test run going. Closing the pipe
            # alone only stops the child at its next write.
            proc.kill()
            raise
        code = proc.wait()
    return code, "".join(collected)


def _finalize(code: int, output: str, *, selected: bool) -> tuple[int, str]:
    """Apply the "no tests collected" rule to a finished pytest run.

    Exit 5 is benign only for a whole-suite run (nothing named): a named
    selection that collects nothing means the selector picked files that
    hold no tests, and passing would report coverage that never ran.

    Args:
        code: pytest's exit code.
        output: The run's combined output.
        selected: Whether specific test files were passed to pytest.

    Returns:
        ``(exit_code, output)``, with an explanation appended when a
        selection collected nothing.
    """
    if code != _PYTEST_NO_TESTS:
        return code, output
    if not selected:
        return 0, output
    return code, output + (
        "\nsmart-test: the selected files collected no tests — failing rather "
        "than reporting a run that tested nothing.\n"
    )
