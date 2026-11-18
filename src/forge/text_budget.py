"""The one margin forge allows over a text-length target.

A budget on text an agent writes (a commit message, a squash message, a
PR wrap-up) names a target the prompt asks for. Nothing is refused on
length: text over the target posts, and only text past this margin says
so, once. Once a message is written it is not written again — a rewrite
loop over a word or two costs more than the words it saves, and length
was never the real problem. What makes a message quick to read is what
it leaves out, which is a content rule (FOUNDATION §6) rather than a
count: the measured waste was process state and bookkeeping, not prose.
"""

from __future__ import annotations

from typing import Final


MARGIN_PERCENT: Final[int] = 20


def ceiling(target: int) -> int:
    """Return the word count past which a budget with *target* warns.

    Integer arithmetic, rounded down, so the threshold never depends on
    float rounding. Nothing refuses at it: a budget warns once above it
    and the text still ships.

    Args:
        target: The budget's target word count.

    Returns:
        *target* plus :data:`MARGIN_PERCENT` percent of it.
    """
    return target + target * MARGIN_PERCENT // 100
