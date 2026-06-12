"""Tests for ``forge.audit.all`` — the audit orchestrator."""

# MOCKING STRATEGY: no sub-audit CLI actually runs — the orchestration logic is
# exercised in isolation.
#   - subprocess.run: replaced by `fake_run` closures returning the canonical
#     `FakeProc` (returncode/stdout/stderr) so no child process spawns.
#   - repo_root / require_cli: stubbed to a tmp_path and a no-op so the run
#     neither touches the real repo nor enforces CLI presence.
#   - git_utils.repo_root.cache_clear: no-op'd to avoid clearing the real cache.
#   - patch(sys.argv): drives main()'s argument parsing (--only, defaults).

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

from forge.audit import all as audit_all
from tests.conftest import FakeProc


if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_read_finding_count_parses_header() -> None:
    """A ``# findings: N`` header line yields the integer."""
    text = "# audit\n# findings: 7\n# generated: ...\n"
    assert audit_all._read_finding_count(text) == 7


def test_read_finding_count_missing_returns_minus_one() -> None:
    """Missing header returns ``-1`` (sentinel for 'unknown')."""
    assert audit_all._read_finding_count("no header here\n") == -1


def test_read_finding_count_invalid_returns_minus_one() -> None:
    """Non-integer findings value returns ``-1``."""
    assert audit_all._read_finding_count("# findings: oops\n") == -1


def test_render_summary_contains_each_subaudit(tmp_path: Path) -> None:
    """The rendered summary lists every result with its exit code and log path."""
    del tmp_path
    results = [
        audit_all.SubResult(
            name="dup",
            exit_code=0,
            log_path="code_health/audit_dup.log",
            finding_count=3,
        ),
        audit_all.SubResult(
            name="deps",
            exit_code=1,
            log_path="code_health/audit_deps.log",
            finding_count=-1,
        ),
    ]
    text = audit_all._render_summary(results)
    assert "dup" in text
    assert "deps" in text
    assert "3" in text  # finding count
    assert "n/a" in text  # for the -1 sentinel
    assert "code_health/audit_dup.log" in text
    assert "# subaudits: 2" in text


def test_main_invokes_every_selected_subaudit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``main()`` calls one sub-audit per name in ``SUB_AUDITS`` by default."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("forge.git_utils.repo_root.cache_clear", lambda: None)
    monkeypatch.setattr(audit_all, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(
        audit_all,
        "require_cli",
        lambda *_a, **_kw: None,
    )

    invoked: list[str] = []

    def fake_run(cmd: list[str], **_kwargs: object) -> object:
        invoked.append(cmd[0])
        return FakeProc(returncode=0)

    monkeypatch.setattr(audit_all.subprocess, "run", fake_run)
    with patch("sys.argv", ["forge-audit-all"]):
        rc = audit_all.main()

    assert rc == 0
    expected_calls = [f"forge-audit-{n}" for n in audit_all.SUB_AUDITS]
    assert invoked == expected_calls
    summary = (tmp_path / "code_health" / "audit_summary.log").read_text()
    assert "# forge-audit-all" in summary
    assert "# subaudits:" in summary


def test_main_only_filters_subaudits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--only dup deps`` runs only those two sub-audits."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(audit_all, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(audit_all, "require_cli", lambda *_a, **_kw: None)

    invoked: list[str] = []

    def fake_run(cmd: list[str], **_kwargs: object) -> object:
        invoked.append(cmd[0])
        return FakeProc(returncode=0)

    monkeypatch.setattr(audit_all.subprocess, "run", fake_run)
    with patch("sys.argv", ["forge-audit-all", "--only", "dup", "deps"]):
        rc = audit_all.main()

    assert rc == 0
    assert invoked == ["forge-audit-dup", "forge-audit-deps"]


def test_main_returns_max_subaudit_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``main()`` returns the maximum exit code across sub-audits."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(audit_all, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(audit_all, "require_cli", lambda *_a, **_kw: None)

    codes = iter([0, 2, 1])

    def fake_run(_cmd: list[str], **_kwargs: object) -> object:
        return FakeProc(returncode=next(codes))

    monkeypatch.setattr(audit_all.subprocess, "run", fake_run)
    with patch("sys.argv", ["forge-audit-all", "--only", "dup", "deps", "orphans"]):
        rc = audit_all.main()

    assert rc == 2
