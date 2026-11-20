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
