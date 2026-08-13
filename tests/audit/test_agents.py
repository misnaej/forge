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
    findings = audit_agents._check_reporter_tools(
        doc,
        frozenset(audit_agents.REPORTER_AGENT_NAMES),
        frozenset(audit_agents._REPORTER_WITH_ARTIFACT_NAMES),
    )
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
    assert (
        audit_agents._check_reporter_tools(
            doc,
            frozenset(audit_agents.REPORTER_AGENT_NAMES),
            frozenset(audit_agents._REPORTER_WITH_ARTIFACT_NAMES),
        )
        == []
    )


def test_check_reporter_tools_skips_reporter_with_artifact() -> None:
    """Reporter-with-artifact agents are exempt from the Write/Edit ban."""
    for name in audit_agents._REPORTER_WITH_ARTIFACT_NAMES:
        fm = {
            "description": "Reviews and produces an artifact file.",
            "tools": ("Read", "Edit", "Write"),
        }
        doc = _agent_doc(frontmatter=fm, path=f"agents/{name}.md")
        assert (
            audit_agents._check_reporter_tools(
                doc,
                frozenset(audit_agents.REPORTER_AGENT_NAMES),
                frozenset(audit_agents._REPORTER_WITH_ARTIFACT_NAMES),
            )
            == []
        )


def test_is_reporter_agent_matches_allowlist() -> None:
    """Filename stem in REPORTER_AGENT_NAMES → True."""
    reporters = frozenset(audit_agents.REPORTER_AGENT_NAMES)
    for name in audit_agents.REPORTER_AGENT_NAMES:
        assert audit_agents._is_reporter_agent(
            _agent_doc(path=f"agents/{name}.md"), reporters
        )


def test_is_reporter_agent_rejects_other_names() -> None:
    """Names outside the allowlist → False, even with reporter-shaped desc."""
    fm = {"description": "Reviews and reports on the diff."}
    doc = _agent_doc(frontmatter=fm, path="agents/pr-manager.md")
    assert not audit_agents._is_reporter_agent(
        doc, frozenset(audit_agents.REPORTER_AGENT_NAMES)
    )


def test_check_reporter_verified_at_flags_missing_header() -> None:
    """Reporter agent body without `verified-at:` yields one MEDIUM finding."""
    doc = _agent_doc(
        body="## Output\nplain report with no contract\n",
        path="agents/design-checker.md",
    )
    findings = audit_agents._check_reporter_verified_at(
        doc, frozenset(audit_agents.REPORTER_AGENT_NAMES)
    )
    assert len(findings) == 1
    assert findings[0].severity == Severity.MEDIUM
    assert "verified-at" in findings[0].message


def test_check_reporter_verified_at_passes_when_header_present() -> None:
    """Body containing `verified-at:` yields zero findings."""
    body = "## Output\nFirst line: verified-at: <sha> (PR #N, branch x)\n"
    doc = _agent_doc(body=body, path="agents/security-checker.md")
    assert (
        audit_agents._check_reporter_verified_at(
            doc, frozenset(audit_agents.REPORTER_AGENT_NAMES)
        )
        == []
    )


def test_check_reporter_verified_at_skips_non_reporter() -> None:
    """Agents not in REPORTER_AGENT_NAMES are not subject to the contract."""
    doc = _agent_doc(body="no verified-at here\n", path="agents/pr-manager.md")
    assert (
        audit_agents._check_reporter_verified_at(
            doc, frozenset(audit_agents.REPORTER_AGENT_NAMES)
        )
        == []
    )


def test_check_header_contract_flags_named_agent_missing_header() -> None:
    """An agent in *names* whose body lacks *header* yields one MEDIUM finding."""
    doc = _agent_doc(
        body="## Output\nplain report, no header at all\n",
        path="agents/prior-art.md",
    )
    findings = audit_agents._check_header_contract(
        doc,
        frozenset(audit_agents.PRIOR_ART_AGENT_NAMES),
        header="prior-art-searched:",
        rationale="the refusal contract depends on it",
    )
    assert len(findings) == 1
    assert findings[0].severity == Severity.MEDIUM
    assert "prior-art-searched:" in findings[0].message
    assert "the refusal contract depends on it" in findings[0].message


def test_check_header_contract_passes_when_header_present() -> None:
    """An agent whose body contains *header* yields zero findings."""
    doc = _agent_doc(
        body="## Output\nprior-art-searched: <hash> (3 queries)\n",
        path="agents/prior-art.md",
    )
    findings = audit_agents._check_header_contract(
        doc,
        frozenset(audit_agents.PRIOR_ART_AGENT_NAMES),
        header="prior-art-searched:",
        rationale="the refusal contract depends on it",
    )
    assert findings == []


