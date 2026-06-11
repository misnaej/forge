"""Orchestrator: run every forge-audit-* script and write a summary log.

Each sub-audit writes its own ``code_health/audit_<name>.log``. This script
also emits ``code_health/audit_summary.log`` with per-audit finding counts
and overall exit-code disposition.

Exit code is the maximum of the sub-audit exit codes.
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from forge.audit.common import CODE_HEALTH_DIR
from forge.git_utils import configure_cli_logging, repo_root, require_cli


configure_cli_logging()
logger = logging.getLogger(__name__)


# Order matters: cheap deterministic scans first, then heavier ones.
SUB_AUDITS: tuple[str, ...] = (
    "suppressions",
    "agents",
    "dup",
    "deps",
    "orphans",
    "data",
    "claims",
)


@dataclass(frozen=True)
class SubResult:
    """Outcome of running one sub-audit.

    Attributes:
        name: Audit short name (e.g. ``"dup"``).
        exit_code: Process exit code from the sub-audit.
        log_path: Repo-relative path to the audit log.
        finding_count: Findings reported, parsed from the log header.
    """

    name: str
    exit_code: int
    log_path: str
    finding_count: int


def _read_finding_count(log_text: str) -> int:
    """Parse the ``# findings: N`` header line from a log.

    Args:
        log_text: Full log contents.

    Returns:
        Integer count, or ``-1`` if the header line is missing.
    """
    for line in log_text.splitlines()[:10]:
        if line.startswith("# findings:"):
            try:
                return int(line.split(":", 1)[1].strip())
            except ValueError:
                return -1
    return -1


def _run_one(name: str, scope: str, roots: list[str] | None) -> SubResult:
    """Invoke a sub-audit CLI and parse its log.

    Invokes the ``forge-audit-<name>`` console script. Fails loudly with
    an install hint when the CLI is absent (FOUNDATION §2).

    Args:
        name: Audit short name (e.g. ``"dup"``).
        scope: ``"full"`` or ``"changed"``, forwarded to the sub-audit.
        roots: Optional ``--roots`` override; forwarded if non-empty.

    Returns:
        ``SubResult`` capturing exit code + finding count.

    Raises:
        SystemExit: If ``forge-audit-<name>`` is not on PATH.
    """
    cli = f"forge-audit-{name}"
    require_cli(cli, caller="forge-audit-all")
    cmd = [cli, "--scope", scope]
    if roots:
        cmd += ["--roots", *roots]
    logger.info("running %s", " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.stdout:
        sys.stdout.write(proc.stdout)
    if proc.stderr:
        sys.stderr.write(proc.stderr)

    log_rel = f"{CODE_HEALTH_DIR}/audit_{name}.log"
    log_abs = repo_root() / log_rel
    if log_abs.exists():
        count = _read_finding_count(log_abs.read_text(encoding="utf-8"))
    else:
        count = -1
    return SubResult(
        name=name,
        exit_code=proc.returncode,
        log_path=log_rel,
        finding_count=count,
    )


def _render_summary(results: list[SubResult]) -> str:
    """Render the aggregate summary log text.

    Args:
        results: Per-sub-audit outcomes.

    Returns:
        Multi-line log content suitable for writing to disk.
    """
    timestamp = datetime.now(UTC).isoformat(timespec="seconds")
    lines = [
        "# forge-audit-all",
        f"# generated: {timestamp}",
        f"# subaudits: {len(results)}",
        "",
        "## Per-audit results",
        "",
        f"{'audit':<14} {'exit':<5} {'findings':<10} log",
    ]
    for r in results:
        count = "n/a" if r.finding_count < 0 else str(r.finding_count)
        lines.append(f"{r.name:<14} {r.exit_code:<5} {count:<10} {r.log_path}")
    lines.append("")
    return "\n".join(lines) + "\n"


def main() -> int:
    """Run every sub-audit and write ``code_health/audit_summary.log``.

    Returns:
        Maximum exit code across sub-audits.
    """
    # Hand-rolled parser (not ``make_audit_parser``) because forge-audit-all
    # adds a ``--only`` switch the per-audit CLIs do not support, and the
    # ``--output`` semantics differ (summary path vs per-audit findings path).
    parser = argparse.ArgumentParser(
        prog="forge-audit-all",
        description="Run every forge-audit-* script and aggregate results.",
    )
    parser.add_argument("--scope", choices=["full", "changed"], default="full")
    parser.add_argument("--roots", nargs="*", default=None)
    parser.add_argument(
        "--only",
        nargs="*",
        choices=SUB_AUDITS,
        help="Run only these sub-audits (default: all).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Override summary log path (default: code_health/audit_summary.log).",
    )
    args = parser.parse_args()

    selected = tuple(args.only) if args.only else SUB_AUDITS
    results = [_run_one(name, args.scope, args.roots) for name in selected]

    if args.output is not None:
        summary_path = args.output
    else:
        summary_path = repo_root() / CODE_HEALTH_DIR / "audit_summary.log"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(_render_summary(results), encoding="utf-8")
    logger.info("wrote %s", summary_path)

    return max((r.exit_code for r in results), default=0)


if __name__ == "__main__":
    sys.exit(main())
