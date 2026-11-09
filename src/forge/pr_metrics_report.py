"""Reading the ``forge-metrics`` database back: report, HTML page, export.

The writer side (schema, recording, GitHub) lives in
:mod:`forge.pr_metrics`; this module only reads rows and renders them.
Rows carry no repo or PR name, so neither does anything rendered here: a
PR is labelled by merge day, short repo id and its ordinal in that repo.
"""

from __future__ import annotations

import html
import secrets
import statistics
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from forge.agent_profile_tokens import MAIN_AGENT, TokenUsage, fmt_tokens
from forge.git_utils import parse_semver, release_of
from forge.pr_metrics_charts import (
    COMPACT_FROM,
    Change,
    Dot,
    Series,
    category_chart,
    fmt_number,
    series_class,
    time_chart,
)
from forge.regen_docs import forge_version


if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable


#: Token table columns, in storage order.
TOKEN_COLUMNS: Final = ("requests", "input", "cache_write", "cache_read", "output")
_SHARED_PREFIX: Final = "forge:"
#: Characters of the repo id shown in PR labels.
SHORT_ID: Final = 6
#: Fewest PRs with tokens a trend compares (two per half).
TREND_MIN_PRS: Final = 4
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
    #: Forge release the PR was built with; ``None`` when unknown.
    forge_version: str | None
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
                forge_version=version,
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
    return fmt_tokens(round(value)) if value >= COMPACT_FROM else f"{value:.1f}"


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


# --- by forge version --------------------------------------------------------

#: Bucket for PRs recorded without a forge version; always sorts last.
UNKNOWN_VERSION: Final = "unknown"


@dataclass(frozen=True)
class VersionStats:
    """Medians for one (forge version, repo) group of PRs.

    Token medians count only PRs carrying token data, so a PR whose
    transcripts were not found never drags a median to zero.
    """

    prs: int
    with_tokens: int
    new_tokens: float | None
    per_line: float | None
    active_min: float | None
    rounds: float | None


def _version_name(row: PrRow) -> str:
    """The row's forge version, :data:`UNKNOWN_VERSION` when not recorded.

    Args:
        row: The PR.

    Returns:
        The bucket name.
    """
    return row.forge_version or UNKNOWN_VERSION


def version_order(rows: list[PrRow]) -> list[str]:
    """The versions in *rows*, release order, :data:`UNKNOWN_VERSION` last.

    Args:
        rows: PRs.

    Returns:
        The version names.
    """
    names = {_version_name(row) for row in rows}
    known = sorted(names - {UNKNOWN_VERSION}, key=lambda v: (_version_key(v), v))
    return known + [UNKNOWN_VERSION] * (UNKNOWN_VERSION in names)


def repo_order(rows: list[PrRow]) -> list[str]:
    """Repo ids by first recorded PR: fixes each repo's colour.

    Args:
        rows: PRs, oldest first.

    Returns:
        The repo ids.
    """
    return list(dict.fromkeys(row.repo_id for row in rows))


def _group_stats(group: list[PrRow]) -> VersionStats:
    """Medians for one group; token medians over PRs with token data only.

    Args:
        group: The group's PRs.

    Returns:
        The stats.
    """
    costed = [row for row in group if row.new_tokens]
    return VersionStats(
        prs=len(group),
        with_tokens=len(costed),
        new_tokens=_median([row.new_tokens for row in costed]),
        per_line=_median([_per_line(row) for row in costed]),
        active_min=_median(
            [None if row.active_s is None else row.active_s / 60 for row in group]
        ),
        rounds=_median([row.review_rounds for row in group]),
    )


def version_stats(rows: list[PrRow]) -> dict[tuple[str, str], VersionStats]:
    """Medians per (forge version, repo id).

    Args:
        rows: PRs.

    Returns:
        ``(version, repo_id)`` → stats, for every group with a PR.
    """
    groups: dict[tuple[str, str], list[PrRow]] = {}
    for row in rows:
        groups.setdefault((_version_name(row), row.repo_id), []).append(row)
    return {key: _group_stats(group) for key, group in groups.items()}


