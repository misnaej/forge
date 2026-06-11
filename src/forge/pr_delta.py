"""pr_delta — thresholds and helpers for pr-manager delta-mode short-circuit.

`forge:pr-manager` short-circuits a full three-agent re-verification when
the diff since the last verified SHA is small and stays out of
high-blast-radius areas. The thresholds + the SHA-extraction helper live
here so the agent prompt, future audit guards, and any consumer wrapper
read them from one source of truth.

The agent prompt references this module by path; the constants are not
imported by the agent runtime (agents are markdown). Anything that
*does* execute (a precommit check, a CI guard, a future helper CLI)
imports from here.
"""

from __future__ import annotations

import re
from typing import Final


# Maximum line-count diff (insertions + deletions) below which a follow-up
# commit is eligible for delta-mode re-verification. Above this threshold
# pr-manager re-invokes the three reporter agents in full.
DELTA_LINE_THRESHOLD: Final[int] = 50


# Path globs that always force a full re-verification when touched, even
# under the line threshold. These are the surfaces where a lint-only or
# typo-fix sweep can still alter design / security / docs semantics.
HIGH_BLAST_RADIUS_PATHS: Final[tuple[str, ...]] = (
    "agents/",
    "claude-hooks/",
    ".githooks/",
    "pyproject.toml",
    "ruff.toml",
    "FOUNDATION.md",
    "CLAUDE.md",
)


# Matches the reporter-agent header contract documented in
# agents/_TEMPLATE.md "Reporter-agent header contract".
# Example: `verified-at: 7ab3e4e   (PR #56, branch fix/foo)`
VERIFIED_AT_RE: Final[re.Pattern[str]] = re.compile(
    r"^verified-at:\s*(?P<sha>[0-9a-f]{7,40})\b",
    re.MULTILINE,
)


def extract_verified_shas(text: str) -> list[str]:
    """Return every ``verified-at:`` SHA referenced in *text*.

    Args:
        text: Raw markdown body (typically a PR comment).

    Returns:
        Ordered list of short SHAs as they appear in the text. Duplicate
        SHAs are preserved; the caller decides whether to dedupe.
    """
    return [m.group("sha") for m in VERIFIED_AT_RE.finditer(text)]


def touches_high_blast_radius(changed_paths: list[str]) -> list[str]:
    """Return the subset of *changed_paths* under :data:`HIGH_BLAST_RADIUS_PATHS`.

    Args:
        changed_paths: Repo-relative paths from ``git diff --name-only``.

    Returns:
        Subset of paths that match any high-blast-radius glob, in input
        order. Empty when no path matches.
    """
    hits: list[str] = []
    for path in changed_paths:
        for glob in HIGH_BLAST_RADIUS_PATHS:
            if glob.endswith("/"):
                if path.startswith(glob):
                    hits.append(path)
                    break
            elif path == glob:
                hits.append(path)
                break
    return hits


def delta_decision(
    *,
    line_count: int,
    changed_paths: list[str],
) -> tuple[bool, str]:
    """Decide whether a follow-up diff qualifies for delta-mode re-check.

    Args:
        line_count: Insertions + deletions in the diff
            (``git diff --shortstat`` sums the two).
        changed_paths: Repo-relative paths from ``git diff --name-only``.

    Returns:
        Tuple of ``(use_delta, reason)``. ``use_delta`` is ``True`` when
        the diff is below :data:`DELTA_LINE_THRESHOLD` AND touches no
        high-blast-radius path. ``reason`` is a one-line human-readable
        explanation suitable for the delta comment body.
    """
    if line_count > DELTA_LINE_THRESHOLD:
        return (
            False,
            f"diff is {line_count} lines (> {DELTA_LINE_THRESHOLD}); "
            "full re-check required",
        )
    hot = touches_high_blast_radius(changed_paths)
    if hot:
        return (
            False,
            f"diff touches high-blast-radius path(s): {', '.join(hot)}; "
            "full re-check required",
        )
    return (
        True,
        f"diff is {line_count} lines under {DELTA_LINE_THRESHOLD}, "
        "no high-blast-radius paths",
    )
