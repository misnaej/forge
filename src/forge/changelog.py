"""Changelog-parsing primitives shared by forge's release tooling.

Single source of truth for recognizing ``## vX.Y.Z`` release headings in
a ``CHANGELOG.md``. Consumed by ``forge-next-prep --promotion-status``
(missing-entry advisory), ``verify-forge-changelog-history``
(dropped-entry guard), and ``forge-release`` (pre-tag CHANGELOG gate) —
and public API for consumer repos composing their own release flow.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from forge.git_utils import parse_semver


if TYPE_CHECKING:
    from collections.abc import Iterator


_HEADING_RE = re.compile(r"^##\s+(v\d+\.\d+\.\d+)\b", re.MULTILINE)

# Any level-2 heading, version-shaped or not — the changelog_version
# pre-commit step validates every ``## `` heading against the release
# recognizer, so a stray ``## Unreleased`` or ``## v1.2`` is flagged
# rather than silently ignored.
_ANY_HEADING_RE = re.compile(r"^##\s+(\S.*?)\s*$", re.MULTILINE)

# New-file hunk header of a unified diff: ``@@ -a,b +c,d @@`` → ``c``.
_HUNK_NEW_START_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def release_headings(text: str) -> set[str]:
    """Return the set of ``## v<semver>`` release headings in *text*.

    The one heading recognizer for the whole package: a level-2 heading
    whose first token is a ``v``-prefixed semver triple, with or without
    a trailing suffix (``## v1.6.0 — 2026-07-01`` and bare ``## v1.6.0``
    both count).

    Args:
        text: CHANGELOG markdown body.

    Returns:
        Each ``vX.Y.Z`` named in a level-2 release heading; empty when none.
    """
    return set(_HEADING_RE.findall(text))


def changelog_lacks_entry(changelog_text: str, tag: str) -> bool:
    """Return ``True`` when *changelog_text* has no ``## <tag>`` heading.

    Drives the promotion advisory in ``forge-next-prep`` and the hard
    pre-tag gate in ``forge-release``; see ``docs/release-process.md`` §5.

    Args:
        changelog_text: Full ``CHANGELOG.md`` contents.
        tag: Release tag to look for, e.g. ``"v1.6.0"``.

    Returns:
        ``True`` when no heading for *tag* is present.
    """
    return tag not in release_headings(changelog_text)


def _recognized_version(token: str) -> str | None:
    """Return the ``vX.Y.Z`` a heading *token* names, or ``None``.

    Routes recognition through :func:`release_headings` so validity here
    can never drift from the tag gate in ``forge-release`` — one
    recognizer for the whole package (a v-less ``0.2.0`` or truncated
    ``v1.2`` is invalid in both places for the same reason).

    Args:
        token: The text after ``## `` on a heading line.

    Returns:
        The recognized ``vX.Y.Z`` triple, or ``None`` when *token* is not
        a release heading.
    """
    found = release_headings(f"## {token}")
    return next(iter(found)) if found else None


def changelog_version_findings(text: str, latest_tag: str | None) -> list[str]:
    """Validate *text*'s release headings against each other and *latest_tag*.

    The single-track declared-version invariant (the CHANGELOG top
    heading names the release being prepared — ``docs/consumer-release.md``
    "Changelog convention"): every ``## `` heading is a recognized
    version, headings strictly decrease, the latest tag has an entry, and
    the top heading never falls behind the latest tag. Equality of top
    heading and latest tag is valid — that is the normal state right
    after a release is cut, until the next PR opens the next heading.

    Args:
        text: Full ``CHANGELOG.md`` contents.
        latest_tag: Latest ``v*`` tag (e.g. ``"v1.2.3"``), or ``None``
            when the repo has no release tags yet (tag-dependent checks
            are skipped).

    Returns:
        Human-readable findings; empty when the changelog is consistent.
    """
    findings: list[str] = []
    versions: list[str] = []
    for token in _ANY_HEADING_RE.findall(text):
        version = _recognized_version(token)
        if version is None:
            findings.append(
                f"CHANGELOG heading `## {token}` is not a valid vX.Y.Z version."
            )
        else:
            versions.append(version)
    if not versions:
        if not findings:
            findings.append("CHANGELOG.md has no `## vX.Y.Z` heading.")
        return findings
    parsed = [parse_semver(v) for v in versions]
    for i in range(1, len(versions)):
        newer, older = parsed[i - 1], parsed[i]
        if newer is not None and older is not None and newer <= older:
            findings.append(
                f"CHANGELOG headings are not strictly decreasing: "
                f"{versions[i - 1]} above {versions[i]}."
            )
    if latest_tag is not None:
        if changelog_lacks_entry(text, latest_tag):
            findings.append(
                f"Latest git tag {latest_tag} has no `## {latest_tag}` "
                "heading in CHANGELOG.md."
            )
        top = parse_semver(versions[0])
        tag = parse_semver(latest_tag)
        if top is not None and tag is not None and top < tag:
            findings.append(
                f"CHANGELOG top version {versions[0]} is behind the "
                f"latest tag {latest_tag}."
            )
    return findings


def _governing_versions(text: str) -> list[str | None]:
    """Map each line in *text* to its governing release version heading.

    Scans the text sequentially, updating the "current" heading as new
    ``## `` lines are encountered. Returns a parallel list mapping each line
    number to the version heading that governs it (the most-recent heading
    above that line, or ``None`` if no heading precedes).

    Args:
        text: Full CHANGELOG.md contents.

    Returns:
        A list where index *i* holds the governing version for line *i*.
    """
    governing: list[str | None] = []
    current: str | None = None
    for line in text.splitlines():
        match = _ANY_HEADING_RE.match(line)
        if match:
            current = _recognized_version(match.group(1))
        governing.append(current)
    return governing


def _iter_added_lines(diff_text: str) -> Iterator[tuple[int, str]]:
    """Yield (line_number, content) pairs for each addition in a unified diff.

    Parses unified-diff format, tracking hunk headers to maintain accurate
    line numbers in the new file, and yields only the content of lines
    starting with ``+`` (excluding ``+++`` file headers and ``-`` deletions).

    Args:
        diff_text: A unified diff in the standard format (output of
            ``git diff``, etc.).

    Yields:
        Tuples of (1-indexed line number in the new file, the added content
        after the ``+`` prefix).
    """
    new_lineno = 0
    for line in diff_text.splitlines():
        hunk = _HUNK_NEW_START_RE.match(line)
        if hunk:
            new_lineno = int(hunk.group(1)) - 1
            continue
        if line.startswith(("+++", "---")):
            continue
        if line.startswith("-"):
            continue
        new_lineno += 1
        if line.startswith("+"):
            yield (new_lineno, line[1:])


def _stranded_from_added(
    governing: list[str | None],
    added_lines: Iterator[tuple[int, str]],
    tag: tuple[int, int, int] | None,
) -> list[str]:
    """Detect released headings that received new content in a diff.

    Iterates over added lines, looks up each line's governing version from
    the governing list, and accumulates distinct versions that are at or
    below *tag* (released before or at the tagging point). Skips blank
    lines and lines that are themselves headings (only non-heading content
    counts as "stranded" — a new heading under a released heading is normal).

    Args:
        governing: Output of :func:`_governing_versions` — a parallel list
            mapping line numbers to governing versions.
        added_lines: Iterator of (line_no, content) from
            :func:`_iter_added_lines`.
        tag: The parsed semver of the latest release tag, or ``None``.

    Returns:
        Distinct stranded versions in file order; empty when none.
    """
    if tag is None:
        return []
    stranded: list[str] = []
    for lineno, added in added_lines:
        if not added.strip() or _ANY_HEADING_RE.match(added):
            continue
        index = lineno - 1
        version = governing[index] if 0 <= index < len(governing) else None
        if version is None:
            continue
        parsed = parse_semver(version)
        if parsed is not None and parsed <= tag and version not in stranded:
            stranded.append(version)
    return stranded


def stranded_added_versions(
    text: str, diff_text: str, latest_tag: str | None
) -> list[str]:
    """Return released heading versions that *diff_text* adds entries under.

    The stranded-entries race: a release tag is cut while a PR is open,
    so the PR's bullets sit under a now-released ``## vX.Y.Z`` heading —
    no merge conflict signals it, and the global top-vs-tag check passes
    on equality. This maps each ``+`` line of a unified diff of
    ``CHANGELOG.md`` to its governing heading in *text* and reports the
    headings at or below *latest_tag* that received non-heading content.

    Args:
        text: Full current ``CHANGELOG.md`` contents (the diff's new side).
        diff_text: Unified diff of ``CHANGELOG.md`` against the base
            branch (new side must match *text*).
        latest_tag: Latest ``v*`` tag, or ``None`` (no tags → nothing can
            be stranded; returns empty).

    Returns:
        Distinct stranded versions in file order; empty when none.
    """
    tag = parse_semver(latest_tag) if latest_tag is not None else None
    if tag is None:
        return []
    governing = _governing_versions(text)
    return _stranded_from_added(governing, _iter_added_lines(diff_text), tag)
