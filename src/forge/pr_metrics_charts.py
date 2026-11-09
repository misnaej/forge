"""Inline SVG charts for the ``forge-metrics`` HTML page.

Pure drawing: callers hand in plain values (columns, series, dots,
version changes), so this module knows nothing of the database or of PR
rows. Every chart has one y scale fitted to its data's actual range, and
every string that reaches the markup is escaped here.
"""

from __future__ import annotations

import html
import math
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Final

from forge.agent_profile_tokens import fmt_tokens


if TYPE_CHECKING:
    from collections.abc import Callable


#: Numbers at or above this print as compact token counts (``42k``).
COMPACT_FROM: Final = 1_000
#: Distinct series colours; series past this share the muted "other" colour.
SERIES_SLOTS: Final = 8
#: Max / min ratio above which a log-capable chart switches to log10.
LOG_SPAN: Final = 100
_VW: Final = 640
_VH: Final = 250
_ML: Final = 56
_MR: Final = 12
_MT: Final = 16
_MB: Final = 64
_PLOT_W: Final = _VW - _ML - _MR
_PLOT_H: Final = _VH - _MT - _MB
_LINEAR_TICKS: Final = 4
#: Closest two version-change labels may sit, in chart units.
_MARK_LABEL_GAP: Final = 40


@dataclass(frozen=True)
class Series:
    """One named line: a value per chart column, ``None`` for a gap."""

    name: str
    values: list[float | None]


@dataclass(frozen=True)
class Dot:
    """One point of a time chart."""

    #: Colour slot (the series' place in the legend).
    series: int
    name: str
    #: ISO day.
    day: str
    value: float
    tip: str


@dataclass(frozen=True)
class Change:
    """A series moving from one version to the next on *day*."""

    day: str
    name: str
    before: str
    after: str


@dataclass(frozen=True)
class Scale:
    """A y scale over ``lo``..``hi``, linear or log10."""

    lo: float
    hi: float
    log: bool
    #: Linear tick spacing; unused when ``log``.
    step: float = 1.0

    def y(self, value: float) -> float:
        """Chart y of *value*.

        Args:
            value: A data value inside the scale.

        Returns:
            The y coordinate, top of the plot = ``hi``.
        """
        if self.log:
            low, high = math.log10(self.lo), math.log10(self.hi)
            share = (math.log10(value) - low) / (high - low)
        else:
            share = (value - self.lo) / (self.hi - self.lo)
        return _MT + (1 - share) * _PLOT_H

    def ticks(self) -> list[float]:
        """Tick values: powers of ten when log, else evenly spaced.

        Returns:
            The values.
        """
        if self.log:
            low, high = round(math.log10(self.lo)), round(math.log10(self.hi))
            return [10.0**e for e in range(low, high + 1)]
        count = round((self.hi - self.lo) / self.step)
        return [self.lo + i * self.step for i in range(count + 1)]


def fit_scale(values: list[float], *, allow_log: bool = False) -> Scale:
    """A scale fitted to the data's actual range.

    Args:
        values: The plotted values; at least one.
        allow_log: Switch to log10 when the range spans over :data:`LOG_SPAN`.

    Returns:
        The scale.
    """
    lo, hi = min(values), max(values)
    if allow_log and lo > 0 and hi / lo > LOG_SPAN:
        return Scale(
            10.0 ** math.floor(math.log10(lo)),
            10.0 ** math.ceil(math.log10(hi)),
            log=True,
        )
    step = _nice_step((hi - lo) or abs(hi) or 1.0)
    low = math.floor(lo / step) * step
    high = math.ceil(hi / step) * step
    return Scale(low, high if high > low else low + step, log=False, step=step)


def _nice_step(span: float) -> float:
    """A 1, 2 or 5 times 10^k tick step giving about four ticks over *span*.

    Args:
        span: The data range; positive.

    Returns:
        The step.
    """
    raw = span / _LINEAR_TICKS
    magnitude = 10.0 ** math.floor(math.log10(raw))
    return next(m * magnitude for m in (1, 2, 5, 10) if m * magnitude >= raw)


def fmt_number(value: float) -> str:
    """An axis, tooltip or table number: compact from 1000, else short decimal.

    Args:
        value: The number.

    Returns:
        The text.
    """
    return fmt_tokens(round(value)) if abs(value) >= COMPACT_FROM else f"{value:.3g}"


