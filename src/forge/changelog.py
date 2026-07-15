"""Changelog-parsing primitives shared by forge's release tooling.

Single source of truth for recognizing ``## vX.Y.Z`` release headings in
a ``CHANGELOG.md``. Consumed by ``forge-next-prep --promotion-status``
(missing-entry advisory), ``verify-forge-changelog-history``
(dropped-entry guard), and ``forge-release`` (pre-tag CHANGELOG gate) —
and public API for consumer repos composing their own release flow.
"""

from __future__ import annotations

import re


_HEADING_RE = re.compile(r"^##\s+(v\d+\.\d+\.\d+)\b", re.MULTILINE)


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