def _version_chart(
    rows: list[PrRow],
    stats: dict[tuple[str, str], VersionStats],
    metric: Callable[[VersionStats], float | None],
    *,
    label: str,
    y_title: str,
) -> str:
    """A per-version line chart, one series per repo.

    Args:
        rows: PRs, oldest first.
        stats: :func:`version_stats` of *rows*.
        metric: The plotted median, ``None`` where unknown.
        label: Accessible name.
        y_title: Y-axis title.

    Returns:
        The ``<svg>``.
    """
    versions = version_order(rows)
    series = []
    for repo_id in repo_order(rows):
        cells = [stats.get((version, repo_id)) for version in versions]
        series.append(
            Series(
                repo_id[:SHORT_ID],
                [metric(cell) if cell else None for cell in cells],
            )
        )
    return category_chart(versions, series, label=label, y_title=y_title)


def version_changes(rows: list[PrRow]) -> list[Change]:
    """Days a repo's merged PRs moved to a different forge version.

    PRs with no recorded version or merge day neither start nor end a move.

    Args:
        rows: PRs, oldest first.

    Returns:
        The moves, in row order.
    """
    changes = []
    last: dict[str, str] = {}
    for row in rows:
        if row.forge_version is None or row.merged_on is None:
            continue
        before = last.get(row.repo_id)
        last[row.repo_id] = row.forge_version
        if before is not None and before != row.forge_version:
            changes.append(
                Change(row.merged_on, row.repo_id[:SHORT_ID], before, row.forge_version)
            )
    return changes


def _time_chart(rows: list[PrRow]) -> str:
    """New tokens per PR by merge day, one dot per PR, version changes marked.

    Args:
        rows: PRs, oldest first.

    Returns:
        The ``<svg>``.
    """
    slot = {repo_id: i for i, repo_id in enumerate(repo_order(rows))}
    dots = [
        Dot(
            slot[row.repo_id],
            row.repo_id[:SHORT_ID],
            row.merged_on,
            float(row.new_tokens),
            f"#{row.ordinal} {row.merged_on} {_version_name(row)}: "
            f"{fmt_tokens(row.new_tokens)}",
        )
        for row in rows
        if row.merged_on and row.new_tokens
    ]
    return time_chart(
        dots,
        version_changes(rows),
        label="New tokens per PR over time, one dot per PR",
        y_title="new tokens",
    )


def _fmt_cell(value: float | None, fmt: Callable[[float], str]) -> str:
    """A table median, ``-`` when unknown.

    Args:
        value: The median.
        fmt: Formatter for a known value.

    Returns:
        The cell text.
    """
    return "-" if value is None else fmt(value)


def _version_table(
    rows: list[PrRow], stats: dict[tuple[str, str], VersionStats]
) -> str:
    """Version by repo table of counts and medians.

    Args:
        rows: PRs, oldest first.
        stats: :func:`version_stats` of *rows*.

    Returns:
        The ``<table>``.
    """
    esc = html.escape
    body = []
    for version in version_order(rows):
        for repo_id in repo_order(rows):
            cell = stats.get((version, repo_id))
            if cell is None:
                continue
            values = (
                str(cell.prs),
                str(cell.with_tokens),
                _fmt_cell(cell.new_tokens, fmt_number),
                _fmt_cell(cell.per_line, lambda v: f"{v:,.0f}"),
                _fmt_cell(cell.active_min, lambda v: f"{v:.0f}"),
                _fmt_cell(cell.rounds, lambda v: f"{v:.1f}"),
            )
            body.append(
                f"<tr><td>{esc(version)}</td><td>{esc(repo_id[:SHORT_ID])}</td>"
                + "".join(f"<td>{esc(v)}</td>" for v in values)
                + "</tr>"
            )
    return (
        '<table class="by-version"><thead><tr><th>version</th><th>repo</th>'
        "<th>PRs</th><th>with tokens</th><th>median new</th>"
        "<th>median new/line</th><th>median active min</th>"
        f"<th>median rounds</th></tr></thead><tbody>{''.join(body)}</tbody></table>"
    )


