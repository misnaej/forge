"""``forge-metrics``: what each merged PR cost, kept in a local database.

One row per merged PR in a SQLite file under the per-user data directory
(:func:`forge.git_utils.forge_data_dir`), shared by every checkout of the
repo and never committed. It lives outside the per-checkout
``code_health/`` (FOUNDATION §13) because one PR can be worked from any
checkout of the repo. Tables:

- ``prs`` — size from GitHub (files, lines, commits), wrap-ups posted, and
  from Claude Code transcripts the review rounds (most runs of one
  reporter type), pre-commit and smart-test runs, active and wall time;
- ``pr_tokens`` — tokens per agent type (``main`` = the main session);
- ``unattributed`` — tokens spent on the base branch, per UTC day.

Tokens are attributed by the ``gitBranch`` Claude Code stamps on every
transcript record, up to the PR's merge (and after any earlier merge of a
PR with the same branch name). Transcripts are purged after about 30
days, so a PR is recorded at merge (``record``, wired into ``/next`` and
the PR monitor) and older ones while their transcripts last
(``backfill``). Reading back — ``report`` and ``export`` — lives in
:mod:`forge.pr_metrics_report`.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import quote

from forge.agent_profile import (
    TranscriptFilter,
    active_and_wall,
    branch_usage,
    repo_transcript_dirs,
)
from forge.agent_profile_tokens import TokenUsage, fmt_tokens, parse_ts
from forge.config import load_config
from forge.git_utils import (
    configure_cli_logging,
    emit,
    forge_data_dir,
    gh_api,
    github_slug,
    repo_root,
    require_cli,
)
from forge.pr_metrics_report import (
    export_payload,
    load_rows,
    render_html,
    render_report,
    usage_values,
)
from forge.regen_docs import forge_version


if TYPE_CHECKING:
    from collections.abc import Iterable

    from forge.agent_profile_tokens import BranchSummary, BranchUsageTally


configure_cli_logging()
logger = logging.getLogger(__name__)

DB_NAME: Final = "metrics.sqlite"
#: Merged PRs ``backfill`` looks at: GitHub's one-page maximum, which
#: reaches back past the transcripts' ~30-day lifetime for most repos.
BACKFILL_WINDOW: Final = 100
#: ``gh api`` may page through many comments; give it longer than the default.
_GH_TIMEOUT_S: Final = 60
#: Work on a branch often starts before its PR is opened.
PRE_PR_SLACK: Final = timedelta(days=1)
#: Reporters every finalization pass runs once; their run counts give
#: the review rounds.
REPORTER_TYPES: Final = (
    "forge:design-checker",
    "forge:security-checker",
    "forge:docs-types-checker",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS prs (
    pr INTEGER PRIMARY KEY,
    title TEXT,
    head_branch TEXT,
    merged_at TEXT,
    files INTEGER,
    additions INTEGER,
    deletions INTEGER,
    commits INTEGER,
    review_rounds INTEGER,
    wrapups_posted INTEGER,
    precommit_runs INTEGER,
    test_runs INTEGER,
    active_s REAL,
    wall_s REAL,
    recorded_at TEXT,
    forge_version TEXT
);
CREATE TABLE IF NOT EXISTS pr_tokens (
    pr INTEGER NOT NULL REFERENCES prs(pr),
    agent_type TEXT NOT NULL,
    requests INTEGER, input INTEGER, cache_write INTEGER,
    cache_read INTEGER, output INTEGER,
    PRIMARY KEY (pr, agent_type)
);
CREATE TABLE IF NOT EXISTS unattributed (
    date TEXT NOT NULL,
    agent_type TEXT NOT NULL,
    requests INTEGER, input INTEGER, cache_write INTEGER,
    cache_read INTEGER, output INTEGER,
    PRIMARY KEY (date, agent_type)
);
"""


@dataclass(frozen=True)
class PrFacts:
    """What GitHub says about one merged PR."""

    number: int
    title: str
    head: str
    merged_at: str | None
    files: int
    additions: int
    deletions: int
    commits: int
    #: Comments opening with a ``verified-at:`` header.
    wrapups_posted: int
    #: Merge time of the newest earlier PR from the same head branch name;
    #: usage up to then belongs to that PR.
    after: str | None = None
    #: When the PR was opened.
    created_at: str | None = None


