"""``forge-metrics``: what each merged PR cost, kept in a local database.

One SQLite file per machine user under the data directory
(:func:`forge.git_utils.forge_data_dir`), shared by every repo and
checkout and never committed. It lives outside the per-checkout
``code_health/`` (FOUNDATION §13) because one PR can be worked from any
checkout of the repo.

The database holds no name: no repo, path, PR number, title, branch or
file name. A repo is a random ``repo_id`` minted on its first record; the
one place that ties it to ``owner/repo`` is ``repo-ids.json`` beside the
database, which nothing rendered or exported prints from it. A PR is
``pr_key`` = ``HMAC-SHA256(machine secret, repo_id:number)`` — the secret
lives in ``repo-ids.json`` and is never exported — so re-recording a PR
replaces its row. Agent types neither forge nor Claude Code ships, and
model ids outside ``[A-Za-z0-9._:-]{1,64}``, are stored as ``other``.
Tables:

- ``prs`` — size from GitHub (files, lines, commits), merge day, wrap-ups
  posted, and from Claude Code transcripts the main-session model, review
  rounds (most runs of one reporter type), pre-commit and smart-test runs,
  active and wall time;
- ``pr_tokens`` — tokens per agent type (``main`` = the main session);
- ``unattributed`` — tokens spent on a repo's base branch, per UTC day;
- ``bench_runs`` / ``bench_tokens`` — ``forge-metrics bench`` runs
  (:mod:`forge.pr_metrics_bench`): one row per task run, keyed by a random
  ``run_id``, with the forge version measured and outcome facts; no
  sandbox name or branch.

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
import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
import subprocess
import uuid
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, NamedTuple
from urllib.parse import quote

from forge.agent_profile import (
    TranscriptFilter,
    active_and_wall,
    branch_usage,
    repo_transcript_dirs,
)
from forge.agent_profile_tokens import MAIN_AGENT, TokenUsage, fmt_tokens, parse_ts
from forge.config import load_config
from forge.git_utils import (
    configure_cli_logging,
    emit,
    forge_data_dir,
    gh_api,
    github_slug,
    release_of,
    repo_root,
    require_cli,
    run_git,
)
from forge.pr_metrics_bench import (
    BenchError,
    bench_data,
    finish_run,
    init_sandbox,
    load_tasks,
    session_line,
    start_run,
)
from forge.pr_metrics_report import (
    export_payload,
    load_bench_rows,
    load_rows,
    render_bench_report,
    render_html,
    render_report,
    usage_values,
)
from forge.regen_docs import forge_version, is_forge_repo
from forge.upgrade import pinned_ref
from forge.verify_agent_doc import shipped_agent_names


if TYPE_CHECKING:
    from collections.abc import Iterable

    from forge.agent_profile_tokens import BranchSummary, BranchUsageTally
    from forge.pr_metrics_bench import BenchOutcome


configure_cli_logging()
logger = logging.getLogger(__name__)

DB_NAME: Final = "metrics.sqlite"
#: ``owner/repo`` → ``repo_id``, plus the machine's key secret; local only,
#: the one file naming a repo.
REPO_IDS_NAME: Final = "repo-ids.json"
#: Field of ``repo-ids.json`` holding the ``pr_key`` HMAC secret.
HMAC_KEY_FIELD: Final = "_secret"
#: Database layout version, in ``PRAGMA user_version`` and the export.
SCHEMA_VERSION: Final = 4
#: Same layout without the bench tables; they are added on open.
_NO_BENCH_VERSION: Final = 3
#: Same layout, ``pr_key`` a plain ``sha256``; rekeyed on open.
_PLAIN_KEY_VERSION: Final = 2
#: Highest PR number tried when recovering a plain key's number.
_REKEY_MAX_PR: Final = 1_000_000
#: The agent harness whose transcripts the figures come from.
TOOL: Final = "claude-code"
_SHARED_PREFIX: Final = "forge:"
#: Claude Code's own subagent types: public names, kept as they are.
BUILTIN_AGENT_TYPES: Final = frozenset(
    {"Explore", "Plan", "general-purpose", "claude-code-guide"}
)
#: Stored label for every agent type neither forge nor Claude Code ships
#: (a repo's own wrappers often carry its name), and for a model id that
#: does not look like one.
OTHER: Final = "other"
#: A model id worth storing; anything else in a transcript is stored as
#: :data:`OTHER`.
_MODEL_ID = re.compile(r"[A-Za-z0-9._:-]{1,64}")
#: Merged PRs ``backfill`` looks at: GitHub's one-page maximum, which
#: reaches back past the transcripts' ~30-day lifetime for most repos.
BACKFILL_WINDOW: Final = 100
#: ``gh api`` may page through many comments; give it longer than the default.
_GH_TIMEOUT_S: Final = 60
#: Local git reads; fetching one missing merge commit gets longer.
_GIT_TIMEOUT_S: Final = 15
_FETCH_TIMEOUT_S: Final = 60
_COMMIT_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
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
CREATE TABLE IF NOT EXISTS bench_runs (
    run_id TEXT PRIMARY KEY,
    forge_version TEXT,
    task_id TEXT NOT NULL,
    run_n INTEGER,
    model TEXT,
    base_tag TEXT,
    started_at TEXT,
    ended_at TEXT,
    pr_opened INTEGER,
    wrapups_posted INTEGER,
    tests_green INTEGER,
    precommit_ok INTEGER,
    checks_passed INTEGER,
    checks_total INTEGER,
    files INTEGER,
    additions INTEGER,
    deletions INTEGER,
    commits INTEGER,
    review_rounds INTEGER,
    precommit_runs INTEGER,
    test_runs INTEGER,
    active_s REAL,
    wall_s REAL,
    recorder_version TEXT
);
CREATE TABLE IF NOT EXISTS bench_tokens (
    run_id TEXT NOT NULL REFERENCES bench_runs(run_id),
    agent_type TEXT NOT NULL,
    requests INTEGER, input INTEGER, cache_write INTEGER,
    cache_read INTEGER, output INTEGER,
    PRIMARY KEY (run_id, agent_type)
);
"""
#: ``bench_runs`` columns, in table order.
_BENCH_FIELDS: Final = (
    "run_id",
    "forge_version",
    "task_id",
    "run_n",
    "model",
    "base_tag",
    "started_at",
    "ended_at",
    "pr_opened",
    "wrapups_posted",
    "tests_green",
    "precommit_ok",
    "checks_passed",
    "checks_total",
    "files",
    "additions",
    "deletions",
    "commits",
    "review_rounds",
    "precommit_runs",
    "test_runs",
    "active_s",
    "wall_s",
    "recorder_version",
)
# Literal SQL: one placeholder per :data:`_BENCH_FIELDS` entry, in order.
# sqlite3 stores a bool as 0 / 1.
_INSERT_BENCH = (
    "INSERT OR REPLACE INTO bench_runs VALUES "
    "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)
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
    #: The commit its merge made on the base branch.
    merge_sha: str | None = None


class MetricsError(RuntimeError):
    """The database or the roster is not in a state forge-metrics can use."""


def pr_key(secret: str, repo_id: str, number: int) -> str:
    """The stored key of PR *number*: stable, but no PR number in sight.

    Keyed with the machine's secret, so the number cannot be recovered
    by hashing small integers with an exported ``repo_id``.

    Args:
        secret: The machine's hex secret (:func:`machine_secret`).
        repo_id: The repo's id.
        number: The PR number.

    Returns:
        ``HMAC-SHA256(secret, repo_id + ":" + number)`` as hex.
    """
    message = f"{repo_id}:{number}".encode()
    return hmac.new(bytes.fromhex(secret), message, hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class RepoKeys:
    """What keys one repo's PRs: its id and the machine's secret."""

    repo_id: str
    secret: str

    def pr_key(self, number: int) -> str:
        """The stored key of this repo's PR *number*.

        Args:
            number: The PR number.

        Returns:
            Its ``pr_key``.
        """
        return pr_key(self.secret, self.repo_id, number)


def _rekey_plain(conn: sqlite3.Connection, secret: str) -> None:
    """Turn plain ``sha256`` keys of a version-2 database into HMAC keys.

    The PR number is recovered by hashing candidates until every key of a
    repo is found; a key not found under :data:`_REKEY_MAX_PR` is kept,
    with a warning.

    Args:
        conn: The database, at :data:`_PLAIN_KEY_VERSION`.
        secret: The machine's hex secret.
    """
    wanted: dict[str, set[str]] = {}
    for key, repo_id in conn.execute("SELECT pr_key, repo_id FROM prs"):
        wanted.setdefault(repo_id, set()).add(key)
    moves: list[tuple[str, str]] = []
    for repo_id, keys in wanted.items():
        for number in range(1, _REKEY_MAX_PR):
            if not keys:
                break
            plain = hashlib.sha256(f"{repo_id}:{number}".encode()).hexdigest()
            if plain in keys:
                keys.discard(plain)
                moves.append((pr_key(secret, repo_id, number), plain))
        if keys:
            logger.warning("forge-metrics: %d PR key(s) not rekeyed", len(keys))
    with conn:
        conn.executemany("UPDATE prs SET pr_key = ? WHERE pr_key = ?", moves)
        conn.executemany("UPDATE pr_tokens SET pr_key = ? WHERE pr_key = ?", moves)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def open_db(path: Path, secret: str) -> sqlite3.Connection:
    """Open the metrics database, creating file and schema on first use.

    Args:
        path: The SQLite file.
        secret: The machine's hex secret, to rekey a version-2 database.

    Returns:
        An open connection.

    Raises:
        MetricsError: The file is a per-repo database of an earlier
            version, or of a schema version this forge does not know.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    has_tables = conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0]
    if version == 0 and has_tables:
        conn.close()
        msg = (
            f"forge-metrics: {path} is a per-repo database of an earlier "
            "version (schema 0); run without --db to import it"
        )
        raise MetricsError(msg)
    if version not in {0, _PLAIN_KEY_VERSION, _NO_BENCH_VERSION, SCHEMA_VERSION}:
        conn.close()
        msg = (
            f"forge-metrics: {path} has schema version {version}, expected "
            f"{SCHEMA_VERSION}; upgrade forge or pass another --db"
        )
        raise MetricsError(msg)
    if version == _PLAIN_KEY_VERSION:
        _rekey_plain(conn, secret)
    if version != SCHEMA_VERSION:
        # Every statement is CREATE ... IF NOT EXISTS: an older layout
        # gains the tables it lacks and keeps its rows.
        conn.executescript(_SCHEMA)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    return conn


def fold_agent_types(tokens: dict[str, TokenUsage]) -> dict[str, TokenUsage]:
    """Per-type usage, every type nobody public ships summed under ``other``.

    A ``forge:`` prefix alone is not enough: anyone can name a local agent
    ``forge:<repo>-reviewer``, so only names on the shipped roster pass.

    Args:
        tokens: Agent type → usage.

    Returns:
        Storable agent type → usage.

    Raises:
        MetricsError: The shipped roster cannot be read; folding every
            forge agent into ``other`` would lose them silently.
    """
    roster = shipped_agent_names()
    if not roster:
        msg = (
            "forge-metrics: the shipped agent roster is empty or unreadable; "
            "reinstall forge (./dev/setup.sh in a forge checkout)"
        )
        raise MetricsError(msg)
    public = (
        {f"{_SHARED_PREFIX}{name}" for name in roster}
        | BUILTIN_AGENT_TYPES
        | {MAIN_AGENT}
    )
    folded: dict[str, TokenUsage] = {}
    for agent_type, usage in tokens.items():
        key = agent_type if agent_type in public else OTHER
        folded.setdefault(key, TokenUsage()).add(usage)
    return folded


def safe_model(model: str | None) -> str | None:
    """*model* when it looks like a model id, ``other`` when not.

    Transcripts are writable by anyone; a free-form string there could
    carry a name into the database.

    Args:
        model: The model read from transcripts, or ``None``.

    Returns:
        The value to store.
    """
    if model is None:
        return None
    return model if _MODEL_ID.fullmatch(model) else OTHER


# --- repo ids --------------------------------------------------------------


def _ids_path() -> Path:
    """The ``repo-ids.json`` file.

    Returns:
        Its path in the data directory.
    """
    return forge_data_dir() / REPO_IDS_NAME


def _read_repo_ids(path: Path) -> dict[str, str]:
    """The ``repo-ids.json`` content; empty when absent or unreadable.

    Args:
        path: The ``repo-ids.json`` file.

    Returns:
        The map, string entries only (``owner/repo`` ids and the secret).
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, str)}


