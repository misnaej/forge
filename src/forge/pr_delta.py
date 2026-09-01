"""pr_delta — thresholds and helpers for pr-manager finalization short-circuits.

`forge:pr-manager` (via the `/pr` skill) has four independent ways to
skip the full three-agent re-verification: **delta mode** (diff since
the last verified SHA is small, out of high-blast-radius areas),
the **docs-only light path** (whole diff doc-shaped), **regen-only
eligibility** (managed artifacts, earned via provenance gates), and the
**light-code path** (small, no added files, no source or blast-radius
path — earned at publish time by the hook's classifier re-run). The
thresholds, path globs, and the SHA-extraction helper live here so the
agent prompt, future audit guards, and any consumer wrapper read them from
one source of truth.

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


# Forge-managed regen artifacts a resync PR's diff may consist entirely
# of. The `/pr` regen-verified light path classifies with
# :func:`regen_only_diff` and then EARNS the escape per-PR by running the
# provenance gates (`forge-precommit --only` over
# :data:`PROVENANCE_GATE_STEPS`) — classification alone never skips
# review, and a provenance failure (including the editable-install
# self-reference case) falls back to the full round. Other
# bootstrap-managed files (badges, hook wrappers) are deliberately
# absent: a diff touching them takes the full path.
MANAGED_REGEN_PATHS: Final[tuple[str, ...]] = (
    "FOUNDATION.md",
    "docs/cli-reference.md",
    "docs/api-digest.md",
)


# The pre-commit steps that byte-verify MANAGED_REGEN_PATHS against the
# installed package. Executable callers (`forge-resync`'s PR-body
# evidence) build their `forge-precommit --only` argv from this tuple;
# the `/pr` skill's prose names the same three steps.
PROVENANCE_GATE_STEPS: Final[tuple[str, ...]] = (
    "foundation_md_check",
    "cli_reference_check",
    "api_digest_check",
)


# Maximum line-count diff (insertions + deletions) below which a PR with
# no added files, no source-under-src change, and no high-blast-radius
# path qualifies for the LIGHT wrap-up (`wrapup-mode: light`): reporters
# skipped, short-form wrap-up, strict pre-commit still in full. The
# escape is never agent discretion — `block_unverified_pr_create`
# re-runs the classifier at publish time and blocks on disagreement.
LIGHT_WRAPUP_LINE_THRESHOLD: Final[int] = 50


# Path prefixes that count as "source" for the light wrap-up: a diff
# touching shipped package code always takes the full reporter round,
# however small. Deliberately narrower than HIGH_BLAST_RADIUS_PATHS
# (adding src/ there would silently narrow the delta path too).
SOURCE_PATHS: Final[tuple[str, ...]] = ("src/",)


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
    pr_cfg = config.read_tool_forge_section(repo_root, "pr")
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


def regen_only_diff(changed_paths: list[str]) -> bool:
    """Return whether every changed path is a forge-managed regen artifact.

    Pure path classifier, same contract shape as :func:`docs_only_diff`
    — it decides *eligibility* for the regen-verified light path, never
    the escape itself: provenance is established by the pre-commit gates
    named on :data:`MANAGED_REGEN_PATHS`, which byte-verify each file
    against the installed package at finalization time.

    Matching is **exact-case, deliberately** — the opposite of
    :func:`touches_high_blast_radius`'s casefolding. The provenance
    gates address files by these exact canonical paths, so on a
    case-sensitive filesystem a case-varied path (``FOUNDATION.MD``)
    is a *different, unverified* file: classifying it eligible while
    the gate self-skips would hand it a reporter-free merge. Casefold
    widens the blast-radius net safely; here it would widen the escape.

    Known residual (documented, accepted): classification sees path
    strings only. A managed path whose content was hand-edited after
    regen classifies as eligible here and is caught by the provenance
    gates instead — that split is the design, not a gap.

    Args:
        changed_paths: Repo-relative paths from ``git diff --name-only``.

    Returns:
        ``True`` when the diff is non-empty and every path is managed;
        ``False`` otherwise.
    """
    if not changed_paths:
        return False
    return all(path in MANAGED_REGEN_PATHS for path in changed_paths)


def touches_source_paths(changed_paths: list[str]) -> list[str]:
    """Return the subset of *changed_paths* under :data:`SOURCE_PATHS`.

    Casefolded prefix match, same rationale as
    :func:`touches_high_blast_radius` — a case-varied path must not
    dodge the source net on a case-insensitive filesystem.

    Args:
        changed_paths: Repo-relative paths from ``git diff --name-only``.

    Returns:
        Subset of paths under a source prefix, in input order.
    """
    return [
        path
        for path in changed_paths
        if any(path.casefold().startswith(prefix) for prefix in SOURCE_PATHS)
    ]


def light_wrapup_decision(
    *,
    line_count: int,
    changed_paths: list[str],
    added_paths: list[str],
) -> tuple[bool, str]:
    """Decide whether a diff qualifies for the light wrap-up path.

    Objective signals only — never agent judgment, checked in this
    order: non-empty diff, no added files (the prior-art gate stays
    independent and fires before any size check), line count under the
    threshold, no high-blast-radius path, no source-package change.
    Mirrors
    :func:`delta_decision`'s ``(verdict, reason)`` shape so callers and
    the publish-time hook re-check render the same trail.

    Args:
        line_count: Insertions + deletions across the whole PR diff.
        changed_paths: Repo-relative paths from ``git diff --name-only``.
        added_paths: Paths from ``git diff --name-only --diff-filter=A``.

    Returns:
        ``(use_light, reason)`` — ``True`` only when every signal
        passes; the reason names the first failing signal otherwise.
    """
    if not changed_paths:
        return (False, "empty diff; nothing to classify")
    if added_paths:
        return (
            False,
            (
                f"diff adds file(s): {', '.join(added_paths)}; the "
                "prior-art gate requires the full wrap-up"
            ),
        )
    if line_count > LIGHT_WRAPUP_LINE_THRESHOLD:
        return (
            False,
            (
                f"diff is {line_count} lines "
                f"(> {LIGHT_WRAPUP_LINE_THRESHOLD}); full wrap-up required"
            ),
        )
    hot = touches_high_blast_radius(changed_paths)
    if hot:
        return (
            False,
            f"diff touches high-blast-radius path(s): {', '.join(hot)}",
        )
    src = touches_source_paths(changed_paths)
    if src:
        return (
            False,
            f"diff touches source package path(s): {', '.join(src)}",
        )
    return (
        True,
        (
            f"diff is {line_count} lines under "
            f"{LIGHT_WRAPUP_LINE_THRESHOLD}, adds no files, touches no "
            "source or high-blast-radius paths"
        ),
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
