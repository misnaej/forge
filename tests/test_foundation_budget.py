"""Shrink-only word budget for FOUNDATION.md — advisory, never a gate.

FOUNDATION.md is inlined into every agent session in every forge consumer
repo, so its size is a per-session cost worth watching. This module
reports that cost; it does not block on it.

**Why it warns rather than fails.** As a hard gate it stopped changes
whose content was wanted, and the way past it was to compress prose
already written as tightly as its meaning allowed — paying in clarity,
in a file whose whole job is to be understood on first read, to buy back
a handful of words. That trade is the wrong way round: a rule nobody can
follow without mangling it is worse than a slightly longer rule. Size is
a review lens here, the same standing FOUNDATION §18 gives cost
("Enforcement is a review lens, never a gate — no build fails on a
duration").

The number still has to be visible, or the ratchet stops working: the
warning names the overage so it shows in every run's summary, and a
reviewer can ask whether the addition earned its words.

forge-docs/ pages are deliberately uncapped by maintainer decision: they
are opened on demand, not loaded every session.
"""

import warnings
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]

# Current reference point; rounded, then only ever lowered as the file
# shrinks. Exceeding it warns — see the module docstring.
FOUNDATION_WORD_BASELINE = 8700
# Headroom allowed between the baseline and the real count before the
# baseline is considered stale and worth lowering.
SLACK = 200


def _foundation_word_count() -> int:
    """Return the whitespace word count of FOUNDATION.md."""
    return len((REPO_ROOT / "FOUNDATION.md").read_text(encoding="utf-8").split())


def test_foundation_word_budget_is_reported() -> None:
    """Report FOUNDATION.md's size against the baseline; never fail on it.

    SCENARIO: the file is at, under, or over the word baseline.
    EXPECTED BEHAVIOR: an overage emits a warning naming the count and
    the overage, so it appears in the run's warning summary and in
    review. The test itself always passes — blocking on this is what
    this module deliberately stopped doing.
    """
    count = _foundation_word_count()
    if count > FOUNDATION_WORD_BASELINE:
        warnings.warn(
            f"FOUNDATION.md has {count} words, baseline is "
            f"{FOUNDATION_WORD_BASELINE} (over by "
            f"{count - FOUNDATION_WORD_BASELINE}). Advisory: condense in "
            "place, or move procedure to a forge-docs page and leave a "
            "one-line rule plus a pointer — but not at the cost of the "
            "rule being understood.",
            stacklevel=1,
        )


def test_foundation_baseline_not_stale() -> None:
    """Report when the file has shrunk well below the baseline.

    SCENARIO: FOUNDATION.md is more than SLACK words under the baseline,
    so the baseline no longer describes the file.
    EXPECTED BEHAVIOR: a warning asking for the baseline to be lowered.
    Advisory for the same reason as the budget itself: a stale reference
    point is a bookkeeping matter, not a defect in the change that
    revealed it.
    """
    count = _foundation_word_count()
    if FOUNDATION_WORD_BASELINE - count > SLACK:
        warnings.warn(
            f"FOUNDATION.md shrank to {count} words; lower "
            f"FOUNDATION_WORD_BASELINE to {count}.",
            stacklevel=1,
        )
