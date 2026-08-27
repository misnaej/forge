"""forge-telemetry — resource-profiling wrapper for test/command runs.

Runs an arbitrary command (``forge-telemetry -- <cmd> ...``) while sampling
the child's **process-tree RSS** and the **host CPU** on a fixed interval,
then writes a plain-text profile to ``code_health/telemetry.log`` and — when
matplotlib is importable and ``[tool.forge.telemetry].plot`` is true — a
``code_health/telemetry.png`` chart rendered headless (Agg backend).

Dependency policy (the ``[telemetry]`` extra ships ``psutil`` +
``matplotlib``): ``psutil`` is required for any sampling at all — the CLI
fails loudly with the extra's install hint (FOUNDATION §2), while the
``forge-smart-test --telemetry`` integration degrades to an unprofiled run
with the same hint logged, never failing the test run itself. ``matplotlib``
is a nice-to-have — the text log always writes; the chart is skipped with a
logged hint when it is absent.

The wrapper is transparent: the child's exit code is propagated unchanged,
and in streaming mode (the CLI) its stdout/stderr pass straight through.
Invocation is always explicit (the CLI or a flag) — there is no ambient
"enabled" switch, so consumers who never ask for telemetry never pay for it.

Exit codes (CLI):
    <child's>  the wrapped command's own exit code, unchanged
    1          psutil not installed (install hint printed)
    2          no command given after ``--``
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from forge.config import read_tool_forge_section
from forge.git_utils import (
    configure_cli_logging,
    missing_dependency_hint,
    repo_root,
    write_step_log,
)


if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from psutil import Process


try:
    import psutil
except ImportError:  # degrade: call sites decide loud (CLI) vs soft (runner)
    psutil = None  # type: ignore[assignment]


configure_cli_logging()
logger = logging.getLogger(__name__)

_BYTES_PER_MB = 1024 * 1024
_DEFAULT_SAMPLE_INTERVAL = 1.0

# A run label suffixes the artifact names (`telemetry_<label>.log/.png`), so
# it must stay a plain filename fragment — same anchored-constant shape as
# doctor.py's plugin-name validator.
_SAFE_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\Z")


def validated_label(label: str) -> str:
    """Return *label* unchanged after validating it as an artifact suffix.

    Loud by design: a silently-dropped label would put a retried run right
    back to clobbering the artifacts it was meant to keep apart (#376), so
    a bad value fails the run instead of degrading.

    Args:
        label: Requested run label; empty means unlabeled (default names).

    Returns:
        The validated label (possibly empty).

    Raises:
        ValueError: If *label* is non-empty and not a safe filename
            fragment (``[A-Za-z0-9][A-Za-z0-9._-]*``).
    """
    if label and not _SAFE_LABEL_RE.match(label):
        msg = (
            f"telemetry label {label!r} is not a safe artifact suffix "
            "(allowed: letters, digits, '.', '_', '-'; must not start "
            "with a separator)"
        )
        raise ValueError(msg)
    return label


def telemetry_available() -> bool:
    """Return whether sampling can run at all (``psutil`` importable)."""
    return psutil is not None


@dataclass(frozen=True)
class Sample:
    """One point of the resource profile.

    Attributes:
        elapsed: Seconds since the child was spawned.
        rss_mb: Resident-set size of the child's whole process tree, in MB.
        cpu_percent: Host-wide CPU utilisation since the previous sample.
    """

    elapsed: float
    rss_mb: float
    cpu_percent: float


def _telemetry_config(root: Path) -> tuple[float, bool]:
    """Read ``[tool.forge.telemetry]``, degrading misshaped values to defaults.

    Args:
        root: Repository root directory.

    Returns:
        ``(sample_interval, plot)`` — interval in seconds (default 1.0,
        floored at 0.1 so a typo cannot busy-spin), and whether to render
        the PNG chart (default ``True``).
    """
    table = read_tool_forge_section(root, "telemetry")
    interval = table.get("sample_interval", _DEFAULT_SAMPLE_INTERVAL)
    if not isinstance(interval, (int, float)) or interval <= 0:
        interval = _DEFAULT_SAMPLE_INTERVAL
    plot = table.get("plot", True)
    return max(float(interval), 0.1), bool(plot)


def _tree_rss_bytes(proc: Process) -> int:
    """Return the summed RSS of *proc* and every live descendant.

    Args:
        proc: The child's ``psutil.Process`` handle.

    Returns:
        Total resident-set bytes across the process tree; ``0`` when the
        whole tree has already exited (a race near process end, not an
        error).
    """
    total = 0
    # AccessDenied is treated like a vanished process: a sandboxed runner or
    # a child that re-execs under another user must degrade the sample, never
    # kill the wrapper mid-run (the child's exit code would be lost).
    try:
        procs = [proc, *proc.children(recursive=True)]  # type: ignore[attr-defined]
        for p in procs:
            try:
                total += p.memory_info().rss
            except (psutil.NoSuchProcess, psutil.AccessDenied):  # type: ignore[union-attr]
                continue
    except (psutil.NoSuchProcess, psutil.AccessDenied):  # type: ignore[union-attr]
        return 0
    return total


def _sample(proc: Process, started: float) -> Sample:
    """Take one sample of the child's tree RSS and host CPU.

    Args:
        proc: The child's ``psutil.Process`` handle.
        started: ``time.monotonic()`` at spawn time.

    Returns:
        The captured :class:`Sample`.
    """
    return Sample(
        elapsed=time.monotonic() - started,
        rss_mb=_tree_rss_bytes(proc) / _BYTES_PER_MB,
        cpu_percent=psutil.cpu_percent(interval=None),  # type: ignore[union-attr]
    )


def _format_log(
    cmd: Sequence[str],
    samples: list[Sample],
    exit_code: int,
    elapsed: float,
) -> str:
    """Render the plain-text telemetry report.

    Args:
        cmd: The wrapped command's argv.
        samples: The captured profile, in time order.
        exit_code: The child's exit code.
        elapsed: Total wall-clock seconds.

    Returns:
        The full ``telemetry.log`` body: a header, one line per sample, and
        a peak/mean summary.
    """
    lines = [
        f"command: {' '.join(cmd)}",
        f"duration: {elapsed:.1f}s   exit code: {exit_code}   samples: {len(samples)}",
        "",
    ]
    lines.extend(
        f"t={s.elapsed:8.1f}s  rss={s.rss_mb:9.1f}MB  cpu={s.cpu_percent:5.1f}%"
        for s in samples
    )
    summary = _summarize(samples)
    if summary is not None:
        lines += [
            "",
            (
                f"peak rss: {summary.peak_rss_mb:.1f}MB   "
                f"mean cpu: {summary.mean_cpu:.1f}%"
            ),
        ]
    return "\n".join(lines)


@dataclass(frozen=True)
class _Summary:
    """Aggregates of one run's samples.

    Attributes:
        peak_rss_mb: Highest process-tree RSS observed, in MB.
        mean_cpu: Mean host CPU percentage across samples.
    """

    peak_rss_mb: float
    mean_cpu: float


@dataclass(frozen=True)
class _RunHistory:
    """Information to append to the telemetry history log.

    Attributes:
        cmd: The wrapped command's argv.
        summary: Aggregates from :func:`_summarize` (``None`` → ``n/a``).
        exit_code: The child's exit code.
        elapsed: True wall-clock seconds, spawn to exit.
    """

    cmd: Sequence[str]
    summary: _Summary | None
    exit_code: int
    elapsed: float


def _summarize(samples: list[Sample]) -> _Summary | None:
    """Return the run's aggregate summary, or ``None`` for empty samples.

    Single source for the peak/mean math shared by the per-run log footer
    and the history line, so the two artifacts can never disagree.

    Args:
        samples: The captured profile, in time order.

    Returns:
        The aggregates, or ``None`` when nothing was sampled.
    """
    if not samples:
        return None
    return _Summary(
        peak_rss_mb=max(s.rss_mb for s in samples),
        mean_cpu=sum(s.cpu_percent for s in samples) / len(samples),
    )


def _append_history(root: Path, history: _RunHistory, label: str) -> None:
    """Append one summary line for this run to ``telemetry_history.log``.

    The per-run log is overwritten by design (step-log convention); this
    sidecar accumulates one ``key=value`` line per run so "what does this
    suite cost, over time" stays answerable (#376). Lives next to the other
    artifacts in ``code_health/`` (gitignored — history is per-workspace).

    Args:
        root: Repository root directory.
        history: Run information to append.
        label: Run label (empty when unlabeled).
    """
    peak = (
        f"{history.summary.peak_rss_mb:.1f}MB" if history.summary is not None else "n/a"
    )
    line = (
        f"ts={datetime.now(UTC).isoformat(timespec='seconds')}  "
        f"label={label or '-'}  exit={history.exit_code}  wall={history.elapsed:.1f}s  "
        f"peak_rss={peak}  cmd={' '.join(history.cmd)}\n"
    )
    out = root / "code_health" / "telemetry_history.log"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a", encoding="utf-8") as fh:
        fh.write(line)


def _render_plot(root: Path, samples: list[Sample], label: str = "") -> None:
    """Write ``code_health/telemetry[_<label>].png``, or log why it was skipped.

    Args:
        root: Repository root directory.
        samples: The captured profile, in time order.
        label: Run label suffixing the filename (empty → default name).
    """
    if not samples:
        return
    try:
        import matplotlib as mpl  # noqa: PLC0415 — optional dep, needed only here

        mpl.use("Agg")
        import matplotlib.pyplot as plt  # noqa: PLC0415 — after backend pin
    except ImportError:
        logger.info(
            "telemetry: %s — skipping the PNG chart (text log written).",
            missing_dependency_hint("matplotlib", extra="telemetry"),
        )
        return
    times = [s.elapsed for s in samples]
    fig, rss_axis = plt.subplots(figsize=(8, 4))
    rss_axis.plot(times, [s.rss_mb for s in samples], color="tab:blue")
    rss_axis.set_xlabel("seconds")
    rss_axis.set_ylabel("process-tree RSS (MB)", color="tab:blue")
    cpu_axis = rss_axis.twinx()
    cpu_axis.plot(times, [s.cpu_percent for s in samples], color="tab:orange")
    cpu_axis.set_ylabel("host CPU (%)", color="tab:orange")
    fig.tight_layout()
    stem = f"telemetry_{label}" if label else "telemetry"
    out = root / "code_health" / f"{stem}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out)
    plt.close(fig)
    logger.info("telemetry: wrote %s", out)


def run_command(
    cmd: Sequence[str],
    root: Path,
    *,
    capture: bool = False,
    cwd: Path | None = None,
    label: str = "",
) -> tuple[int, str]:
    """Run *cmd* under resource sampling and write the telemetry artifacts.

    The caller must have checked :func:`telemetry_available` — this function
    assumes ``psutil`` is importable. Output handling is the caller's
    contract: streaming mode (the CLI) inherits stdio so the child talks to
    the terminal directly; capture mode (the smart-test runner) collects
    combined stdout+stderr via a spool file, avoiding the pipe-deadlock a
    concurrently-sampled ``PIPE`` read would risk.

    Args:
        cmd: The command argv to wrap.
        root: Repository root (artifact destination).
        capture: Collect and return the child's combined output instead of
            streaming it.
        cwd: Working directory for the child (default: the caller's).
        label: Run label suffixing the artifacts
            (``telemetry_<label>.log/.png``) so a retry or a multi-tier run
            never overwrites the run before it; empty keeps the default
            names.

    Returns:
        ``(exit_code, output)`` — the child's exit code unchanged, and its
        combined output when *capture* is set (empty string otherwise).

    Raises:
        ValueError: If *label* is not a safe artifact suffix.
    """
    label = validated_label(label)
    interval, plot = _telemetry_config(root)
    started = time.monotonic()
    # ExitStack rather than a plain `with`: the spool exists only in capture
    # mode, and its lifetime must span the whole sampling loop up to the
    # post-exit read below.
    with contextlib.ExitStack() as stack:
        spool = stack.enter_context(tempfile.TemporaryFile()) if capture else None
        child = subprocess.Popen(
            list(cmd),
            cwd=cwd,
            stdout=spool if capture else None,
            stderr=subprocess.STDOUT if capture else None,
        )
        proc = psutil.Process(child.pid)  # type: ignore[union-attr]
        psutil.cpu_percent(interval=None)  # type: ignore[union-attr] — prime
        samples: list[Sample] = []
        # Sample first, then wait: the first sample lands at t≈0 (as the old
        # sample-then-sleep loop did), and `wait(timeout=)` returns the
        # moment the child exits instead of at the next tick — the old
        # poll+sleep shape added up to one full interval of latency and
        # quantized the reported duration to it (#376).
        while True:
            samples.append(_sample(proc, started))
            try:
                child.wait(timeout=interval)
            except subprocess.TimeoutExpired:
                continue
            break
        # True spawn→exit wall time — taken before the spool read, whose
        # cost for a large captured output must not inflate the figure.
        elapsed = time.monotonic() - started
        output = ""
        if spool is not None:
            spool.seek(0)
            output = spool.read().decode(errors="replace")
    log_name = f"telemetry_{label}" if label else "telemetry"
    log_path = write_step_log(
        root, log_name, _format_log(cmd, samples, child.returncode, elapsed)
    )
    logger.info("telemetry: wrote %s", log_path)
    history = _RunHistory(cmd, _summarize(samples), child.returncode, elapsed)
    _append_history(root, history, label)
    if plot:
        _render_plot(root, samples, label)
    return child.returncode, output


def main(argv: list[str] | None = None) -> int:
    """Run the telemetry CLI: ``forge-telemetry -- <cmd> ...``.

    Args:
        argv: Optional argument vector (defaults to ``sys.argv``).

    Returns:
        The wrapped command's exit code; ``1`` when ``psutil`` is missing;
        ``2`` when no command follows ``--``.
    """
    raw = sys.argv[1:] if argv is None else argv
    # Flags (none today, but -h/--help must work) are parsed only from the
    # tokens BEFORE the literal `--`; everything after is the child argv,
    # untouched — deliberately not argparse.REMAINDER, whose flag-vs-operand
    # ambiguity is exactly what a wrapped command must not inherit.
    if "--" in raw:
        split = raw.index("--")
        flags, cmd = raw[:split], raw[split + 1 :]
    else:
        flags, cmd = raw, []
    parser = argparse.ArgumentParser(
        prog="forge-telemetry",
        description="Sample process-tree RSS + host CPU while a command runs; "
        "write code_health/telemetry.log (+ .png with matplotlib).",
    )
    parser.add_argument(
        "--label",
        default=os.environ.get("FORGE_TELEMETRY_LABEL", ""),
        help="suffix the artifacts as telemetry_<label>.log/.png so a retry "
        "never overwrites the run before it (env: FORGE_TELEMETRY_LABEL; "
        "the flag wins)",
    )
    args = parser.parse_args(flags)
    if not cmd:
        logger.error(
            "forge-telemetry: no command given — usage: forge-telemetry -- <cmd> ..."
        )
        return 2
    if not telemetry_available():
        logger.error(
            "forge-telemetry: %s",
            missing_dependency_hint("psutil", extra="telemetry"),
        )
        return 1
    try:
        exit_code, _ = run_command(cmd, repo_root(), label=args.label)
    except ValueError:
        logger.exception("forge-telemetry error")
        return 2
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
