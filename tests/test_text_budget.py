"""Tests for forge.text_budget: the one margin over a text-length target."""

from __future__ import annotations

import pytest

from forge.text_budget import ceiling


@pytest.mark.parametrize(
    ("target", "expected"),
    [(0, 0), (7, 8), (45, 54), (50, 60), (120, 144), (160, 192)],
)
def test_ceiling_adds_twenty_percent_of_the_target(target: int, expected: int) -> None:
    """The ceiling is the target plus 20 percent, rounded down (7 -> 8, not 9).

    Args:
        target: Budget target word count.
        expected: Most words the budget accepts.
    """
    assert ceiling(target) == expected