def _repo_legend(rows: list[PrRow]) -> str:
    """Legend naming each repo's colour by short id.

    Args:
        rows: PRs, oldest first.

    Returns:
        A ``<p>``.
    """
    items = "".join(
        f'<span class="swatch {series_class(i)}"></span>{html.escape(r[:SHORT_ID])}'
        for i, r in enumerate(repo_order(rows))
    )
    return f'<p class="legend">repo:{items}</p>'


def _version_section(rows: list[PrRow], *, over_time: bool = True) -> str:
    """The by-version charts and table.

    Args:
        rows: PRs, oldest first.
        over_time: Include the per-PR chart by merge day; the public page,
            which carries no dates, leaves it out.

    Returns:
        HTML.
    """
    stats = version_stats(rows)
    over_time_chart = (
        "<h3>New tokens per PR over time</h3>"
        '<p class="muted">One dot per PR with token data. Dashed lines: a '
        "repo moved to a new forge version.</p>"
        f'<div class="wrap">{_time_chart(rows)}</div>'
        if over_time
        else ""
    )

    def by_version(
        title: str, y_title: str, metric: Callable[[VersionStats], float | None]
    ) -> str:
        svg = _version_chart(rows, stats, metric, label=title, y_title=y_title)
        return f'<h3>{html.escape(title)}</h3><div class="wrap">{svg}</div>'

    return "".join(
        (
            "<h2>By forge version</h2>",
            (
                '<p class="muted">Medians per version and repo. PRs without token '
                "data are left out of token medians and show as gaps, never zeros; "
                f"PRs with no recorded version are grouped as {UNKNOWN_VERSION}, "
                "last.</p>"
            ),
            _repo_legend(rows),
            by_version(
                "New tokens per changed line by forge version (median)",
                "new tokens / line",
                lambda s: s.per_line,
            ),
            over_time_chart,
            by_version(
                "Active minutes per PR by forge version (median)",
                "active minutes",
                lambda s: s.active_min,
            ),
            by_version(
                "Review rounds by forge version (median)",
                "review rounds",
                lambda s: s.rounds,
            ),
            f'<div class="wrap">{_version_table(rows, stats)}</div>',
        )
    )


_HTML_STYLE = """
:root { --bg: #fff; --fg: #1f2328; --muted: #656d76; --main: #2f6fde;
  --helper: #9ab8ee; --rule: #d0d7de;
  --s1: #2a78d6; --s2: #eb6834; --s3: #1baf7a; --s4: #eda100;
  --s5: #e87ba4; --s6: #008300; --s7: #6250d6; --s8: #e34948; }
@media (prefers-color-scheme: dark) { :root { --bg: #0d1117; --fg: #e6edf3;
  --muted: #8d96a0; --main: #4c8df6; --helper: #2a4a80; --rule: #30363d;
  --s1: #3987e5; --s2: #d95926; --s3: #199e70; --s4: #c98500;
  --s5: #d55181; --s6: #008300; --s7: #9085e9; --s8: #e66767; } }
body { background: var(--bg); color: var(--fg); margin: 0 auto; max-width: 960px;
  padding: 16px; font: 14px/1.5 system-ui, sans-serif; }
svg { width: 100%; height: auto; border-bottom: 1px solid var(--rule); }
svg.chart { min-width: 560px; border: 0; }
.main { fill: var(--main); } .helper { fill: var(--helper); }
.legend span { display: inline-block; width: 10px; height: 10px; margin: 0 4px 0 12px; }
.s1 { color: var(--s1); } .s2 { color: var(--s2); } .s3 { color: var(--s3); }
.s4 { color: var(--s4); } .s5 { color: var(--s5); } .s6 { color: var(--s6); }
.s7 { color: var(--s7); } .s8 { color: var(--s8); } .other { color: var(--muted); }
.swatch { background: currentColor; border-radius: 50%; }
.series path { fill: none; stroke: currentColor; stroke-width: 2; }
.series circle { fill: currentColor; stroke: var(--bg); stroke-width: 2; }
.grid { stroke: var(--rule); stroke-width: 1; } .axis { stroke: var(--muted); }
.mark { stroke: var(--muted); stroke-dasharray: 3 3; }
.tick, .axis-title, .mark-label { fill: var(--muted); font-size: 11px; }
table { border-collapse: collapse; width: 100%; font-variant-numeric: tabular-nums; }
th, td { padding: 4px 8px; border-bottom: 1px solid var(--rule); text-align: right; }
th:nth-child(-n+2), td:nth-child(-n+2), td:last-child, th:last-child {
  text-align: left; }
.by-version td:last-child, .by-version th:last-child { text-align: right; }
.public td:nth-child(-n+4), .public th:nth-child(-n+4) { text-align: left; }
.wrap { overflow-x: auto; } p.muted { color: var(--muted); }
"""
_PR_COLUMNS: Final = ("tokens", "new", "cache", "per_line", "rounds", "wall", "active")
_PR_HEADER: Final = (
    "<th>tokens</th><th>new</th><th>cache%</th><th>new/line</th>"
    "<th>rounds</th><th>wall</th><th>active</th><th>lines</th>"
    "<th>top helpers (new)</th>"
)