def test_check_header_contract_skips_agent_outside_names() -> None:
    """An agent not in *names* is exempt regardless of body content."""
    doc = _agent_doc(
        body="## Output\nno header of any kind here\n",
        path="agents/pr-manager.md",
    )
    findings = audit_agents._check_header_contract(
        doc,
        frozenset(audit_agents.PRIOR_ART_AGENT_NAMES),
        header="prior-art-searched:",
        rationale="the refusal contract depends on it",
    )
    assert findings == []


def test_check_reporter_verified_at_uses_shared_header_contract_helper() -> None:
    """_check_reporter_verified_at still flags a reporter missing verified-at:.

    Regression guard for the shared-helper wiring: `_check_reporter_verified_at`
    now delegates to `_check_header_contract`, so this exercises that the
    delegation preserves the original reporter contract rather than
    duplicating it as a separate check.
    """
    doc = _agent_doc(
        body="## Output\nno contract header present\n",
        path="agents/design-checker.md",
    )
    findings = audit_agents._check_reporter_verified_at(
        doc, frozenset(audit_agents.REPORTER_AGENT_NAMES)
    )
    assert len(findings) == 1
    assert findings[0].severity == Severity.MEDIUM
    assert "verified-at:" in findings[0].message


def test_check_reporter_tools_flags_prior_art_agent_holding_write() -> None:
    """A prior-art agent doc with Write in tools gets the reporter-tools MEDIUM.

    Regression on the REAL wiring: `_per_agent_findings` is called with only
    the BASE reporter set — the prior-art union happens inside it. Dropping
    `| prior_art` from `_per_agent_findings` makes this fail, which a
    caller-supplied union could not detect.
    """
    fm = {
        "description": "Searches the API digest before a new helper is written.",
        "tools": ("Read", "Grep", "Write"),
    }
    doc = _agent_doc(frontmatter=fm, path="agents/prior-art.md")
    findings = audit_agents._per_agent_findings(
        doc,
        set(),
        frozenset(audit_agents.REPORTER_AGENT_NAMES),
        frozenset(audit_agents._REPORTER_WITH_ARTIFACT_NAMES),
    )
    tool_findings = [f for f in findings if "'Write'" in f.message]
    assert tool_findings
    assert {f.severity for f in tool_findings} == {Severity.MEDIUM}


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
# _check_section_order — canonical H2 sections in template order
# ---------------------------------------------------------------------------


def test_check_section_order_in_order_returns_empty() -> None:
    """All four canonical sections present in template order yield []."""
    body = (
        "# Agent\n\n"
        "## Workflow\nstep\n\n"
        "## Scope Boundaries\ntext\n\n"
        "## Output\ntext\n\n"
        "## Success Criteria\ntext\n"
    )
    assert audit_agents._check_section_order(_agent_doc(body=body)) == []


def test_check_section_order_flags_swapped_pair() -> None:
    """Swapping one adjacent pair yields exactly one LOW finding naming it."""
    body = (
        "# Agent\n\n"
        "## Scope Boundaries\ntext\n\n"
        "## Workflow\nstep\n\n"
        "## Output\ntext\n\n"
        "## Success Criteria\ntext\n"
    )
    doc = _agent_doc(body=body, path="agents/swapped.md")
    findings = audit_agents._check_section_order(doc)
    assert len(findings) == 1
    finding = findings[0]
    assert finding.severity == Severity.LOW
    assert finding.path == doc.path
    assert finding.line == 0
    assert "canonical sections out of template order: found" in finding.message
    assert "'## Scope Boundaries' → '## Workflow'" in finding.message
    assert "(see _TEMPLATE.md)" in finding.message


def test_check_section_order_missing_section_not_double_reported() -> None:
    """A missing section alone is not a section-order finding.

    The remaining three sections in template order yield no finding; a
    present-but-out-of-order subset yields exactly one finding that does
    not name the absent section — that's `_check_required_sections`'s job.
    """
    in_order_body = (
        "# Agent\n\n"
        "## Workflow\nstep\n\n"
        "## Scope Boundaries\ntext\n\n"
        "## Success Criteria\ntext\n"
    )
    assert audit_agents._check_section_order(_agent_doc(body=in_order_body)) == []

    out_of_order_body = (
        "# Agent\n\n"
        "## Scope Boundaries\ntext\n\n"
        "## Workflow\nstep\n\n"
        "## Success Criteria\ntext\n"
    )
    findings = audit_agents._check_section_order(_agent_doc(body=out_of_order_body))
    assert len(findings) == 1
    assert "Output" not in findings[0].message