def _write_repo_ids(path: Path, ids: dict[str, str]) -> None:
    """Save *ids* atomically, readable by the owner only (it holds the secret).

    Args:
        path: The ``repo-ids.json`` file.
        ids: Its full content.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    # Created owner-only before the secret is written: a default-umask file
    # would hold it world-readable until a later chmod. A leftover from a
    # crash is removed first: reopening it would keep its old mode.
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(ids, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)


def machine_secret() -> str:
    """The machine's ``pr_key`` secret, minted (32 random bytes) on first use.

    Returns:
        The secret as hex.
    """
    path = _ids_path()
    ids = _read_repo_ids(path)
    if HMAC_KEY_FIELD not in ids:
        ids[HMAC_KEY_FIELD] = secrets.token_hex(32)
        _write_repo_ids(path, ids)
    return ids[HMAC_KEY_FIELD]


def lookup_repo_id(slug: str) -> str | None:
    """The random id standing for repo *slug* in the database, if minted.

    Args:
        slug: ``owner/repo``.

    Returns:
        The id, or ``None`` when the repo was never recorded.
    """
    return _read_repo_ids(_ids_path()).get(slug) if "/" in slug else None


def ensure_repo_id(slug: str) -> str:
    """The id of repo *slug*, minted (``uuid4``) and saved on first use.

    Args:
        slug: ``owner/repo``.

    Returns:
        The id.
    """
    path = _ids_path()
    ids = _read_repo_ids(path)
    if slug not in ids:
        ids[slug] = str(uuid.uuid4())
        _write_repo_ids(path, ids)
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
    repo: RepoKeys,
    facts: PrFacts,
    tally: BranchUsageTally,
    *,
    version: str | None,
) -> None:
    """Replace *facts*' PR row with what GitHub and the transcripts say now.

    Args:
        conn: The metrics database.
        repo: The PR's repo.
        facts: The PR, from GitHub.
        tally: Transcript usage of every checkout of the repo.
        version: The forge release the PR was built with
            (:func:`version_at_merge`); ``None`` when unknown.
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
        "model": safe_model(model),
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
        "forge_version": version,
    }
    with conn:
        key = repo.pr_key(facts.number)
        _write_pr(conn, key, repo.repo_id, values, tokens)


