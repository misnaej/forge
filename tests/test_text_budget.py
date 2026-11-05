"""Tests for forge.text_budget: the one margin over a text-length target."""

from __future__ import annotations

import pytest

from forge.text_budget import ceiling


@pytest.mark.parametrize(
    ("target", "expected"),
    [(0, 0), (50, 55), (120, 132), (160, 176)],
)
def test_ceiling_adds_ten_percent_of_the_target(target: int, expected: int) -> None:
    """The ceiling is the target plus 10 percent, rounded down.

    Args:
        target: Budget target word count.
        expected: Most words the budget accepts.
    """
    assert ceiling(target) == expected
