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
    """run_all writes one log per step under code_health/.

    SCENARIO: run_all executes the full step sequence end-to-end and
    must persist each step's output to its own log file.
    MOCK SETUP: swaps step_docstrings, step_test_naming,
    step_repo_structure, step_pip_audit, and step_docstring_coverage
    with `_stub_*` helpers so no real CLI / network call fires; ruff,
    manifest_json, and plugin_version run their real shell-out path
    against the empty tmp_path repo (which short-circuits to skip).
    EXPECTED BEHAVIOR: code_health/ exists and contains a log file for
    every step in the sequence.
    """
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
    """main() returns 0 when every step is skipped or passed.

    SCENARIO: a clean repo where no check has anything to report —
    main() must exit 0 and print the all-clear summary.
    MOCK SETUP: get_repo_root is pinned to tmp_path; step_docstrings,
    step_test_naming, step_repo_structure, step_pip_audit, and
    step_docstring_coverage are stubbed to pass/skip; sys.argv is
    patched to the bare `forge-precommit` invocation to drive main().
    EXPECTED BEHAVIOR: main() returns 0 and stdout reports "All checks
    passed".
    """
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
    """--json emits a parseable list of step results without progress lines.

    SCENARIO: a tool consuming forge-precommit programmatically passes
    --json and must receive machine-readable results, not the human
    progress banners.
    MOCK SETUP: get_repo_root is pinned to tmp_path; step_docstrings,
    step_test_naming, step_repo_structure, step_pip_audit, and
    step_docstring_coverage are stubbed to pass/skip; sys.argv is
    patched to `forge-precommit --json`.
    EXPECTED BEHAVIOR: stdout parses as a JSON list whose step names
    cover the full sequence.
    """
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
    """A non_blocking step that fails reports WARN but main() returns 0.

    SCENARIO: an advisory (non-blocking) step fails — main() must
    surface it as a WARN without flipping the overall exit code.
    MOCK SETUP: get_repo_root is pinned to tmp_path; step_docstrings,
    step_test_naming, step_repo_structure, and step_docstring_coverage
    are stubbed to pass/skip; step_pip_audit is replaced with a failing
    non_blocking StepResult; sys.argv is patched to the bare
    `forge-precommit` invocation.
    EXPECTED BEHAVIOR: main() returns 0; stdout prints WARN, the
    all-blocking-passed summary, and names pip_audit with its log path.
    """
    monkeypatch.setattr(precommit, "get_repo_root", lambda: tmp_path)
    _stub_docstrings_passing(monkeypatch)
    _stub_test_naming_passing(monkeypatch)
    _stub_repo_structure_passing(monkeypatch)
    _stub_docstring_coverage_skipped(monkeypatch)

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
    """On blocking failure, the summary names every failed step + its log.

    SCENARIO: two blocking steps fail in the same run — the failure
    summary must enumerate each one with a pointer to its log.
    MOCK SETUP: get_repo_root is pinned to tmp_path; step_repo_structure,
    step_pip_audit, and step_docstring_coverage are stubbed to pass/skip;
    step_docstrings and step_test_naming are replaced with failing
    StepResults; sys.argv is patched to the bare `forge-precommit`
    invocation.
    EXPECTED BEHAVIOR: main() returns 1; stdout prints the failure
    header and one "<step>: see code_health/<step>.log" line per
    failed step.
    """
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


# ---------------------------------------------------------------------------
# Step framework: registry, resolution, CLI overrides (#6)
# ---------------------------------------------------------------------------


def _write_pyproject(tmp_path: Path, body: str) -> None:
    """Write *body* as ``pyproject.toml`` in *tmp_path* (config-test helper).

    Args:
        tmp_path: Temporary directory path.
        body: TOML content to write.
    """
    (tmp_path / "pyproject.toml").write_text(body, encoding="utf-8")


