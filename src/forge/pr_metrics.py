"""``forge-metrics``: what each merged PR cost, kept in a local database.

One row per merged PR in a SQLite file under the per-user data directory
(:func:`forge.git_utils.forge_data_dir`), shared by every checkout of the
repo and never committed:

- ``prs`` — size from GitHub (files, lines, commits), wrap-ups posted, and
  from Claude Code transcripts the review rounds (most runs of one
  reporter type), pre-commit and smart-test runs, active and wall time;
- ``pr_tokens`` — tokens per agent type (``main`` = the main session);
- ``unattributed`` — tokens spent on the base branch, per UTC day.

Tokens are attributed by the ``gitBranch`` Claude Code stamps on every
transcript record, up to the PR's merge (and after any earlier merge of a
PR with the same branch name). Transcripts are purged after about 30 days, so a PR is
recorded at merge (``record``, wired into ``/next`` and the PR monitor) and
older ones while their transcripts last (``backfill``). ``report`` reads
the database; ``export`` writes figures only — no titles, branch, file or
agent names beyond forge's own — for sharing with forge.
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import sqlite3
import statistics
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import quote

from forge.agent_profile import active_and_wall, branch_usage, repo_transcript_dirs
from forge.agent_profile_tokens import MAIN_AGENT, TokenUsage, fmt_tokens, parse_ts
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
from forge.regen_docs import forge_version


if TYPE_CHECKING:
    from collections.abc import Iterable

    from forge.agent_profile_tokens import BranchSummary, BranchUsageTally


configure_cli_logging()
logger = logging.getLogger(__name__)

DB_NAME: Final = "metrics.sqlite"
EXPORT_SCHEMA: Final = 1
#: Merged PRs ``backfill`` looks at: GitHub's one-page maximum, which
#: reaches back past the transcripts' ~30-day lifetime for most repos.
BACKFILL_WINDOW: Final = 100
#: Agent types ``export`` names; anything else (consumer wrappers carry
#: repo names) is summed into ``other``.
_SHARED_PREFIX: Final = "forge:"
_OTHER: Final = "other"
#: ``gh api`` may page through many comments; give it longer than the default.
_GH_TIMEOUT_S: Final = 60
#: Reporters every finalization pass runs once; their run counts give
#: the review rounds.
REPORTER_TYPES: Final = (
    "forge:design-checker",
    "forge:security-checker",
    "forge:docs-types-checker",
)
#: Fewest PRs with tokens a trend compares (two per half).
TREND_MIN_PRS: Final = 4
#: Medians below this print with a decimal, not as compact token counts.
_SMALL_MEDIAN: Final = 1_000
_CHART_W: Final = 640
_CHART_H: Final = 160

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
_TOKEN_COLUMNS: Final = ("requests", "input", "cache_write", "cache_read", "output")


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


def _usage_values(usage: TokenUsage) -> tuple[int, ...]:
    """*usage* in :data:`_TOKEN_COLUMNS` order.

    Args:
        usage: Summed usage.

    Returns:
        The column values.
    """
    return (
        usage.requests,
        usage.input_tokens,
        usage.cache_write_tokens,
        usage.cache_read_tokens,
        usage.output_tokens,
    )


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
    """Review rounds: the most runs any one reporter type made.

    Each finalization pass runs every reporter once, so the busiest
    reporter's run count is the number of passes.

    Args:
        summary: The PR's records.

    Returns:
        The count; ``0`` when no reporter ran.
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
                (facts.number, agent_type, *_usage_values(usage))
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
        (day, agent_type, *_usage_values(usage))
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


# --- reading back ----------------------------------------------------------


@dataclass(frozen=True)
class PrRow:
    """One recorded PR with its tokens, as the report and export read it."""

    pr: int
    title: str
    merged_at: str | None
    files: int
    additions: int
    deletions: int
    commits: int
    review_rounds: int | None
    wrapups_posted: int
    precommit_runs: int | None
    test_runs: int | None
    active_s: float | None
    wall_s: float | None
    forge_version: str
    tokens: dict[str, TokenUsage]

    @property
    def total_tokens(self) -> int:
        """Every agent's tokens summed, cache reads included."""
        return sum(usage.total_tokens for usage in self.tokens.values())

    @property
    def new_tokens(self) -> int:
        """Every agent's tokens not served from cache."""
        return sum(usage.new_tokens for usage in self.tokens.values())

    @property
    def cache_share(self) -> float | None:
        """Cache reads as a share of all tokens; ``None`` with no tokens."""
        total = self.total_tokens
        return 1 - self.new_tokens / total if total else None

    @property
    def changed_lines(self) -> int:
        """Lines added plus removed."""
        return self.additions + self.deletions