def _unattributed_rows(
    repo_id: str, days: dict[tuple[str, str], TokenUsage]
) -> list[tuple[Any, ...]]:
    """Per-day base-branch usage as ``unattributed`` rows, agent types folded.

    Args:
        repo_id: The repo's id.
        days: ``(day, agent type)`` → usage.

    Returns:
        Rows for :data:`_UPSERT_UNATTRIBUTED`.
    """
    per_day: dict[str, dict[str, TokenUsage]] = {}
    for (day, agent_type), usage in days.items():
        per_day.setdefault(day, {})[agent_type] = usage
    return [
        (repo_id, day, agent_type, *usage_values(usage))
        for day, types in sorted(per_day.items())
        for agent_type, usage in sorted(fold_agent_types(types).items())
    ]


def _keep_unattributed(
    conn: sqlite3.Connection,
    repo_id: str,
    days: dict[tuple[str, str], TokenUsage],
) -> None:
    """Upsert per-day base-branch usage, never shrinking, never adding.

    A row is replaced by a fuller count, never summed with it, so writing
    the same figures twice leaves them as they were.

    Args:
        conn: The metrics database.
        repo_id: The repo's id.
        days: ``(day, agent type)`` → usage.
    """
    with conn:
        conn.executemany(_UPSERT_UNATTRIBUTED, _unattributed_rows(repo_id, days))


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