def _pr_cells(row: PrRow) -> str:
    """One PR's number cells, from ``tokens`` to top helpers.

    Args:
        row: The PR.

    Returns:
        The ``<td>`` run.
    """
    esc = html.escape
    cells = _cells(row)
    return (
        "".join(f"<td>{esc(cells[c])}</td>" for c in _PR_COLUMNS)
        + f"<td>{row.changed_lines}</td><td>{esc(cells['helpers'])}</td>"
    )


def _page(title: str, intro: str, body: str) -> str:
    """The self-contained page shell: inline style, nothing fetched.

    Args:
        title: Page title, unescaped.
        intro: Line under the title, unescaped.
        body: Page content, already escaped.

    Returns:
        The page.
    """
    esc = html.escape
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{esc(title)}</title><style>{_HTML_STYLE}</style></head><body>"
        f'<h1>{esc(title)}</h1><p class="muted">{esc(intro)}</p>'
        f"{body}</body></html>\n"
    )


def render_html(rows: list[PrRow]) -> str:
    """One self-contained HTML page: charts, tables and trend, nothing fetched.

    Args:
        rows: PRs, oldest first.

    Returns:
        The page.
    """
    esc = html.escape
    body_rows = "".join(
        f"<tr><td>{esc(row.label)}</td><td>{esc(row.model or '-')}</td>"
        f"{_pr_cells(row)}</tr>"
        for row in reversed(rows)
    )
    trend = "".join(f"<p>{esc(line)}</p>" for line in trend_lines(rows))
    body = (
        f"{_bar_chart(rows)}"
        '<p class="legend"><span class="main" style="background:var(--main)">'
        '</span>main session<span style="background:var(--helper)"></span>'
        "helpers</p>"
        f"{trend}{_version_section(rows)}<h2>Per PR</h2>"
        '<div class="wrap"><table><thead><tr><th>PR</th><th>model</th>'
        f"{_PR_HEADER}</tr></thead>"
        f"<tbody>{body_rows}</tbody></table></div>"
    )
    intro = f"{len(rows)} merged PRs, oldest left. Generated by forge-metrics."
    return _page("PR metrics", intro, body)


# --- public variant ----------------------------------------------------------


def public_order(
    rows: list[PrRow],
    shuffle: Callable[[list[PrRow]], None] | None = None,
) -> list[PrRow]:
    """*rows* grouped by forge version (release order), shuffled in each group.

    Shuffling removes the merge order, which would otherwise date each PR
    against the releases around it.

    Args:
        rows: PRs.
        shuffle: Shuffles a list in place; the seam tests replace.

    Returns:
        The reordered rows.
    """
    if shuffle is None:
        shuffle = secrets.SystemRandom().shuffle
    ordered: list[PrRow] = []
    for version in version_order(rows):
        group = [row for row in rows if _version_name(row) == version]
        shuffle(group)
        ordered += group
    return ordered


