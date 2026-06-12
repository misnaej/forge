"""Tests for forge.precommit dispatcher."""

# MOCKING STRATEGY: no real check runs — every external dependency of the
# dispatcher is swapped out so run_all/main orchestration is tested in
# isolation.
#   - shutil.which / _run: stub tool presence and command exit codes.
#   - step_* functions: replaced by `_stub_*` helpers returning canned
#     StepResults, so the sequence/exit-code logic is exercised without
#     running the real checks.
#   - patch(sys.argv): drives main()'s argument parsing.

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from forge import precommit


if TYPE_CHECKING:
    from pathlib import Path


def test_step_ruff_skipped_when_no_source_dirs(tmp_path: Path) -> None:
    """step_ruff returns a skipped result when no candidate dirs exist."""
    result = precommit.step_ruff(tmp_path)
    assert result.skipped
    assert result.passed
    assert "skipped" in result.output


def test_step_ruff_hard_fails_when_fix_forge_ruff_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """step_ruff exits 2 when ``fix-forge-ruff`` is not on PATH."""
    (tmp_path / "src").mkdir()
    monkeypatch.setattr(precommit.shutil, "which", lambda _name: None)
    with pytest.raises(SystemExit) as exc_info:
        precommit.step_ruff(tmp_path)
    assert exc_info.value.code == 2


def test_step_ruff_shells_out_to_fix_forge_ruff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """step_ruff delegates to the fix-forge-ruff CLI with source dirs."""
    (tmp_path / "src").mkdir()
    monkeypatch.setattr(
        precommit.shutil, "which", lambda _name: "/usr/bin/fix-forge-ruff"
    )
    calls: list[list[str]] = []

    def _fake_run(cmd: list[str], **_kwargs: object) -> tuple[bool, str]:
        calls.append(cmd)
        return True, "ruff output"

    monkeypatch.setattr(precommit, "_run", _fake_run)
    result = precommit.step_ruff(tmp_path)
    assert result.passed
    assert calls
    assert calls[0][0] == "fix-forge-ruff"
    assert "src" in calls[0]


def test_step_ruff_propagates_nonzero_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When fix-forge-ruff exits non-zero, step_ruff fails."""
    (tmp_path / "src").mkdir()
    monkeypatch.setattr(
        precommit.shutil, "which", lambda _name: "/usr/bin/fix-forge-ruff"
    )
    monkeypatch.setattr(precommit, "_run", lambda *_a, **_kw: (False, "E501 ..."))
    result = precommit.step_ruff(tmp_path)
    assert not result.passed
    assert "E501" in result.output


def test_step_docstrings_hard_fails_when_cli_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """step_docstrings exits 2 when verify-forge-docstrings is missing."""
    monkeypatch.setattr(precommit.shutil, "which", lambda _name: None)
    with pytest.raises(SystemExit) as exc_info:
        precommit.step_docstrings(tmp_path)
    assert exc_info.value.code == 2


def test_step_test_naming_hard_fails_when_cli_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """step_test_naming exits 2 when verify-forge-test-naming is missing."""
    monkeypatch.setattr(precommit.shutil, "which", lambda _name: None)
    with pytest.raises(SystemExit) as exc_info:
        precommit.step_test_naming(tmp_path)
    assert exc_info.value.code == 2


def test_step_docstring_coverage_hard_fails_when_cli_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """step_docstring_coverage exits 2 when its CLI is missing from PATH."""
    monkeypatch.setattr(precommit.shutil, "which", lambda _name: None)
    with pytest.raises(SystemExit) as exc_info:
        precommit.step_docstring_coverage(tmp_path)
    assert exc_info.value.code == 2


def test_step_repo_structure_skipped_without_repo_structure_md(
    tmp_path: Path,
) -> None:
    """step_repo_structure is skipped when REPO_STRUCTURE.md is absent."""
    result = precommit.step_repo_structure(tmp_path)
    assert result.skipped
    assert result.passed


def test_step_repo_structure_hard_fails_when_cli_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """step_repo_structure exits 2 when verify-forge-repo-structure is missing."""
    (tmp_path / "REPO_STRUCTURE.md").write_text("# Repo Structure\n")
    monkeypatch.setattr(precommit.shutil, "which", lambda _name: None)
    with pytest.raises(SystemExit) as exc_info:
        precommit.step_repo_structure(tmp_path)
    assert exc_info.value.code == 2


def test_step_commit_types_parity_skipped_when_hook_absent(
    tmp_path: Path,
) -> None:
    """step_commit_types_parity is skipped when the shell hook file is absent."""
    result = precommit.step_commit_types_parity(tmp_path)
    assert result.skipped
    assert result.passed


def test_step_commit_types_parity_hard_fails_when_cli_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """step_commit_types_parity exits 2 when forge-gen-commit-types is missing."""
    hooks_dir = tmp_path / "claude-hooks"
    hooks_dir.mkdir()
    (hooks_dir / "check_commit_format.sh").write_text("#!/usr/bin/env bash\n")
    monkeypatch.setattr(precommit.shutil, "which", lambda _name: None)
    with pytest.raises(SystemExit) as exc_info:
        precommit.step_commit_types_parity(tmp_path)
    assert exc_info.value.code == 2


def test_step_manifest_json_shells_out_to_verify_forge_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """step_manifest_json always shells out; the CLI owns the skip decision."""
    monkeypatch.setattr(
        precommit.shutil, "which", lambda _name: "/usr/bin/verify-forge-manifest"
    )
    calls: list[list[str]] = []

    def _fake_run(cmd: list[str], **_kwargs: object) -> tuple[bool, str]:
        calls.append(cmd)
        return True, "OK"

    monkeypatch.setattr(precommit, "_run", _fake_run)
    result = precommit.step_manifest_json(tmp_path)
    assert result.passed
    assert calls
    assert calls[0] == ["verify-forge-manifest"]


def test_step_manifest_json_marks_skipped_from_cli_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the CLI reports it skipped, the StepResult mirrors that."""
    monkeypatch.setattr(
        precommit.shutil, "which", lambda _name: "/usr/bin/verify-forge-manifest"
    )
    monkeypatch.setattr(
        precommit,
        "_run",
        lambda *_a, **_kw: (True, "(no .claude-plugin/ dir — skipped)\n"),
    )
    result = precommit.step_manifest_json(tmp_path)
    assert result.passed
    assert result.skipped


