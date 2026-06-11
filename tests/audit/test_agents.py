"""Tests for ``forge.audit.agents`` — agent-template audit."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

from forge.audit import agents as audit_agents
from forge.audit.common import Severity


if TYPE_CHECKING:
    from pathlib import Path

    import pytest


# ---------------------------------------------------------------------------
# Frontmatter parsing
# ---------------------------------------------------------------------------


def test_split_frontmatter_extracts_simple_key_value() -> None:
    """Simple `key: value` lines land in the dict as strings."""
    text = "---\nname: foo\nmodel: sonnet\n---\nbody\n"
    fm, body = audit_agents._split_frontmatter(text)
    assert fm == {"name": "foo", "model": "sonnet"}
    assert body == "body\n"


def test_split_frontmatter_extracts_list_blocks() -> None:
    """`key:` followed by `- item` lines becomes a tuple."""
    text = "---\nname: foo\ntools:\n  - Bash\n  - Read\n---\nbody\n"
    fm, body = audit_agents._split_frontmatter(text)
    assert fm["tools"] == ("Bash", "Read")
    assert body == "body\n"


def test_split_frontmatter_no_block_returns_empty_dict() -> None:
    """A file without frontmatter returns empty dict + full text."""
    text = "# Header\nbody only\n"
    fm, body = audit_agents._split_frontmatter(text)
    assert fm == {}
    assert body == text


# ---------------------------------------------------------------------------
# Code-block stripping + word count
# ---------------------------------------------------------------------------


def test_strip_code_blocks_removes_fenced_content() -> None:
    """Triple-backtick blocks are removed from the body."""
    body = "before\n```python\nfoo = 1\nbar = 2\n```\nafter\n"
    stripped = audit_agents._strip_code_blocks(body)
    assert "foo = 1" not in stripped
    assert "before" in stripped
    assert "after" in stripped


def test_word_count_excludes_code() -> None:
    """Embedded code does not inflate the word count."""
    body_no_code = "one two three four five"
    assert audit_agents._word_count(body_no_code) == 5


# ---------------------------------------------------------------------------
# Per-agent check functions
# ---------------------------------------------------------------------------


def _agent_doc(
    *,
    body: str = "",
    frontmatter: dict[str, object] | None = None,
    path: str = "agents/test.md",
) -> audit_agents.AgentDoc:
    """Build an ``AgentDoc`` directly for unit tests.

    Args:
        body: Body text (also used for ``body_no_code`` — no code stripping).
        frontmatter: Optional frontmatter dict; defaults to empty.
        path: Repo-relative path to record.

    Returns:
        Synthetic ``AgentDoc`` suitable for the check functions.
    """
    return audit_agents.AgentDoc(
        path=path,
        frontmatter=frontmatter or {},
        body=body,
        body_no_code=body,
    )


def test_check_word_count_high_above_hard_cap() -> None:
    """Bodies over WORD_CAP_HIGH yield a HIGH finding."""
    long_body = " ".join(["word"] * (audit_agents.WORD_CAP_HIGH + 10))
    findings = audit_agents._check_word_count(_agent_doc(body=long_body))
    assert len(findings) == 1
    assert findings[0].severity == Severity.HIGH


def test_check_word_count_medium_above_target() -> None:
    """Bodies in 800..1500 yield a MEDIUM finding."""
    body = " ".join(["word"] * (audit_agents.WORD_CAP_MEDIUM + 10))
    findings = audit_agents._check_word_count(_agent_doc(body=body))
    assert len(findings) == 1
    assert findings[0].severity == Severity.MEDIUM


def test_check_word_count_clean_below_target() -> None:
    """Bodies under 800 yield zero findings."""
    body = " ".join(["word"] * 100)
    assert audit_agents._check_word_count(_agent_doc(body=body)) == []


def test_check_frontmatter_flags_missing_keys() -> None:
    """One HIGH per missing required key."""
    findings = audit_agents._check_frontmatter(_agent_doc(frontmatter={"name": "x"}))
    missing_keys = {f.message for f in findings}
    assert any("description" in m for m in missing_keys)
    assert any("tools" in m for m in missing_keys)
    assert any("model" in m for m in missing_keys)


def test_check_description_shape_flags_role_label() -> None:
    """Role-shaped descriptions yield a MEDIUM finding."""
    fm = {"description": "Agent for performing checks."}
    findings = audit_agents._check_description_shape(_agent_doc(frontmatter=fm))
    assert len(findings) == 1
    assert findings[0].severity == Severity.MEDIUM


def test_check_description_shape_accepts_routing_trigger() -> None:
    """'Use proactively when …' shape yields zero findings."""
    fm = {"description": "Use proactively when CI fails on docstring drift."}
    assert audit_agents._check_description_shape(_agent_doc(frontmatter=fm)) == []


def test_check_reporter_tools_flags_write_edit() -> None:
    """Reporter agents must not carry Write/Edit tools."""
    fm = {
        "description": "Reviews PR diffs for security issues.",
        "tools": ("Read", "Edit", "Write"),
    }
    doc = _agent_doc(frontmatter=fm, path="agents/security-checker.md")
    findings = audit_agents._check_reporter_tools(doc)
    assert {f.severity for f in findings} == {Severity.MEDIUM}
    assert any("'Edit'" in f.message for f in findings)
    assert any("'Write'" in f.message for f in findings)


def test_check_reporter_tools_skips_actor() -> None:
    """Actors (name not in REPORTER_AGENT_NAMES) are exempt."""
    fm = {
        "description": "Applies ruff fixes and commits the result.",
        "tools": ("Read", "Edit", "Write"),
    }
    doc = _agent_doc(frontmatter=fm, path="agents/git-commit-push.md")
    assert audit_agents._check_reporter_tools(doc) == []


def test_check_reporter_tools_skips_reporter_with_artifact() -> None:
    """Reporter-with-artifact agents are exempt from the Write/Edit ban."""
    for name in audit_agents._REPORTER_WITH_ARTIFACT_NAMES:
        fm = {
            "description": "Reviews and produces an artifact file.",
            "tools": ("Read", "Edit", "Write"),
        }
        doc = _agent_doc(frontmatter=fm, path=f"agents/{name}.md")
        assert audit_agents._check_reporter_tools(doc) == []


def test_is_reporter_agent_matches_allowlist() -> None:
    """Filename stem in REPORTER_AGENT_NAMES → True."""
    for name in audit_agents.REPORTER_AGENT_NAMES:
        assert audit_agents._is_reporter_agent(_agent_doc(path=f"agents/{name}.md"))


def test_is_reporter_agent_rejects_other_names() -> None:
    """Names outside the allowlist → False, even with reporter-shaped desc."""
    fm = {"description": "Reviews and reports on the diff."}
    doc = _agent_doc(frontmatter=fm, path="agents/pr-manager.md")
    assert not audit_agents._is_reporter_agent(doc)


def test_check_reporter_verified_at_flags_missing_header() -> None:
    """Reporter agent body without `verified-at:` yields one MEDIUM finding."""
    doc = _agent_doc(
        body="## Output\nplain report with no contract\n",
        path="agents/design-checker.md",
    )
    findings = audit_agents._check_reporter_verified_at(doc)
    assert len(findings) == 1
    assert findings[0].severity == Severity.MEDIUM
    assert "verified-at" in findings[0].message


def test_check_reporter_verified_at_passes_when_header_present() -> None:
    """Body containing `verified-at:` yields zero findings."""
    body = "## Output\nFirst line: verified-at: <sha> (PR #N, branch x)\n"
    doc = _agent_doc(body=body, path="agents/security-checker.md")
    assert audit_agents._check_reporter_verified_at(doc) == []


def test_check_reporter_verified_at_skips_non_reporter() -> None:
    """Agents not in REPORTER_AGENT_NAMES are not subject to the contract."""
    doc = _agent_doc(body="no verified-at here\n", path="agents/pr-manager.md")
    assert audit_agents._check_reporter_verified_at(doc) == []


def test_check_required_sections_flags_missing() -> None:
    """Body missing required H2s yields one LOW per missing section."""
    body = "# Agent\n\nparagraph\n\n## Workflow\nstep\n"
    findings = audit_agents._check_required_sections(_agent_doc(body=body))
    messages = {f.message for f in findings}
    assert any("Scope Boundaries" in m for m in messages)
    assert any("Output" in m for m in messages)
    assert any("Success Criteria" in m for m in messages)
    assert not any("Workflow" in m for m in messages)


# ---------------------------------------------------------------------------
# Shared-substring detection
# ---------------------------------------------------------------------------


def test_ngrams_returns_token_windows() -> None:
    """Sliding windows of size n cover every position."""
    out = audit_agents._ngrams(["a", "b", "c", "d"], 2)
    assert out == {"a b", "b c", "c d"}


def test_check_foundation_restatements_flags_shared_8gram() -> None:
    """Body containing an 8-token foundation substring is flagged HIGH."""
    foundation_text = "The C901 limit applies to McCabe complexity in functions today."
    ngrams = audit_agents._ngrams(
        audit_agents._tokens(foundation_text), audit_agents.SHARED_TOKEN_MIN
    )
    body = (
        "Note: the c901 limit applies to mccabe complexity "
        "in functions today as policy."
    )
    findings = audit_agents._check_foundation_restatements(
        _agent_doc(body=body), ngrams
    )
    assert len(findings) == 1
    assert findings[0].severity == Severity.HIGH


def test_check_foundation_restatements_clean_when_no_overlap() -> None:
    """Disjoint content yields zero findings."""
    ngrams = audit_agents._ngrams(
        audit_agents._tokens("Some unrelated FOUNDATION sentences about CI."),
        audit_agents.SHARED_TOKEN_MIN,
    )
    body = " ".join(["agent"] * 30)
    assert (
        audit_agents._check_foundation_restatements(_agent_doc(body=body), ngrams) == []
    )


def test_cross_agent_duplicates_flags_shared_ngrams_across_pair() -> None:
    """An 8-token substring shared by two agents flags both."""
    shared = "this exact phrase repeats across two distinct agent files"
    agents = [
        _agent_doc(body=f"x x x {shared} y y y", path="agents/a.md"),
        _agent_doc(body=f"z z z {shared} w w w", path="agents/b.md"),
    ]
    findings = audit_agents._cross_agent_duplicate_findings(agents)
    paths = {f.path for f in findings}
    assert paths == {"agents/a.md", "agents/b.md"}


def test_cross_agent_duplicates_clean_when_no_overlap() -> None:
    """Disjoint agents yield zero cross-duplicate findings."""
    agents = [
        _agent_doc(
            body="alpha beta gamma delta epsilon zeta eta theta", path="agents/a.md"
        ),
        _agent_doc(body="iota kappa lambda mu nu xi omicron pi", path="agents/b.md"),
    ]
    assert audit_agents._cross_agent_duplicate_findings(agents) == []


# ---------------------------------------------------------------------------
# Orchestration: run() on a synthetic agents dir
# ---------------------------------------------------------------------------


def test_run_writes_log_and_returns_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """run() always returns 0 and writes ``code_health/audit_agents.log``."""
    (tmp_path / "FOUNDATION.md").write_text("FOUNDATION content for compliance.")
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "good.md").write_text(
        "---\n"
        "name: good\n"
        "description: Use proactively when CI fails.\n"
        "tools:\n  - Read\n  - Grep\n"
        "model: haiku\n"
        "---\n\n"
        "# Good\n\nshort body.\n\n"
        "## Workflow\nstep\n\n"
        "## Scope Boundaries\n\n### I WILL\n- x\n\n"
        "## Output\nok\n\n"
        "## Success Criteria\n- pass\n"
    )
    (agents_dir / "_TEMPLATE.md").write_text("# Template — should be skipped\n")
    monkeypatch.setattr("forge.audit.agents.repo_root", lambda: tmp_path)
    monkeypatch.setattr("forge.audit.common.repo_root", lambda: tmp_path)

    with patch("sys.argv", ["forge-audit-agents"]):
        rc = audit_agents.main()

    assert rc == 0
    log = (tmp_path / "code_health" / "audit_agents.log").read_text()
    assert "# forge-audit-agents" in log
    assert "| good" in log
    assert "_TEMPLATE" not in log  # underscore-prefixed files skipped


def test_run_skips_underscore_prefixed_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Files starting with ``_`` are not treated as agents."""
    del monkeypatch
    (tmp_path / "FOUNDATION.md").write_text("Foundation text.")
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "_TEMPLATE.md").write_text("template content")
    (agents_dir / "_DRAFT.md").write_text("draft content")

    paths = audit_agents._iter_agent_files(tmp_path)
    assert paths == []


