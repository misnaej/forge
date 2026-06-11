"""Tests for ``forge.audit.claims`` domain-claim extraction."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from forge.audit import claims, common
from forge.audit.claims import (
    ClaimsConfig,
    _comment_findings,
    _docstring_findings,
    _is_suppression_comment,
    _looks_like_claim,
    _matched_terms,
    load_repo_lexicon,
    run,
)
from forge.audit.common import Scope, Severity


if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def fake_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create an empty repo root and point common at it.

    Returns:
        The repo root path.
    """
    (tmp_path / "src").mkdir()
    monkeypatch.setattr(common, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(claims, "repo_root", lambda: tmp_path)
    return tmp_path


def _write(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` after creating parent dirs.

    Args:
        path: Destination file path.
        text: Content to write.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_looks_like_claim_matches_comparison_with_equals() -> None:
    """A 'lower X = more Y' sentence is recognized as a claim."""
    assert _looks_like_claim("lower KL = more conserved sequence")


def test_looks_like_claim_matches_causation_verb() -> None:
    """A 'X causes Y' sentence is recognized as a claim."""
    assert _looks_like_claim("the gradient causes overfitting")


def test_looks_like_claim_matches_equation_form() -> None:
    """A bare 'X = Y' equation form is recognized."""
    assert _looks_like_claim("density = mass / volume")


def test_looks_like_claim_negative_on_neutral_prose() -> None:
    """Plain prose without comparison or equation does not match."""
    assert not _looks_like_claim("This function returns a list.")


def test_matched_terms_returns_lowercase_overlap() -> None:
    """Lexicon match is case-insensitive and sorted."""
    terms = _matched_terms(
        "RMSD and KL are key metrics",
        frozenset({"rmsd", "kl", "irrelevant"}),
    )
    assert terms == ["kl", "rmsd"]


def test_is_suppression_comment_recognizes_noqa() -> None:
    """Comments beginning with known directive prefixes are flagged as suppressions."""
    assert _is_suppression_comment("x = 1  # noqa: E501")
    assert _is_suppression_comment("y = 2  # type: ignore[arg-type]")
    assert not _is_suppression_comment("z = 3  # explanatory comment")


def test_docstring_findings_extracts_claim_with_lexicon_match() -> None:
    """A docstring claim with a lexicon term yields a REVIEW finding."""
    source = '"""Module.\n\nLower KL = more conserved sequence.\n"""\n'
    source_lines = source.splitlines()
    findings = _docstring_findings(
        source_lines,
        "Module.\n\nLower KL = more conserved sequence.\n",
        docstring_lineno=1,
        rel="src/m.py",
        lexicon=frozenset({"kl", "conserved"}),
    )
    assert len(findings) == 1
    assert findings[0].severity is Severity.REVIEW
    assert "kl" in findings[0].message
    assert "Lower KL" in findings[0].evidence[0]


def test_docstring_findings_filters_lines_without_lexicon_match() -> None:
    """Claim-shaped lines without any lexicon term are dropped."""
    source_lines = ["", '"""Lower foo = higher bar."""', ""]
    findings = _docstring_findings(
        source_lines,
        "Lower foo = higher bar.",
        docstring_lineno=2,
        rel="src/m.py",
        lexicon=frozenset({"kl"}),
    )
    assert findings == []


def test_comment_findings_extracts_inline_claim() -> None:
    """A causal inline ``#`` comment with a lexicon term is reported."""
    text = "x = 1  # higher latency causes throughput drop\n"
    findings = _comment_findings(
        text,
        text.splitlines(),
        rel="src/x.py",
        lexicon=frozenset({"latency", "throughput"}),
    )
    assert len(findings) == 1
    assert findings[0].severity is Severity.REVIEW


def test_comment_findings_skips_suppression_comments() -> None:
    """``# noqa`` / ``# type: ignore`` lines are not treated as claims."""
    text = "x = 1  # noqa: E501\n"
    findings = _comment_findings(
        text,
        text.splitlines(),
        rel="src/x.py",
        lexicon=frozenset({"loss"}),
    )
    assert findings == []


def test_load_repo_lexicon_merges_toml_extension(fake_repo: Path) -> None:
    """A repo-level ``forge-audit-claims.toml`` extends the default lexicon."""
    _write(
        fake_repo / "forge-audit-claims.toml",
        'lexicon = ["KL", "RMSD"]\n',
    )
    lex = load_repo_lexicon(use_default=True)
    assert "kl" in lex
    assert "rmsd" in lex
    assert "gradient" in lex  # default term still present


def test_load_repo_lexicon_no_default(fake_repo: Path) -> None:
    """``use_default=False`` drops the built-in seed terms."""
    _write(fake_repo / "forge-audit-claims.toml", 'lexicon = ["kl"]\n')
    lex = load_repo_lexicon(use_default=False)
    assert lex == frozenset({"kl"})


def test_load_repo_lexicon_no_config_returns_default(fake_repo: Path) -> None:
    """Without a config file the default lexicon is returned unchanged."""
    lex = load_repo_lexicon(use_default=True)
    assert "gradient" in lex


def test_run_extracts_claims_from_module(fake_repo: Path) -> None:
    """End-to-end: a module with a lexicon-matching claim produces a REVIEW finding."""
    _write(
        fake_repo / "src" / "mod.py",
        '"""Module summary.\n\nLower KL = more conserved sequence position.\n"""\n',
    )
    code = run(
        Scope.FULL,
        [fake_repo / "src"],
        ClaimsConfig(lexicon=frozenset({"kl", "conserved"})),
    )
    log_path = fake_repo / "code_health" / "audit_claims.log"
    log_text = log_path.read_text(encoding="utf-8")
    assert "[REVIEW]" in log_text
    assert "Lower KL" in log_text
    assert code == 0


def test_run_zero_findings_when_lexicon_empty(fake_repo: Path) -> None:
    """An empty lexicon never matches; log shows zero findings."""
    _write(
        fake_repo / "src" / "mod.py",
        '"""Lower KL = more conserved."""\n',
    )
    code = run(
        Scope.FULL,
        [fake_repo / "src"],
        ClaimsConfig(lexicon=frozenset()),
    )
    log_path = fake_repo / "code_health" / "audit_claims.log"
    log_text = log_path.read_text(encoding="utf-8")
    assert "# findings: 0" in log_text
    assert code == 0
