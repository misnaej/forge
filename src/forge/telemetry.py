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
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
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
    try:
        procs = [proc, *proc.children(recursive=True)]  # type: ignore[attr-defined]
        for p in procs:
            try:
                total += p.memory_info().rss
            except psutil.NoSuchProcess:  # type: ignore[union-attr]
                continue
    except psutil.NoSuchProcess:  # type: ignore[union-attr]
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
    if samples:
        peak = max(s.rss_mb for s in samples)
        mean_cpu = sum(s.cpu_percent for s in samples) / len(samples)
        lines += ["", f"peak rss: {peak:.1f}MB   mean cpu: {mean_cpu:.1f}%"]
    return "\n".join(lines)


def _render_plot(root: Path, samples: list[Sample]) -> None:
    """Write ``code_health/telemetry.png``, or log why it was skipped.

    Args:
        root: Repository root directory.
        samples: The captured profile, in time order.
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
    out = root / "code_health" / "telemetry.png"
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

    Returns:
        ``(exit_code, output)`` — the child's exit code unchanged, and its
        combined output when *capture* is set (empty string otherwise).
    """
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
        while child.poll() is None:
            samples.append(_sample(proc, started))
            time.sleep(interval)
        elapsed = time.monotonic() - started
        output = ""
        if spool is not None:
            spool.seek(0)
            output = spool.read().decode(errors="replace")
    write_step_log(
        root, "telemetry", _format_log(cmd, samples, child.returncode, elapsed)
    )
    logger.info("telemetry: wrote %s", root / "code_health" / "telemetry.log")
    if plot:
        _render_plot(root, samples)
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
    parser.parse_args(flags)
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
    exit_code, _ = run_command(cmd, repo_root())
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
