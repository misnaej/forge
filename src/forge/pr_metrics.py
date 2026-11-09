"""``forge-metrics``: what each merged PR cost, kept in a local database.

One SQLite file per machine user under the data directory
(:func:`forge.git_utils.forge_data_dir`), shared by every repo and
checkout and never committed. It lives outside the per-checkout
``code_health/`` (FOUNDATION §13) because one PR can be worked from any
checkout of the repo.

The database holds no name: no repo, path, PR number, title, branch or
file name. A repo is a random ``repo_id`` minted on its first record; the
one place that ties it to ``owner/repo`` is ``repo-ids.json`` beside the
database, which nothing rendered or exported reads. A PR is ``pr_key`` =
``sha256(repo_id:number)``, so re-recording a PR replaces its row. Agent
types neither forge nor Claude Code ships are stored as ``other``.
Tables:

- ``prs`` — size from GitHub (files, lines, commits), merge day, wrap-ups
  posted, and from Claude Code transcripts the main-session model, review
  rounds (most runs of one reporter type), pre-commit and smart-test runs,
  active and wall time;
- ``pr_tokens`` — tokens per agent type (``main`` = the main session);
- ``unattributed`` — tokens spent on a repo's base branch, per UTC day.

Tokens are attributed by the ``gitBranch`` Claude Code stamps on every
transcript record, up to the PR's merge (and after any earlier merge of a
PR with the same branch name). Transcripts are purged after about 30
days, so a PR is recorded at merge (``record``, wired into ``/next`` and
the PR monitor) and older ones while their transcripts last
(``backfill``). Reading back — ``report`` and ``export`` — lives in
:mod:`forge.pr_metrics_report`. Per-repo databases of earlier versions are
imported, anonymised, and deleted on first run (:func:`migrate_legacy`).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import timedelta
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
    fold_agent_types,
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
#: ``owner/repo`` → ``repo_id``; local only, the one file naming a repo.
REPO_IDS_NAME: Final = "repo-ids.json"
#: Database layout version, in ``PRAGMA user_version`` and the export.
SCHEMA_VERSION: Final = 2
#: The agent harness whose transcripts the figures come from.
TOOL: Final = "claude-code"
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
    pr_key TEXT PRIMARY KEY,
    repo_id TEXT NOT NULL,
    merged_on TEXT,
    tool TEXT,
    model TEXT,
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
    forge_version TEXT
);
CREATE TABLE IF NOT EXISTS pr_tokens (
    pr_key TEXT NOT NULL REFERENCES prs(pr_key),
    agent_type TEXT NOT NULL,
    requests INTEGER, input INTEGER, cache_write INTEGER,
    cache_read INTEGER, output INTEGER,
    PRIMARY KEY (pr_key, agent_type)
);
CREATE TABLE IF NOT EXISTS unattributed (
    repo_id TEXT NOT NULL,
    date TEXT NOT NULL,
    agent_type TEXT NOT NULL,
    requests INTEGER, input INTEGER, cache_write INTEGER,
    cache_read INTEGER, output INTEGER,
    PRIMARY KEY (repo_id, date, agent_type)
);
"""
#: ``prs`` columns after ``pr_key`` and ``repo_id``, in table order.
_PR_FIELDS: Final = (
    "merged_on",
    "tool",
    "model",
    "files",
    "additions",
    "deletions",
    "commits",
    "review_rounds",
    "wrapups_posted",
    "precommit_runs",
    "test_runs",
    "active_s",
    "wall_s",
    "forge_version",
)
# An upsert keeps the row's rowid, the report's same-day tie-break. Literal
# SQL: the column list must stay in :data:`_PR_FIELDS` order.
_UPSERT_PR = (
    "INSERT INTO prs (pr_key, repo_id, merged_on, tool, model, files, additions, "
    "deletions, commits, review_rounds, wrapups_posted, precommit_runs, test_runs, "
    "active_s, wall_s, forge_version) "
    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
    "ON CONFLICT(pr_key) DO UPDATE SET "
    "merged_on = excluded.merged_on, tool = excluded.tool, model = excluded.model, "
    "files = excluded.files, additions = excluded.additions, "
    "deletions = excluded.deletions, commits = excluded.commits, "
    "review_rounds = excluded.review_rounds, wrapups_posted = excluded.wrapups_posted, "
    "precommit_runs = excluded.precommit_runs, test_runs = excluded.test_runs, "
    "active_s = excluded.active_s, wall_s = excluded.wall_s, "
    "forge_version = excluded.forge_version"
)
_UPSERT_UNATTRIBUTED = (
    "INSERT INTO unattributed VALUES (?,?,?,?,?,?,?,?) "
    "ON CONFLICT(repo_id, date, agent_type) DO UPDATE SET "
    "requests = excluded.requests, input = excluded.input, "
    "cache_write = excluded.cache_write, "
    "cache_read = excluded.cache_read, output = excluded.output "
    "WHERE excluded.requests >= unattributed.requests"
)