def open_db(path: Path) -> sqlite3.Connection:
    """Open the metrics database, creating file and schema on first use.

    Args:
        path: The SQLite file.

    Returns:
        An open connection.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    return conn


def pr_summary(tally: BranchUsageTally, facts: PrFacts) -> BranchSummary | None:
    """The PR's share of its head branch's transcript records.

    Only records after the previous same-named PR merged and up to this
    one's merge count, so a reused branch name is not charged twice.

    Args:
        tally: Transcript records of every checkout of the repo.
        facts: The PR.

    Returns:
        The summary, or ``None`` when no record falls in the window.
    """
    return tally.summary(
        facts.head, after=parse_ts(facts.after), until=parse_ts(facts.merged_at)
    )


def review_rounds(summary: BranchSummary) -> int:
    """Review rounds on the PR, from reporter runs.

    Each finalization pass runs every reporter once.

    Args:
        summary: The PR's records.

    Returns:
        The most runs any one reporter type made; ``0`` when none ran.
    """
    return max(summary.runs.get(agent, 0) for agent in REPORTER_TYPES)


def record_pr(
    conn: sqlite3.Connection,
    facts: PrFacts,
    tally: BranchUsageTally,
    *,
    now: datetime,
) -> None:
    """Replace *facts*' PR rows with what GitHub and the transcripts say now.

    Args:
        conn: The metrics database.
        facts: The PR, from GitHub.
        tally: Transcript usage of every checkout of the repo.
        now: Recording time.
    """
    summary = pr_summary(tally, facts)
    active = wall = precommit = tests = rounds = None
    tokens: dict[str, TokenUsage] = {}
    if summary is not None:
        active, wall = active_and_wall(summary.timestamps)
        precommit, tests = summary.precommit_runs, summary.test_runs
        rounds, tokens = review_rounds(summary), summary.tokens
    with conn:
        conn.execute("DELETE FROM pr_tokens WHERE pr = ?", (facts.number,))
        conn.execute("DELETE FROM prs WHERE pr = ?", (facts.number,))
        conn.execute(
            "INSERT INTO prs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                facts.number,
                facts.title,
                facts.head,
                facts.merged_at,
                facts.files,
                facts.additions,
                facts.deletions,
                facts.commits,
                rounds,
                facts.wrapups_posted,
                precommit,
                tests,
                active,
                wall,
                now.isoformat(timespec="seconds"),
                forge_version(),
            ),
        )
        conn.executemany(
            "INSERT INTO pr_tokens VALUES (?,?,?,?,?,?,?)",
            [
                (facts.number, agent_type, *usage_values(usage))
                for agent_type, usage in sorted(tokens.items())
            ],
        )


def record_unattributed(
    conn: sqlite3.Connection, tally: BranchUsageTally, base_branch: str
) -> None:
    """Keep base-branch usage per day and agent type.

    A day's figure only grows: once its oldest transcripts are purged, a
    later scan sees less, and that must not overwrite the fuller count.

    Args:
        conn: The metrics database.
        tally: Transcript usage.
        base_branch: The repo's base branch.
    """
    rows = [
        (day, agent_type, *usage_values(usage))
        for (day, agent_type), usage in sorted(tally.daily(base_branch).items())
    ]
    with conn:
        conn.executemany(
            "INSERT INTO unattributed VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(date, agent_type) DO UPDATE SET "
            "requests = excluded.requests, input = excluded.input, "
            "cache_write = excluded.cache_write, "
            "cache_read = excluded.cache_read, output = excluded.output "
            "WHERE excluded.requests >= unattributed.requests",
            rows,
        )


# --- GitHub ----------------------------------------------------------------


def _int(value: object) -> int:
    """A JSON count as int, ``0`` for anything else.

    Args:
        value: The decoded value.

    Returns:
        *value* when it is a non-negative int, else ``0``.
    """
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return 0


def facts_from_api(
    pull: dict[str, Any], wrapups: int, same_head_merges: Iterable[str] = ()
) -> PrFacts | None:
    """Build :class:`PrFacts` from a ``GET /pulls/N`` response.

    Args:
        pull: The decoded pull request.
        wrapups: Wrap-ups posted on it.
        same_head_merges: Merge times of PRs from the same head branch name.

    Returns:
        The facts, or ``None`` when the response names no PR number.
    """
    number = pull.get("number")
    if not isinstance(number, int) or isinstance(number, bool):
        return None
    head = pull.get("head")
    ref = head.get("ref") if isinstance(head, dict) else None
    merged_at = pull.get("merged_at")
    merged_at = merged_at if isinstance(merged_at, str) else None
    created = pull.get("created_at")
    merged_time = parse_ts(merged_at)
    earlier = [
        (when, raw)
        for raw in same_head_merges
        if (when := parse_ts(raw)) is not None
        and (merged_time is None or when < merged_time)
    ]
    return PrFacts(
        number=number,
        title=str(pull.get("title") or ""),
        head=ref if isinstance(ref, str) else "",
        merged_at=merged_at,
        files=_int(pull.get("changed_files")),
        additions=_int(pull.get("additions")),
        deletions=_int(pull.get("deletions")),
        commits=_int(pull.get("commits")),
        wrapups_posted=wrapups,
        after=max(earlier)[1] if earlier else None,
        created_at=created if isinstance(created, str) else None,
    )


def fetch_facts(number: int) -> PrFacts | None:
    """Read PR *number* and count its wrap-ups through ``gh api``.

    Args:
        number: The PR.

    Returns:
        The facts, or ``None`` when GitHub could not be read.
    """
    raw = gh_api(f"repos/{{owner}}/{{repo}}/pulls/{number}")
    try:
        pull = json.loads(raw) if raw else None
    except ValueError:
        pull = None
    if not isinstance(pull, dict):
        return None
    # One number per page; summing them counts across pages.
    pages = gh_api(
        "--paginate",
        f"repos/{{owner}}/{{repo}}/issues/{number}/comments",
        "--jq",
        '[.[] | select((.body // "") | startswith("verified-at:"))] | length',
        timeout=_GH_TIMEOUT_S,
    )
    wrapups = sum(int(line) for line in (pages or "").split() if line.isdigit())
    head = pull.get("head")
    ref = head.get("ref") if isinstance(head, dict) else None
    merges = (
        gh_api(
            "repos/{owner}/{repo}/pulls?state=closed&per_page=100"
            f"&head={{owner}}:{quote(ref, safe='')}",
            "--jq",
            ".[] | .merged_at // empty",
            timeout=_GH_TIMEOUT_S,
        )
        if isinstance(ref, str) and ref
        else None
    )
    return facts_from_api(pull, wrapups, (merges or "").split())


def merged_prs() -> list[tuple[int, str]]:
    """The newest merged PRs, as ``(number, head branch)``.

    Returns:
        Up to :data:`BACKFILL_WINDOW` PRs, newest first; empty when
        GitHub could not be read.
    """
    raw = gh_api(
        f"repos/{{owner}}/{{repo}}/pulls?state=closed&per_page={BACKFILL_WINDOW}",
        "--jq",
        r'.[] | select(.merged_at != null) | "\(.number)\t\(.head.ref)"',
        timeout=_GH_TIMEOUT_S,
    )
    found: list[tuple[int, str]] = []
    for line in (raw or "").splitlines():
        number, _, head = line.partition("\t")
        if number.isdigit() and head:
            found.append((int(number), head))
    return found


def backfill(
    conn: sqlite3.Connection,
    merged: Iterable[tuple[int, str]],
    tally: BranchUsageTally,
    *,
    limit: int,
) -> list[int]:
    """Record merged PRs not yet in the database whose branch has usage.

    Args:
        conn: The metrics database.
        merged: ``(number, head branch)`` candidates, newest first.
        tally: Transcript usage.
        limit: Most PRs to record.

    Returns:
        The PR numbers recorded.
    """
    recorded = {row[0] for row in conn.execute("SELECT pr FROM prs")}
    with_usage = tally.branches_with_usage()
    todo = [n for n, head in merged if n not in recorded and head in with_usage]
    done: list[int] = []
    for number in todo[:limit]:
        facts = fetch_facts(number)
        if facts is None:
            logger.warning("forge-metrics: could not read PR #%s; skipped", number)
            continue
        record_pr(conn, facts, tally, now=datetime.now(UTC))
        done.append(number)
    return done


# --- CLI -------------------------------------------------------------------


def default_db_path(root: Path) -> Path | None:
    """Where this repo's metrics database lives.

    Args:
        root: The checkout.

    Returns:
        ``<data dir>/metrics.sqlite``, or ``None`` when *root* has no
        GitHub ``origin`` to key it by.
    """
    slug = github_slug(root)
    return forge_data_dir(slug) / DB_NAME if slug else None


def _scan(
    root: Path, explicit: list[Path] | None, keep: TranscriptFilter | None = None
) -> BranchUsageTally:
    """Read the transcripts of every checkout of the repo (or *explicit*).

    Args:
        root: The checkout.
        explicit: Directories to read instead, when given.
        keep: Files worth parsing; all when ``None``.

    Returns:
        The usage tally.
    """
    dirs = explicit or repo_transcript_dirs(root)
    if not dirs:
        logger.info("forge-metrics: no Claude Code transcripts found; tokens unknown")
    return branch_usage(dirs, keep)


def pr_filter(facts: PrFacts, base_branch: str) -> TranscriptFilter:
    """The transcript files one PR's record needs.

    A file last written before the PR was opened (less a day, for work
    started on the branch before the PR existed) cannot hold its records.

    Args:
        facts: The PR.
        base_branch: Also kept, for the unattributed bucket.

    Returns:
        The filter.
    """
    opened = parse_ts(facts.created_at)
    return TranscriptFilter(
        since=opened - PRE_PR_SLACK if opened else None,
        branches=frozenset({facts.head, base_branch}),
    )


def _build_parser() -> argparse.ArgumentParser:
    """The ``forge-metrics`` argument parser.

    Returns:
        The parser.
    """
    parser = argparse.ArgumentParser(
        prog="forge-metrics",
        description="Per-PR cost metrics (tokens, time, size) in a local database.",
    )
    parser.add_argument("--db", type=Path, help="Database file (default: per repo).")
    parser.add_argument(
        "--transcripts",
        type=Path,
        action="append",
        help="Transcript directory to read instead of every checkout's (repeatable).",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    record = sub.add_parser("record", help="Record one merged PR (replaces its rows).")
    record.add_argument("--pr", type=int, required=True)
    fill = sub.add_parser("backfill", help="Record merged PRs not yet recorded.")
    fill.add_argument("--limit", type=int, default=20)
    report = sub.add_parser("report", help="Print per-PR cost and the trend.")
    report.add_argument("--last", type=int, default=20)
    report.add_argument("--html", type=Path, help="Also write a self-contained page.")
    export = sub.add_parser("export", help="Write figures-only JSON for sharing.")
    export.add_argument("--out", type=Path, help="File to write (default: stdout).")
    return parser


def _record(conn: sqlite3.Connection, root: Path, args: argparse.Namespace) -> int:
    """Run ``record`` or ``backfill``.

    Args:
        conn: The metrics database.
        root: The checkout.
        args: Parsed arguments.

    Returns:
        Exit code.
    """
    require_cli("gh", caller="forge-metrics", hint="Install GitHub CLI (gh).")
    base = load_config(root).base_branch
    if args.command == "backfill":
        # One walk serves every PR: each is a windowed summary of it.
        tally = _scan(root, args.transcripts)
        record_unattributed(conn, tally, base)
        done = backfill(conn, merged_prs(), tally, limit=args.limit)
        emit(f"forge-metrics: recorded {len(done)} PR(s): {done}")
        return 0
    facts = fetch_facts(args.pr)
    if facts is None:
        logger.error("forge-metrics: could not read PR #%s through gh", args.pr)
        return 1
    tally = _scan(root, args.transcripts, pr_filter(facts, base))
    record_unattributed(conn, tally, base)
    record_pr(conn, facts, tally, now=datetime.now(UTC))
    summary = pr_summary(tally, facts)
    tokens = sum(u.new_tokens for u in summary.tokens.values()) if summary else 0
    emit(
        f"forge-metrics: recorded PR #{facts.number} ({fmt_tokens(tokens)} new tokens)"
    )
    return 0


def _read(conn: sqlite3.Connection, args: argparse.Namespace) -> int:
    """Run ``report`` or ``export``.

    Args:
        conn: The metrics database.
        args: Parsed arguments.

    Returns:
        Exit code.
    """
    if args.command == "report":
        rows = load_rows(conn, last=args.last)
        emit(render_report(rows))
        if args.html:
            args.html.write_text(render_html(rows), encoding="utf-8")
            emit(f"Wrote {args.html}")
        return 0
    text = json.dumps(export_payload(load_rows(conn)), indent=2)
    if args.out:
        args.out.write_text(text + "\n", encoding="utf-8")
        emit(f"Wrote {args.out}")
    else:
        emit(text)
    return 0


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``forge-metrics``.

    Args:
        argv: Arguments; ``sys.argv[1:]`` when ``None``.

    Returns:
        Exit code: 0 success, 1 GitHub unreadable, 2 no database location.
    """
    args = _build_parser().parse_args(argv)
    root = repo_root()
    db = args.db or default_db_path(root)
    if db is None:
        logger.error(
            "forge-metrics: no GitHub origin remote to key the database by; "
            "pass --db PATH"
        )
        return 2
    conn = open_db(db)
    try:
        if args.command in {"record", "backfill"}:
            return _record(conn, root, args)
        return _read(conn, args)
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
