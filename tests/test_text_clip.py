"""Tests for the shared ``truncate`` text cut."""

from __future__ import annotations

from forge.text_clip import truncate


def test_under_limit_is_unchanged() -> None:
    """Text shorter than the limit comes back as-is, with no marker."""
    assert truncate("abc", 5) == "abc"


def test_at_limit_is_unchanged() -> None:
    """Text exactly at the limit is not cut — the boundary is inclusive."""
    assert truncate("abcde", 5) == "abcde"


def test_over_limit_keeps_limit_then_default_ellipsis() -> None:
    """Over the limit keeps *limit* characters and appends the marker after."""
    assert truncate("abcdef", 5) == "abcde…"


def test_custom_suffix_is_appended_outside_the_limit() -> None:
    """A caller's marker is appended whole, not counted inside the limit."""
    assert truncate("abcdef", 3, suffix="\n… (truncated)") == "abc\n… (truncated)"


def test_empty_suffix_is_a_bare_slice() -> None:
    """An empty suffix reduces the cut to a plain prefix slice."""
    assert truncate("abcdef", 4, suffix="") == "abcd"