def record_bench_run(conn: sqlite3.Connection, outcome: BenchOutcome) -> None:
    """Store one benchmark run and its tokens per agent type.

    The outcome carries no sandbox name, branch or path; agent types and
    the model are folded as for PRs.

    Args:
        conn: The metrics database.
        outcome: What the run measured.
    """
    summary = outcome.summary
    active = wall = rounds = precommit = tests = None
    tokens: dict[str, TokenUsage] = {}
    model = None
    if summary is not None:
        active, wall = active_and_wall(summary.timestamps)
        rounds, precommit, tests = (
            review_rounds(summary),
            summary.precommit_runs,
            summary.test_runs,
        )
        tokens, model = summary.tokens, summary.model
    values = {
        "run_id": outcome.run_id,
        "forge_version": outcome.version,
        "task_id": outcome.task_id,
        "run_n": outcome.run_n,
        "model": safe_model(model),
        "base_tag": outcome.base_tag,
        "started_at": outcome.started_at,
        "ended_at": outcome.ended_at,
        "pr_opened": outcome.pr_opened,
        "wrapups_posted": outcome.wrapups_posted,
        "tests_green": outcome.tests_green,
        "precommit_ok": outcome.precommit_ok,
        "checks_passed": outcome.checks_passed,
        "checks_total": outcome.checks_total,
        "files": outcome.files,
        "additions": outcome.additions,
        "deletions": outcome.deletions,
        "commits": outcome.commits,
        "review_rounds": rounds,
        "precommit_runs": precommit,
        "test_runs": tests,
        "active_s": active,
        "wall_s": wall,
        "recorder_version": forge_version(),
    }
    with conn:
        conn.execute(_INSERT_BENCH, tuple(values[f] for f in _BENCH_FIELDS))
        conn.execute("DELETE FROM bench_tokens WHERE run_id = ?", (outcome.run_id,))
        conn.executemany(
            "INSERT INTO bench_tokens VALUES (?,?,?,?,?,?,?)",
            [
                (outcome.run_id, agent_type, *usage_values(usage))
                for agent_type, usage in sorted(fold_agent_types(tokens).items())
            ],
        )