def test_run_writes_log_when_no_agent_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No real agent files (only `_TEMPLATE.md`) still produces an empty log."""
    (tmp_path / "FOUNDATION.md").write_text("Foundation text.")
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "_TEMPLATE.md").write_text("template content")
    monkeypatch.setattr("forge.audit.agents.repo_root", lambda: tmp_path)
    monkeypatch.setattr("forge.audit.common.repo_root", lambda: tmp_path)

    with patch("sys.argv", ["forge-audit-agents"]):
        rc = audit_agents.main()

    assert rc == 0
    log = (tmp_path / "code_health" / "audit_agents.log").read_text()
    assert "No agent files found." in log


def test_run_records_critical_finding_when_foundation_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Missing FOUNDATION.md → CRITICAL finding in the log, exit 0."""
    # agents/ exists but FOUNDATION.md does not — misconfigured repo.
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "a.md").write_text("# A\n")
    monkeypatch.setattr("forge.audit.agents.repo_root", lambda: tmp_path)
    monkeypatch.setattr("forge.audit.common.repo_root", lambda: tmp_path)

    with patch("sys.argv", ["forge-audit-agents"]):
        rc = audit_agents.main()

    assert rc == 0  # non-blocking
    log = (tmp_path / "code_health" / "audit_agents.log").read_text()
    assert "CRITICAL" in log
    assert "FOUNDATION.md not found" in log
