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
- ``forge-continuation reset`` — replace the written section with a
  skeleton (§10's five headings), regenerate the panel, never delete the
  file. Refuses while the panel shows live work unless ``--force``.

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
from typing import Final

from forge.continuation_state import (
    BEGIN_MARKER,
    CONTINUATION_PATH,
    END_MARKER,
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


#: The five headings of the written-section skeleton (§10).
_WRITTEN_SKELETON: Final = """\
## Intent

## Decisions and why

## What was rejected

## Open question

## Next step
"""


def reset_note(root: Path, *, force: bool = False) -> int:
    """Replace the written section with a skeleton and regenerate the panel.

    Refuses while the panel shows live work (unpushed commits, dirty tree,
    open PR, stale wrap-up) unless *force*; never deletes the file.

    Args:
        root: Repo root.
        force: Override the live-work refusal.

    Returns:
        ``0`` on success, ``1`` when refused.
    """
    if is_ci():
        logger.info("forge-continuation: CI run — reset skipped.")
        return 0
    path = root / CONTINUATION_PATH
    if not force and path.is_file():
        text = path.read_text(encoding="utf-8")
        blockers = _live_work_signals(root, text)
        if blockers:
            for b in blockers:
                logger.warning("forge-continuation: reset refused — %s", b)
            logger.warning("forge-continuation: rerun with --force to override.")
            return 1
    # Regenerate the panel first so the note always carries a fresh panel.
    write_state(root)
    if not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"{BEGIN_MARKER}\n{END_MARKER}\n\n{_WRITTEN_SKELETON}", encoding="utf-8"
        )
        logger.info(
            "forge-continuation: reset — created %s with skeleton.", CONTINUATION_PATH
        )
        return 0
    text = path.read_text(encoding="utf-8")
    problems = marker_problems(text)
    if problems:
        for p in problems:
            logger.error("forge-continuation: %s", p)
        return 1
    text.index(BEGIN_MARKER)
    end = text.index(END_MARKER) + len(END_MARKER)
    new_text = text[:end] + "\n\n" + _WRITTEN_SKELETON
    path.write_text(new_text, encoding="utf-8")
    logger.info("forge-continuation: reset — written section replaced with skeleton.")
    return 0


def _live_work_signals(root: Path, text: str) -> list[str]:
    """Return live-work signals visible in the current panel text.

    Args:
        root: Repo root.
        text: The note's current content.

    Returns:
        Blocking signal descriptions; empty when the panel shows no live work.
    """
    signals: list[str] = []
    panel = text
    if BEGIN_MARKER in panel and END_MARKER in panel:
        panel = panel[panel.index(BEGIN_MARKER) : panel.index(END_MARKER)]
    if "unpushed:" in panel and "unpushed: 0" not in panel:
        signals.append("the panel shows unpushed commits")
    try:
        dirty = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        ).stdout.strip()
        if dirty:
            signals.append("the working tree is dirty")
    except (OSError, subprocess.SubprocessError):
        pass
    if "pr:" in panel and "pr: none" not in panel and "pr: unknown" not in panel:
        signals.append("the panel shows an open PR")
    return signals


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
    reset = sub.add_parser(
        "reset",
        help="Replace the written section with a skeleton and regenerate the "
        "panel; refuses on live work unless --force.",
    )
    reset.add_argument(
        "--force",
        action="store_true",
        help="Override the live-work refusal (unpushed commits, dirty tree, open PR).",
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
    if args.command == "reset":
        return reset_note(root, force=args.force)
    return refresh_state(root, with_pr=args.with_pr, attempt=args.attempt)


if __name__ == "__main__":
    sys.exit(main())