# --- migration -------------------------------------------------------------


# Legacy per-repo stores were written only by v11.1.0: delete this
# section (and its call in ``main``) after the next major release.


def _legacy_rows(
    old: sqlite3.Connection, repo: RepoKeys
) -> list[tuple[str, dict[str, Any]]]:
    """A per-repo database's PRs as ``(pr_key, values)``, names dropped.

    Args:
        old: The legacy database, read-only.
        repo: The keys its repo now goes by.

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
        rows.append((repo.pr_key(number), {"number": number, **values}))
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


def _import_legacy(conn: sqlite3.Connection, path: Path, repo: RepoKeys) -> int:
    """Copy one per-repo database into *conn*, anonymised, in one transaction.

    Every write replaces (PR rows by ``pr_key``, base-branch days by the
    fuller count), so importing the same file twice — a crash between
    import and delete — leaves the same rows, nothing counted twice.

    Args:
        conn: The machine database.
        path: The legacy file.
        repo: The keys its repo goes by.

    Returns:
        PRs imported.
    """
    old = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = _legacy_rows(old, repo)
        tokens = _legacy_tokens(old, "SELECT * FROM pr_tokens")
        days = _legacy_tokens(old, "SELECT * FROM unattributed")
    finally:
        old.close()
    flat = {(day, t): u for day, types in days.items() for t, u in types.items()}
    with conn:
        for key, values in rows:
            usage = tokens.get(values.pop("number"), {})
            _write_pr(conn, key, repo.repo_id, values, usage)
        conn.executemany(_UPSERT_UNATTRIBUTED, _unattributed_rows(repo.repo_id, flat))
    return len(rows)


def legacy_paths(data_dir: Path) -> list[Path]:
    """Per-repo databases of earlier versions still on disk.

    Args:
        data_dir: The forge data directory.

    Returns:
        ``<data dir>/<owner>/<repo>/metrics.sqlite`` files; usually none.
    """
    if not data_dir.is_dir():
        return []
    return sorted(data_dir.glob(f"*/*/{DB_NAME}"))


def migrate_legacy(conn: sqlite3.Connection, paths: list[Path], secret: str) -> int:
    """Import per-repo databases of earlier versions, then delete them.

    Those lived at ``<data dir>/<owner>/<repo>/metrics.sqlite`` and held
    titles and branch names. Each is copied without them, then removed
    with its now-empty directories. Running again finds nothing; a file
    that cannot be read is left in place with a warning.

    Args:
        conn: The machine database.
        paths: The files, from :func:`legacy_paths`.
        secret: The machine's hex secret.

    Returns:
        PRs imported.
    """
    moved = 0
    for path in paths:
        repo_dir = path.parent
        slug = f"{repo_dir.parent.name}/{repo_dir.name}"
        repo = RepoKeys(ensure_repo_id(slug), secret)
        try:
            count = _import_legacy(conn, path, repo)
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
    merge_sha = pull.get("merge_commit_sha")
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
        merge_sha=merge_sha if _is_sha(merge_sha) else None,
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


class MergedPr(NamedTuple):
    """One merged PR from GitHub's closed-PR list."""

    number: int
    head: str
    #: Its merge commit; ``None`` when GitHub gave none.
    merge_sha: str | None


