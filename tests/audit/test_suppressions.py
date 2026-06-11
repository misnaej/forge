"""Tests for ``forge.audit.suppressions``."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from forge.audit import common, suppressions
from forge.audit.common import Scope, Severity
from forge.audit.suppressions import (
    SuppressionsConfig,
    _noqa_findings,
    _parse_codes,
    _pragma_findings,
    _type_ignore_findings,
    resolve_ruff_rule,
    run,
)


if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def fake_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create a src-layout repo and point common.repo_root at it.

    Returns:
        The repo root path.
    """
    (tmp_path / "src").mkdir()
    monkeypatch.setattr(common, "repo_root", lambda: tmp_path)
    return tmp_path


def _write(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` after creating parent directories.

    Args:
        path: Destination file path.
        text: Body content.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_parse_codes_returns_empty_for_none() -> None:
    """``None`` (no codes after ``noqa:``) yields an empty list."""
    assert _parse_codes(None) == []


def test_parse_codes_splits_comma_separated_codes() -> None:
    """Whitespace and commas are stripped; codes upper-cased."""
    assert _parse_codes("e501, plr0913,  ARG001 ") == ["E501", "PLR0913", "ARG001"]


def test_noqa_findings_bare_is_high_severity() -> None:
    """A bare ``# noqa`` (no code) is reported as HIGH."""
    findings = _noqa_findings("a.py", 1, "x = 1  # noqa", rule_cache={})
    assert len(findings) == 1
    assert findings[0].severity is Severity.HIGH
    assert "bare" in findings[0].message


def test_noqa_findings_specific_code_is_medium(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``# noqa: E501`` is MEDIUM and includes a resolved rule descriptor."""
    monkeypatch.setattr(
        suppressions,
        "resolve_ruff_rule",
        lambda code, _cache: (f"name-{code.lower()}", f"summary for {code}"),
    )
    findings = _noqa_findings(
        "a.py",
        7,
        "very_long_line_here  # noqa: E501",
        rule_cache={},
    )
    assert len(findings) == 1
    f = findings[0]
    assert f.severity is Severity.MEDIUM
    assert "E501" in f.message
    assert any("E501 (name-e501)" in e for e in f.evidence)


def test_noqa_findings_unresolved_code_still_reports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unknown code (``ruff rule`` failed) still yields a MEDIUM finding."""
    monkeypatch.setattr(
        suppressions,
        "resolve_ruff_rule",
        lambda _code, _cache: None,
    )
    findings = _noqa_findings(
        "a.py",
        1,
        "x = 1  # noqa: XYZ999",
        rule_cache={},
    )
    assert len(findings) == 1
    assert "rule unresolved" in findings[0].evidence[1]


def test_type_ignore_bare_is_medium() -> None:
    """Bare ``# type: ignore`` (no error code list) is MEDIUM."""
    findings = _type_ignore_findings("a.py", 4, "x = y  # type: ignore")
    assert len(findings) == 1
    assert findings[0].severity is Severity.MEDIUM


def test_type_ignore_with_codes_is_low() -> None:
    """``# type: ignore[arg-type]`` is LOW (specific)."""
    findings = _type_ignore_findings("a.py", 5, "x = y  # type: ignore[arg-type]")
    assert len(findings) == 1
    assert findings[0].severity is Severity.LOW
    assert "arg-type" in findings[0].message


def test_pragma_findings_returns_low() -> None:
    """``# pragma: no cover`` is reported as LOW."""
    findings = _pragma_findings("a.py", 9, "if False: pass  # pragma: no cover")
    assert len(findings) == 1
    assert findings[0].severity is Severity.LOW


def test_pragma_findings_empty_when_absent() -> None:
    """A line without the pragma yields no findings."""
    assert _pragma_findings("a.py", 1, "x = 1") == []


def test_resolve_ruff_rule_caches_results(monkeypatch: pytest.MonkeyPatch) -> None:
    """Repeated lookups for the same code don't re-invoke subprocess."""
    calls = {"n": 0}

    def fake_run(_cmd: list[str], **_kw: object) -> object:
        """Pretend to be ``ruff rule`` returning a fixed JSON payload.

        Args:
            _cmd: Ignored command argv.
            **_kw: Ignored keyword arguments.

        Returns:
            A stand-in object with ``returncode`` / ``stdout`` / ``stderr``.
        """
        calls["n"] += 1
        return type(
            "P",
            (),
            {
                "returncode": 0,
                "stdout": '{"name": "line-too-long", "summary": "Line too long"}',
                "stderr": "",
            },
        )

    monkeypatch.setattr(suppressions.subprocess, "run", fake_run)
    cache: dict[str, tuple[str, str] | None] = {}
    a = resolve_ruff_rule("E501", cache)
    b = resolve_ruff_rule("E501", cache)
    assert a == b == ("line-too-long", "Line too long")
    assert calls["n"] == 1


def test_run_writes_log_with_findings(
    fake_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Integration: end-to-end run finds suppressions in a sample file."""
    monkeypatch.setattr(
        suppressions,
        "resolve_ruff_rule",
        lambda code, _cache: (code.lower(), f"summary {code}"),
    )
    _write(
        fake_repo / "src" / "mod.py",
        "x = 1  # noqa: E501\n"
        "y = 2  # noqa\n"
        "z = 3  # type: ignore[arg-type]\n"
        "w = 4  # pragma: no cover\n",
    )
    code = run(Scope.FULL, [fake_repo / "src"], SuppressionsConfig())
    log_path = fake_repo / "code_health" / "audit_suppressions.log"
    log_text = log_path.read_text(encoding="utf-8")
    assert "[HIGH]" in log_text
    assert "[MEDIUM]" in log_text
    assert "[LOW]" in log_text
    assert "E501" in log_text
    assert "bare `# noqa`" in log_text
    assert code == 1


def test_run_clean_file_returns_zero(fake_repo: Path) -> None:
    """A file with no suppressions yields exit 0 and no findings."""
    _write(fake_repo / "src" / "clean.py", "x = 1\n")
    code = run(Scope.FULL, [fake_repo / "src"], SuppressionsConfig())
    log_path = fake_repo / "code_health" / "audit_suppressions.log"
    log_text = log_path.read_text(encoding="utf-8")
    assert "# findings: 0" in log_text
    assert code == 0
