"""forge-plan-check — the command line over the plan checks.

A thin dispatcher, separate so the dependency direction stays one way:
the single-plan checks (:mod:`forge.plan_staleness`) and the set checks
(:mod:`forge.plan_tracks`) both import the shared helpers, and only this
module imports both. What each subcommand checks, and the exit codes,
are documented in those two modules.
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import TYPE_CHECKING

from forge.config import load_config
from forge.git_utils import (
    configure_cli_logging,
    emit,
    fetch_quietly,
    repo_root,
    resolve_base_branch_ref,
)
from forge.plan_staleness import (
    Report,
    check_drift,
    check_overlap,
    check_prerequisites,
    git_merge_log,
    parse_timestamp,
)
from forge.plan_staleness_gh import GhSource
from forge.plan_tracks import (
    check_current,
    check_merge_order,
    check_tracks,
    fetch_pr_heads,
    git_merge_probe,
)


if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path

    from forge.plan_staleness import GitHubSource


configure_cli_logging()
logger = logging.getLogger(__name__)


def _since_arg(value: str) -> datetime:
    """Argparse type for ``--since``: an ISO-8601 timestamp.

    Args:
        value: Command-line text to parse.

    Returns:
        The parsed timestamp.
    """
    try:
        return parse_timestamp(value)
    except ValueError as exc:
        msg = f"not an ISO-8601 timestamp: {value!r}"
        raise argparse.ArgumentTypeError(msg) from exc


def _add_base(parser: argparse.ArgumentParser) -> None:
    """Add the shared ``--base`` option to *parser*.

    Args:
        parser: A subcommand parser.
    """
    parser.add_argument(
        "--base", help="Base branch (default: [tool.forge].base_branch)."
    )


def _build_parser() -> argparse.ArgumentParser:
    """Return the ``forge-plan-check`` argument parser."""
    parser = argparse.ArgumentParser(
        prog="forge-plan-check",
        description="Mechanical premise and prerequisite checks for a plan, or a "
        "set of issues or PRs (exit 0 clean, 1 finding, 2 unknown or refused).",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    prereq = sub.add_parser("prerequisites", help="Have all Requires: entries landed?")
    prereq.add_argument("issue", type=int)
    drift = sub.add_parser("drift", help="Merges touching the plan's files since.")
    drift.add_argument("issue", type=int)
    drift.add_argument(
        "--since",
        type=_since_arg,
        help="ISO-8601 cut-off (default: when the authenticated plan was posted).",
    )
    _add_base(drift)
    overlap = sub.add_parser("overlap", help="Open issues naming a merged PR's files.")
    overlap.add_argument("pr", type=int)
    tracks = sub.add_parser(
        "tracks", help="Open issues sharing files with each other and with open PRs."
    )
    tracks.add_argument(
        "issues", type=int, nargs="*", help="Issues to consider (default: all open)."
    )
    tracks.add_argument(
        "--focus",
        type=int,
        nargs="+",
        default=[],
        metavar="ISSUE",
        help="Report only the overlaps involving these issues (e.g. newly filed).",
    )
    current = sub.add_parser(
        "current", help="Merges touching each issue's files since it was filed."
    )
    current.add_argument("issues", type=int, nargs="+")
    _add_base(current)
    order = sub.add_parser(
        "merge-order", help="Probe open PRs for conflicts and suggest a merge order."
    )
    order.add_argument(
        "prs", type=int, nargs="*", help="PRs to order (default: all open)."
    )
    _add_base(order)
    return parser


def _base_ref(base: str | None, root: Path) -> tuple[str, str] | Report:
    """Resolve and refresh the base ref, or return the refusal.

    Args:
        base: ``--base`` value, or None for the configured base branch.
        root: Repository root.

    Returns:
        ``(base_branch, base_ref)``, or a report holding the refusal.
    """
    base_branch = base or load_config(root).base_branch
    base_ref = resolve_base_branch_ref(root, base_branch)
    report = Report()
    if base_ref is None:
        report.unknown(f"refused: base branch {base_branch!r} does not resolve")
        return report
    if base_ref.startswith("origin/") and not fetch_quietly(
        root, "origin", base_branch
    ):
        report.unknown(f"unknown: could not fetch {base_ref}; history may be stale")
        return report
    return base_branch, base_ref


def _local_note(report: Report, base_ref: str) -> Report:
    """Open *report* with a note when history came from an unfetched local ref.

    Args:
        report: The check's report.
        base_ref: The resolved base ref.

    Returns:
        *report*, annotated.
    """
    if not base_ref.startswith("origin/"):
        report.lines.insert(
            0, f"note: history read from local {base_ref}; it was not fetched"
        )
    return report


def _run_drift(args: argparse.Namespace, source: GitHubSource) -> Report:
    """Resolve and refresh the base ref, then run the drift check.

    Args:
        args: Parsed command-line arguments.
        source: GitHub reader to query.

    Returns:
        The drift report.
    """
    root = repo_root()
    resolved = _base_ref(args.base, root)
    if isinstance(resolved, Report):
        return resolved
    base_ref = resolved[1]
    report = check_drift(
        source, args.issue, since=args.since, log_merges=git_merge_log(root, base_ref)
    )
    return _local_note(report, base_ref)


def _run_current(args: argparse.Namespace, source: GitHubSource) -> Report:
    """Resolve and refresh the base ref, then run the set currency check.

    Args:
        args: Parsed command-line arguments.
        source: GitHub reader to query.

    Returns:
        The report.
    """
    root = repo_root()
    resolved = _base_ref(args.base, root)
    if isinstance(resolved, Report):
        return resolved
    base_ref = resolved[1]
    report = check_current(
        source, args.issues, log_merges=git_merge_log(root, base_ref)
    )
    return _local_note(report, base_ref)


def _run_merge_order(args: argparse.Namespace, source: GitHubSource) -> Report:
    """List open PRs on the base, fetch their heads, then probe and order them.

    Args:
        args: Parsed command-line arguments.
        source: GitHub reader to query.

    Returns:
        The report.
    """
    root = repo_root()
    resolved = _base_ref(args.base, root)
    if isinstance(resolved, Report):
        return resolved
    base_branch, base_ref = resolved
    report = Report()
    listed = source.open_pull_requests()
    if listed is None:
        report.unknown("unknown: could not list open PRs from GitHub")
        return report
    named = set(args.prs)
    for number in sorted(named - {pr.number for pr in listed}):
        report.unknown(f"refused: PR #{number} is not open")
    candidates = []
    for pr in listed:
        if named and pr.number not in named:
            continue
        if pr.base_ref != base_branch:
            report.note(f"note: PR #{pr.number} targets another branch; skipped")
            continue
        candidates.append(pr)
    fetched = fetch_pr_heads(root, candidates, report)
    report.absorb(check_merge_order(fetched, base_ref, git_merge_probe(root)))
    return _local_note(report, base_ref)


def main(argv: list[str] | None = None, source: GitHubSource | None = None) -> int:
    """Run one ``forge-plan-check`` subcommand and print its report.

    Args:
        argv: Argument vector (defaults to ``sys.argv``).
        source: GitHub reads; defaults to :class:`GhSource`.

    Returns:
        0 clean, 1 finding, 2 unknown or refused.
    """
    args = _build_parser().parse_args(argv)
    gh: GitHubSource = source if source is not None else GhSource()
    runners = {
        "prerequisites": lambda: check_prerequisites(gh, args.issue),
        "drift": lambda: _run_drift(args, gh),
        "overlap": lambda: check_overlap(gh, args.pr),
        "tracks": lambda: check_tracks(gh, args.issues, focus=args.focus),
        "current": lambda: _run_current(args, gh),
        "merge-order": lambda: _run_merge_order(args, gh),
    }
    report = runners[args.command]()
    emit(report.render())
    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())