def test_check_section_order_body_startswith_first_section() -> None:
    """A body beginning with the first section (no leading newline) is in order."""
    body = (
        "## Workflow\nstep\n\n"
        "## Scope Boundaries\ntext\n\n"
        "## Output\ntext\n\n"
        "## Success Criteria\ntext\n"
    )
    assert audit_agents._check_section_order(_agent_doc(body=body)) == []


def test_check_section_order_all_sections_missing_returns_empty() -> None:
    """A body with none of the canonical sections yields []."""
    body = "# Agent\n\nno canonical sections here\n"
    assert audit_agents._check_section_order(_agent_doc(body=body)) == []


def test_per_agent_findings_includes_section_order_finding() -> None:
    """`_per_agent_findings` surfaces the section-order finding end to end."""
    fm = {
        "name": "test",
        "description": "Use proactively when validating agent files.",
        "tools": ("Read",),
        "model": "haiku",
    }
    body = (
        "# Agent\n\n"
        "## Scope Boundaries\ntext\n\n"
        "## Workflow\nstep\n\n"
        "## Output\ntext\n\n"
        "## Success Criteria\ntext\n"
    )
    doc = _agent_doc(body=body, frontmatter=fm, path="agents/test.md")
    findings = audit_agents._per_agent_findings(
        doc,
        set(),
        frozenset(audit_agents.REPORTER_AGENT_NAMES),
        frozenset(audit_agents._REPORTER_WITH_ARTIFACT_NAMES),
    )
    assert any("out of template order" in f.message for f in findings)


# ---------------------------------------------------------------------------
# _sanitize_fragment — trust-boundary guard for code_health/audit_agents.log
#
# Consumer-authored `.claude/agents/*.md` frontmatter reaches the log via
# this helper, and code_health/*.log is trusted ground truth for other
# forge agents (FOUNDATION §13). These cases exercise the injection and
# truncation contract directly.
# ---------------------------------------------------------------------------


def test_sanitize_fragment_escapes_embedded_newline() -> None:
    r"""A crafted value with an embedded newline is neutralized.

    A raw newline in a log line lets a crafted frontmatter value forge a
    fake finding row. `repr()` renders it as the two-character escape
    `\\n`, not an actual line break, so the injected text stays inside
    one log line.
    """
    payload = "Edit\nINJECTED: fake finding"
    result = audit_agents._sanitize_fragment(payload)
    assert "\\n" in result
    assert "\n" not in result


def test_sanitize_fragment_escapes_ansi_control_sequence() -> None:
    r"""A crafted value with an ANSI escape sequence is neutralized.

    Raw control bytes (e.g. terminal color codes) could make a log line
    misrender or hide content when viewed in a terminal. `repr()`
    escapes the control byte to its printable `\\x1b` form.
    """
    payload = "\x1b[31mEdit"
    result = audit_agents._sanitize_fragment(payload)
    assert "\\x1b" in result
    assert "\x1b" not in result


def test_sanitize_fragment_truncates_over_limit_with_ellipsis() -> None:
    """A value beyond the default 40-char limit is clipped with an ellipsis.

    Matches the implementation's exact contract: the first `limit`
    characters plus a trailing `…` marker, then `repr()`-quoted — so an
    unbounded crafted value can't blow out the log line length.
    """
    payload = "x" * 50
    result = audit_agents._sanitize_fragment(payload)
    expected = repr(payload[:40] + "…")
    assert result == expected
    assert "x" * 41 not in result  # the 41st original char never appears


def test_sanitize_fragment_leaves_short_value_unclipped() -> None:
    """A value at or under the limit is repr-quoted without an ellipsis."""
    payload = "Read"
    result = audit_agents._sanitize_fragment(payload)
    assert result == repr(payload)
    assert "…" not in result


def test_check_reporter_tools_crafted_decoy_never_reaches_message() -> None:
    """A crafted, injection-shaped decoy tool name is filtered, not logged.

    Mirrors `test_check_reporter_tools_flags_write_edit` but adds a decoy
    tool that *looks* like `"Edit"` plus an injected fake-finding line.
    `_check_reporter_tools` matches tool names by exact equality against
    `REPORTER_FORBIDDEN_TOOLS`, so the decoy never matches and never
    reaches `_sanitize_fragment` / the finding message — only the
    genuine `"Edit"` entry does, quoted as plain `repr()` output.
    """
    fm = {
        "description": "Reviews PR diffs for security issues.",
        "tools": ("Read", "Edit", "Edit\nINJECTED: fake finding"),
    }
    doc = _agent_doc(frontmatter=fm, path="agents/security-checker.md")
    findings = audit_agents._check_reporter_tools(
        doc,
        frozenset(audit_agents.REPORTER_AGENT_NAMES),
        frozenset(audit_agents._REPORTER_WITH_ARTIFACT_NAMES),
    )
    assert len(findings) == 1
    assert findings[0].message.count("mutating tool") == 1
    assert "'Edit'" in findings[0].message
    assert "INJECTED" not in findings[0].message


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