def load_rows(conn: sqlite3.Connection, *, last: int | None = None) -> list[PrRow]:
    """Recorded PRs, oldest merge first.

    Args:
        conn: The metrics database.
        last: Keep only the newest *last* PRs; all when ``None``.

    Returns:
        The rows.
    """
    query = (
        "SELECT pr, title, merged_at, files, additions, deletions, commits, "
        "review_rounds, wrapups_posted, precommit_runs, test_runs, active_s, "
        "wall_s, forge_version FROM prs ORDER BY merged_at DESC, pr DESC"
    )
    picked = conn.execute(query).fetchall()
    picked = picked[:last] if last is not None else picked
    tokens: dict[int, dict[str, TokenUsage]] = {}
    for pr, agent, *counts in conn.execute("SELECT * FROM pr_tokens"):
        tokens.setdefault(pr, {})[agent] = TokenUsage(
            input_tokens=counts[1],
            cache_write_tokens=counts[2],
            cache_read_tokens=counts[3],
            output_tokens=counts[4],
            requests=counts[0],
        )
    return [
        PrRow(*row[:13], forge_version=row[13] or "", tokens=tokens.get(row[0], {}))
        for row in reversed(picked)
    ]


def _fmt_s(seconds: float | None) -> str:
    """Seconds as ``1h05``, ``12m`` or ``-``.

    Args:
        seconds: A duration, or ``None`` when unknown.

    Returns:
        The compact form.
    """
    if seconds is None:
        return "-"
    hours, minutes = divmod(round(seconds / 60), 60)
    return f"{hours}h{minutes:02d}" if hours else f"{minutes}m"


def _top_helpers(row: PrRow, count: int = 2) -> str:
    """The costliest helper types of a PR by new tokens, ``type 12k`` each.

    Args:
        row: The PR.
        count: How many to name.

    Returns:
        A comma-separated list, ``-`` when it used none.
    """
    helpers = sorted(
        ((t, u.new_tokens) for t, u in row.tokens.items() if t != MAIN_AGENT),
        key=lambda item: -item[1],
    )
    named = [f"{t.removeprefix(_SHARED_PREFIX)} {fmt_tokens(n)}" for t, n in helpers]
    return ", ".join(named[:count]) or "-"


def _per_line(row: PrRow) -> float | None:
    """New (uncached) tokens per changed line; ``None`` without either.

    Args:
        row: The PR.

    Returns:
        The ratio.
    """
    if not row.changed_lines or not row.new_tokens:
        return None
    return row.new_tokens / row.changed_lines


def trend_lines(rows: list[PrRow]) -> list[str]:
    """Compare the older and newer half of *rows* on median cost.

    Args:
        rows: PRs, oldest first.

    Returns:
        Summary lines; one saying there is too little data when fewer
        than four PRs carry tokens.
    """
    costed = [r for r in rows if r.new_tokens]
    if len(costed) < TREND_MIN_PRS:
        return [
            (
                f"Trend: fewer than {TREND_MIN_PRS} PRs with token data; "
                "nothing to compare yet."
            )
        ]
    half = len(costed) // 2
    older, newer = costed[:half], costed[half:]
    lines = []
    for label, metric in (
        ("new tokens per PR", lambda r: r.new_tokens),
        ("new tokens per changed line", _per_line),
        ("review rounds", lambda r: r.review_rounds),
    ):
        before = [v for v in map(metric, older) if v is not None]
        after = [v for v in map(metric, newer) if v is not None]
        if not before or not after:
            continue
        old_m, new_m = statistics.median(before), statistics.median(after)
        change = f" ({(new_m - old_m) / old_m:+.0%})" if old_m else ""
        lines.append(
            f"Median {label}: older {len(older)} PRs {_fmt_median(old_m)}, "
            f"newer {len(newer)} PRs {_fmt_median(new_m)}{change}"
        )
    return lines


def _fmt_median(value: float) -> str:
    """A median for the trend line: compact tokens, or one decimal when small.

    Args:
        value: The median.

    Returns:
        ``42k`` / ``3.1M`` style at a thousand and above, else ``1.5``.
    """
    return fmt_tokens(round(value)) if value >= _SMALL_MEDIAN else f"{value:.1f}"


