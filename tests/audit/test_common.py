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
from tests.audit.conftest import write_pyproject


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


@pytest.fixture
def fake_repo_with_decoys(fake_repo: Path) -> Path:
    """Extend ``fake_repo`` with ``docs/`` and ``data/`` decoy directories.

    Both exist on disk — so the broad ``DEFAULT_ROOTS`` guess would pick
    them up — but neither is named in a declared ``source_dirs`` /
    ``test_dirs`` layout, letting tests assert the declared layout excludes
    them.

    Returns:
        The repo root path (same as ``fake_repo``).
    """
    (fake_repo / "docs").mkdir()
    (fake_repo / "data").mkdir()
    return fake_repo


def test_resolve_roots_declared_layout_excludes_default_roots_extras(
    fake_repo_with_decoys: Path,
) -> None:
    """Declared source_dirs/test_dirs win over the broad DEFAULT_ROOTS guess.

    ``docs/`` and ``data/`` exist on disk (decoys) but are not declared, so
    a repo that stated its layout gets exactly that layout — not the
    DEFAULT_ROOTS extras where spurious audit findings live.
    """
    write_pyproject(
        fake_repo_with_decoys,
        '[tool.forge]\nsource_dirs = ["src"]\ntest_dirs = ["tests"]\n',
    )
    out = resolve_roots(None)
    names = {p.name for p in out}
    assert names == {"src", "tests"}


def test_resolve_roots_no_declared_layout_keeps_default_roots_fallback(
    fake_repo_with_decoys: Path,
) -> None:
    """With no source_dirs configured, the DEFAULT_ROOTS guess is unchanged.

    Same tree as the declared-layout test (decoys included) but no
    pyproject.toml — the decoy dirs ARE picked up, proving the fallback
    path is untouched by the declared-layout preference.
    """
    out = resolve_roots(None)
    names = {p.name for p in out}
    assert {"src", "tests", "docs", "data"} <= names


def test_resolve_roots_explicit_list_wins_over_declared_layout(
    fake_repo_with_decoys: Path,
) -> None:
    """An explicit --roots list overrides the declared-layout preference too."""
    write_pyproject(
        fake_repo_with_decoys,
        '[tool.forge]\nsource_dirs = ["src"]\ntest_dirs = ["tests"]\n',
    )
    out = resolve_roots(["docs"])
    assert len(out) == 1
    assert out[0].name == "docs"


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


def test_under_module_prefix_respects_dotted_boundaries() -> None:
    """Matches the prefix and its dotted children, never lexical near-misses."""
    assert common.under_module_prefix("forge.audit", "forge.audit")
    assert common.under_module_prefix("forge.audit.deps", "forge.audit")
    assert not common.under_module_prefix("forge.auditor", "forge.audit")


def test_sanitize_log_text_escapes_control_characters() -> None:
    """Newlines and ANSI escapes are repr-escaped; tab and text pass through.

    Guards the FOUNDATION §13 trust boundary: untrusted content (git
    filenames, config-supplied layer names) must not forge log lines.
    """
    assert common.sanitize_log_text("a\nb") == "a\\nb"
    assert common.sanitize_log_text("\x1b[31mred") == "\\x1b[31mred"
    assert common.sanitize_log_text("keep\ttab") == "keep\ttab"
    assert common.sanitize_log_text("plain") == "plain"


def test_finding_render_keeps_injected_newline_on_one_line() -> None:
    """A crafted message cannot spoof a second finding line in the log."""
    finding = Finding(
        audit="layering",
        severity=Severity.HIGH,
        path="src/x.py",
        line=1,
        message="bad\n[HIGH] pyproject.toml:1 fake injected finding",
    )
    rendered = finding.render()
    assert rendered.count("[HIGH]") == 2  # escaped text, not a real line
    lines = [ln for ln in rendered.splitlines() if ln.strip()]
    assert len(lines) == 1