@dataclass(frozen=True)
class PrFacts:
    """What GitHub says about one merged PR (held in memory, never stored)."""

    number: int
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
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    return conn


def pr_key(repo_id: str, number: int) -> str:
    """The stored key of PR *number*: stable, but no PR number in sight.

    Args:
        repo_id: The repo's id.
        number: The PR number.

    Returns:
        ``sha256(repo_id + ":" + number)`` as hex.
    """
    return hashlib.sha256(f"{repo_id}:{number}".encode()).hexdigest()


# --- repo ids --------------------------------------------------------------


def _read_repo_ids(path: Path) -> dict[str, str]:
    """The ``owner/repo`` → id map; empty when absent or unreadable.

    Args:
        path: The ``repo-ids.json`` file.

    Returns:
        The map, string entries only.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, str)}


def lookup_repo_id(slug: str) -> str | None:
    """The random id standing for repo *slug* in the database, if minted.

    Args:
        slug: ``owner/repo``.

    Returns:
        The id, or ``None`` when the repo was never recorded.
    """
    return _read_repo_ids(forge_data_dir() / REPO_IDS_NAME).get(slug)


def ensure_repo_id(slug: str) -> str:
    """The id of repo *slug*, minted (``uuid4``) and saved on first use.

    Args:
        slug: ``owner/repo``.

    Returns:
        The id.
    """
    path = forge_data_dir() / REPO_IDS_NAME
    ids = _read_repo_ids(path)
    if slug in ids:
        return ids[slug]
    ids[slug] = str(uuid.uuid4())
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(ids, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)
    return ids[slug]


# --- recording -------------------------------------------------------------


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


def _write_pr(
    conn: sqlite3.Connection,
    key: str,
    repo_id: str,
    values: dict[str, Any],
    tokens: dict[str, TokenUsage],
) -> None:
    """Upsert one ``prs`` row and replace its ``pr_tokens`` rows.

    Args:
        conn: The metrics database (caller holds the transaction).
        key: The PR's ``pr_key``.
        repo_id: Its repo's id.
        values: :data:`_PR_FIELDS` → value.
        tokens: Agent type → usage, folded on the way in.
    """
    conn.execute(_UPSERT_PR, (key, repo_id, *(values[f] for f in _PR_FIELDS)))
    conn.execute("DELETE FROM pr_tokens WHERE pr_key = ?", (key,))
    conn.executemany(
        "INSERT INTO pr_tokens VALUES (?,?,?,?,?,?,?)",
        [
            (key, agent_type, *usage_values(usage))
            for agent_type, usage in sorted(fold_agent_types(tokens).items())
        ],
    )


def record_pr(
    conn: sqlite3.Connection,
    repo_id: str,
    facts: PrFacts,
    tally: BranchUsageTally,
) -> None:
    """Replace *facts*' PR row with what GitHub and the transcripts say now.

    Args:
        conn: The metrics database.
        repo_id: The PR's repo id.
        facts: The PR, from GitHub.
        tally: Transcript usage of every checkout of the repo.
    """
    summary = pr_summary(tally, facts)
    active = wall = precommit = tests = rounds = model = None
    tokens: dict[str, TokenUsage] = {}
    if summary is not None:
        active, wall = active_and_wall(summary.timestamps)
        precommit, tests = summary.precommit_runs, summary.test_runs
        rounds, tokens, model = review_rounds(summary), summary.tokens, summary.model
    values = {
        "merged_on": facts.merged_at[:10] if facts.merged_at else None,
        "tool": TOOL,
        "model": model,
        "files": facts.files,
        "additions": facts.additions,
        "deletions": facts.deletions,
        "commits": facts.commits,
        "review_rounds": rounds,
        "wrapups_posted": facts.wrapups_posted,
        "precommit_runs": precommit,
        "test_runs": tests,
        "active_s": active,
        "wall_s": wall,
        "forge_version": forge_version(),
    }
    with conn:
        _write_pr(conn, pr_key(repo_id, facts.number), repo_id, values, tokens)


def _keep_unattributed(
    conn: sqlite3.Connection,
    repo_id: str,
    days: dict[tuple[str, str], TokenUsage],
) -> None:
    """Upsert per-day base-branch usage, agent types folded, never shrinking.

    Args:
        conn: The metrics database.
        repo_id: The repo's id.
        days: ``(day, agent type)`` → usage.
    """
    per_day: dict[str, dict[str, TokenUsage]] = {}
    for (day, agent_type), usage in days.items():
        per_day.setdefault(day, {})[agent_type] = usage
    rows = [
        (repo_id, day, agent_type, *usage_values(usage))
        for day, types in sorted(per_day.items())
        for agent_type, usage in sorted(fold_agent_types(types).items())
    ]
    with conn:
        conn.executemany(_UPSERT_UNATTRIBUTED, rows)


def record_unattributed(
    conn: sqlite3.Connection, repo_id: str, tally: BranchUsageTally, base_branch: str
) -> None:
    """Keep base-branch usage per day and agent type.

    A day's figure only grows: once its oldest transcripts are purged, a
    later scan sees less, and that must not overwrite the fuller count.

    Args:
        conn: The metrics database.
        repo_id: The repo's id.
        tally: Transcript usage.
        base_branch: The repo's base branch.
    """
    _keep_unattributed(conn, repo_id, tally.daily(base_branch))


# --- migration -------------------------------------------------------------


def _legacy_rows(
    old: sqlite3.Connection, repo_id: str
) -> list[tuple[str, dict[str, Any]]]:
    """A per-repo database's PRs as ``(pr_key, values)``, names dropped.

    Args:
        old: The legacy database, read-only.
        repo_id: The id its repo now goes by.

    Returns:
        The rows, oldest merge first.
    """
    query = (
        "SELECT pr, merged_at, files, additions, deletions, commits, "
        "review_rounds, wrapups_posted, precommit_runs, test_runs, active_s, "
        "wall_s, forge_version FROM prs ORDER BY merged_at, pr"
    )
    rows = []
    for number, merged_at, *rest in old.execute(query):
        # Every field after merged_on/tool/model maps one to one.
        values = dict(zip(_PR_FIELDS[3:], rest, strict=True))
        values |= {
            "merged_on": merged_at[:10] if merged_at else None,
            "tool": TOOL,
            "model": None,
        }
        rows.append((pr_key(repo_id, number), {"number": number, **values}))
    return rows


def _legacy_tokens(
    old: sqlite3.Connection, query: str
) -> dict[Any, dict[str, TokenUsage]]:
    """Usage per key (PR number or day) per agent type from a legacy table.

    Args:
        old: The legacy database.
        query: Selects key, agent type, then the token columns.

    Returns:
        Key → agent type → usage.
    """
    out: dict[Any, dict[str, TokenUsage]] = {}
    for key, agent, requests, inp, cache_w, cache_r, output in old.execute(query):
        out.setdefault(key, {})[agent] = TokenUsage(
            input_tokens=inp,
            cache_write_tokens=cache_w,
            cache_read_tokens=cache_r,
            output_tokens=output,
            requests=requests,
        )
    return out


def _import_legacy(conn: sqlite3.Connection, path: Path, repo_id: str) -> int:
    """Copy one per-repo database into *conn*, anonymised.

    Args:
        conn: The machine database.
        path: The legacy file.
        repo_id: The id its repo goes by.

    Returns:
        PRs imported.
    """
    old = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = _legacy_rows(old, repo_id)
        tokens = _legacy_tokens(old, "SELECT * FROM pr_tokens")
        days = _legacy_tokens(old, "SELECT * FROM unattributed")
    finally:
        old.close()
    with conn:
        for key, values in rows:
            usage = tokens.get(values.pop("number"), {})
            _write_pr(conn, key, repo_id, values, usage)
    _keep_unattributed(
        conn,
        repo_id,
        {(day, t): u for day, types in days.items() for t, u in types.items()},
    )
    return len(rows)


def migrate_legacy(conn: sqlite3.Connection, data_dir: Path) -> int:
    """Import every per-repo database of earlier versions, then delete it.

    Those lived at ``<data dir>/<owner>/<repo>/metrics.sqlite`` and held
    titles and branch names. Each is copied without them, then removed
    with its now-empty directories. Running again finds nothing; a file
    that cannot be read is left in place with a warning.

    Args:
        conn: The machine database.
        data_dir: The forge data directory.

    Returns:
        PRs imported.
    """
    moved = 0
    for path in sorted(data_dir.glob(f"*/*/{DB_NAME}")):
        repo_dir = path.parent
        slug = f"{repo_dir.parent.name}/{repo_dir.name}"
        repo_id = ensure_repo_id(slug)
        try:
            count = _import_legacy(conn, path, repo_id)
        except sqlite3.Error as exc:
            logger.warning("forge-metrics: could not import %s (%s); kept", path, exc)
            continue
        for leftover in (path, *repo_dir.glob(f"{DB_NAME}-*")):
            leftover.unlink(missing_ok=True)
        for directory in (repo_dir, repo_dir.parent):
            if not any(directory.iterdir()):
                directory.rmdir()
        logger.info("forge-metrics: moved %d PR(s) into the machine database", count)
        moved += count
    return moved


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
    repo_id: str,
    merged: Iterable[tuple[int, str]],
    tally: BranchUsageTally,
    *,
    limit: int,
) -> list[int]:
    """Record merged PRs not yet in the database whose branch has usage.

    Args:
        conn: The metrics database.
        repo_id: The repo's id.
        merged: ``(number, head branch)`` candidates, newest first.
        tally: Transcript usage.
        limit: Most PRs to record.

    Returns:
        The PR numbers recorded.
    """
    recorded = {row[0] for row in conn.execute("SELECT pr_key FROM prs")}
    with_usage = tally.branches_with_usage()
    todo = [
        n
        for n, head in merged
        if pr_key(repo_id, n) not in recorded and head in with_usage
    ]
    done: list[int] = []
    for number in todo[:limit]:
        facts = fetch_facts(number)
        if facts is None:
            logger.warning("forge-metrics: could not read PR #%s; skipped", number)
            continue
        record_pr(conn, repo_id, facts, tally)
        done.append(number)
    return done


# --- CLI -------------------------------------------------------------------


def default_db_path() -> Path:
    """Where the machine's metrics database lives.

    Returns:
        ``<data dir>/metrics.sqlite``.
    """
    return forge_data_dir() / DB_NAME


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
    parser.add_argument(
        "--db", type=Path, help="Database file (default: one per machine)."
    )
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
    report.add_argument(
        "--repo",
        help="Only one repo: '.' for this one, its owner/repo, or a repo id prefix.",
    )
    export = sub.add_parser("export", help="Write the anonymised rows as JSON.")
    export.add_argument("--out", type=Path, help="File to write (default: stdout).")
    return parser


def _record(
    conn: sqlite3.Connection, root: Path, repo_id: str, args: argparse.Namespace
) -> int:
    """Run ``record`` or ``backfill``.

    Args:
        conn: The metrics database.
        root: The checkout.
        repo_id: The checkout's repo id.
        args: Parsed arguments.

    Returns:
        Exit code.
    """
    require_cli("gh", caller="forge-metrics", hint="Install GitHub CLI (gh).")
    base = load_config(root).base_branch
    if args.command == "backfill":
        # One walk serves every PR: each is a windowed summary of it.
        tally = _scan(root, args.transcripts)
        record_unattributed(conn, repo_id, tally, base)
        done = backfill(conn, repo_id, merged_prs(), tally, limit=args.limit)
        emit(f"forge-metrics: recorded {len(done)} PR(s): {done}")
        return 0
    facts = fetch_facts(args.pr)
    if facts is None:
        logger.error("forge-metrics: could not read PR #%s through gh", args.pr)
        return 1
    tally = _scan(root, args.transcripts, pr_filter(facts, base))
    record_unattributed(conn, repo_id, tally, base)
    record_pr(conn, repo_id, facts, tally)
    summary = pr_summary(tally, facts)
    tokens = sum(u.new_tokens for u in summary.tokens.values()) if summary else 0
    emit(
        f"forge-metrics: recorded PR #{facts.number} ({fmt_tokens(tokens)} new tokens)"
    )
    return 0


def _repo_prefix(root: Path, wanted: str | None) -> str | None:
    """The repo id prefix ``report --repo`` selects.

    Args:
        root: The checkout.
        wanted: ``.``, ``owner/repo``, an id prefix, or ``None`` for all.

    Returns:
        The prefix (empty for all), or ``None`` when the repo has no id.
    """
    if not wanted:
        return ""
    if wanted != "." and "/" not in wanted:
        return wanted.lower()
    slug = github_slug(root) if wanted == "." else wanted
    return lookup_repo_id(slug) if slug else None


def _read(conn: sqlite3.Connection, root: Path, args: argparse.Namespace) -> int:
    """Run ``report`` or ``export``.

    Args:
        conn: The metrics database.
        root: The checkout.
        args: Parsed arguments.

    Returns:
        Exit code.
    """
    if args.command == "report":
        prefix = _repo_prefix(root, args.repo)
        if prefix is None:
            logger.error("forge-metrics: no PRs recorded for %s", args.repo)
            return 2
        rows = load_rows(conn, last=args.last, repo_prefix=prefix)
        emit(render_report(rows))
        if args.html:
            args.html.write_text(render_html(rows), encoding="utf-8")
            emit(f"Wrote {args.html}")
        return 0
    payload = export_payload(load_rows(conn), schema=SCHEMA_VERSION)
    text = json.dumps(payload, indent=2)
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
        Exit code: 0 success, 1 GitHub unreadable, 2 no repo to record
        for or to report on.
    """
    args = _build_parser().parse_args(argv)
    root = repo_root()
    conn = open_db(args.db or default_db_path())
    try:
        if args.db is None:
            migrate_legacy(conn, forge_data_dir())
        if args.command not in {"record", "backfill"}:
            return _read(conn, root, args)
        slug = github_slug(root)
        if slug is None:
            logger.error("forge-metrics: no GitHub origin remote to record PRs for")
            return 2
        return _record(conn, root, ensure_repo_id(slug), args)
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