# ---------------------------------------------------------------------------
# _iter_agent_files — default-dir union vs explicit --roots scoping
# ---------------------------------------------------------------------------


def test_iter_agent_files_unions_default_dirs(tmp_path: Path) -> None:
    """No explicit roots → both default dirs (agents/, .claude/agents/) scanned."""
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "shipped.md").write_text("# Shipped\n")
    (tmp_path / ".claude" / "agents").mkdir(parents=True)
    (tmp_path / ".claude" / "agents" / "consumer.md").write_text("# Consumer\n")

    paths = audit_agents._iter_agent_files(tmp_path)

    names = {p.name for p in paths}
    assert names == {"shipped.md", "consumer.md"}


def test_iter_agent_files_explicit_roots_scopes_to_given_roots(
    tmp_path: Path,
) -> None:
    """Explicit `roots` restrict the scan to exactly those directories."""
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "shipped.md").write_text("# Shipped\n")
    custom = tmp_path / "custom"
    custom.mkdir()
    (custom / "only.md").write_text("# Only\n")

    paths = audit_agents._iter_agent_files(tmp_path, [custom])

    assert paths == [custom / "only.md"]


def test_iter_agent_files_empty_roots_list_falls_back_to_defaults(
    tmp_path: Path,
) -> None:
    """An empty `roots` list (falsy) falls back to the default dir union."""
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "shipped.md").write_text("# Shipped\n")

    paths = audit_agents._iter_agent_files(tmp_path, [])

    assert paths == [tmp_path / "agents" / "shipped.md"]


# ---------------------------------------------------------------------------
# _effective_reporter_sets — consumer [tool.forge.audit_agents] union
# ---------------------------------------------------------------------------


def test_effective_reporter_sets_unions_consumer_config_onto_shipped(
    tmp_path: Path,
) -> None:
    """A consumer `reporter_agents` list is unioned onto the shipped tuple."""
    (tmp_path / "pyproject.toml").write_text(
        '[tool.forge.audit_agents]\nreporter_agents = ["custom-reporter"]\n'
    )
    reporters, _artifact_reporters = audit_agents._effective_reporter_sets(tmp_path)
    assert reporters == frozenset(audit_agents.REPORTER_AGENT_NAMES) | {
        "custom-reporter"
    }


def test_effective_reporter_sets_unions_artifact_reporters(tmp_path: Path) -> None:
    """A consumer `reporter_with_artifact_agents` list unions onto the shipped set."""
    (tmp_path / "pyproject.toml").write_text(
        "[tool.forge.audit_agents]\n"
        'reporter_with_artifact_agents = ["custom-artifact-reporter"]\n'
    )
    _reporters, artifact_reporters = audit_agents._effective_reporter_sets(tmp_path)
    assert artifact_reporters == frozenset(
        audit_agents._REPORTER_WITH_ARTIFACT_NAMES
    ) | {"custom-artifact-reporter"}


def test_effective_reporter_sets_shipped_name_cannot_gain_artifact_exemption(
    tmp_path: Path,
) -> None:
    """A shipped reporter name in consumer artifact config is ignored.

    The artifact set REMOVES the Write/Edit check, so letting consumer
    config add a shipped reporter (e.g. `design-checker`) would be a
    coverage-shrink vector despite the additive framing. Only genuinely
    new custom names are accepted.
    """
    (tmp_path / "pyproject.toml").write_text(
        "[tool.forge.audit_agents]\n"
        'reporter_with_artifact_agents = ["design-checker", "my-artifact"]\n'
    )
    _reporters, artifact_reporters = audit_agents._effective_reporter_sets(tmp_path)
    assert "my-artifact" in artifact_reporters
    assert artifact_reporters == frozenset(
        audit_agents._REPORTER_WITH_ARTIFACT_NAMES
    ) | {"my-artifact"}
    assert "design-checker" not in artifact_reporters


def test_effective_reporter_sets_non_list_value_degrades_to_shipped_only(
    tmp_path: Path,
) -> None:
    """A non-list `reporter_agents` value (e.g. a bare string) adds nothing."""
    (tmp_path / "pyproject.toml").write_text(
        '[tool.forge.audit_agents]\nreporter_agents = "oops"\n'
    )
    reporters, _artifact_reporters = audit_agents._effective_reporter_sets(tmp_path)
    assert reporters == frozenset(audit_agents.REPORTER_AGENT_NAMES)


