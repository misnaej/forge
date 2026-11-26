"""Tests for forge.text_budget: the one ceiling-and-refusal convention."""

from __future__ import annotations

import pytest

from forge.text_budget import length_verdict, refusal_threshold


@pytest.mark.parametrize(
    ("ceiling", "expected"),
    [(0, 0), (7, 8), (12, 14), (45, 54), (50, 60), (120, 144), (160, 192)],
)
def test_refusal_threshold_adds_twenty_percent_rounded_down(
    ceiling: int, expected: int
) -> None:
    """Behavior: threshold is the ceiling plus 20 percent, rounded down (7 -> 8, not 9).

    Args:
        ceiling: Stated limit for a kind of text.
        expected: Last count not refused.
    """
    assert refusal_threshold(ceiling) == expected


@pytest.mark.parametrize(
    ("words", "expected"),
    [
        (0, "ok"),
        (44, "ok"),
        (45, "ok"),
        (46, "warn"),
        (54, "warn"),
        (55, "refuse"),
        (200, "refuse"),
    ],
)
def test_length_verdict_bands_at_their_boundaries(words: int, expected: str) -> None:
    """Behavior: ok at the ceiling, warn one over, warn at 54, refuse one past it.

    Args:
        words: Word count under test, for a ceiling of 45.
        expected: The band it falls in.
    """
    assert length_verdict(words, 45) == expected


@pytest.mark.parametrize(
    ("words", "expected"),
    [(12, "ok"), (13, "warn"), (14, "warn"), (15, "refuse")],
)
def test_length_verdict_is_relative_to_the_ceiling_given(
    words: int, expected: str
) -> None:
    """Behavior: the same counts band differently for a ceiling of 12.

    Args:
        words: Word count under test.
        expected: The band it falls in.
    """
    assert length_verdict(words, 12) == expected


@pytest.mark.parametrize(
    ("ceiling", "refuse_over"), [(12, 24), (12, 13), (12, 12), (45, 100)]
)
def test_refusal_threshold_refuse_over_is_absolute(
    ceiling: int, refuse_over: int
) -> None:
    """Behavior: refuse_over is the threshold, not a width added to the ceiling.

    Args:
        ceiling: Stated limit for a kind of text.
        refuse_over: Last acceptable count, equal to the ceiling in one case.
    """
    assert refusal_threshold(ceiling, refuse_over=refuse_over) == refuse_over


def test_refusal_threshold_rejects_refuse_over_below_ceiling() -> None:
    """Behavior: a refusal threshold under the ceiling is rejected."""
    with pytest.raises(ValueError, match="below the ceiling"):
        refusal_threshold(12, refuse_over=11)


@pytest.mark.parametrize("words", [5, 12, 13])
def test_length_verdict_rejects_refuse_over_below_ceiling_at_any_length(
    words: int,
) -> None:
    """Behavior: an incoherent refuse_over raises whatever the word count.

    Detects the guard running after the band comparison: a wrong constant
    must fail on the first call, not on the first text long enough to
    reach it. Word counts at and under the ceiling are the cases an
    early "ok" return would let through.

    Args:
        words: Word count under test, for a ceiling of 12.
    """
    with pytest.raises(ValueError, match="below the ceiling"):
        length_verdict(words, 12, refuse_over=11)


@pytest.mark.parametrize(
    ("words", "refuse_over", "expected"),
    [
        (12, 24, "ok"),
        (13, 24, "warn"),
        (24, 24, "warn"),
        (25, 24, "refuse"),
        (12, 12, "ok"),
        (13, 12, "refuse"),
    ],
)
def test_length_verdict_bands_with_refuse_over(
    words: int, refuse_over: int, expected: str
) -> None:
    """Behavior: with refuse_over, warn band is ceiling..refuse_over; equal: none.

    Args:
        words: Word count under test, for a ceiling of 12.
        refuse_over: Last acceptable count.
        expected: The band it falls in.
    """
    assert length_verdict(words, 12, refuse_over=refuse_over) == expected