def test_step_plugin_version_shells_out_to_verify_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """step_plugin_version always shells out; the CLI owns the skip decision."""
    monkeypatch.setattr(
        precommit.shutil,
        "which",
        lambda _name: "/usr/bin/verify-forge-plugin-version",
    )
    calls: list[list[str]] = []

    def _fake_run(cmd: list[str], **_kwargs: object) -> tuple[bool, str]:
        calls.append(cmd)
        return True, "ok"

    monkeypatch.setattr(precommit, "_run", _fake_run)
    result = precommit.step_plugin_version(tmp_path)
    assert result.passed
    assert calls
    assert calls[0] == ["verify-forge-plugin-version"]


def test_step_plugin_version_marks_skipped_from_cli_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the CLI reports it skipped, the StepResult mirrors that."""
    monkeypatch.setattr(
        precommit.shutil,
        "which",
        lambda _name: "/usr/bin/verify-forge-plugin-version",
    )
    monkeypatch.setattr(
        precommit,
        "_run",
        lambda *_a, **_kw: (True, "(no git tags yet — skipped)\n"),
    )
    result = precommit.step_plugin_version(tmp_path)
    assert result.passed
    assert result.skipped


def _stub_docstrings_passing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub ``step_docstrings`` to skip ``verify-forge-docstrings`` check."""

    def _stub(_root: object) -> precommit.StepResult:
        return precommit.StepResult(
            name="docstring_verification",
            passed=True,
            output="(stubbed)",
            skipped=False,
        )

    monkeypatch.setattr(precommit, "step_docstrings", _stub)


def _stub_test_naming_passing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub ``step_test_naming`` to skip the ``verify-forge-test-naming`` call."""

    def _stub(_root: object) -> precommit.StepResult:
        return precommit.StepResult(
            name="test_naming_check",
            passed=True,
            output="(stubbed)",
            skipped=False,
        )

    monkeypatch.setattr(precommit, "step_test_naming", _stub)


def _stub_repo_structure_passing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub ``step_repo_structure`` to skip the repo-structure CLI call."""

    def _stub(_root: object) -> precommit.StepResult:
        return precommit.StepResult(
            name="repo_structure_check",
            passed=True,
            output="(stubbed)",
            skipped=False,
        )

    monkeypatch.setattr(precommit, "step_repo_structure", _stub)


