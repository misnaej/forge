"""Tests for ``forge.pr_metrics_charts`` — the inline SVG charts."""

from __future__ import annotations

import re

from forge.pr_metrics_charts import (
    Change,
    Dot,
    Series,
    category_chart,
    fit_scale,
    time_chart,
)


#: Right edge of the plot area in chart units (viewBox width minus margin).
_PLOT_RIGHT = 640 - 12
_PLOT_LEFT = 56


def _dot(day: str, value: float, series: int = 0) -> Dot:
    """A time-chart point.

    Args:
        day: ISO day.
        value: Its value.
        series: Colour slot.

    Returns:
        The dot.
    """
    return Dot(series, f"r{series}", day, value, f"{day}: {value}")


def _mark_xs(svg: str) -> list[float]:
    """X positions of the version-change lines in *svg*.

    Args:
        svg: Chart markup.

    Returns:
        The x coordinates.
    """
    return [float(x) for x in re.findall(r'class="mark" x1="([\d.]+)"', svg)]


def test_scale_switches_to_log_only_past_a_hundredfold_range() -> None:
    """Behavior: log10 above a 100x span when allowed; linear otherwise."""
    assert fit_scale([10.0, 5000.0], allow_log=True).log
    assert not fit_scale([10.0, 900.0], allow_log=True).log
    assert not fit_scale([10.0, 5000.0]).log


def test_time_chart_draws_a_dot_per_point_and_notes_log_scale() -> None:
    """Behavior: one circle per dot in one <svg>; a 100x range says log scale."""
    svg = time_chart(
        [_dot("2026-09-01", 10), _dot("2026-09-02", 50_000, series=1)],
        [],
        label="l",
        y_title="y",
    )

    assert svg.count("<svg") == 1
    assert svg.count("<circle") == 2
    assert "log scale" in svg


def test_marks_outside_the_dot_range_still_fall_inside_the_plot() -> None:
    """Regression: a change after the last dot widens the x range, not overflows."""
    svg = time_chart(
        [_dot("2026-09-01", 10), _dot("2026-09-02", 20)],
        [Change("2026-09-10", "r0", "1.0.0", "1.1.0")],
        label="l",
        y_title="y",
    )

    [x] = _mark_xs(svg)
    assert _PLOT_LEFT <= x <= _PLOT_RIGHT


def test_same_day_changes_share_one_mark_naming_every_move() -> None:
    """Behavior: same-day moves → one line labelled 'n changes', each in the tip."""
    svg = time_chart(
        [_dot("2026-09-01", 10), _dot("2026-09-05", 20)],
        [
            Change("2026-09-03", "aaaaaa", "1.0.0", "1.1.0"),
            Change("2026-09-03", "bbbbbb", "0.9.0", "1.1.0"),
        ],
        label="l",
        y_title="y",
    )

    assert len(_mark_xs(svg)) == 1
    assert ">2 changes<" in svg
    assert "aaaaaa 1.0.0 → 1.1.0" in svg
    assert "bbbbbb 0.9.0 → 1.1.0" in svg


def test_category_chart_breaks_lines_at_gaps() -> None:
    """Behavior: a None value is a gap — two path segments, no dot at zero."""
    svg = category_chart(
        ["a", "b", "c"],
        [Series("r", [1.0, None, 3.0])],
        label="l",
        y_title="y",
    )

    assert svg.count("<circle") == 2
    [path] = re.findall(r'<path d="([^"]+)"', svg)
    assert path.count("M") == 2
    assert "L" not in path