def merged_prs() -> list[MergedPr]:
    """The newest merged PRs.

    Returns:
        Up to :data:`BACKFILL_WINDOW` PRs, newest first; empty when
        GitHub could not be read.
    """
    raw = gh_api(
        f"repos/{{owner}}/{{repo}}/pulls?state=closed&per_page={BACKFILL_WINDOW}",
        "--jq",
        r".[] | select(.merged_at != null) | "
        r'"\(.number)\t\(.head.ref)\t\(.merge_commit_sha // "")"',
        timeout=_GH_TIMEOUT_S,
    )
    found: list[MergedPr] = []
    for line in (raw or "").splitlines():
        number, head, sha = ([*line.split("\t"), "", ""])[:3]
        if number.isdigit() and head:
            found.append(MergedPr(int(number), head, sha if _is_sha(sha) else None))
    return found


# --- forge version at merge --------------------------------------------------


def _is_sha(value: object) -> bool:
    """Whether *value* is a full hex commit id (safe to hand to git).

    Args:
        value: A field read from GitHub.

    Returns:
        ``True`` for 40 or 64 hex characters.
    """
    return isinstance(value, str) and bool(_COMMIT_SHA.fullmatch(value))


def _git(root: Path, *args: str, timeout: float = _GIT_TIMEOUT_S) -> str | None:
    """Run a read-only git command in *root*; ``None`` when it fails.

    Args:
        root: The checkout.
        *args: Argv tail.
        timeout: Bound in seconds.

    Returns:
        Trimmed stdout, or ``None``.
    """
    try:
        return run_git(*args, cwd=root, log_errors=False, timeout=timeout)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return None


def version_at_merge(root: Path, merge_sha: str | None) -> str | None:
    """The forge release a PR was built with, read from its merge commit.

    In forge itself: the newest release tag the merge commit contains. In
    a consumer: the forge-scripts pin in ``pyproject.toml`` at that commit.
    Never the version installed now — a PR recorded late would otherwise
    carry today's forge. A merge commit missing locally is fetched once.

    Args:
        root: The checkout.
        merge_sha: The PR's merge commit, or ``None``.

    Returns:
        ``X.Y.Z``, or ``None`` when it cannot be told.
    """
    if not _is_sha(merge_sha):
        return None
    sha = str(merge_sha)
    if _git(root, "cat-file", "-e", f"{sha}^{{commit}}") is None:
        _git(root, "fetch", "--quiet", "origin", sha, timeout=_FETCH_TIMEOUT_S)
    if is_forge_repo(root):
        tag = _git(root, "describe", "--tags", "--abbrev=0", "--match", "v[0-9]*", sha)
        return release_of(tag)
    text = _git(root, "show", f"{sha}:pyproject.toml")
    return release_of(pinned_ref(text)) if text else None


def pending_prs(
    conn: sqlite3.Connection,
    repo: RepoKeys,
    merged: Iterable[MergedPr],
    tally: BranchUsageTally,
    *,
    limit: int,
) -> list[int]:
    """The merged PRs backfill would record: not yet in the database, with usage.

    Args:
        conn: The metrics database.
        repo: The repo.
        merged: Candidates, newest first.
        tally: Transcript usage.
        limit: Most PRs to pick.

    Returns:
        The PR numbers, recorded order (oldest first).
    """
    recorded = {row[0] for row in conn.execute("SELECT pr_key FROM prs")}
    with_usage = tally.branches_with_usage()
    todo = [
        pr.number
        for pr in merged
        if repo.pr_key(pr.number) not in recorded and pr.head in with_usage
    ]
    # Newest *limit*, recorded oldest first: rowid is the report's
    # same-day tie-break.
    return list(reversed(todo[:limit]))


def backfill(
    conn: sqlite3.Connection,
    root: Path,
    repo: RepoKeys,
    tally: BranchUsageTally,
    numbers: Iterable[int],
) -> list[int]:
    """Record the given merged PRs, skipping any that cannot be read.

    Args:
        conn: The metrics database.
        root: The checkout, for each PR's forge version.
        repo: The repo.
        tally: Transcript usage.
        numbers: PR numbers to record, in order.

    Returns:
        The PR numbers recorded.
    """
    done: list[int] = []
    for number in numbers:
        facts = fetch_facts(number)
        if facts is None:
            logger.warning("forge-metrics: could not read PR #%s; skipped", number)
            continue
        record_pr(
            conn, repo, facts, tally, version=version_at_merge(root, facts.merge_sha)
        )
        done.append(number)
    return done