def test_effective_reporter_sets_non_string_items_dropped(tmp_path: Path) -> None:
    """Non-string items in the consumer list are dropped, strings kept."""
    (tmp_path / "pyproject.toml").write_text(
        '[tool.forge.audit_agents]\nreporter_agents = [1, 2, "valid-name"]\n'
    )
    reporters, _artifact_reporters = audit_agents._effective_reporter_sets(tmp_path)
    assert reporters == frozenset(audit_agents.REPORTER_AGENT_NAMES) | {"valid-name"}


def test_effective_reporter_sets_missing_config_returns_shipped_defaults(
    tmp_path: Path,
) -> None:
    """No `[tool.forge.audit_agents]` section → exactly the shipped defaults."""
    reporters, artifact_reporters = audit_agents._effective_reporter_sets(tmp_path)
    assert reporters == frozenset(audit_agents.REPORTER_AGENT_NAMES)
    assert artifact_reporters == frozenset(audit_agents._REPORTER_WITH_ARTIFACT_NAMES)


# ---------------------------------------------------------------------------
# run() roots param — default union vs explicit scoping
# ---------------------------------------------------------------------------


def test_run_roots_param_empty_scans_default_union(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`run(scope, [], config)` scans the default agents/ + .claude/agents/ union."""
    (tmp_path / "FOUNDATION.md").write_text("Foundation text.")
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "shipped.md").write_text("# Shipped\n\n## Workflow\nx\n")
    (tmp_path / ".claude" / "agents").mkdir(parents=True)
    (tmp_path / ".claude" / "agents" / "consumer.md").write_text(
        "# Consumer\n\n## Workflow\nx\n"
    )
    monkeypatch.setattr("forge.audit.agents.repo_root", lambda: tmp_path)
    monkeypatch.setattr("forge.audit.common.repo_root", lambda: tmp_path)

    rc = audit_agents.run(
        audit_agents.Scope.FULL,
        [],
        audit_agents.AgentsConfig(output=tmp_path / "log.log"),
    )

    assert rc == 0
    log = (tmp_path / "log.log").read_text()
    assert "| shipped" in log
    assert "| consumer" in log


def test_run_roots_param_explicit_scopes_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`run(scope, [custom], config)` scans only the explicit root."""
    (tmp_path / "FOUNDATION.md").write_text("Foundation text.")
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "shipped.md").write_text("# Shipped\n\n## Workflow\nx\n")
    custom = tmp_path / "custom"
    custom.mkdir()
    (custom / "only.md").write_text("# Only\n\n## Workflow\nx\n")
    monkeypatch.setattr("forge.audit.agents.repo_root", lambda: tmp_path)
    monkeypatch.setattr("forge.audit.common.repo_root", lambda: tmp_path)

    rc = audit_agents.run(
        audit_agents.Scope.FULL,
        [custom],
        audit_agents.AgentsConfig(output=tmp_path / "log.log"),
    )

    assert rc == 0
    log = (tmp_path / "log.log").read_text()
    assert "| only" in log
    assert "| shipped" not in log


# ---------------------------------------------------------------------------
# main() --roots argument resolution
# ---------------------------------------------------------------------------


def test_main_resolves_roots_arg_dropping_nonexistent_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`main()` resolves `--roots` to absolute paths, dropping nonexistent dirs.

    SCENARIO: a consumer passes `--roots` naming one real and one
        missing directory; only the real one reaches `run()`.
    MOCK SETUP: `audit_agents.run` is patched to a recorder capturing its
        `roots` argument instead of doing a real scan; `repo_root` is
        patched to `tmp_path`. `sys.argv` requests both an existing
        (`keepme`) and a nonexistent (`missing`) directory.
    EXPECTED BEHAVIOR: the recorded `roots` list contains only the
        resolved absolute path to `keepme`.
    """
    (tmp_path / "keepme").mkdir()
    recorded: dict[str, object] = {}

    def _fake_run(scope: object, roots: list[Path], config: object) -> int:
        recorded["roots"] = roots
        return 0

    monkeypatch.setattr("forge.audit.agents.repo_root", lambda: tmp_path)
    monkeypatch.setattr(audit_agents, "run", _fake_run)

    with patch("sys.argv", ["forge-audit-agents", "--roots", "keepme", "missing"]):
        rc = audit_agents.main()

    assert rc == 0
    assert recorded["roots"] == [(tmp_path / "keepme").resolve()]
