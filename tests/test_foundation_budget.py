"""Shrink-only word budget for FOUNDATION.md.

FOUNDATION.md is inlined into every agent session in every forge consumer
repo, so its size is a per-session cost. This ratchet caps its whitespace
word count (`len(text.split())`, the counting `forge-audit-agents` uses).
forge-docs/ pages are deliberately uncapped by maintainer decision: they
are opened on demand, not loaded every session.
"""

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]

# Current ceiling; only ever lowered.
FOUNDATION_WORD_BASELINE = 8661
# Headroom allowed between the baseline and the real count before it is stale.
SLACK = 200


def _foundation_word_count() -> int:
    """Return the whitespace word count of FOUNDATION.md."""
    return len((REPO_ROOT / "FOUNDATION.md").read_text(encoding="utf-8").split())


def test_foundation_within_word_budget() -> None:
    """FOUNDATION.md must not exceed the word baseline."""
    count = _foundation_word_count()
    assert count <= FOUNDATION_WORD_BASELINE, (
        f"FOUNDATION.md has {count} words, baseline is "
        f"{FOUNDATION_WORD_BASELINE} (over by {count - FOUNDATION_WORD_BASELINE}). "
        "Condense in place, or move procedure to a forge-docs page and "
        "leave a one-line rule plus a pointer."
    )


def test_foundation_baseline_not_stale() -> None:
    """The baseline must track the real count within SLACK words."""
    count = _foundation_word_count()
    assert FOUNDATION_WORD_BASELINE - count <= SLACK, (
        f"FOUNDATION.md shrank to {count} words; lower "
        f"FOUNDATION_WORD_BASELINE to {count}."
    )
