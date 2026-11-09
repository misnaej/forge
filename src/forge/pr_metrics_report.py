"""Reading the ``forge-metrics`` database back: report, HTML page, export.

The writer side (schema, recording, GitHub) lives in
:mod:`forge.pr_metrics`; this module only reads rows and renders them.
Rows carry no repo or PR name, so neither does anything rendered here: a
PR is labelled by merge day, short repo id and its ordinal in that repo.
"""

from __future__ import annotations

import html
import statistics
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from forge.agent_profile_tokens import MAIN_AGENT, TokenUsage, fmt_tokens
from forge.git_utils import parse_semver
from forge.regen_docs import forge_version


if TYPE_CHECKING:
    import sqlite3


#: Token table columns, in storage order.
TOKEN_COLUMNS: Final = ("requests", "input", "cache_write", "cache_read", "output")
_SHARED_PREFIX: Final = "forge:"
#: Characters of the repo id shown in PR labels.
SHORT_ID: Final = 6
#: Fewest PRs with tokens a trend compares (two per half).
TREND_MIN_PRS: Final = 4
#: Medians below this print with a decimal, not as compact token counts.
_SMALL_MEDIAN: Final = 1_000
_CHART_W: Final = 640
_CHART_H: Final = 160


def usage_values(usage: TokenUsage) -> tuple[int, ...]:
    """*usage* in :data:`TOKEN_COLUMNS` order.

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


@dataclass(frozen=True)
class PrRow:
    """One recorded PR with its tokens, as the report and export read it."""

    pr_key: str
    repo_id: str
    #: The PR's place among its repo's recorded PRs, 1 = oldest merge.
    ordinal: int
    merged_on: str | None
    tool: str
    model: str | None
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

    @property
    def label(self) -> str:
        """``<merge day> <short repo id>#<ordinal>``: names the PR without names."""
        return f"{self.merged_on or '?'} {self.repo_id[:SHORT_ID]}#{self.ordinal}"


def load_rows(
    conn: sqlite3.Connection, *, last: int | None = None, repo_prefix: str = ""
) -> list[PrRow]:
    """Recorded PRs, oldest merge first.

    Args:
        conn: The metrics database.
        last: Keep only the newest *last* PRs; all when ``None``.
        repo_prefix: Keep only repos whose id starts with it; all when empty.

    Returns:
        The rows; ordinals count every recorded PR of the repo.
    """
    # rowid breaks same-day ties in first-recorded order: an upsert keeps it,
    # and PRs are recorded as they merge. Literal SQL, no interpolated names.
    query = (
        "SELECT pr_key, repo_id, merged_on, tool, model, files, additions, "
        "deletions, commits, review_rounds, wrapups_posted, precommit_runs, "
        "test_runs, active_s, wall_s, forge_version FROM prs "
        "ORDER BY merged_on, rowid"
    )
    tokens: dict[str, dict[str, TokenUsage]] = {}
    for key, agent, *counts in conn.execute("SELECT * FROM pr_tokens"):
        tokens.setdefault(key, {})[agent] = TokenUsage(
            input_tokens=counts[1],
            cache_write_tokens=counts[2],
            cache_read_tokens=counts[3],
            output_tokens=counts[4],
            requests=counts[0],
        )
    seen: dict[str, int] = {}
    rows: list[PrRow] = []
    for key, repo_id, *rest in conn.execute(query):
        seen[repo_id] = seen.get(repo_id, 0) + 1
        if not repo_id.startswith(repo_prefix):
            continue
        *fields, version = rest
        rows.append(
            PrRow(
                key,
                repo_id,
                seen[repo_id],
                *fields,
                forge_version=version or "",
                tokens=tokens.get(key, {}),
            )
        )
    return rows[-last:] if last else rows


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
        f"{'PR (merged repo#n)':<22} {'tokens':>7} {'new':>6} {'cache%':>6} "
        f"{'new/line':>8} {'rounds':>6} {'wall':>6} {'active':>6}  "
        "top helpers (new)"
    )
    lines = [header]
    for row in reversed(rows):
        cells = _cells(row)
        lines.append(
            f"{row.label:<22} {cells['tokens']:>7} "
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
            f"<g><title>{html.escape(row.label)}: "
            f"{fmt_tokens(row.new_tokens)} new tokens</title>"
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
th:nth-child(-n+2), td:nth-child(-n+2), td:last-child, th:last-child {
  text-align: left; }
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
        f"<tr><td>{esc(row.label)}</td><td>{esc(row.model or '-')}</td>"
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
        f'{trend}<div class="wrap"><table><thead><tr><th>PR</th><th>model</th>'
        "<th>tokens</th><th>new</th><th>cache%</th><th>new/line</th>"
        "<th>rounds</th><th>wall</th><th>active</th><th>lines</th>"
        "<th>top helpers (new)</th></tr></thead>"
        f"<tbody>{body_rows}</tbody></table></div></body></html>\n"
    )