def refresh_versions(
    conn: sqlite3.Connection, root: Path, repo: RepoKeys, merged: Iterable[MergedPr]
) -> int:
    """Rewrite the forge version of PRs already recorded, nothing else.

    Only the version is touched: re-recording whole rows would wipe the
    tokens of PRs whose transcripts are purged since.

    Args:
        conn: The metrics database.
        root: The checkout.
        repo: The repo.
        merged: PRs to look at.

    Returns:
        Rows whose version changed.
    """
    recorded = dict(conn.execute("SELECT pr_key, forge_version FROM prs"))
    changes = []
    for pr in merged:
        key = repo.pr_key(pr.number)
        if key not in recorded:
            continue
        version = version_at_merge(root, pr.merge_sha)
        if version != recorded[key]:
            changes.append((version, key))
    with conn:
        conn.executemany("UPDATE prs SET forge_version = ? WHERE pr_key = ?", changes)
    return len(changes)


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
    fill.add_argument(
        "--refresh",
        action="store_true",
        help="Also rewrite the forge version of PRs already recorded.",
    )
    report = sub.add_parser("report", help="Print per-PR cost and the trend.")
    report.add_argument("--last", type=int, default=20)
    report.add_argument("--html", type=Path, help="Also write a self-contained page.")
    report.add_argument(
        "--repo",
        help="Only one repo: '.' for this one, its owner/repo, or a repo id prefix.",
    )
    export = sub.add_parser("export", help="Write the anonymised rows as JSON.")
    export.add_argument("--out", type=Path, help="File to write (default: stdout).")
    bench = sub.add_parser(
        "bench",
        help="Run fixed tasks by hand in a sandbox repo to compare forge versions.",
        description=(
            "Fixed tasks, run by hand in a normal session in a private sandbox "
            "repo, to compare forge versions. Each run costs a full PR's tokens."
        ),
    )
    _add_bench_commands(bench)
    return parser


def _add_bench_commands(bench: argparse.ArgumentParser) -> None:
    """Add ``bench``'s subcommands.

    Args:
        bench: The ``bench`` parser.
    """
    steps = bench.add_subparsers(dest="bench_command", required=True)
    init = steps.add_parser("init", help="Fill an empty private sandbox repo.")
    init.add_argument("--repo", required=True, help="owner/name of the sandbox.")
    start = steps.add_parser(
        "start", help="Put the sandbox clone on a fresh branch; print the prompt."
    )
    start.add_argument("task", help="Task id.")
    start.add_argument(
        "--clone", type=Path, help="Sandbox clone (default: one under the data dir)."
    )
    finish = steps.add_parser(
        "finish", help="Record the started run, close its PR, delete its branch."
    )
    finish.add_argument("--clone", type=Path, help="Sandbox clone (default: start's).")
    report = steps.add_parser("report", help="Compare versions per task.")
    report.add_argument("--task", help="Only this task.")


def _record(
    conn: sqlite3.Connection, root: Path, repo: RepoKeys, args: argparse.Namespace
) -> int:
    """Run ``record`` or ``backfill``.

    Args:
        conn: The metrics database.
        root: The checkout.
        repo: The checkout's repo.
        args: Parsed arguments.

    Returns:
        Exit code.
    """
    require_cli("gh", caller="forge-metrics", hint="Install GitHub CLI (gh).")
    base = load_config(root).base_branch
    if args.command == "backfill":
        # One walk serves every PR: each is a windowed summary of it.
        tally = _scan(root, args.transcripts)
        record_unattributed(conn, repo.repo_id, tally, base)
        merged = merged_prs()
        if args.refresh:
            changed = refresh_versions(conn, root, repo, merged)
            emit(f"forge-metrics: forge version rewritten on {changed} PR(s)")
        picked = pending_prs(conn, repo, merged, tally, limit=args.limit)
        done = backfill(conn, root, repo, tally, picked)
        emit(f"forge-metrics: recorded {len(done)} PR(s): {done}")
        return 0
    facts = fetch_facts(args.pr)
    if facts is None:
        logger.error("forge-metrics: could not read PR #%s through gh", args.pr)
        return 1
    tally = _scan(root, args.transcripts, pr_filter(facts, base))
    record_unattributed(conn, repo.repo_id, tally, base)
    version = version_at_merge(root, facts.merge_sha)
    record_pr(conn, repo, facts, tally, version=version)
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