def _stub_pip_audit_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub ``step_pip_audit`` to skip — tests must not hit the OSV network call."""

    def _stub(_root: object) -> precommit.StepResult:
        return precommit.StepResult(
            name="pip_audit",
            passed=True,
            output="(stubbed)",
            skipped=True,
        )

    monkeypatch.setattr(precommit, "step_pip_audit", _stub)


def _stub_docstring_coverage_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub ``step_docstring_coverage`` to skip — avoid the interrogate dependency."""

    def _stub(_root: object) -> precommit.StepResult:
        return precommit.StepResult(
            name="docstring_coverage",
            passed=True,
            output="(stubbed)",
            skipped=True,
            non_blocking=True,
        )

    monkeypatch.setattr(precommit, "step_docstring_coverage", _stub)


def test_run_all_writes_code_health_logs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run_all writes one log per step under code_health/."""
    _stub_docstrings_passing(monkeypatch)
    _stub_test_naming_passing(monkeypatch)
    _stub_repo_structure_passing(monkeypatch)
    _stub_pip_audit_skipped(monkeypatch)
    _stub_docstring_coverage_skipped(monkeypatch)
    precommit.run_all(repo_root=tmp_path, print_progress=False)
    log_dir = tmp_path / "code_health"
    assert log_dir.is_dir()
    expected = {
        "ruff.log",
        "docstring_verification.log",
        "docstring_coverage.log",
        "test_naming_check.log",
        "repo_structure_check.log",
        "manifest_json.log",
        "plugin_version.log",
        "pip_audit.log",
    }
    assert expected <= {p.name for p in log_dir.iterdir()}


def test_main_exit_code_zero_when_all_pass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """main() returns 0 when every step is skipped or passed."""
    monkeypatch.setattr(precommit, "get_repo_root", lambda: tmp_path)
    _stub_docstrings_passing(monkeypatch)
    _stub_test_naming_passing(monkeypatch)
    _stub_repo_structure_passing(monkeypatch)
    _stub_pip_audit_skipped(monkeypatch)
    _stub_docstring_coverage_skipped(monkeypatch)
    with patch.object(precommit.sys, "argv", ["forge-precommit"]):
        rc = precommit.main()
    assert rc == 0
    out = capsys.readouterr().out
    assert "All checks passed" in out


def test_main_emits_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--json emits a parseable list of step results without progress lines."""
    monkeypatch.setattr(precommit, "get_repo_root", lambda: tmp_path)
    _stub_docstrings_passing(monkeypatch)
    _stub_test_naming_passing(monkeypatch)
    _stub_repo_structure_passing(monkeypatch)
    _stub_pip_audit_skipped(monkeypatch)
    _stub_docstring_coverage_skipped(monkeypatch)
    with patch.object(precommit.sys, "argv", ["forge-precommit", "--json"]):
        precommit.main()
    parsed = json.loads(capsys.readouterr().out)
    assert isinstance(parsed, list)
    assert {r["name"] for r in parsed} >= {
        "ruff",
        "docstring_verification",
        "test_naming_check",
        "repo_structure_check",
        "manifest_json",
        "plugin_version",
        "pip_audit",
    }


def test_step_pip_audit_skipped_when_cli_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """step_pip_audit is skipped when ``pip-audit`` is not on PATH."""
    monkeypatch.setattr(precommit.shutil, "which", lambda _name: None)
    result = precommit.step_pip_audit(tmp_path)
    assert result.skipped
    assert result.passed


def test_count_pip_audit_advisories_counts_pysec_and_ghsa_ids() -> None:
    """_count_pip_audit_advisories tallies PYSEC and GHSA advisory IDs."""
    output = (
        "Name    Version  ID              Fix Versions\n"
        "------  -------- --------------- ------------\n"
        "pkg-a   1.0.0    PYSEC-2024-123  1.0.1\n"
        "pkg-b   2.0.0    GHSA-abcd-efgh-ijkl  2.0.1\n"
        "pkg-c   3.0.0    PYSEC-2025-7    3.0.1\n"
    )
    assert precommit._count_pip_audit_advisories(output) == 3


