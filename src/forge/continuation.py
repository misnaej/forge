"""forge-continuation — keep ``.plan/CONTINUATION.md``'s status panel current.

The note opens with a fixed-size status panel that forge rewrites from
git and GitHub state, followed by a written section with a line budget
that only the agent and the user edit (FOUNDATION §10). The panel format,
the git-only fields and the rewrite live in :mod:`forge.continuation_state`
so ``forge-precommit`` can record every commit attempt without importing
this module's GitHub readers; this module adds the PR fields and the CLI.

Usage:

- ``forge-continuation state`` — rewrite the panel from local git state,
  carrying the last PR fields forward with their as-of label (offline).
- ``forge-continuation state --with-pr`` — also re-read the open PR's
  mergeability, CI and wrap-up freshness from GitHub.
- ``forge-continuation state --attempt <result>`` — record a commit
  attempt's result: ``passed``, ``blocked:<step>``, ``error`` or
  ``interrupted`` (free-form, sanitized).
- ``forge-continuation check`` — report the written section's line usage
  against ``[tool.forge.continuation].judgment_max_lines``; exits ``1``
  when the panel markers are mismatched or duplicated.

The first write over an old-format note drops the retired activity
ledger, digest and archive pointer and keeps the rest verbatim.
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from pathlib import Path

from forge.continuation_state import (
    CONTINUATION_PATH,
    PrState,
    judgment_max_lines,
    marker_problems,
    sanitize,
    short_head,
    write_state,
    written_line_count,
)
from forge.git_utils import (
    GH_TIMEOUT_S,
    base_sync,
    behind_ahead,
    configure_cli_logging,
    emit,
)
from forge.pr_plan import wrapup_freshness
from forge.pr_wrapup_compose import summarize_rollup
from forge.run_context import is_ci


configure_cli_logging()
logger = logging.getLogger(__name__)


_PR_FIELDS = "number,state,headRefOid,baseRefName,mergeable,statusCheckRollup,isDraft"
_NO_PR_MARKER = "no pull requests found"
_SHORT_SHA = 7
_FRESHNESS_WORD = {True: "fresh", False: "stale", None: "unknown"}


def _no_pr(root: Path) -> PrState:
    """Return the PR fields for a branch with no open PR.

    Args:
        root: Repo root.

    Returns:
        ``none`` fields labelled with the local HEAD.
    """
    return PrState(
        number="none",
        mergeable="n/a",
        ci="n/a",
        wrapup="n/a",
        as_of=short_head(root),
    )


def read_pr_state(root: Path) -> PrState | None:
    """Read the current branch's open PR from GitHub.

    Behind-base is counted against the local ``origin/<base>`` ref, never
    fetched, so the read costs two ``gh`` calls and no network git.

    Args:
        root: Repo root (``gh`` resolves the branch's PR from it).

    Returns:
        The PR's fields; ``none`` fields when the branch has no open PR;
        ``None`` when GitHub could not be read, so the caller carries the
        last known fields forward instead of overwriting them.
    """
    try:
        proc = subprocess.run(
            ["gh", "pr", "view", "--json", _PR_FIELDS],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
            timeout=GH_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("forge-continuation: could not read the PR (%s)", exc)
        return None
    if proc.returncode != 0:
        if _NO_PR_MARKER in proc.stderr.lower():
            return _no_pr(root)
        logger.warning(
            "forge-continuation: could not read the PR: %s",
            proc.stderr.strip() or f"gh exited {proc.returncode}",
        )
        return None
    try:
        view = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(view, dict) or str(view.get("state") or "") != "OPEN":
        return _no_pr(root)
    number = int(view.get("number") or 0)
    base = str(view.get("baseRefName") or "main")
    counts = None if base.startswith("-") else behind_ahead(root, f"origin/{base}")
    rollup = view.get("statusCheckRollup")
    return PrState(
        number=str(number),
        mergeable=base_sync(
            str(view.get("mergeable") or ""), counts[0] if counts else None
        ).summary,
        ci=sanitize(
            summarize_rollup(rollup, is_draft=view.get("isDraft") is True)
            if isinstance(rollup, list)
            else "no checks"
        ),
        wrapup=_FRESHNESS_WORD[wrapup_freshness(number).fresh],
        as_of=sanitize(str(view.get("headRefOid") or "")[:_SHORT_SHA]),
    )


def refresh_state(
    root: Path, *, with_pr: bool = False, attempt: str | None = None
) -> int:
    """Rewrite the status panel, optionally re-reading the PR first.

    Args:
        root: Repo root.
        with_pr: Re-read the open PR's fields from GitHub.
        attempt: A commit attempt's result to record, or ``None``.

    Returns:
        ``0`` when written or deliberately skipped; ``1`` when the note's
        markers refused the write.
    """
    if is_ci():
        logger.info("forge-continuation: CI run — status panel not written.")
        return 0
    pr = read_pr_state(root) if with_pr else None
    return 1 if write_state(root, attempt=attempt, pr=pr) == "refused" else 0


def check_note(root: Path) -> int:
    """Report the written section's line usage against its budget.

    Args:
        root: Repo root.

    Returns:
        ``1`` when the panel markers are mismatched or duplicated; else ``0``.
    """
    path = root / CONTINUATION_PATH
    if not path.is_file():
        emit(f"{CONTINUATION_PATH}: absent — nothing to check")
        return 0
    text = path.read_text(encoding="utf-8")
    problems = marker_problems(text)
    if problems:
        for problem in problems:
            emit(f"{CONTINUATION_PATH}: {problem}")
        return 1
    used, budget = written_line_count(text), judgment_max_lines(root)
    verdict = "over budget — trim it" if used > budget else "within budget"
    emit(f"{CONTINUATION_PATH}: written section {used}/{budget} lines ({verdict})")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    """Build the ``forge-continuation`` argument parser.

    Returns:
        The parser.
    """
    parser = argparse.ArgumentParser(
        prog="forge-continuation",
        description=(
            "Keep .plan/CONTINUATION.md's generated status panel current and "
            "check its written section's line budget."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    state = sub.add_parser(
        "state",
        help="Rewrite the status panel; every byte outside it stays identical.",
    )
    state.add_argument(
        "--with-pr",
        action="store_true",
        help="Re-read the open PR's mergeability, CI and wrap-up freshness "
        "from GitHub; without it the last PR fields carry forward with "
        "their as-of label.",
    )
    state.add_argument(
        "--attempt",
        metavar="RESULT",
        help=(
            "Record a commit attempt: `passed`, `blocked:<step>`, `error` "
            "or `interrupted` (free-form; sanitized)."
        ),
    )
    sub.add_parser(
        "check",
        help="Report the written section's line usage against "
        "[tool.forge.continuation].judgment_max_lines; exit 1 on bad markers.",
    )
    return parser


def main(argv: list[str] | None = None, *, repo_root: Path | None = None) -> int:
    """Run ``forge-continuation``.

    Args:
        argv: Argument vector; ``None`` reads ``sys.argv``.
        repo_root: Repository whose note is written; ``None`` uses the
            current directory, as the CLI does.

    Returns:
        The subcommand's exit code.
    """
    args = _build_parser().parse_args(argv)
    root = repo_root if repo_root is not None else Path.cwd()
    if args.command == "check":
        return check_note(root)
    return refresh_state(root, with_pr=args.with_pr, attempt=args.attempt)


if __name__ == "__main__":
    sys.exit(main())