def export_payload(rows: list[PrRow], *, schema: int) -> dict[str, Any]:
    """The stored rows as they are: already anonymised, so nothing is dropped.

    Args:
        rows: PRs, oldest first.
        schema: The database schema version the rows follow.

    Returns:
        The JSON-ready payload.
    """
    return {
        "schema": schema,
        "forge_version": forge_version(),
        "prs": [
            {
                "pr_key": row.pr_key,
                "repo_id": row.repo_id,
                "merged_on": row.merged_on,
                "tool": row.tool,
                "model": row.model,
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
                "tokens": {
                    agent_type: dict(
                        zip(TOKEN_COLUMNS, usage_values(usage), strict=True)
                    )
                    for agent_type, usage in sorted(row.tokens.items())
                },
            }
            for row in rows
        ],
    }


# --- bench -----------------------------------------------------------------


@dataclass(frozen=True)
class BenchRow:
    """One recorded benchmark run with its tokens."""

    task_id: str
    forge_version: str
    run_n: int
    setup_ok: bool | None
    timed_out: bool | None
    pr_opened: bool | None
    tests_green: bool | None
    precommit_ok: bool | None
    checks_passed: int
    checks_total: int
    review_rounds: int | None
    additions: int
    deletions: int
    active_s: float | None
    wall_s: float | None
    tokens: dict[str, TokenUsage]

    @property
    def new_tokens(self) -> int:
        """Every agent's tokens not served from cache."""
        return sum(usage.new_tokens for usage in self.tokens.values())

    @property
    def cache_share(self) -> float | None:
        """Cache reads as a share of all tokens; ``None`` with no tokens."""
        total = sum(usage.total_tokens for usage in self.tokens.values())
        return 1 - self.new_tokens / total if total else None


def _fact(value: int | None) -> bool | None:
    """A stored ``0`` / ``1`` fact as a bool.

    Args:
        value: The stored value.

    Returns:
        The bool, ``None`` when not recorded.
    """
    return None if value is None else bool(value)


def load_bench_rows(
    conn: sqlite3.Connection, *, task: str | None = None
) -> list[BenchRow]:
    """Recorded benchmark runs, oldest first.

    Args:
        conn: The metrics database.
        task: Only this task's runs; all when ``None``.

    Returns:
        The rows.
    """
    tokens: dict[str, dict[str, TokenUsage]] = {}
    for run_id, agent, *counts in conn.execute("SELECT * FROM bench_tokens"):
        tokens.setdefault(run_id, {})[agent] = TokenUsage(
            input_tokens=counts[1],
            cache_write_tokens=counts[2],
            cache_read_tokens=counts[3],
            output_tokens=counts[4],
            requests=counts[0],
        )
    query = (
        "SELECT run_id, task_id, forge_version, run_n, setup_ok, timed_out, "
        "pr_opened, tests_green, precommit_ok, checks_passed, checks_total, "
        "review_rounds, additions, deletions, active_s, wall_s FROM bench_runs "
        "ORDER BY started_at, rowid"
    )
    rows = []
    for record in conn.execute(query):
        run_id, task_id, version, run_n = record[:4]
        facts = record[4:9]
        passed, total, rounds, additions, deletions, active, wall = record[9:]
        if task is not None and task_id != task:
            continue
        rows.append(
            BenchRow(
                task_id,
                version,
                run_n,
                *(_fact(v) for v in facts),
                checks_passed=passed or 0,
                checks_total=total or 0,
                review_rounds=rounds,
                additions=additions or 0,
                deletions=deletions or 0,
                active_s=active,
                wall_s=wall,
                tokens=tokens.get(run_id, {}),
            )
        )
    return rows