def series_class(index: int) -> str:
    """CSS class of the *index*-th series: a fixed slot, never cycled.

    Args:
        index: The series' place in the legend.

    Returns:
        ``s1``..``s8``, or ``other`` past the eighth series.
    """
    return f"s{index + 1}" if index < SERIES_SLOTS else "other"


def _svg(label: str, y_title: str, body: str) -> str:
    """Wrap chart *body* in an ``<svg>`` with plot frame and y-axis title.

    Args:
        label: Accessible name.
        y_title: The y-axis title.
        body: Inner SVG markup, already escaped.

    Returns:
        The element.
    """
    esc = html.escape
    mid = _MT + _PLOT_H / 2
    base = _MT + _PLOT_H
    return (
        f'<svg class="chart" viewBox="0 0 {_VW} {_VH}" role="img" '
        f'aria-label="{esc(label)}">'
        f'<text class="axis-title" x="12" y="{mid:.0f}" text-anchor="middle" '
        f'transform="rotate(-90 12 {mid:.0f})">{esc(y_title)}</text>'
        f'<line class="axis" x1="{_ML}" x2="{_VW - _MR}" y1="{base}" y2="{base}"/>'
        f"{body}</svg>"
    )


def _y_axis(scale: Scale) -> str:
    """Grid lines and tick labels for *scale*, noting a log scale.

    Args:
        scale: The y scale.

    Returns:
        SVG markup.
    """
    parts = []
    for tick in scale.ticks():
        y = scale.y(tick)
        parts.append(
            f'<line class="grid" x1="{_ML}" x2="{_VW - _MR}" y1="{y:.1f}" '
            f'y2="{y:.1f}"/><text class="tick" x="{_ML - 6}" y="{y + 4:.1f}" '
            f'text-anchor="end">{html.escape(fmt_number(tick))}</text>'
        )
    if scale.log:
        parts.append(
            f'<text class="tick" x="{_ML + 4}" y="{_MT + 10}">log scale</text>'
        )
    return "".join(parts)


def _plot(
    label: str,
    y_title: str,
    values: list[float],
    layers: Callable[[Scale], list[str]],
    *,
    allow_log: bool = False,
) -> str:
    """One chart: fit its single scale, draw the axis, then *layers*.

    Args:
        label: Accessible name.
        y_title: The y-axis title.
        values: Every plotted value; empty draws "no data".
        layers: Scale → SVG markup pieces drawn over the axis.
        allow_log: Let the scale switch to log10.

    Returns:
        The ``<svg>``.
    """
    if not values:
        empty = (
            f'<text class="tick" x="{_ML + _PLOT_W / 2:.0f}" '
            f'y="{_MT + _PLOT_H / 2:.0f}" text-anchor="middle">no data</text>'
        )
        return _svg(label, y_title, empty)
    scale = fit_scale(values, allow_log=allow_log)
    return _svg(label, y_title, _y_axis(scale) + "".join(layers(scale)))


def _x_label(x: float, text: str) -> str:
    """One slanted x-axis label under the plot.

    Args:
        x: Its x coordinate.
        text: Unescaped label.

    Returns:
        SVG markup.
    """
    y = _MT + _PLOT_H + 14
    return (
        f'<text class="tick" text-anchor="end" '
        f'transform="translate({x:.1f} {y}) rotate(-35)">{html.escape(text)}</text>'
    )


def _series(
    index: int,
    name: str,
    points: list[tuple[float, float | None, str]],
    scale: Scale,
    *,
    joined: bool,
) -> str:
    """One series' marks; ``None`` values break the line (a gap, never zero).

    Args:
        index: The colour slot.
        name: Shown in each tooltip.
        points: ``(x, value or None, tooltip)`` in x order.
        scale: The y scale.
        joined: Draw a line through consecutive known points.

    Returns:
        SVG markup.
    """
    esc = html.escape
    path: list[str] = []
    dots: list[str] = []
    pen = "M"
    for x, value, tip in points:
        if value is None:
            pen = "M"
            continue
        y = scale.y(value)
        path.append(f"{pen}{x:.1f},{y:.1f}")
        pen = "L"
        dots.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4">'
            f"<title>{esc(name)} {esc(tip)}</title></circle>"
        )
    line = f'<path d="{" ".join(path)}"/>' if joined and path else ""
    return f'<g class="series {series_class(index)}">{line}{"".join(dots)}</g>'