def _names(step_defs: list[precommit.StepDef]) -> list[str]:
    """Return the names of resolved ``StepDef`` entries (readability helper).

    Args:
        step_defs: List of step definitions.

    Returns:
        List of step names extracted from the definitions.
    """
    return [d.name for d in step_defs]


def _present(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every binary resolve on PATH (so ``require_cli`` passes)."""
    monkeypatch.setattr(precommit.shutil, "which", lambda name: f"/usr/bin/{name}")


def test_forge_step_config_reads_section(tmp_path: Path) -> None:
    """`_forge_step_config` returns the `[tool.forge.<step>]` table."""
    _write_pyproject(tmp_path, '[tool.forge.doctest]\npaths = ["lib"]\n')
    assert precommit._forge_step_config(tmp_path, "doctest") == {"paths": ["lib"]}


def test_forge_step_config_missing_returns_empty(tmp_path: Path) -> None:
    """`_forge_step_config` returns `{}` when the section or pyproject is absent."""
    assert precommit._forge_step_config(tmp_path, "doctest") == {}
    _write_pyproject(tmp_path, "[tool.forge]\n")
    assert precommit._forge_step_config(tmp_path, "doctest") == {}


def test_validate_step_names_accepts_known() -> None:
    """`_validate_step_names` is silent for registered step names."""
    precommit._validate_step_names(["ruff", "doctest", "pip_audit"])


def test_validate_step_names_rejects_unknown() -> None:
    """`_validate_step_names` raises ValueError naming the offender and valid set."""
    with pytest.raises(ValueError, match="unknown step name") as exc:
        precommit._validate_step_names(["ruff", "nope"])
    assert "nope" in str(exc.value)
    assert "ruff" in str(exc.value)


def test_resolve_steps_default_excludes_opt_in(tmp_path: Path) -> None:
    """The default run set is the default-on steps; opt-in steps stay out."""
    names = _names(precommit._resolve_steps(tmp_path))
    assert "ruff" in names
    assert "doctest" not in names
    assert "typecheck" not in names
    assert "doc_consistency" not in names


def test_resolve_steps_enable_adds_opt_in(tmp_path: Path) -> None:
    """`[tool.forge.precommit] enable` opts a normally-off step in."""
    _write_pyproject(tmp_path, '[tool.forge.precommit]\nenable = ["doctest"]\n')
    assert "doctest" in _names(precommit._resolve_steps(tmp_path))


def test_resolve_steps_disable_removes_default(tmp_path: Path) -> None:
    """`[tool.forge.precommit] disable` force-skips a default step."""
    _write_pyproject(tmp_path, '[tool.forge.precommit]\ndisable = ["pip_audit"]\n')
    assert "pip_audit" not in _names(precommit._resolve_steps(tmp_path))


def test_resolve_steps_disable_beats_enable(tmp_path: Path) -> None:
    """When a name is in both `enable` and `disable`, `disable` wins."""
    _write_pyproject(
        tmp_path,
        '[tool.forge.precommit]\nenable = ["doctest"]\ndisable = ["doctest"]\n',
    )
    assert "doctest" not in _names(precommit._resolve_steps(tmp_path))


def test_resolve_steps_skip_removes(tmp_path: Path) -> None:
    """The `skip` argument removes a step for this run only."""
    assert "ruff" not in _names(precommit._resolve_steps(tmp_path, skip=["ruff"]))


def test_resolve_steps_only_overrides_in_registry_order(tmp_path: Path) -> None:
    """`only=[...]` runs exactly those steps, ordered by the registry not the arg."""
    resolved = _names(precommit._resolve_steps(tmp_path, only=["pip_audit", "ruff"]))
    assert resolved == ["ruff", "pip_audit"]


def test_resolve_steps_unknown_name_raises(tmp_path: Path) -> None:
    """An unknown name in config / skip / only raises ValueError."""
    with pytest.raises(ValueError, match="unknown step name"):
        precommit._resolve_steps(tmp_path, only=["bogus"])


def test_split_csv_flattens_repeats_and_commas() -> None:
    """`_split_csv` flattens repeated and comma-separated values, dropping blanks."""
    assert precommit._split_csv(["a,b", "c"]) == ["a", "b", "c"]
    assert precommit._split_csv(["a, ,b"]) == ["a", "b"]
    assert precommit._split_csv([]) == []


def test_run_all_only_dispatches_monkeypatched_steps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run_all(only=...) runs exactly the named steps via the module dispatch.

    MOCK SETUP: ``step_ruff`` and ``step_pip_audit`` are replaced with
    canned passing stubs; run_all is called with ``only`` those two.
    EXPECTED BEHAVIOR: both stubs run (proving the registry resolves the
    monkeypatched functions, not its captured references) and no other
    step executes.
    """

    def _ruff(_root: object) -> precommit.StepResult:
        return precommit.StepResult(name="ruff", passed=True, output="x")

    def _audit(_root: object) -> precommit.StepResult:
        return precommit.StepResult(name="pip_audit", passed=True, output="x")

    monkeypatch.setattr(precommit, "step_ruff", _ruff)
    monkeypatch.setattr(precommit, "step_pip_audit", _audit)
    results = precommit.run_all(
        tmp_path, print_progress=False, only=["ruff", "pip_audit"]
    )
    assert [r.name for r in results] == ["ruff", "pip_audit"]


def test_main_only_flag_runs_subset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`--only ruff --json` runs just ruff and emits a one-entry JSON list.

    MOCK SETUP: get_repo_root pinned to tmp_path; ``step_ruff`` stubbed to
    pass; argv drives main() with ``--only ruff --json``.
    """
    monkeypatch.setattr(precommit, "get_repo_root", lambda: tmp_path)

    def _ruff(_root: object) -> precommit.StepResult:
        return precommit.StepResult(name="ruff", passed=True, output="x")

    monkeypatch.setattr(precommit, "step_ruff", _ruff)
    with patch.object(
        precommit.sys, "argv", ["forge-precommit", "--only", "ruff", "--json"]
    ):
        rc = precommit.main()
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert [r["name"] for r in data] == ["ruff"]


def test_main_unknown_step_name_exits_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An unknown `--skip` name prints a clean error and exits 1 (no traceback)."""
    monkeypatch.setattr(precommit, "get_repo_root", lambda: tmp_path)
    with patch.object(precommit.sys, "argv", ["forge-precommit", "--skip", "bogus"]):
        rc = precommit.main()
    assert rc == 1
    assert "unknown step name" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Opt-in steps: doctest (#5), typecheck (#48), doc_consistency (#4)
# ---------------------------------------------------------------------------


def test_step_doctest_passes_non_blocking_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Doctest passes as non-blocking when `blocking` is unset.

    MOCK SETUP: pytest present on PATH; ``_run`` returns a passing canned
    result; no ``[tool.forge.doctest]`` config so defaults apply.
    """
    _present(monkeypatch)
    monkeypatch.setattr(precommit, "_run", lambda _cmd, **_kw: (True, "3 passed"))
    result = precommit.step_doctest(tmp_path)
    assert result.passed
    assert result.non_blocking
    assert not result.skipped


def test_step_doctest_uses_configured_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Doctest runs `pytest --doctest-modules` over `[tool.forge.doctest].paths`."""
    _present(monkeypatch)
    _write_pyproject(tmp_path, '[tool.forge.doctest]\npaths = ["lib", "app"]\n')
    captured: dict[str, list[str]] = {}

    def _run(cmd: list[str], **_kw: object) -> tuple[bool, str]:
        captured["cmd"] = cmd
        return True, "1 passed"

    monkeypatch.setattr(precommit, "_run", _run)
    precommit.step_doctest(tmp_path)
    assert "--doctest-modules" in captured["cmd"]
    assert captured["cmd"][-2:] == ["lib", "app"]


def test_step_doctest_no_examples_is_skipped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pytest 'no tests ran' (exit 5) counts as a skip, not a failure."""
    _present(monkeypatch)
    monkeypatch.setattr(
        precommit, "_run", lambda _cmd, **_kw: (False, "no tests ran in 0.01s")
    )
    result = precommit.step_doctest(tmp_path)
    assert result.skipped
    assert result.passed


def test_step_doctest_blocking_config_is_blocking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`blocking = true` makes a failing doctest a blocking failure."""
    _present(monkeypatch)
    _write_pyproject(tmp_path, "[tool.forge.doctest]\nblocking = true\n")
    monkeypatch.setattr(precommit, "_run", lambda _cmd, **_kw: (False, "1 failed"))
    result = precommit.step_doctest(tmp_path)
    assert not result.passed
    assert not result.non_blocking


def test_step_doctest_missing_pytest_exits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Doctest fails loudly (SystemExit) when pytest is not on PATH."""
    monkeypatch.setattr(precommit.shutil, "which", lambda _name: None)
    with pytest.raises(SystemExit):
        precommit.step_doctest(tmp_path)


def test_step_typecheck_default_pyrefly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Typecheck defaults to the `pyrefly check` command and is non-blocking.

    MOCK SETUP: pyrefly present on PATH; ``_run`` captures the command and
    returns a passing result; no config so the default checker applies.
    """
    _present(monkeypatch)
    captured: dict[str, list[str]] = {}

    def _run(cmd: list[str], **_kw: object) -> tuple[bool, str]:
        captured["cmd"] = cmd
        return True, "0 errors"

    monkeypatch.setattr(precommit, "_run", _run)
    result = precommit.step_typecheck(tmp_path)
    assert captured["cmd"][:2] == ["pyrefly", "check"]
    assert result.passed
    assert result.non_blocking


def test_step_typecheck_none_skips(tmp_path: Path) -> None:
    """`checker = "none"` skips the step without invoking any tool."""
    _write_pyproject(tmp_path, '[tool.forge.typecheck]\nchecker = "none"\n')
    result = precommit.step_typecheck(tmp_path)
    assert result.skipped
    assert result.passed


def test_step_typecheck_unknown_checker_fails(tmp_path: Path) -> None:
    """An unrecognized checker name fails with a message listing valid names."""
    _write_pyproject(tmp_path, '[tool.forge.typecheck]\nchecker = "bogus"\n')
    result = precommit.step_typecheck(tmp_path)
    assert not result.passed
    assert "pyrefly" in result.output


def test_step_typecheck_missing_binary_exits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A configured-but-absent checker binary fails loudly (SystemExit)."""
    monkeypatch.setattr(precommit.shutil, "which", lambda _name: None)
    with pytest.raises(SystemExit):
        precommit.step_typecheck(tmp_path)


def test_step_typecheck_blocking_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`blocking = true` makes a checker error a blocking failure."""
    _present(monkeypatch)
    _write_pyproject(
        tmp_path, '[tool.forge.typecheck]\nchecker = "mypy"\nblocking = true\n'
    )
    monkeypatch.setattr(precommit, "_run", lambda _cmd, **_kw: (False, "error: x"))
    result = precommit.step_typecheck(tmp_path)
    assert not result.passed
    assert not result.non_blocking


def test_step_doc_consistency_non_blocking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """doc_consistency mirrors the CLI exit and is always non-blocking."""
    _present(monkeypatch)
    monkeypatch.setattr(precommit, "_run", lambda _cmd, **_kw: (False, "drift"))
    result = precommit.step_doc_consistency(tmp_path)
    assert not result.passed
    assert result.non_blocking


def test_step_doc_consistency_missing_cli_exits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """doc_consistency fails loudly when its CLI is not on PATH."""
    monkeypatch.setattr(precommit.shutil, "which", lambda _name: None)
    with pytest.raises(SystemExit):
        precommit.step_doc_consistency(tmp_path)
