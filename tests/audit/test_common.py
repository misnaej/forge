"""Tests for ``forge.audit.common`` helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from forge.audit import common
from forge.audit.common import (
    Finding,
    Scope,
    Severity,
    exit_code_for,
    iter_files,
    make_audit_parser,
    relpath,
    resolve_roots,
    write_log,
)


if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def fake_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create a minimal repo-like tree and point helpers at it.

    Returns:
        The repo root path.
    """
    pkg = tmp_path / "src" / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "a.py").write_text("x = 1\n", encoding="utf-8")
    (pkg / "b.py").write_text("y = 2\n", encoding="utf-8")
    cache = pkg / "__pycache__"
    cache.mkdir()
    (cache / "skip.py").write_text("", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("", encoding="utf-8")

    monkeypatch.setattr(common, "repo_root", lambda: tmp_path)
    return tmp_path


def test_finding_render_includes_severity_and_location() -> None:
    """Finding.render() prints severity, path:line, and message header."""
    f = Finding(
        audit="dup",
        severity=Severity.HIGH,
        path="src/a.py",
        line=42,
        message="duplicate body",
    )
    out = f.render()
    assert "[HIGH] src/a.py:42 duplicate body" in out
    assert out.endswith("\n\n")


def test_finding_render_with_evidence_indents_block() -> None:
    """Evidence lines render indented under the header."""
    f = Finding(
        audit="dup",
        severity=Severity.HIGH,
        path="src/a.py",
        line=1,
        message="m",
        evidence=("line one", "line two"),
    )
    out = f.render()
    assert "    line one" in out
    assert "    line two" in out


def test_make_audit_parser_exposes_scope_roots_output() -> None:
    """Shared parser defines the three required flags."""
    parser = make_audit_parser("forge-audit-x", "test parser")
    ns = parser.parse_args(["--scope", "changed"])
    assert ns.scope == "changed"
    assert ns.roots is None
    assert ns.output is None


def test_make_audit_parser_rejects_invalid_scope() -> None:
    """An unknown --scope value triggers argparse error (SystemExit)."""
    parser = make_audit_parser("forge-audit-x", "test parser")
    with pytest.raises(SystemExit):
        parser.parse_args(["--scope", "garbage"])


def test_resolve_roots_autodetects_existing_dirs(fake_repo: Path) -> None:
    """resolve_roots(None) picks up only directories that actually exist."""
    out = resolve_roots(None)
    names = {p.name for p in out}
    assert "src" in names
    assert "tests" in names


def test_resolve_roots_respects_explicit_list(fake_repo: Path) -> None:
    """resolve_roots(["src"]) returns only the requested existing dir."""
    out = resolve_roots(["src"])
    assert len(out) == 1
    assert out[0].name == "src"


def test_iter_files_full_scope_walks_src_skips_pycache(fake_repo: Path) -> None:
    """Full scope yields .py files under roots, skipping __pycache__."""
    paths = list(iter_files(Scope.FULL, [fake_repo / "src"]))
    names = sorted(p.name for p in paths)
    assert names == ["a.py", "b.py"]


def test_iter_files_changed_scope_delegates_to_git(
    fake_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Changed scope reads from get_modified_files instead of walking."""
    monkeypatch.setattr(common, "get_modified_files", lambda **_: ["src/pkg/a.py"])
    paths = list(iter_files(Scope.CHANGED, []))
    assert len(paths) == 1
    assert paths[0].name == "a.py"


def test_relpath_renders_repo_relative(fake_repo: Path) -> None:
    """relpath() strips the repo root prefix."""
    assert relpath(fake_repo / "src" / "pkg" / "a.py") == "src/pkg/a.py"


def test_write_log_creates_code_health_dir_and_writes_header(fake_repo: Path) -> None:
    """write_log emits header + finding count and creates code_health/."""
    findings = [
        Finding(
            audit="dup",
            severity=Severity.HIGH,
            path="src/a.py",
            line=1,
            message="m",
        ),
    ]
    path = write_log("dup", findings, summary="one duplicate")
    text = path.read_text(encoding="utf-8")
    assert "# forge-audit-dup" in text
    assert "# findings: 1" in text
    assert "one duplicate" in text
    assert "[HIGH] src/a.py:1 m" in text


def test_write_log_handles_zero_findings(fake_repo: Path) -> None:
    """Empty findings list still produces a parseable log."""
    path = write_log("dup", [], summary="clean")
    text = path.read_text(encoding="utf-8")
    assert "# findings: 0" in text
    assert "clean" in text


def test_exit_code_for_returns_zero_when_only_review() -> None:
    """REVIEW-only findings should not block: exit 0."""
    findings = [
        Finding(
            audit="claims",
            severity=Severity.REVIEW,
            path="a",
            line=1,
            message="m",
        ),
    ]
    assert exit_code_for(findings) == 0


def test_exit_code_for_returns_one_on_high_or_above() -> None:
    """Any HIGH or CRITICAL finding triggers exit 1."""
    findings = [
        Finding(
            audit="dup",
            severity=Severity.HIGH,
            path="a",
            line=1,
            message="m",
        ),
    ]
    assert exit_code_for(findings) == 1
