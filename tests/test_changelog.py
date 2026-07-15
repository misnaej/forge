"""Tests for the ``forge.changelog`` module.

The single source of truth for recognizing ``## vX.Y.Z`` release headings,
shared by ``verify-forge-changelog-history``, ``forge-next-prep``, and
``forge-release``.
"""

from __future__ import annotations

from forge import changelog


# ---------------------------------------------------------------------------
# release_headings
# ---------------------------------------------------------------------------


def test_release_headings_parses_semver_level2_only() -> None:
    """``release_headings`` picks up ``## vX.Y.Z`` lines and ignores other markup."""
    text = (
        "# Changelog\n"
        "\n"
        "## v1.1.0\n"
        "\n"
        "Some prose about v1.1.0.\n"
        "\n"
        "### Sub-heading ignored\n"
        "\n"
        "## v1.0.0\n"
        "\n"
        "Not a heading: ## v0.9.0 inline\n"
    )
    assert changelog.release_headings(text) == {"v1.0.0", "v1.1.0"}


def test_release_headings_returns_empty_when_no_semver_headings() -> None:
    """No ``## vX.Y.Z`` lines in *text* returns an empty set."""
    text = "# Changelog\n\nSome unreleased notes.\n\n### Details\n"
    assert changelog.release_headings(text) == set()


def test_release_headings_matches_heading_with_trailing_suffix() -> None:
    """A heading with a trailing date suffix still matches on the tag alone."""
    text = "## v1.6.0 — 2026-07-01\n"
    assert changelog.release_headings(text) == {"v1.6.0"}


# ---------------------------------------------------------------------------
# changelog_lacks_entry
# ---------------------------------------------------------------------------


def test_changelog_lacks_entry_true_when_tag_absent() -> None:
    """A tag with no matching heading is reported as missing."""
    text = "## v1.0.0\n"
    assert changelog.changelog_lacks_entry(text, "v1.1.0") is True


def test_changelog_lacks_entry_false_when_tag_present() -> None:
    """A tag with a matching heading is reported as present."""
    text = "## v1.1.0\n\n## v1.0.0\n"
    assert changelog.changelog_lacks_entry(text, "v1.1.0") is False


def test_changelog_lacks_entry_true_on_empty_text() -> None:
    """Empty changelog text has no entries, so any tag is missing."""
    assert changelog.changelog_lacks_entry("", "v1.0.0") is True
