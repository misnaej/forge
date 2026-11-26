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

forge-docs/ pages carry ceilings too, by a later maintainer decision
reversing the earlier one that left them uncapped. The reasoning that
exempted them — they are read on demand, not loaded every session — is
why their ceilings are generous rather than why they have none: without
any, moving procedure out of FOUNDATION reduces the session cost and
hides the growth somewhere nobody measures, and the detail an agent
reads on demand gets worse in exactly that blind spot.

Each page's ceiling is its own, seeded from its size when the ceilings
were introduced, because a reference catalogue and a procedure page have
no common right length. A new page must be added to the table, so
growing the surface is deliberate rather than incidental.
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

# Per-page ceilings for the pages FOUNDATION points at, rounded up from
# each page's size when ceilings were introduced. A ceiling, never a
# target: the shortest page carrying its detail is the right one
# (`forge-docs/instruction-compaction.md`). Lower one when a page
# shrinks; raise one only when the added detail is worth naming in the
# PR that raises it.
FORGE_DOCS_CEILINGS: dict[str, int] = {
    "agent-contracts.md": 1000,
    "backlog-tracks.md": 1300,
    "ci-recipe.md": 4400,
    "configuration.md": 9800,
    "continuation.md": 800,
    "instruction-compaction.md": 1200,
    "git-safety.md": 2500,
    "integration-staleness.md": 800,
    "metrics.md": 2400,
    "precommit-fixer.md": 1300,
    "smart-test.md": 1700,
    "token-optimization.md": 1400,
}


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


def test_forge_docs_pages_report_their_own_budgets() -> None:
    """Report each pointed page against its ceiling; never fail on it.

    SCENARIO: a page under ``forge-docs/`` is at, under or over its own
    ceiling, or is new and absent from the table.
    EXPECTED BEHAVIOR: an overage or an untabled page warns, naming the
    page, so moving procedure out of FOUNDATION cannot hide growth where
    nothing measures it. Never fails — same standing as the FOUNDATION
    budget above.
    """
    pages = sorted((REPO_ROOT / "forge-docs").glob("*.md"))
    for page in pages:
        count = len(page.read_text(encoding="utf-8").split())
        ceiling = FORGE_DOCS_CEILINGS.get(page.name)
        if ceiling is None:
            warnings.warn(
                f"forge-docs/{page.name} has no ceiling in "
                f"FORGE_DOCS_CEILINGS ({count} words). Add one, so a new "
                "pointed page is a deliberate addition to the surface.",
                stacklevel=1,
            )
            continue
        if count > ceiling:
            warnings.warn(
                f"forge-docs/{page.name} has {count} words, ceiling is "
                f"{ceiling} (over by {count - ceiling}). Advisory: a "
                "ceiling is not a target — condense, or say in the PR why "
                "the added detail earns its words.",
                stacklevel=1,
            )