def category_chart(
    columns: list[str], series: list[Series], *, label: str, y_title: str
) -> str:
    """A line chart over named columns, one line per series.

    Args:
        columns: X-axis categories, in order.
        series: Lines; each carries one value per column.
        label: Accessible name.
        y_title: Y-axis title.

    Returns:
        The ``<svg>``.
    """
    step = _PLOT_W / max(len(columns), 1)
    xs = [_ML + (i + 0.5) * step for i in range(len(columns))]

    def layers(scale: Scale) -> list[str]:
        parts = [_x_label(x, column) for x, column in zip(xs, columns, strict=True)]
        for index, line in enumerate(series):
            points = [
                (x, value, f"{column}: {'-' if value is None else fmt_number(value)}")
                for x, column, value in zip(xs, columns, line.values, strict=True)
            ]
            parts.append(_series(index, line.name, points, scale, joined=True))
        return parts

    values = [v for line in series for v in line.values if v is not None]
    return _plot(label, y_title, values, layers)


def _change_marks(changes: list[Change], x_of: Callable[[str], float]) -> str:
    """Dashed lines on days a series moved version; tooltips list each move.

    Args:
        changes: The moves.
        x_of: ISO day → chart x.

    Returns:
        SVG markup.
    """
    esc = html.escape
    by_day: dict[str, list[Change]] = {}
    for change in changes:
        by_day.setdefault(change.day, []).append(change)
    parts = []
    labelled = -math.inf
    for day in sorted(by_day):
        moves = by_day[day]
        x = x_of(day)
        tip = "; ".join(f"{m.name} {m.before} → {m.after}" for m in moves)
        parts.append(
            f'<line class="mark" x1="{x:.1f}" x2="{x:.1f}" y1="{_MT}" '
            f'y2="{_MT + _PLOT_H}"><title>{esc(day)}: {esc(tip)}</title></line>'
        )
        if x - labelled >= _MARK_LABEL_GAP:
            labelled = x
            text = moves[0].after if len(moves) == 1 else f"{len(moves)} changes"
            parts.append(
                f'<text class="mark-label" x="{x - 2:.1f}" y="{_MT - 4}" '
                f'text-anchor="end">{esc(text)}<title>{esc(tip)}</title></text>'
            )
    return "".join(parts)


def time_chart(
    dots: list[Dot], changes: list[Change], *, label: str, y_title: str
) -> str:
    """Dots by day, version changes marked; log scale past a 100x range.

    The x range spans every dot *and* every change, so no mark falls
    outside the plot.

    Args:
        dots: The points.
        changes: Version moves to mark.
        label: Accessible name.
        y_title: Y-axis title.

    Returns:
        The ``<svg>``.
    """
    days = [date.fromisoformat(d.day).toordinal() for d in dots] + [
        date.fromisoformat(c.day).toordinal() for c in changes
    ]
    first = min(days, default=0)
    last = max(days, default=0)
    span = (last - first) or 1

    def x_of(day: str) -> float:
        return _ML + (date.fromisoformat(day).toordinal() - first) / span * _PLOT_W

    def layers(scale: Scale) -> list[str]:
        ticks = sorted({first, first + span // 2, last})
        parts = [_x_label(x_of(d), d) for d in map(_iso, ticks)]
        parts.append(_change_marks(changes, x_of))
        for index in sorted({d.series for d in dots}):
            own = [d for d in dots if d.series == index]
            points: list[tuple[float, float | None, str]] = [
                (x_of(d.day), d.value, d.tip) for d in own
            ]
            parts.append(_series(index, own[0].name, points, scale, joined=False))
        return parts

    return _plot(label, y_title, [d.value for d in dots], layers, allow_log=True)


def _iso(ordinal: int) -> str:
    """An ordinal day as ISO text.

    Args:
        ordinal: :meth:`date.toordinal` value.

    Returns:
        ``YYYY-MM-DD``.
    """
    return date.fromordinal(ordinal).isoformat()