def render_report(rows: list[PrRow]) -> str:
    """The terminal report: one line per PR, then the trend.

    Args:
        rows: PRs, oldest first.

    Returns:
        The report text.
    """
    if not rows:
        return "No PRs recorded yet. Run `forge-metrics backfill` or `record --pr N`."
    header = (
        f"{'PR':>6} {'merged':<10} {'tokens':>7} {'new':>6} {'cache%':>6} "
        f"{'new/line':>8} {'rounds':>6} {'wall':>6} {'active':>6}  "
        "top helpers (new)"
    )
    lines = [header]
    for row in reversed(rows):
        cells = _cells(row)
        lines.append(
            f"{row.pr:>6} {cells['merged']:<10} {cells['tokens']:>7} "
            f"{cells['new']:>6} {cells['cache']:>6} {cells['per_line']:>8} "
            f"{cells['rounds']:>6} {cells['wall']:>6} {cells['active']:>6}  "
            f"{cells['helpers']}"
        )
    note = "new = input + cache write + output; cache% = cache reads of all tokens."
    return "\n".join([*lines, "", note, *trend_lines(rows)])


def _cells(row: PrRow) -> dict[str, str]:
    """One PR's report cells, shared by the terminal table and the page.

    Args:
        row: The PR.

    Returns:
        Column → formatted text.
    """
    per_line = _per_line(row)
    cache = row.cache_share
    return {
        "merged": (row.merged_at or "")[:10],
        "tokens": fmt_tokens(row.total_tokens),
        "new": fmt_tokens(row.new_tokens),
        "cache": f"{cache:.0%}" if cache is not None else "-",
        "per_line": f"{per_line:,.0f}" if per_line else "-",
        "rounds": "-" if row.review_rounds is None else str(row.review_rounds),
        "wall": _fmt_s(row.wall_s),
        "active": _fmt_s(row.active_s),
        "helpers": _top_helpers(row),
    }


def _bar_chart(rows: list[PrRow]) -> str:
    """An inline SVG bar chart of new tokens per PR, main session vs helpers.

    Args:
        rows: PRs, oldest first.

    Returns:
        The ``<svg>`` element.
    """
    peak = max((r.new_tokens for r in rows), default=0) or 1
    width = _CHART_W / max(len(rows), 1)
    bars = []
    for i, row in enumerate(rows):
        main = row.tokens.get(MAIN_AGENT, TokenUsage()).new_tokens
        x = i * width
        total_h = _CHART_H * row.new_tokens / peak
        main_h = _CHART_H * main / peak
        bars.append(
            f"<g><title>#{row.pr}: {fmt_tokens(row.new_tokens)} new tokens</title>"
            f'<rect class="helper" x="{x:.1f}" y="{_CHART_H - total_h:.1f}" '
            f'width="{width * 0.8:.1f}" height="{total_h - main_h:.1f}"/>'
            f'<rect class="main" x="{x:.1f}" y="{_CHART_H - main_h:.1f}" '
            f'width="{width * 0.8:.1f}" height="{main_h:.1f}"/></g>'
        )
    return (
        f'<svg viewBox="0 0 {_CHART_W} {_CHART_H}" role="img" '
        'aria-label="New tokens per PR, oldest to newest">' + "".join(bars) + "</svg>"
    )


_HTML_STYLE = """
:root { --bg: #fff; --fg: #1f2328; --muted: #656d76; --main: #2f6fde;
  --helper: #9ab8ee; --rule: #d0d7de; }
@media (prefers-color-scheme: dark) { :root { --bg: #0d1117; --fg: #e6edf3;
  --muted: #8d96a0; --main: #4c8df6; --helper: #2a4a80; --rule: #30363d; } }
body { background: var(--bg); color: var(--fg); margin: 0 auto; max-width: 960px;
  padding: 16px; font: 14px/1.5 system-ui, sans-serif; }
svg { width: 100%; height: auto; border-bottom: 1px solid var(--rule); }
.main { fill: var(--main); } .helper { fill: var(--helper); }
.legend span { display: inline-block; width: 10px; height: 10px; margin: 0 4px 0 12px; }
table { border-collapse: collapse; width: 100%; font-variant-numeric: tabular-nums; }
th, td { padding: 4px 8px; border-bottom: 1px solid var(--rule); text-align: right; }
th:nth-child(2), td:nth-child(2), td:last-child, th:last-child { text-align: left; }
.wrap { overflow-x: auto; } p.muted { color: var(--muted); }
"""