def render_public_html(rows: list[PrRow]) -> str:
    """The public page: by-version charts and per-PR numbers, no dates.

    No merge day, no ordinal and no over-time chart reach it; repos show
    only by short random id.

    Args:
        rows: PRs in :func:`public_order`.

    Returns:
        The page.
    """
    esc = html.escape
    body_rows = "".join(
        f"<tr><td>{esc(_version_name(row))}</td>"
        f"<td>{esc(row.repo_id[:SHORT_ID])}</td><td>{esc(row.tool)}</td>"
        f"<td>{esc(row.model or '-')}</td>{_pr_cells(row)}</tr>"
        for row in rows
    )
    body = (
        f"{_version_section(rows, over_time=False)}<h2>Per PR</h2>"
        '<p class="muted">Grouped by forge version, in random order.</p>'
        '<div class="wrap"><table class="public"><thead><tr><th>version</th>'
        f"<th>repo</th><th>tool</th><th>model</th>{_PR_HEADER}</tr></thead>"
        f"<tbody>{body_rows}</tbody></table></div>"
    )
    intro = (
        f"{len(rows)} merged PRs built with forge, by forge version. Repos are "
        "random ids; no names and no dates. Generated by forge-metrics publish."
    )
    return _page("Forge PR metrics", intro, body)


def public_payload(rows: list[PrRow], *, schema: int) -> dict[str, Any]:
    """The public page's rows as JSON: no keys, names or dates.

    Args:
        rows: PRs in :func:`public_order`.
        schema: The database schema version the rows follow.

    Returns:
        The JSON-ready payload.
    """
    return {
        "schema": schema,
        "generator": release_of(forge_version()),
        "prs": [
            {
                "repo_id": row.repo_id[:SHORT_ID],
                "forge_version": row.forge_version,
                "tool": row.tool,
                "model": row.model,
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
                "tokens": _token_dict(row),
            }
            for row in rows
        ],
    }


def _token_dict(row: PrRow) -> dict[str, dict[str, int]]:
    """*row*'s tokens as ``agent type → column → count``.

    Args:
        row: The PR.

    Returns:
        The mapping, agent types sorted.
    """
    return {
        agent_type: dict(zip(TOKEN_COLUMNS, usage_values(usage), strict=True))
        for agent_type, usage in sorted(row.tokens.items())
    }


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
                "tokens": _token_dict(row),
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
        "SELECT run_id, task_id, forge_version, run_n, "
        "pr_opened, tests_green, precommit_ok, checks_passed, checks_total, "
        "review_rounds, additions, deletions, active_s, wall_s FROM bench_runs "
        "ORDER BY started_at, rowid"
    )
    rows = []
    for record in conn.execute(query):
        run_id, task_id, version, run_n = record[:4]
        facts = record[4:7]
        passed, total, rounds, additions, deletions, active, wall = record[7:]
        if task is not None and task_id != task:
            continue
        rows.append(
            BenchRow(
                task_id,
                version or "unknown",
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
    f"{'wall':>6} {'rounds':>6} {'lines':>6} {'pr':>5} "
    f"{'tests':>5} {'checks':>6} {'commit':>6}"
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
        f"{_count([r.pr_opened for r in runs]):>5} "
        f"{_count([r.tests_green for r in runs]):>5} {checks:>6} "
        f"{_count([r.precommit_ok for r in runs]):>6}"
    )


def render_bench_report(rows: list[BenchRow]) -> str:
    """Per task, each version's runs side by side: medians and fact counts.

    Args:
        rows: Benchmark runs.

    Returns:
        The report text.
    """
    if not rows:
        return "No benchmark runs recorded yet. Run `forge-metrics bench start <task>`."
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
        "reached it: PR opened, sandbox tests green, task checks, pre-commit "
        "verdict (commit). version = forge in the sandbox clone at finish."
    )
    return "\n".join([*lines, notes])
