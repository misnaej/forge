"""Tests for the ``forge.changelog`` module.

The single source of truth for recognizing ``## vX.Y.Z`` release headings,
shared by ``verify-forge-changelog-history``, ``forge-next-prep``, and
``forge-release``.
"""

from __future__ import annotations

import pytest

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


# ---------------------------------------------------------------------------
# changelog_version_findings
# ---------------------------------------------------------------------------


def test_version_findings_clean_changelog_passes() -> None:
    """Dated headings, strictly decreasing, top == latest tag → no findings."""
    text = "# Changelog\n\n## v1.1.0 — 2026-07-01\n\n- a\n\n## v1.0.0\n\n- b\n"
    assert changelog.changelog_version_findings(text, "v1.1.0") == []


def test_version_findings_flags_unreleased_heading() -> None:
    """`## Unreleased` is not a recognized version — flagged, not ignored."""
    text = "## Unreleased\n\n## v1.0.0\n"
    findings = changelog.changelog_version_findings(text, "v1.0.0")
    assert any("## Unreleased" in f for f in findings)


def test_version_findings_flags_vless_and_truncated_headings() -> None:
    """v-less `## 1.0.0` and truncated `## v1.2` fail the shared recognizer."""
    text = "## 1.1.0\n\n## v1.2\n"
    findings = changelog.changelog_version_findings(text, None)
    assert len(findings) == 2
    assert all("not a valid vX.Y.Z" in f for f in findings)


def test_version_findings_invalid_only_omits_no_heading_message() -> None:
    """A flagged invalid heading suppresses the separate 'no heading' error."""
    findings = changelog.changelog_version_findings("## v1.2\n", None)
    assert len(findings) == 1
    assert "no `## vX.Y.Z` heading" not in findings[0]


def test_version_findings_empty_text_reports_no_heading() -> None:
    """No headings at all → the single 'no heading' finding."""
    findings = changelog.changelog_version_findings("# Changelog\n", None)
    assert findings == ["CHANGELOG.md has no `## vX.Y.Z` heading."]


def test_version_findings_duplicate_heading_breaks_monotonicity() -> None:
    """A duplicated version fails the strictly-decreasing rule."""
    text = "## v1.0.0\n\n## v1.0.0\n"
    findings = changelog.changelog_version_findings(text, None)
    assert any("not strictly decreasing" in f for f in findings)


def test_version_findings_out_of_order_headings_flagged() -> None:
    """An older version above a newer one fails monotonicity."""
    text = "## v1.0.0\n\n## v1.1.0\n"
    findings = changelog.changelog_version_findings(text, None)
    assert any("not strictly decreasing" in f for f in findings)


def test_version_findings_latest_tag_missing_entry() -> None:
    """The latest tag must have a matching heading."""
    text = "## v1.0.0\n"
    findings = changelog.changelog_version_findings(text, "v1.1.0")
    assert any("has no `## v1.1.0` heading" in f for f in findings)


def test_version_findings_top_behind_latest_tag() -> None:
    """Top heading older than the latest tag → 'behind' finding."""
    text = "## v1.0.0\n\n## v0.9.0\n"
    findings = changelog.changelog_version_findings(text, "v1.1.0")
    assert any("behind the latest tag v1.1.0" in f for f in findings)


def test_version_findings_no_tags_skips_tag_checks() -> None:
    """With no tags yet, only heading validity + ordering are checked."""
    text = "## v0.1.0\n"
    assert changelog.changelog_version_findings(text, None) == []


# ---------------------------------------------------------------------------
# stranded_added_versions
# ---------------------------------------------------------------------------


_STRAND_TEXT = (
    "# Changelog\n\n## v0.2.0 — 2026-07-01\n\n- new bullet\n\n## v0.1.0\n\n- old\n"
)

_STRAND_DIFF = (
    "--- a/CHANGELOG.md\n"
    "+++ b/CHANGELOG.md\n"
    "@@ -1,7 +1,9 @@\n"
    " # Changelog\n"
    " \n"
    " ## v0.2.0 — 2026-07-01\n"
    " \n"
    "+- new bullet\n"
    "+\n"
    " ## v0.1.0\n"
    " \n"
    " - old\n"
)


def test_stranded_detects_entry_under_released_heading() -> None:
    """Added bullet under a heading equal to the latest tag → stranded."""
    result = changelog.stranded_added_versions(_STRAND_TEXT, _STRAND_DIFF, "v0.2.0")
    assert result == ["v0.2.0"]


