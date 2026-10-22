"""Shared "keep N characters, then add a marker" text cut.

Forge bounds untrusted or unbounded text before it reaches a PR body, a
CI log, a terminal or a ``code_health/`` log. The plain form of that cut
— keep the first *limit* characters and append a marker after them — is
this module's one helper, so every site applying it agrees on where the
marker goes. Callers keep their own marker, any budget arithmetic and
any stripping: a site that counts the marker inside a fixed width, or
trims trailing whitespace from the kept part, is a different cut and
stays hand-written where it is.
"""

from __future__ import annotations


def truncate(text: str, limit: int, *, suffix: str = "…") -> str:
    """Return *text* cut to *limit* characters plus *suffix* when longer.

    The suffix is appended after the kept characters, never counted
    inside *limit*, so a cut result is ``limit + len(suffix)`` long.

    Args:
        text: The text to bound.
        limit: Maximum characters kept from *text*.
        suffix: Marker appended when *text* was cut.

    Returns:
        *text* itself, or its first *limit* characters followed by
        *suffix*.
    """
    if len(text) <= limit:
        return text
    return text[:limit] + suffix
