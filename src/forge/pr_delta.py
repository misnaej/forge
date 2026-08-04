"""pr_delta — thresholds and helpers for pr-manager finalization short-circuits.

`forge:pr-manager` (via the `/pr` skill) has two independent ways to skip
the full three-agent re-verification: **delta mode**, when the diff since
the last verified SHA is small and stays out of high-blast-radius areas,
and the **docs-only light path**, when the whole diff is doc-shaped and
touches no high-blast-radius path. The thresholds, path globs, and the
SHA-extraction helper live here so the agent prompt, future audit guards,
and any consumer wrapper read them from one source of truth.

The agent prompt references this module by path; the constants are not
imported by the agent runtime (agents are markdown). Anything that
*does* execute (a precommit check, a CI guard, a future helper CLI)
imports from here.
"""

from __future__ import annotations

import re
from fnmatch import fnmatch
from typing import TYPE_CHECKING, Final

from forge import config


if TYPE_CHECKING:
    from pathlib import Path


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
    "skills/",
    ".claude-plugin/",
    ".claude/",
    ".github/workflows/",
    "pyproject.toml",
    "ruff.toml",
    "FOUNDATION.md",
    "CLAUDE.md",
)


# Globs a diff may consist ENTIRELY of and still qualify as "docs-only"
# for the light finalization path: no design/security reporter round, no
# strict whole-tree pre-commit — only path-relevant gates. Consumers add
# extra globs via ``[tool.forge.pr].docs_only_globs`` (additive; the
# built-ins always apply). High-blast-radius paths trump these globs:
# agent/skill/hook markdown IS shipped behavior, never "docs".
#
# Extension-anchored on purpose: ``fnmatch``'s ``*`` crosses ``/``, so
# ``*.md`` already covers ``docs/**/*.md`` — while a directory glob like
# ``docs/*`` would match ANY extension nested under it (``docs/evil.py``),
# a working review bypass. Consumer extra globs inherit the same fnmatch
# semantics; prefer extension-anchored forms.
DOCS_ONLY_GLOBS: Final[tuple[str, ...]] = (
    "*.md",
    "*.rst",
    "*.txt",
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
        # Case-folded: on a case-insensitive filesystem (APFS default)
        # `Agents/x.md` lands in the same on-disk directory as `agents/`,
        # so exact-case matching would let it dodge the blast-radius list.
        folded = path.casefold()
        for glob in HIGH_BLAST_RADIUS_PATHS:
            if glob.endswith("/"):
                if folded.startswith(glob):
                    hits.append(path)
                    break
            elif folded == glob.casefold():
                hits.append(path)
                break
    return hits


def configured_docs_only_globs(repo_root: Path) -> tuple[str, ...]:
    """Return the consumer's extra docs-only globs from ``[tool.forge.pr]``.

    The Python-side reader for the ``docs_only_globs`` key — callers pass
    the result to :func:`docs_only_diff` as ``extra_globs`` instead of
    parsing ``pyproject.toml`` themselves. Additive by contract: the
    built-in :data:`DOCS_ONLY_GLOBS` always apply regardless of config.

    Args:
        repo_root: Git repo root containing ``pyproject.toml``.

    Returns:
        Configured glob strings, or ``()`` when the key (or the file) is
        absent or malformed.
    """
    data = config.read_pyproject_raw(repo_root)
    pr_cfg = ((data.get("tool") or {}).get("forge") or {}).get("pr") or {}
    globs = pr_cfg.get("docs_only_globs")
    if not isinstance(globs, list):
        return ()
    return tuple(g for g in globs if isinstance(g, str))


def docs_only_diff(
    changed_paths: list[str],
    extra_globs: tuple[str, ...] = (),
) -> bool:
    """Return whether a diff qualifies for the docs-only light path.

    True only when every changed path matches a docs glob AND no path is
    high-blast-radius — ``agents/``, ``skills/``, ``claude-hooks/`` and
    friends are shipped behavior in doc-shaped files, so they always take
    the full verification round regardless of extension.

    Known residual (documented, accepted): classification sees path
    strings only — a symlinked ``*.md`` or an injection-shaped prose
    change is not detected here; the human PR review and the
    docs-types-checker (which still runs on the light path) remain the
    reviewers of record for doc content.

    Args:
        changed_paths: Repo-relative paths from ``git diff --name-only``.
        extra_globs: Consumer additions from
            ``[tool.forge.pr].docs_only_globs`` — additive to
            :data:`DOCS_ONLY_GLOBS`, never replacing them.

    Returns:
        ``True`` when the light finalization path applies; ``False`` for
        an empty diff (nothing to classify), any high-blast-radius hit,
        or any path outside the combined glob set.
    """
    if not changed_paths or touches_high_blast_radius(changed_paths):
        return False
    globs = tuple(g.casefold() for g in DOCS_ONLY_GLOBS + tuple(extra_globs))
    return all(
        any(fnmatch(path.casefold(), g) for g in globs) for path in changed_paths
    )


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
            (
                f"diff is {line_count} lines (> {DELTA_LINE_THRESHOLD}); "
                "full re-check required"
            ),
        )
    hot = touches_high_blast_radius(changed_paths)
    if hot:
        return (
            False,
            (
                f"diff touches high-blast-radius path(s): {', '.join(hot)}; "
                "full re-check required"
            ),
        )
    return (
        True,
        (
            f"diff is {line_count} lines under {DELTA_LINE_THRESHOLD}, "
            "no high-blast-radius paths"
        ),
    )
