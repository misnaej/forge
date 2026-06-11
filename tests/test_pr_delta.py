"""Unit tests for forge.pr_delta — thresholds and helpers."""

from __future__ import annotations

from forge.pr_delta import (
    DELTA_LINE_THRESHOLD,
    HIGH_BLAST_RADIUS_PATHS,
    VERIFIED_AT_RE,
    delta_decision,
    extract_verified_shas,
    touches_high_blast_radius,
)


def test_verified_at_re_captures_canonical_form() -> None:
    """The regex pulls a 7-40 hex SHA out of a canonical header line."""
    m = VERIFIED_AT_RE.search("verified-at: 7ab3e4e   (PR #56, branch fix/foo)")
    assert m is not None
    assert m.group("sha") == "7ab3e4e"


def test_verified_at_re_rejects_non_hex() -> None:
    """A header with shell-injection-shaped payload extracts no SHA."""
    m = VERIFIED_AT_RE.search("verified-at: $(evil_command)   (PR #1, branch x)")
    assert m is None


def test_verified_at_re_rejects_short_sha() -> None:
    """SHAs under 7 chars are not matched (avoids false positives on numbers)."""
    assert VERIFIED_AT_RE.search("verified-at: abc123  (PR #1)") is None


def test_extract_verified_shas_returns_all_in_order() -> None:
    """Multiple verified-at lines yield all SHAs in input order."""
    text = (
        "verified-at: 7ab3e4e   (PR #56, branch x)\n\n"
        "some prose\n\n"
        "verified-at: ae79c0b   (PR #56, branch x)\n"
    )
    assert extract_verified_shas(text) == ["7ab3e4e", "ae79c0b"]


def test_extract_verified_shas_skips_injection_payload() -> None:
    """Lines with non-hex SHAs are silently ignored."""
    text = "verified-at: $(evil)   (PR #1)\nverified-at: 7ab3e4e   (PR #1)\n"
    assert extract_verified_shas(text) == ["7ab3e4e"]


def test_extract_verified_shas_empty_when_no_header() -> None:
    """Empty list when the text has no verified-at lines."""
    assert extract_verified_shas("just some prose with no header\n") == []


def test_touches_high_blast_radius_matches_directory_prefix() -> None:
    """Paths under a `dir/` glob are flagged."""
    hits = touches_high_blast_radius(["agents/foo.md", "src/forge/x.py"])
    assert hits == ["agents/foo.md"]


def test_touches_high_blast_radius_matches_exact_file() -> None:
    """Exact-match file globs (no trailing slash) are flagged."""
    hits = touches_high_blast_radius(["pyproject.toml", "src/forge/x.py"])
    assert hits == ["pyproject.toml"]


def test_touches_high_blast_radius_empty_when_clean() -> None:
    """Empty list when no path matches any glob."""
    assert touches_high_blast_radius(["src/forge/x.py", "tests/test_x.py"]) == []


def test_delta_decision_under_threshold_no_hot_paths_uses_delta() -> None:
    """Diff under threshold + no hot paths → use_delta True."""
    use_delta, reason = delta_decision(
        line_count=DELTA_LINE_THRESHOLD - 1, changed_paths=["src/foo.py"]
    )
    assert use_delta is True
    assert "under" in reason


def test_delta_decision_at_threshold_uses_delta() -> None:
    """Diff exactly at threshold is still eligible (boundary inclusive)."""
    use_delta, _ = delta_decision(
        line_count=DELTA_LINE_THRESHOLD, changed_paths=["src/foo.py"]
    )
    assert use_delta is True


def test_delta_decision_above_threshold_forces_full() -> None:
    """Diff above threshold → use_delta False, reason cites line count."""
    use_delta, reason = delta_decision(
        line_count=DELTA_LINE_THRESHOLD + 1, changed_paths=["src/foo.py"]
    )
    assert use_delta is False
    assert "full re-check required" in reason


def test_delta_decision_high_blast_radius_path_forces_full() -> None:
    """Hot path under threshold → still forces full re-check."""
    use_delta, reason = delta_decision(line_count=10, changed_paths=["agents/foo.md"])
    assert use_delta is False
    assert "high-blast-radius" in reason


def test_high_blast_radius_paths_is_non_empty() -> None:
    """Guard against accidental empty constant (would disable the gate)."""
    assert len(HIGH_BLAST_RADIUS_PATHS) > 0