def _next_run_n(conn: sqlite3.Connection, task_id: str) -> int:
    """One past the highest recorded run number of *task_id*.

    Args:
        conn: The metrics database.
        task_id: The task.

    Returns:
        The number to try first.
    """
    row = conn.execute(
        "SELECT MAX(run_n) FROM bench_runs WHERE task_id = ?", (task_id,)
    ).fetchone()
    return (row[0] or 0) + 1


def _bench_start(conn: sqlite3.Connection, data: Path, args: argparse.Namespace) -> int:
    """Run ``bench start``: prepare the clone, print the prompt and the command.

    Args:
        conn: The metrics database.
        data: The bench data directory.
        args: Parsed arguments.

    Returns:
        Exit code: 0 ready, 2 unknown task.
    """
    tasks = load_tasks(data)
    task = tasks.get(args.task)
    if task is None:
        logger.error("forge-metrics bench: unknown task; known: %s", ", ".join(tasks))
        return 2
    marker, prompt = start_run(
        task, data=data, first_n=_next_run_n(conn, task.id), clone=args.clone
    )
    emit(f"forge-metrics bench: {task.id} run {marker.run_n} on {marker.branch}")
    emit("\nStart a session in the clone:\n")
    emit(f"    {session_line(marker)}")
    emit("\nPaste this prompt, then run `forge-metrics bench finish` when done:\n")
    emit(prompt)
    return 0


def _bench(conn: sqlite3.Connection, args: argparse.Namespace) -> int:
    """Run ``bench init``, ``start``, ``finish`` or ``report``.

    Args:
        conn: The metrics database.
        args: Parsed arguments.

    Returns:
        Exit code.
    """
    if args.bench_command == "report":
        emit(render_bench_report(load_bench_rows(conn, task=args.task)))
        return 0
    require_cli("gh", caller="forge-metrics", hint="Install GitHub CLI (gh).")
    try:
        with bench_data() as data:
            if args.bench_command == "start":
                return _bench_start(conn, data, args)
            if args.bench_command == "finish":
                outcome = finish_run(
                    data=data,
                    record=lambda o: record_bench_run(conn, o),
                    clone=args.clone,
                )
                emit(
                    f"forge-metrics bench: recorded {outcome.task_id} run "
                    f"{outcome.run_n} (forge {outcome.version or 'unknown'})"
                )
                return 0
            sandbox = init_sandbox(args.repo, data=data)
    except BenchError as exc:
        return _refuse(exc)
    emit(f"forge-metrics bench: sandbox ready at {sandbox.base_tag}")
    return 0


def _refuse(exc: MetricsError | BenchError) -> int:
    """Report a refusal the user can act on, without a traceback.

    Called from ``except`` blocks so the message is logged as a plain error:
    a refusal is an expected outcome with its own wording, not a crash.

    Args:
        exc: The refusal.

    Returns:
        Exit code ``2``.
    """
    logger.error("%s", exc)
    return 2


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``forge-metrics``.

    Args:
        argv: Arguments; ``sys.argv[1:]`` when ``None``.

    Returns:
        Exit code: 0 success, 1 GitHub unreadable, 2 no repo to record
        for or to report on, or a database or roster forge cannot use.
    """
    args = _build_parser().parse_args(argv)
    root = repo_root()
    secret = machine_secret()
    try:
        conn = open_db(args.db or default_db_path(), secret)
    except MetricsError as exc:
        return _refuse(exc)
    try:
        legacy = legacy_paths(forge_data_dir()) if args.db is None else []
        if legacy:
            migrate_legacy(conn, legacy, secret)
        if args.command == "bench":
            return _bench(conn, args)
        if args.command not in {"record", "backfill"}:
            return _read(conn, root, args)
        slug = github_slug(root)
        if slug is None:
            logger.error("forge-metrics: no GitHub origin remote to record PRs for")
            return 2
        return _record(conn, root, RepoKeys(ensure_repo_id(slug), secret), args)
    except MetricsError as exc:
        return _refuse(exc)
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
