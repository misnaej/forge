"""The one margin forge allows over a text-length target.

A budget on text an agent writes (a commit message, a squash message, a
PR wrap-up) names a target. Text a little over it still passes, with a
warning; only text past the ceiling is refused. Refusing at the first
word over sent agents round a rewrite loop over a word or two, which
costs more than the words saved.
"""

from __future__ import annotations

from typing import Final


MARGIN_PERCENT: Final[int] = 10


def ceiling(target: int) -> int:
    """Return the most words a budget with *target* accepts.

    Integer arithmetic, rounded down, so the ceiling never depends on
    float rounding.

    Args:
        target: The budget's target word count.

    Returns:
        *target* plus :data:`MARGIN_PERCENT` percent of it.
    """
    return target + target * MARGIN_PERCENT // 100