def _median(values: list[float | None]) -> float | None:
    """The median of the known values.

    Args:
        values: Values, ``None`` for unknown.

    Returns:
        The median, ``None`` when nothing is known.
    """
    known = [v for v in values if v is not None]
    return statistics.median(known) if known else None


def _count(values: list[bool | None]) -> str:
    """``true/known`` for one fact across runs.

    Args:
        values: The fact per run, ``None`` when not reached.

    Returns:
        ``n/m``; ``-`` when no run reached it.
    """
    known = [v for v in values if v is not None]
    return f"{sum(known)}/{len(known)}" if known else "-"


def _version_key(version: str) -> tuple[int, int, int] | tuple[float]:
    """Sort key putting versions in release order, unknown shapes last.

    Args:
        version: ``vX.Y.Z``.

    Returns:
        The key.
    """
    return parse_semver(version) or (float("inf"),)


_BENCH_HEADER: Final = (
    f"{'version':<10} {'runs':>4} {'new':>6} {'cache%':>6} {'active':>6} "
    f"{'wall':>6} {'rounds':>6} {'lines':>6} {'setup':>5} {'pr':>5} "
    f"{'tests':>5} {'checks':>6} {'commit':>6} {'timeout':>7}"
)


def _bench_line(version: str, runs: list[BenchRow]) -> str:
    """One version's medians and fact counts for one task.

    Args:
        version: The forge version.
        runs: Its runs of the task.

    Returns:
        The table line.
    """
    new = _median([r.new_tokens or None for r in runs])
    cache = _median([r.cache_share for r in runs])
    rounds = _median([r.review_rounds for r in runs])
    lines = _median([r.additions + r.deletions for r in runs])
    checks = f"{sum(r.checks_passed for r in runs)}/{sum(r.checks_total for r in runs)}"
    return (
        f"{version:<10} {len(runs):>4} "
        f"{fmt_tokens(round(new)) if new is not None else '-':>6} "
        f"{f'{cache:.0%}' if cache is not None else '-':>6} "
        f"{_fmt_s(_median([r.active_s for r in runs])):>6} "
        f"{_fmt_s(_median([r.wall_s for r in runs])):>6} "
        f"{_fmt_median(rounds) if rounds is not None else '-':>6} "
        f"{round(lines) if lines is not None else '-':>6} "
        f"{_count([r.setup_ok for r in runs]):>5} "
        f"{_count([r.pr_opened for r in runs]):>5} "
        f"{_count([r.tests_green for r in runs]):>5} {checks:>6} "
        f"{_count([r.precommit_ok for r in runs]):>6} "
        f"{_count([r.timed_out for r in runs]):>7}"
    )


def render_bench_report(rows: list[BenchRow]) -> str:
    """Per task, each version's runs side by side: medians and fact counts.

    Args:
        rows: Benchmark runs.

    Returns:
        The report text.
    """
    if not rows:
        return "No benchmark runs recorded yet. Run `forge-metrics bench run`."
    by_task: dict[str, dict[str, list[BenchRow]]] = {}
    for row in rows:
        by_task.setdefault(row.task_id, {}).setdefault(row.forge_version, []).append(
            row
        )
    lines: list[str] = []
    for task_id in sorted(by_task):
        lines += [f"task {task_id}", _BENCH_HEADER]
        versions = by_task[task_id]
        lines += [
            _bench_line(version, versions[version])
            for version in sorted(versions, key=_version_key)
        ]
        lines.append("")
    notes = (
        "Medians over runs: new = input + cache write + output tokens; lines = "
        "added + removed.\nCounts are runs where the fact held / runs that "
        "reached it: setup ready, PR opened, sandbox tests green, task checks, "
        "pre-commit verdict (commit), agent timed out."
    )
    return "\n".join([*lines, notes])