def test_stranded_silent_when_heading_leads_tag() -> None:
    """Same diff but the receiving heading is ahead of the latest tag → clean."""
    result = changelog.stranded_added_versions(_STRAND_TEXT, _STRAND_DIFF, "v0.1.9")
    assert result == []


def test_stranded_ignores_added_heading_lines_and_blanks() -> None:
    """A diff that only opens a new heading (plus blanks) strands nothing."""
    text = "## v0.3.0\n\n## v0.2.0\n\n- old\n"
    diff = (
        "--- a/CHANGELOG.md\n"
        "+++ b/CHANGELOG.md\n"
        "@@ -1,3 +1,5 @@\n"
        "+## v0.3.0\n"
        "+\n"
        " ## v0.2.0\n"
        " \n"
        " - old\n"
    )
    assert changelog.stranded_added_versions(text, diff, "v0.2.0") == []


def test_stranded_no_tags_returns_empty() -> None:
    """Without any release tag nothing can be stranded."""
    assert changelog.stranded_added_versions(_STRAND_TEXT, _STRAND_DIFF, None) == []


# ---------------------------------------------------------------------------
# top_release_heading
# ---------------------------------------------------------------------------


def test_top_release_heading_returns_first_version() -> None:
    """The topmost recognized heading wins, dated form included."""
    text = "# Changelog\n\n## v1.1.0 — 2026-07-24\n\n## v1.0.0\n"
    assert changelog.top_release_heading(text) == "v1.1.0"


def test_top_release_heading_skips_non_version_headings() -> None:
    """A stray non-version heading above the top version is skipped."""
    text = "## Unreleased\n\n## v1.0.0\n"
    assert changelog.top_release_heading(text) == "v1.0.0"


def test_top_release_heading_none_without_versions() -> None:
    """No recognized release heading → None."""
    assert changelog.top_release_heading("# Changelog\n") is None


# ---------------------------------------------------------------------------
# action_items
# ---------------------------------------------------------------------------


def test_action_items_extracts_marker_under_heading() -> None:
    """A single ``**Action:**`` line under a heading is attributed to it."""
    text = "## v1.2.0\n\n**Action:** do the thing.\n"
    assert changelog.action_items(text) == [("v1.2.0", "do the thing.")]


def test_action_items_attributes_to_nearest_governing_heading() -> None:
    """Two headings each with their own marker, independently in file order."""
    text = (
        "## v2.0.0\n"
        "\n"
        "**Action:** upgrade config.\n"
        "\n"
        "## v1.0.0\n"
        "\n"
        "**Action:** migrate data.\n"
    )
    assert changelog.action_items(text) == [
        ("v2.0.0", "upgrade config."),
        ("v1.0.0", "migrate data."),
    ]


def test_action_items_ignores_markers_above_first_heading() -> None:
    """A marker line in prose before any release heading is excluded."""
    text = "**Action:** should not count.\n\n## v1.0.0\n\n- normal bullet\n"
    assert changelog.action_items(text) == []


@pytest.mark.parametrize("bullet", ["- ", "* ", ""])
def test_action_items_accepts_optional_list_bullet(bullet: str) -> None:
    """The marker matches with a ``-`` bullet, a ``*`` bullet, or bare.

    Args:
        bullet: The list-bullet prefix (or empty string) placed before
            the ``**Action:**`` marker.
    """
    text = f"## v1.0.0\n\n{bullet}**Action:** do it.\n"
    assert changelog.action_items(text) == [("v1.0.0", "do it.")]


def test_action_items_returns_empty_when_no_markers() -> None:
    """Headings with no ``**Action:**`` lines yield an empty list."""
    text = "## v1.1.0\n\n- a bullet\n\n## v1.0.0\n\n- another bullet\n"
    assert changelog.action_items(text) == []


def test_action_items_ignores_near_miss_marker_text() -> None:
    """Text resembling the marker but missing the exact bold-colon shape is excluded."""
    text = (
        "## v1.0.0\n"
        "\n"
        "Action: not bolded, should not match.\n"
        "**Action**: colon outside bold, should not match.\n"
    )
    assert changelog.action_items(text) == []


def test_action_items_skips_action_under_unrecognized_heading() -> None:
    """Markers under unrecognized headings (e.g. `## Unreleased`) are excluded."""
    text = "## Unreleased\n\n**Action:** should be excluded.\n"
    assert changelog.action_items(text) == []