def test_step_pip_audit_below_threshold_emits_no_banner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Residual count at or under the threshold leaves the output unchanged."""
    monkeypatch.setattr(precommit.shutil, "which", lambda _name: "/fake/pip-audit")
    fake_output = "pkg-a 1.0.0 PYSEC-2024-1\npkg-b 2.0.0 PYSEC-2024-2\n"
    monkeypatch.setattr(precommit, "_run", lambda *_a, **_kw: (False, fake_output))
    result = precommit.step_pip_audit(tmp_path)
    assert result.non_blocking
    assert not result.passed
    assert "⚠️" not in result.output
    assert result.output == fake_output


def test_step_pip_audit_at_threshold_boundary_emits_no_banner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Count exactly at the threshold (not strictly greater) leaves output unbannered.

    Documents the strict-greater-than semantics of the escalation
    check; a regression to `>=` would surface here.
    """
    monkeypatch.setattr(precommit.shutil, "which", lambda _name: "/fake/pip-audit")
    at = precommit._PIP_AUDIT_LOUDNESS_THRESHOLD
    fake_output = "".join(f"pkg-{i} 1.0.0 PYSEC-2024-{i}\n" for i in range(at))
    monkeypatch.setattr(precommit, "_run", lambda *_a, **_kw: (False, fake_output))
    result = precommit.step_pip_audit(tmp_path)
    assert "⚠️" not in result.output


def test_step_pip_audit_above_threshold_prepends_loud_banner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Residual count above the threshold prefixes a loud nudge banner.

    The banner names the count and references the threshold so a
    contributor reading the WARN line knows whether the count is in
    the "single-PR drift" range or the "accumulated tech-debt" range.
    """
    monkeypatch.setattr(precommit.shutil, "which", lambda _name: "/fake/pip-audit")
    over = precommit._PIP_AUDIT_LOUDNESS_THRESHOLD + 5
    fake_output = "".join(f"pkg-{i} 1.0.0 PYSEC-2024-{i}\n" for i in range(over))
    monkeypatch.setattr(precommit, "_run", lambda *_a, **_kw: (False, fake_output))
    result = precommit.step_pip_audit(tmp_path)
    assert result.non_blocking
    assert "⚠️" in result.output
    assert str(over) in result.output
    assert "Consider filing a tracking issue" in result.output


def test_step_pip_audit_passing_run_emits_no_banner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A clean pip-audit (no findings) does NOT trigger the banner code path."""
    monkeypatch.setattr(precommit.shutil, "which", lambda _name: "/fake/pip-audit")
    monkeypatch.setattr(
        precommit, "_run", lambda *_a, **_kw: (True, "No known vulnerabilities found")
    )
    result = precommit.step_pip_audit(tmp_path)
    assert result.passed
    assert "⚠️" not in result.output


def test_non_blocking_warning_does_not_fail_main(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A non_blocking step that fails reports WARN but main() returns 0."""
    monkeypatch.setattr(precommit, "get_repo_root", lambda: tmp_path)
    _stub_docstrings_passing(monkeypatch)
    _stub_test_naming_passing(monkeypatch)
    _stub_repo_structure_passing(monkeypatch)

    def _failing_non_blocking(_root: object) -> precommit.StepResult:
        return precommit.StepResult(
            name="pip_audit",
            passed=False,
            output="(simulated CVE finding)",
            non_blocking=True,
        )

    monkeypatch.setattr(precommit, "step_pip_audit", _failing_non_blocking)
    with patch.object(precommit.sys, "argv", ["forge-precommit"]):
        rc = precommit.main()
    assert rc == 0
    out = capsys.readouterr().out
    assert "WARN" in out
    assert "All blocking checks passed" in out
    # Non-blocking failure should name the step + point at its log
    assert "pip_audit: see code_health/pip_audit.log" in out


def test_main_lists_failed_steps_with_log_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """On blocking failure, the summary names every failed step + its log."""
    monkeypatch.setattr(precommit, "get_repo_root", lambda: tmp_path)
    _stub_repo_structure_passing(monkeypatch)
    _stub_pip_audit_skipped(monkeypatch)
    _stub_docstring_coverage_skipped(monkeypatch)

    def _failing_docstrings(_root: object) -> precommit.StepResult:
        return precommit.StepResult(
            name="docstring_verification",
            passed=False,
            output="(simulated docstring error)",
        )

    def _failing_test_naming(_root: object) -> precommit.StepResult:
        return precommit.StepResult(
            name="test_naming_check",
            passed=False,
            output="(simulated naming violation)",
        )

    monkeypatch.setattr(precommit, "step_docstrings", _failing_docstrings)
    monkeypatch.setattr(precommit, "step_test_naming", _failing_test_naming)
    with patch.object(precommit.sys, "argv", ["forge-precommit"]):
        rc = precommit.main()
    assert rc == 1
    out = capsys.readouterr().out
    # Header
    assert "Pre-commit checks failed:" in out
    # Each failed step listed with its log path
    assert "docstring_verification: see code_health/docstring_verification.log" in out
    assert "test_naming_check: see code_health/test_naming_check.log" in out
