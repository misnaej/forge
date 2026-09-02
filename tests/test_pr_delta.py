"""Unit tests for forge.pr_delta — thresholds and helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from forge.pr_delta import (
    DELTA_LINE_THRESHOLD,
    DOCS_ONLY_GLOBS,
    HIGH_BLAST_RADIUS_PATHS,
    LIGHT_WRAPUP_LINE_THRESHOLD,
    MANAGED_REGEN_PATHS,
    PROVENANCE_GATE_STEPS,
    VERIFIED_AT_RE,
    configured_docs_only_globs,
    delta_decision,
    docs_only_diff,
    extract_verified_shas,
    light_wrapup_decision,
    regen_only_diff,
    touches_high_blast_radius,
    touches_source_paths,
)


if TYPE_CHECKING:
    from pathlib import Path


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


def test_provenance_gate_steps_pins_literal_contents() -> None:
    """The gate-step names are pinned so a silent edit is caught.

    `forge-resync`'s `--only` argv and the `/pr` skill's prose both name
    these three steps directly; a silent reorder or rename here would
    desync them without either failing loudly.
    """
    assert PROVENANCE_GATE_STEPS == (
        "foundation_md_check",
        "cli_reference_check",
        "api_digest_check",
    )


def test_docs_only_all_docs_paths_qualify() -> None:
    """A diff of only changelog/docs/markdown files takes the light path."""
    assert docs_only_diff(["CHANGELOG.md", "docs/audit-pack.md", "README.md"])


def test_docs_only_mixed_code_disqualifies() -> None:
    """Any source/test file in the diff forces the full round."""
    assert not docs_only_diff(["CHANGELOG.md", "src/forge/release.py"])


def test_docs_only_empty_diff_is_not_docs_only() -> None:
    """No changed paths → nothing to classify; full round."""
    assert not docs_only_diff([])


def test_docs_only_high_blast_radius_markdown_disqualifies() -> None:
    """Doc-shaped files under shipped-behavior paths are never docs-only.

    Agent prompts, skills, hooks, and the plugin manifest are executable
    surface — a `*.md` glob match must not exempt them from the
    design/security round.
    """
    assert not docs_only_diff(["agents/pr-manager.md"])
    assert not docs_only_diff(["skills/pr/SKILL.md"])
    assert not docs_only_diff(["CLAUDE.md"])
    assert not docs_only_diff([".claude-plugin/plugin.json", "CHANGELOG.md"])


def test_docs_only_extra_globs_are_additive() -> None:
    """Consumer globs extend the built-ins; built-ins keep applying."""
    files = ["CHANGELOG.md", "notes/design.adoc"]
    assert not docs_only_diff(files)
    assert docs_only_diff(files, extra_globs=("*.adoc",))


def test_docs_only_covers_changelog() -> None:
    """The built-in set covers the single-track every-PR CHANGELOG case."""
    assert docs_only_diff(["CHANGELOG.md"])
    assert all(g.startswith("*.") for g in DOCS_ONLY_GLOBS)


def test_docs_only_nested_non_doc_file_disqualifies() -> None:
    """A non-doc file nested under docs/ must not pass (fnmatch bypass)."""
    assert not docs_only_diff(["docs/evil.py"])
    assert not docs_only_diff(["docs/setup.sh", "CHANGELOG.md"])


def test_docs_only_nested_markdown_still_qualifies() -> None:
    """Extension-anchored globs still cover nested markdown under docs/."""
    assert docs_only_diff(["docs/audit-pack.md", "docs/deep/nested/guide.md"])


def test_docs_only_case_insensitive_blast_radius_collision() -> None:
    """Case-varied blast-radius paths cannot dodge classification.

    On a case-insensitive filesystem (APFS default) `Agents/x.md` lands
    in the same on-disk directory as `agents/` — the classifier folds
    case before comparing so the collision counts as blast-radius.
    """
    assert not docs_only_diff(["Agents/evil.md"])
    assert not docs_only_diff(["Claude-Hooks/x.md"])
    assert touches_high_blast_radius(["Agents/evil.md"]) == ["Agents/evil.md"]


def test_high_blast_radius_covers_workflows_and_claude_dir() -> None:
    """CI workflow definitions and project-local Claude config are hot paths."""
    assert touches_high_blast_radius([".github/workflows/ci.yml"])
    assert touches_high_blast_radius([".claude/hooks/custom.sh"])


def test_configured_docs_only_globs_reads_pyproject(tmp_path: Path) -> None:
    """A configured `docs_only_globs` list is read back as a tuple of strings."""
    (tmp_path / "pyproject.toml").write_text(
        '[tool.forge.pr]\ndocs_only_globs = ["*.adoc", "*.ipynb"]\n'
    )
    assert configured_docs_only_globs(tmp_path) == ("*.adoc", "*.ipynb")


def test_configured_docs_only_globs_empty_when_missing(tmp_path: Path) -> None:
    """No pyproject.toml at all → empty tuple, not raise."""
    assert configured_docs_only_globs(tmp_path) == ()


def test_configured_docs_only_globs_ignores_non_string_items(tmp_path: Path) -> None:
    """Non-string list items are filtered out; a non-list value yields empty."""
    (tmp_path / "pyproject.toml").write_text(
        '[tool.forge.pr]\ndocs_only_globs = ["*.adoc", 7]\n'
    )
    assert configured_docs_only_globs(tmp_path) == ("*.adoc",)

    (tmp_path / "pyproject.toml").write_text('[tool.forge.pr]\ndocs_only_globs = "x"\n')
    assert configured_docs_only_globs(tmp_path) == ()


def test_regen_only_diff_empty_is_false() -> None:
    """No changed paths → nothing to classify; not eligible."""
    assert not regen_only_diff([])


def test_regen_only_diff_all_managed_is_true() -> None:
    """A diff of every managed regen path together qualifies."""
    assert regen_only_diff(list(MANAGED_REGEN_PATHS))
    assert regen_only_diff(["FOUNDATION.md"])


def test_regen_only_diff_mixed_managed_and_unmanaged_is_false() -> None:
    """Any non-managed path in the diff disqualifies the whole diff."""
    assert not regen_only_diff(["FOUNDATION.md", "src/forge/pr_delta.py"])


def test_regen_only_diff_case_varied_path_not_eligible() -> None:
    """Case-varied paths are NOT eligible — casefolding here would be a bypass.

    The provenance gates address files by exact canonical path; a
    case-varied path is a different, unverified file, so widening the
    match with casefold (as the blast-radius check does) would let it
    dodge inspection while the exact-case gate silently self-skips.
    """
    assert not regen_only_diff(["FOUNDATION.MD"])
    assert not regen_only_diff(["Docs/Cli-Reference.md"])


def test_touches_source_paths_matches_src_prefix() -> None:
    """A path under `src/` is flagged as source."""
    hits = touches_source_paths(["src/forge/pr_delta.py", "tests/test_pr_delta.py"])
    assert hits == ["src/forge/pr_delta.py"]


def test_touches_source_paths_empty_when_clean() -> None:
    """Empty list when no path is under a source prefix."""
    assert touches_source_paths(["tests/test_pr_delta.py", "docs/guide.md"]) == []


def test_touches_source_paths_case_insensitive_collision() -> None:
    """A case-varied `Src/` path cannot dodge the source net (APFS collision)."""
    assert touches_source_paths(["Src/forge/pr_delta.py"]) == ["Src/forge/pr_delta.py"]


def test_light_wrapup_decision_empty_diff_refuses() -> None:
    """An empty diff refuses the light wrap-up — nothing to classify."""
    use_light, reason = light_wrapup_decision(
        line_count=0, changed_paths=[], added_paths=[]
    )
    assert use_light is False
    assert "empty diff" in reason


def test_light_wrapup_decision_added_file_refuses_naming_file() -> None:
    """An added file refuses and names it — the prior-art gate stays independent."""
    use_light, reason = light_wrapup_decision(
        line_count=5, changed_paths=["src/foo.py"], added_paths=["src/foo.py"]
    )
    assert use_light is False
    assert "src/foo.py" in reason
    assert "prior-art" in reason


def test_light_wrapup_decision_over_threshold_refuses() -> None:
    """A diff one line over the threshold refuses."""
    use_light, reason = light_wrapup_decision(
        line_count=LIGHT_WRAPUP_LINE_THRESHOLD + 1,
        changed_paths=["tests/foo.py"],
        added_paths=[],
    )
    assert use_light is False
    assert "51" in reason


def test_light_wrapup_decision_at_threshold_boundary_succeeds() -> None:
    """A diff exactly at the threshold succeeds — the code checks `>`, not `>=`."""
    use_light, _reason = light_wrapup_decision(
        line_count=LIGHT_WRAPUP_LINE_THRESHOLD,
        changed_paths=["tests/foo.py"],
        added_paths=[],
    )
    assert use_light is True


def test_light_wrapup_decision_high_blast_radius_refuses() -> None:
    """A high-blast-radius path refuses even under the line threshold."""
    use_light, reason = light_wrapup_decision(
        line_count=5, changed_paths=["agents/foo.md"], added_paths=[]
    )
    assert use_light is False
    assert "high-blast-radius" in reason


def test_light_wrapup_decision_source_path_refuses() -> None:
    """A `src/` path refuses even under the line threshold."""
    use_light, reason = light_wrapup_decision(
        line_count=5, changed_paths=["src/forge/pr_delta.py"], added_paths=[]
    )
    assert use_light is False
    assert "source package path" in reason


def test_light_wrapup_decision_success_reason_names_signals() -> None:
    """A qualifying diff's reason cites the line count and the passed signals."""
    use_light, reason = light_wrapup_decision(
        line_count=10, changed_paths=["tests/foo.py"], added_paths=[]
    )
    assert use_light is True
    assert "10 lines" in reason
    assert "adds no files" in reason


def test_light_wrapup_decision_added_file_precedence_over_threshold() -> None:
    """An added-file refusal is reported even when the line count also disqualifies.

    Pins the check order in `light_wrapup_decision`: the added-files
    refusal fires before the line-threshold check, so with BOTH signals
    failing the reason names the added file, not the line count.
    """
    use_light, reason = light_wrapup_decision(
        line_count=LIGHT_WRAPUP_LINE_THRESHOLD + 1,
        changed_paths=["src/foo.py"],
        added_paths=["src/foo.py"],
    )
    assert use_light is False
    assert "src/foo.py" in reason
    assert "prior-art" in reason