def render_html(rows: list[PrRow]) -> str:
    """One self-contained HTML page: chart, table and trend, nothing fetched.

    Args:
        rows: PRs, oldest first.

    Returns:
        The page.
    """
    esc = html.escape
    columns = ("tokens", "new", "cache", "per_line", "rounds", "wall", "active")
    body_rows = "".join(
        f"<tr><td>{row.pr}</td><td>{esc(row.title)}</td>"
        + "".join(f"<td>{esc(cells[c])}</td>" for c in columns)
        + f"<td>{row.changed_lines}</td><td>{esc(cells['helpers'])}</td></tr>"
        for row in reversed(rows)
        for cells in (_cells(row),)
    )
    trend = "".join(f"<p>{esc(line)}</p>" for line in trend_lines(rows))
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>PR metrics</title><style>{_HTML_STYLE}</style></head><body>"
        f'<h1>PR metrics</h1><p class="muted">{len(rows)} merged PRs, '
        "oldest left. Generated by forge-metrics.</p>"
        f"{_bar_chart(rows)}"
        '<p class="legend"><span class="main" style="background:var(--main)">'
        '</span>main session<span style="background:var(--helper)"></span>'
        "helpers</p>"
        f'{trend}<div class="wrap"><table><thead><tr><th>PR</th><th>title</th>'
        "<th>tokens</th><th>new</th><th>cache%</th><th>new/line</th>"
        "<th>rounds</th><th>wall</th><th>active</th><th>lines</th>"
        "<th>top helpers (new)</th></tr></thead>"
        f"<tbody>{body_rows}</tbody></table></div></body></html>\n"
    )


def _shared_tokens(tokens: dict[str, TokenUsage]) -> dict[str, dict[str, int]]:
    """Per-type token counts with every non-forge type folded into ``other``.

    Args:
        tokens: Agent type → usage.

    Returns:
        Exportable type → column → count.
    """
    folded: dict[str, TokenUsage] = {}
    for agent_type, usage in tokens.items():
        shared = agent_type == MAIN_AGENT or agent_type.startswith(_SHARED_PREFIX)
        folded.setdefault(agent_type if shared else _OTHER, TokenUsage()).add(usage)
    return {
        agent_type: dict(zip(_TOKEN_COLUMNS, _usage_values(usage), strict=True))
        for agent_type, usage in sorted(folded.items())
    }


def export_payload(rows: list[PrRow]) -> dict[str, Any]:
    """Figures only, safe to share: no titles, numbers, branches or dates.

    Args:
        rows: PRs, oldest first.

    Returns:
        The JSON-ready payload; PRs keyed by ordinal (1 = oldest).
    """
    return {
        "schema": EXPORT_SCHEMA,
        "forge_version": forge_version(),
        "prs": [
            {
                "ordinal": ordinal,
                "forge_version": row.forge_version,
                "files": row.files,
                "additions": row.additions,
                "deletions": row.deletions,
                "commits": row.commits,
                "review_rounds": row.review_rounds,
                "wrapups_posted": row.wrapups_posted,
                "precommit_runs": row.precommit_runs,
                "test_runs": row.test_runs,
                "active_s": row.active_s,
                "wall_s": row.wall_s,
                "tokens": _shared_tokens(row.tokens),
            }
            for ordinal, row in enumerate(rows, start=1)
        ],
    }


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


def _scan(root: Path, explicit: list[Path] | None) -> BranchUsageTally:
    """Read the transcripts of every checkout of the repo (or *explicit*).

    Args:
        root: The checkout.
        explicit: Directories to read instead, when given.

    Returns:
        The usage tally.
    """
    dirs = explicit or repo_transcript_dirs(root)
    if not dirs:
        logger.info("forge-metrics: no Claude Code transcripts found; tokens unknown")
    return branch_usage(dirs)


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
    tally = _scan(root, args.transcripts)
    record_unattributed(conn, tally, load_config(root).base_branch)
    if args.command == "backfill":
        done = backfill(conn, merged_prs(), tally, limit=args.limit)
        emit(f"forge-metrics: recorded {len(done)} PR(s): {done}")
        return 0
    facts = fetch_facts(args.pr)
    if facts is None:
        logger.error("forge-metrics: could not read PR #%s through gh", args.pr)
        return 1
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
