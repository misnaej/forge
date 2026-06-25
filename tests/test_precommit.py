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
import shutil
from typing import TYPE_CHECKING, NamedTuple
from unittest.mock import patch

import pytest

from forge import precommit
from forge.pip_audit_json import AuditRun


if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Null Objects — reused across env_sync test groups
# ---------------------------------------------------------------------------


class FakeEP(NamedTuple):
    """Null-object entry point for _installed_console_scripts tests.

    Attributes:
        name: Entry-point name (e.g. ``"mycli"``).
        group: Entry-point group (e.g. ``"console_scripts"``).
    """

    name: str
    group: str


class FakeDist:
    """Null-object distribution for _installed_console_scripts tests.

    Attributes:
        entry_points: Fake list of entry points supplied at construction.
    """

    def __init__(self, eps: list[FakeEP]) -> None:
        """Store fake entry points.

        Args:
            eps: List of fake entry points to expose as ``entry_points``.
        """
        self.entry_points: list[FakeEP] = eps


def _write_precommit_cfg(repo_root: Path, body: str) -> None:
    """Write a ``[tool.forge.precommit]`` block to a tmp pyproject.toml.

    Args:
        repo_root: Directory to drop ``pyproject.toml`` in.
        body: TOML lines placed under ``[tool.forge.precommit]``.
    """
    (repo_root / "pyproject.toml").write_text(
        f"[tool.forge.precommit]\n{body}\n", encoding="utf-8"
    )


def _audit_run(n_vulns: int) -> AuditRun:
    """Return a fake AuditRun with n_vulns PYSEC findings for pip_audit step tests.

    Args:
        n_vulns: Number of vulnerabilities to include.

    Returns:
        An AuditRun with parseable data containing n_vulns findings, empty
        stderr, and returncode 1 when n_vulns > 0 or 0 when clean.
    """
    data: dict = {
        "dependencies": [
            {
                "name": f"pkg{i}",
                "version": "1.0",
                "vulns": [
                    {
                        "id": f"PYSEC-2024-{i}",
                        "aliases": [f"CVE-2024-{i}"],
                        "fix_versions": ["1.1"],
                        "description": "desc",
                    }
                ],
            }
            for i in range(n_vulns)
        ]
    }
    return AuditRun(data=data, stderr="", returncode=1 if n_vulns else 0)


def _write_project_scripts_pyproject(
    repo_root: Path,
    name: str,
    scripts: dict[str, str],
    *,
    env_sync_blocking: bool | None = None,
) -> None:
    """Write a [project] + [project.scripts] pyproject.toml for env_sync tests.

    Args:
        repo_root: Directory to drop ``pyproject.toml`` in.
        name: Package name for the ``[project] name`` key.
        scripts: Dict whose keys become ``[project.scripts]`` entry names;
            values are ignored (only the key set matters for _declared_scripts).
        env_sync_blocking: When not None, appends
            ``[tool.forge.env_sync] blocking = true/false``.
    """
    parts = [f'[project]\nname = "{name}"\n\n[project.scripts]\n']
    parts.extend(f'{script_name} = "pkg:main"\n' for script_name in scripts)
    if env_sync_blocking is not None:
        val = "true" if env_sync_blocking else "false"
        parts.append(f"\n[tool.forge.env_sync]\nblocking = {val}\n")
    (repo_root / "pyproject.toml").write_text("".join(parts), encoding="utf-8")


def test_resolve_scope_defaults_to_all(tmp_path: Path) -> None:
    """With no config, every step resolves to whole-tree 'all' scope."""
    assert precommit._resolve_scope(tmp_path, "ruff") == "all"
    assert precommit._resolve_scope(tmp_path, "docstring_verification") == "all"


def test_resolve_scope_global_key(tmp_path: Path) -> None:
    """A global `scope = "diff"` applies to every scope-aware step."""
    _write_precommit_cfg(tmp_path, 'scope = "diff"')
    assert precommit._resolve_scope(tmp_path, "ruff") == "diff"
    assert precommit._resolve_scope(tmp_path, "test_naming_check") == "diff"


def test_resolve_scope_per_step_override_wins(tmp_path: Path) -> None:
    """A per-step override beats the global default."""
    _write_precommit_cfg(
        tmp_path, 'scope = "all"\n[tool.forge.precommit.scope_overrides]\nruff = "diff"'
    )
    assert precommit._resolve_scope(tmp_path, "ruff") == "diff"
    assert precommit._resolve_scope(tmp_path, "docstring_verification") == "all"


def test_resolve_scope_invalid_falls_back_to_all(tmp_path: Path) -> None:
    """An unrecognised scope value degrades to 'all', never raises."""
    _write_precommit_cfg(tmp_path, 'scope = "nonsense"')
    assert precommit._resolve_scope(tmp_path, "ruff") == "all"


def test_scope_aware_steps_forward_resolved_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """docstring/test-naming steps forward `--scope diff` from config."""
    _write_precommit_cfg(tmp_path, 'scope = "diff"')
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/x")
    calls: list[list[str]] = []

    def _fake_run(cmd: list[str], **_kwargs: object) -> tuple[bool, str]:
        calls.append(cmd)
        return True, ""

    monkeypatch.setattr(precommit, "_run", _fake_run)
    precommit.step_docstrings(tmp_path)
    precommit.step_test_naming(tmp_path)
    assert calls[0] == ["verify-forge-docstrings", "--scope", "diff"]
    assert calls[1] == ["verify-forge-test-naming", "--scope", "diff"]


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
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    with pytest.raises(SystemExit) as exc_info:
        precommit.step_ruff(tmp_path)
    assert exc_info.value.code == 2


def test_step_ruff_shells_out_to_fix_forge_ruff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """step_ruff delegates to the fix-forge-ruff CLI with the resolved scope."""
    (tmp_path / "src").mkdir()
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/fix-forge-ruff")
    calls: list[list[str]] = []

    def _fake_run(cmd: list[str], **_kwargs: object) -> tuple[bool, str]:
        calls.append(cmd)
        return True, "ruff output"

    monkeypatch.setattr(precommit, "_run", _fake_run)
    result = precommit.step_ruff(tmp_path)
    assert result.passed
    assert calls
    assert calls[0] == ["fix-forge-ruff", "--scope", "all"]


def test_step_ruff_propagates_nonzero_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When fix-forge-ruff exits non-zero, step_ruff fails."""
    (tmp_path / "src").mkdir()
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/fix-forge-ruff")
    monkeypatch.setattr(precommit, "_run", lambda *_a, **_kw: (False, "E501 ..."))
    result = precommit.step_ruff(tmp_path)
    assert not result.passed
    assert "E501" in result.output


def test_step_docstrings_hard_fails_when_cli_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """step_docstrings exits 2 when verify-forge-docstrings is missing."""
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    with pytest.raises(SystemExit) as exc_info:
        precommit.step_docstrings(tmp_path)
    assert exc_info.value.code == 2


def test_step_test_naming_hard_fails_when_cli_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """step_test_naming exits 2 when verify-forge-test-naming is missing."""
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    with pytest.raises(SystemExit) as exc_info:
        precommit.step_test_naming(tmp_path)
    assert exc_info.value.code == 2


def test_step_docstring_coverage_hard_fails_when_cli_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """step_docstring_coverage exits 2 when its CLI is missing from PATH."""
    monkeypatch.setattr(shutil, "which", lambda _name: None)
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
    monkeypatch.setattr(shutil, "which", lambda _name: None)
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
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    with pytest.raises(SystemExit) as exc_info:
        precommit.step_commit_types_parity(tmp_path)
    assert exc_info.value.code == 2


def test_step_manifest_json_shells_out_to_verify_forge_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """step_manifest_json always shells out; the CLI owns the skip decision."""
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/verify-forge-manifest")
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
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/verify-forge-manifest")
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
        shutil,
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
        shutil,
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


def _setup_release_guard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    plugin_version: str | None,
    latest_tag: str | None,
    dual_track: bool = True,
) -> None:
    """Stage a repo for step_release_tag_guard: dual-track + plugin.json + tag.

    Args:
        tmp_path: Repo root.
        monkeypatch: pytest fixture.
        plugin_version: Version to write into ``.claude-plugin/plugin.json``;
            ``None`` writes no manifest.
        latest_tag: Tag ``latest_v_tag`` is stubbed to return (e.g.
            ``"v1.24.1"``); ``None`` stubs no tags.
        dual_track: When ``True`` (default), writes ``dev_branch = "dev"`` so
            the guard treats the repo as dual-track; ``False`` leaves it
            single-track.
    """
    body = '[tool.forge]\ndev_branch = "dev"\n' if dual_track else "[tool.forge]\n"
    (tmp_path / "pyproject.toml").write_text(body, encoding="utf-8")
    if plugin_version is not None:
        manifest = tmp_path / ".claude-plugin" / "plugin.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(f'{{"version": "{plugin_version}"}}', encoding="utf-8")
    monkeypatch.setattr(precommit, "latest_v_tag", lambda _root: latest_tag)


def test_release_guard_passes_when_one_minor_ahead(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """plugin.json one minor ahead of the latest tag is the normal case."""
    _setup_release_guard(
        tmp_path, monkeypatch, plugin_version="1.25.0", latest_tag="v1.24.1"
    )
    result = precommit.step_release_tag_guard(tmp_path)
    assert result.passed
    assert not result.skipped


def test_release_guard_passes_when_one_patch_ahead(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A single patch bump ahead of the latest tag is allowed."""
    _setup_release_guard(
        tmp_path, monkeypatch, plugin_version="1.24.2", latest_tag="v1.24.1"
    )
    assert precommit.step_release_tag_guard(tmp_path).passed


def test_release_guard_blocks_on_skipped_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A two-minor gap (an intermediate release never tagged) is blocked (#66)."""
    _setup_release_guard(
        tmp_path, monkeypatch, plugin_version="1.26.0", latest_tag="v1.24.1"
    )
    result = precommit.step_release_tag_guard(tmp_path)
    assert not result.passed
    assert not result.skipped
    assert "forge-next-prep --tag" in result.output


def test_release_guard_skips_single_track(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A single-track repo never triggers the dev-tagging cadence guard."""
    _setup_release_guard(
        tmp_path,
        monkeypatch,
        plugin_version="1.26.0",
        latest_tag="v1.24.1",
        dual_track=False,
    )
    result = precommit.step_release_tag_guard(tmp_path)
    assert result.passed
    assert result.skipped


def test_release_guard_skips_when_not_ahead(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """plugin.json equal to the latest tag (reproduced release) → skip."""
    _setup_release_guard(
        tmp_path, monkeypatch, plugin_version="2.0.0", latest_tag="v2.0.0"
    )
    result = precommit.step_release_tag_guard(tmp_path)
    assert result.passed
    assert result.skipped


def test_release_guard_skips_without_plugin_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No .claude-plugin/plugin.json → nothing to guard, skip."""
    _setup_release_guard(
        tmp_path, monkeypatch, plugin_version=None, latest_tag="v1.24.1"
    )
    result = precommit.step_release_tag_guard(tmp_path)
    assert result.passed
    assert result.skipped


def test_release_guard_skips_on_non_semver_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unparseable plugin.json version degrades to skip, never raises."""
    _setup_release_guard(
        tmp_path, monkeypatch, plugin_version="rolling", latest_tag="v1.24.1"
    )
    result = precommit.step_release_tag_guard(tmp_path)
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


def _stub_env_sync_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub ``step_env_sync`` to skip in integration tests."""

    def _stub(_root: object) -> precommit.StepResult:
        return precommit.StepResult(
            name="env_sync",
            passed=True,
            output="(stubbed)",
            skipped=True,
        )

    monkeypatch.setattr(precommit, "step_env_sync", _stub)


def test_run_all_writes_code_health_logs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run_all writes one log per step under code_health/.

    SCENARIO: run_all executes the full step sequence end-to-end and
    must persist each step's output to its own log file.
    MOCK SETUP: swaps step_env_sync, step_docstrings, step_test_naming,
    step_repo_structure, step_pip_audit, and step_docstring_coverage
    with `_stub_*` helpers so no real CLI / network call fires; ruff,
    manifest_json, and plugin_version run their real shell-out path
    against the empty tmp_path repo (which short-circuits to skip).
    EXPECTED BEHAVIOR: code_health/ exists and contains a log file for
    every step in the sequence.
    """
    _stub_env_sync_skipped(monkeypatch)
    _stub_docstrings_passing(monkeypatch)
    _stub_test_naming_passing(monkeypatch)
    _stub_repo_structure_passing(monkeypatch)
    _stub_pip_audit_skipped(monkeypatch)
    _stub_docstring_coverage_skipped(monkeypatch)
    precommit.run_all(repo_root=tmp_path, print_progress=False)
    log_dir = tmp_path / "code_health"
    assert log_dir.is_dir()
    expected = {
        "env_sync.log",
        "ruff.log",
        "docstring_verification.log",
        "docstring_coverage.log",
        "test_naming_check.log",
        "repo_structure_check.log",
        "manifest_json.log",
        "plugin_version.log",
        "pip_audit.log",
        "cve_usage.log",
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


def test_step_pip_audit_loud_warn_when_cli_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing pip-audit is a loud non-blocking WARN, never a silent skip.

    pip-audit ships as a core dependency (#71); a missing binary means a
    broken install, and a security gate that quietly no-ops gives false
    assurance — so the step surfaces it visibly without refusing the commit.

    SCENARIO: run_json returns None — the missing-binary sentinel.
    MOCK SETUP: precommit.pip_audit_json.run_json → None.
    EXPECTED BEHAVIOR: not skipped, not passed, non_blocking, "did NOT run" in output.
    """
    monkeypatch.setattr(precommit.pip_audit_json, "run_json", lambda _root: None)
    result = precommit.step_pip_audit(tmp_path)
    assert not result.skipped
    assert not result.passed
    assert result.non_blocking
    assert "did NOT run" in result.output


def test_step_pip_audit_non_blocking_by_default_on_findings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CVE findings render as a non-blocking WARN when no blocking opt-in.

    SCENARIO: pip-audit present, finds 1 CVE, repo has no
        ``[tool.forge.pip_audit].blocking`` key.
    MOCK SETUP: precommit.pip_audit_json.run_json → _audit_run(1) (1 finding,
        parseable data, returncode 1).
    EXPECTED BEHAVIOR: ``passed=False`` but ``non_blocking=True`` (WARN).
    """
    monkeypatch.setattr(
        precommit.pip_audit_json, "run_json", lambda _root: _audit_run(1)
    )
    result = precommit.step_pip_audit(tmp_path)
    assert not result.passed
    assert result.non_blocking


def test_step_pip_audit_blocking_when_opted_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``[tool.forge.pip_audit].blocking = true`` makes a CVE finding a hard FAIL.

    SCENARIO: same finding as the default case, but the repo opts into
        blocking via ``[tool.forge.pip_audit]``.
    MOCK SETUP: precommit.pip_audit_json.run_json → _audit_run(1); a
        ``pyproject.toml`` carrying the blocking key in ``tmp_path``.
    EXPECTED BEHAVIOR: ``passed=False`` AND ``non_blocking=False`` (FAIL).
    """
    (tmp_path / "pyproject.toml").write_text(
        "[tool.forge.pip_audit]\nblocking = true\n"
    )
    monkeypatch.setattr(
        precommit.pip_audit_json, "run_json", lambda _root: _audit_run(1)
    )
    result = precommit.step_pip_audit(tmp_path)
    assert not result.passed
    assert not result.non_blocking


def test_step_cve_usage_skips_without_pattern_file(tmp_path: Path) -> None:
    """cve_usage self-skips (opt-in by presence) when no pattern map exists."""
    result = precommit.step_cve_usage(tmp_path)
    assert result.skipped
    assert result.passed
    assert "skipped" in result.output


def test_step_cve_usage_non_blocking_warn_on_findings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A finding (CLI exit 1) is a non-blocking WARN, mirroring pip_audit."""
    (tmp_path / "cve_usage_patterns.toml").write_text("['CVE-1']\npackage='x'\n")
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/x")
    monkeypatch.setattr(precommit, "_run", lambda *_a, **_kw: (False, "1 finding"))
    result = precommit.step_cve_usage(tmp_path)
    assert not result.passed
    assert result.non_blocking
    assert not result.skipped


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
    """Residual count at or under the threshold leaves the output without a banner."""
    monkeypatch.setattr(
        precommit.pip_audit_json, "run_json", lambda _root: _audit_run(2)
    )
    result = precommit.step_pip_audit(tmp_path)
    assert result.non_blocking
    assert not result.passed
    assert "⚠️" not in result.output


def test_step_pip_audit_at_threshold_boundary_emits_no_banner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Count exactly at the threshold (not strictly greater) leaves output unbannered.

    Documents the strict-greater-than semantics of the escalation
    check; a regression to `>=` would surface here.
    """
    at = precommit._PIP_AUDIT_LOUDNESS_THRESHOLD
    monkeypatch.setattr(
        precommit.pip_audit_json, "run_json", lambda _root: _audit_run(at)
    )
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
    over = precommit._PIP_AUDIT_LOUDNESS_THRESHOLD + 5
    monkeypatch.setattr(
        precommit.pip_audit_json, "run_json", lambda _root: _audit_run(over)
    )
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
    monkeypatch.setattr(
        precommit.pip_audit_json, "run_json", lambda _root: _audit_run(0)
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
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")


def test_forge_step_config_reads_section(tmp_path: Path) -> None:
    """`_forge_step_config` returns the `[tool.forge.<step>]` table."""
    _write_pyproject(tmp_path, '[tool.forge.doctest]\npaths = ["lib"]\n')
    assert precommit._forge_step_config(tmp_path, "doctest") == {"paths": ["lib"]}


def test_forge_step_config_missing_returns_empty(tmp_path: Path) -> None:
    """`_forge_step_config` returns `{}` when the section or pyproject is absent."""
    assert precommit._forge_step_config(tmp_path, "doctest") == {}
    _write_pyproject(tmp_path, "[tool.forge]\n")
    assert precommit._forge_step_config(tmp_path, "doctest") == {}


def test_cfg_str_list_narrows_list_values() -> None:
    """`_cfg_str_list` returns a list value as `list[str]`, stringifying items."""
    assert precommit._cfg_str_list({"paths": ["a", "b"]}, "paths", ["x"]) == ["a", "b"]
    assert precommit._cfg_str_list({"paths": [1, 2]}, "paths", ["x"]) == ["1", "2"]


def test_cfg_str_list_falls_back_on_missing_or_scalar() -> None:
    """`_cfg_str_list` returns the default when the key is absent or not a list.

    A scalar like ``paths = "src"`` falls back rather than being iterated
    character-by-character into the subprocess argv.
    """
    assert precommit._cfg_str_list({}, "paths", ["src"]) == ["src"]
    assert precommit._cfg_str_list({"paths": "src"}, "paths", ["src"]) == ["src"]


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


def test_resolve_steps_only_still_honors_skip(tmp_path: Path) -> None:
    """`skip` subtracts from the `only` set too — it is never silently ignored."""
    resolved = _names(
        precommit._resolve_steps(
            tmp_path, only=["ruff", "pip_audit"], skip=["pip_audit"]
        )
    )
    assert resolved == ["ruff"]


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
    result; a ``src/`` dir so smart-detect resolves a scan root.
    """
    (tmp_path / "src").mkdir()
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
    (tmp_path / "lib").mkdir()
    (tmp_path / "app").mkdir()
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
    (tmp_path / "src").mkdir()
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
    (tmp_path / "src").mkdir()
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
    (tmp_path / "src").mkdir()
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    with pytest.raises(SystemExit):
        precommit.step_doctest(tmp_path)


def test_step_typecheck_default_pyrefly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Typecheck defaults to the `pyrefly check` command and is non-blocking.

    MOCK SETUP: pyrefly present on PATH; ``_run`` captures the command and
    returns a passing result; a ``src/`` dir so smart-detect resolves a root.
    """
    (tmp_path / "src").mkdir()
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


def test_step_typecheck_missing_pyrefly_exits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An opted-in-but-absent pyrefly binary fails loudly (SystemExit)."""
    (tmp_path / "src").mkdir()
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    with pytest.raises(SystemExit):
        precommit.step_typecheck(tmp_path)


def test_step_typecheck_blocking_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`blocking = true` makes a pyrefly error a blocking failure."""
    (tmp_path / "src").mkdir()
    _present(monkeypatch)
    _write_pyproject(tmp_path, "[tool.forge.typecheck]\nblocking = true\n")
    monkeypatch.setattr(precommit, "_run", lambda _cmd, **_kw: (False, "error: x"))
    result = precommit.step_typecheck(tmp_path)
    assert not result.passed
    assert not result.non_blocking


def test_step_typecheck_drops_option_like_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An option-like `paths` entry never reaches the pyrefly subprocess.

    The shared resolver only returns existing in-repo dirs, so a value like
    ``--output=x`` (no such dir) is dropped — preventing flag injection. With
    nothing left to scan the step skips cleanly rather than running pyrefly
    with an attacker-controlled flag.
    """
    ran: list[list[str]] = []
    monkeypatch.setattr(
        precommit, "_run", lambda cmd, **_kw: ran.append(cmd) or (True, "")
    )
    _write_pyproject(tmp_path, '[tool.forge.typecheck]\npaths = ["--output=x"]\n')
    result = precommit.step_typecheck(tmp_path)
    assert result.skipped
    assert result.passed
    assert ran == []  # pyrefly never invoked with the injected flag


def test_step_doctest_drops_paths_escaping_repo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `paths` entry resolving outside the repo never reaches pytest.

    The resolver drops repo-escaping paths, so the step skips instead of
    scanning ``/etc`` — the path-traversal guard, expressed as a clean skip.
    """
    ran: list[list[str]] = []
    monkeypatch.setattr(
        precommit, "_run", lambda cmd, **_kw: ran.append(cmd) or (True, "")
    )
    _write_pyproject(tmp_path, '[tool.forge.doctest]\npaths = ["/etc"]\n')
    result = precommit.step_doctest(tmp_path)
    assert result.skipped
    assert result.passed
    assert ran == []


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
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    with pytest.raises(SystemExit):
        precommit.step_doc_consistency(tmp_path)


# ---------------------------------------------------------------------------
# step_pip_audit — sidecar writing and parse-error handling
# ---------------------------------------------------------------------------


def test_step_pip_audit_writes_sidecar_when_data_parseable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """step_pip_audit writes the pip-audit JSON sidecar when data is parseable.

    SCENARIO: pip-audit returns 1 finding (parseable data).
    MOCK SETUP: precommit.pip_audit_json.run_json → _audit_run(1).
    EXPECTED BEHAVIOR: code_health/pip_audit.json created and its contents
        round-trip to the same data dict.
    """
    run = _audit_run(1)
    monkeypatch.setattr(precommit.pip_audit_json, "run_json", lambda _root: run)
    precommit.step_pip_audit(tmp_path)
    sidecar = tmp_path / "code_health" / "pip_audit.json"
    assert sidecar.exists()
    assert json.loads(sidecar.read_text()) == run.data


def test_step_pip_audit_does_not_write_sidecar_on_parse_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """step_pip_audit skips sidecar creation when pip-audit produces non-JSON output.

    SCENARIO: pip-audit present but stdout is not parseable JSON.
    MOCK SETUP: precommit.pip_audit_json.run_json → AuditRun(data=None, ...).
    EXPECTED BEHAVIOR: sidecar not created; non_blocking True; "no parseable
        JSON" in output; passed False.
    """
    bad_run = AuditRun(data=None, stderr="kaboom", returncode=1)
    monkeypatch.setattr(precommit.pip_audit_json, "run_json", lambda _root: bad_run)
    result = precommit.step_pip_audit(tmp_path)
    assert not (tmp_path / "code_health" / "pip_audit.json").exists()
    assert result.non_blocking
    assert "no parseable JSON" in result.output
    assert not result.passed


# ---------------------------------------------------------------------------
# step_cve_usage — sidecar forwarding
# ---------------------------------------------------------------------------


def test_step_cve_usage_passes_audit_json_when_sidecar_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """step_cve_usage forwards --audit-json to the CLI when the sidecar exists.

    SCENARIO: pattern file and code_health/pip_audit.json both present.
    MOCK SETUP: shutil.which → present; _run captures argv and returns clean.
    EXPECTED BEHAVIOR: "--audit-json" and the sidecar path appear in the
        subprocess argv, so the two steps share one pip-audit scan (#78).
    """
    (tmp_path / "cve_usage_patterns.toml").write_text(
        '["CVE-1"]\npackage = "x"\n', encoding="utf-8"
    )
    sidecar = tmp_path / "code_health" / "pip_audit.json"
    sidecar.parent.mkdir(parents=True)
    sidecar.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/x")
    captured_argv: list[str] = []

    def _fake_run(cmd: list[str], **_kw: object) -> tuple[bool, str]:
        captured_argv.extend(cmd)
        return True, "clean"

    monkeypatch.setattr(precommit, "_run", _fake_run)
    precommit.step_cve_usage(tmp_path)
    assert "--audit-json" in captured_argv
    assert precommit.PIP_AUDIT_SIDECAR in captured_argv


def test_step_cve_usage_runs_bare_when_sidecar_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """step_cve_usage omits --audit-json from the CLI call when the sidecar is absent.

    SCENARIO: pattern file present; code_health/pip_audit.json not present.
    MOCK SETUP: shutil.which → present; _run captures argv.
    EXPECTED BEHAVIOR: "--audit-json" NOT in argv — CLI falls back to its
        own pip-audit invocation.
    """
    (tmp_path / "cve_usage_patterns.toml").write_text(
        '["CVE-1"]\npackage = "x"\n', encoding="utf-8"
    )
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/x")
    captured_argv: list[str] = []

    def _fake_run(cmd: list[str], **_kw: object) -> tuple[bool, str]:
        captured_argv.extend(cmd)
        return True, "clean"

    monkeypatch.setattr(precommit, "_run", _fake_run)
    precommit.step_cve_usage(tmp_path)
    assert "verify-forge-cve-usage" in captured_argv
    assert "--audit-json" not in captured_argv


def test_declared_scripts_happy_path(tmp_path: Path) -> None:
    """_declared_scripts returns (name, script_set) for a valid pyproject."""
    _write_project_scripts_pyproject(tmp_path, "mypkg", {"mycli": "", "another": ""})
    result = precommit._declared_scripts(tmp_path)
    assert result is not None
    assert result[0] == "mypkg"
    assert result[1] == {"mycli", "another"}


def test_declared_scripts_returns_none_when_name_missing(tmp_path: Path) -> None:
    """_declared_scripts returns None when [project] has no ``name`` key."""
    (tmp_path / "pyproject.toml").write_text(
        '[project.scripts]\nmycli = "pkg:main"\n', encoding="utf-8"
    )
    assert precommit._declared_scripts(tmp_path) is None


def test_declared_scripts_returns_none_when_scripts_key_absent(
    tmp_path: Path,
) -> None:
    """_declared_scripts returns None when [project.scripts] is absent."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "mypkg"\n', encoding="utf-8"
    )
    assert precommit._declared_scripts(tmp_path) is None


def test_declared_scripts_returns_none_when_scripts_empty(tmp_path: Path) -> None:
    """_declared_scripts returns None when [project.scripts] is an empty table."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "mypkg"\n\n[project.scripts]\n', encoding="utf-8"
    )
    assert precommit._declared_scripts(tmp_path) is None


def test_installed_console_scripts_returns_console_script_names_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_installed_console_scripts returns only console_scripts group entries."""
    eps = [FakeEP("mycli", "console_scripts"), FakeEP("myapp", "gui_scripts")]
    monkeypatch.setattr(
        precommit.importlib.metadata, "distribution", lambda _n: FakeDist(eps)
    )
    result = precommit._installed_console_scripts("mypkg")
    assert result == {"mycli"}


def test_installed_console_scripts_returns_none_when_package_not_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_installed_console_scripts returns None when the package is not installed."""

    def _raise(_name: str) -> object:
        raise precommit.importlib.metadata.PackageNotFoundError(_name)

    monkeypatch.setattr(precommit.importlib.metadata, "distribution", _raise)
    assert precommit._installed_console_scripts("missing-pkg") is None


# ---------------------------------------------------------------------------
# step_env_sync (integration — all patch is_non_interactive)
# ---------------------------------------------------------------------------


def test_step_env_sync_skips_in_ci_non_interactive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """step_env_sync skips in CI without touching distribution metadata.

    SCENARIO: is_non_interactive returns True — the step must short-circuit
    immediately without inspecting pyproject.toml or importlib.metadata.
    MOCK SETUP: is_non_interactive stubbed to True.
    EXPECTED BEHAVIOR: passed True, skipped True, "non-interactive" in output.
    """
    monkeypatch.setattr(precommit, "is_non_interactive", lambda: True)
    result = precommit.step_env_sync(tmp_path)
    assert result.passed
    assert result.skipped
    assert "non-interactive" in result.output


def test_step_env_sync_skips_when_no_declared_scripts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """step_env_sync skips when pyproject has no usable [project.scripts].

    SCENARIO: no pyproject.toml in tmp_path — _declared_scripts returns None.
    MOCK SETUP: is_non_interactive stubbed to False; no pyproject written.
    EXPECTED BEHAVIOR: passed True, skipped True, "[project.scripts]" in output.
    """
    monkeypatch.setattr(precommit, "is_non_interactive", lambda: False)
    result = precommit.step_env_sync(tmp_path)
    assert result.passed
    assert result.skipped
    assert "[project.scripts]" in result.output


def test_step_env_sync_skips_when_package_not_installed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """step_env_sync skips when the package is declared but not installed.

    SCENARIO: pyproject declares mypkg with one script, but distribution
    raises PackageNotFoundError — nothing to compare against.
    MOCK SETUP: is_non_interactive→False; pyproject written; distribution
    stubbed to raise PackageNotFoundError.
    EXPECTED BEHAVIOR: passed True, skipped True, "not installed" in output.
    """
    monkeypatch.setattr(precommit, "is_non_interactive", lambda: False)
    _write_project_scripts_pyproject(tmp_path, "mypkg", {"mycli": ""})

    def _raise(_name: str) -> object:
        raise precommit.importlib.metadata.PackageNotFoundError(_name)

    monkeypatch.setattr(precommit.importlib.metadata, "distribution", _raise)
    result = precommit.step_env_sync(tmp_path)
    assert result.passed
    assert result.skipped
    assert "not installed" in result.output


def test_step_env_sync_passes_when_all_scripts_registered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """step_env_sync passes when every declared script is installed.

    SCENARIO: pyproject declares {mycli, helper}; distribution reports both
    as console_scripts — no gap between declared and installed.
    MOCK SETUP: is_non_interactive→False; pyproject written; distribution→
    FakeDist([FakeEP("mycli","console_scripts"), FakeEP("helper","console_scripts")]).
    EXPECTED BEHAVIOR: passed True, skipped False, "installed" in output,
    non_blocking False.
    """
    monkeypatch.setattr(precommit, "is_non_interactive", lambda: False)
    _write_project_scripts_pyproject(tmp_path, "mypkg", {"mycli": "", "helper": ""})
    eps = [FakeEP("mycli", "console_scripts"), FakeEP("helper", "console_scripts")]
    monkeypatch.setattr(
        precommit.importlib.metadata, "distribution", lambda _n: FakeDist(eps)
    )
    result = precommit.step_env_sync(tmp_path)
    assert result.passed
    assert not result.skipped
    assert "installed" in result.output
    assert not result.non_blocking


def test_step_env_sync_blocks_by_default_when_script_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """step_env_sync blocks (non_blocking=False) when a script is missing and no config.

    SCENARIO: pyproject declares {mycli, new-cli}; distribution has only mycli;
    no [tool.forge.env_sync] written — blocking defaults to True.
    MOCK SETUP: is_non_interactive→False; pyproject written without env_sync config;
    distribution→FakeDist([FakeEP("mycli","console_scripts")]).
    EXPECTED BEHAVIOR: passed False, skipped False, non_blocking False,
    "new-cli" in output, "setup.sh" in output.
    """
    monkeypatch.setattr(precommit, "is_non_interactive", lambda: False)
    _write_project_scripts_pyproject(tmp_path, "mypkg", {"mycli": "", "new-cli": ""})
    eps = [FakeEP("mycli", "console_scripts")]
    monkeypatch.setattr(
        precommit.importlib.metadata, "distribution", lambda _n: FakeDist(eps)
    )
    result = precommit.step_env_sync(tmp_path)
    assert not result.passed
    assert not result.skipped
    assert not result.non_blocking
    assert "new-cli" in result.output
    assert "setup.sh" in result.output


def test_step_env_sync_warns_not_blocks_when_blocking_false(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """step_env_sync warns when [tool.forge.env_sync].blocking=false.

    SCENARIO: same missing-script situation as blocks_by_default, but
    [tool.forge.env_sync] blocking=false downgrades the result to WARN.
    MOCK SETUP: is_non_interactive→False; pyproject written with blocking=false;
    distribution→FakeDist([FakeEP("mycli","console_scripts")]).
    EXPECTED BEHAVIOR: passed False, skipped False, non_blocking True,
    "new-cli" in output.
    """
    monkeypatch.setattr(precommit, "is_non_interactive", lambda: False)
    _write_project_scripts_pyproject(
        tmp_path, "mypkg", {"mycli": "", "new-cli": ""}, env_sync_blocking=False
    )
    eps = [FakeEP("mycli", "console_scripts")]
    monkeypatch.setattr(
        precommit.importlib.metadata, "distribution", lambda _n: FakeDist(eps)
    )
    result = precommit.step_env_sync(tmp_path)
    assert not result.passed
    assert not result.skipped
    assert result.non_blocking
    assert "new-cli" in result.output


# ---------------------------------------------------------------------------
# env_sync — forge-scripts version-pin drift (#107)
# ---------------------------------------------------------------------------


def _write_deps_pyproject(repo_root: Path, deps: list[str]) -> None:
    """Write a ``[project]`` pyproject (no scripts) with given dependencies.

    Args:
        repo_root: Directory to drop ``pyproject.toml`` in.
        deps: Requirement strings for ``[project.dependencies]``.
    """
    dep_lines = ", ".join(f'"{d}"' for d in deps)
    (repo_root / "pyproject.toml").write_text(
        f'[project]\nname = "consumer"\ndependencies = [{dep_lines}]\n',
        encoding="utf-8",
    )


def test_step_env_sync_warns_on_forge_scripts_pin_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A forge-scripts == pin ahead of the install produces a non-blocking WARN.

    SCENARIO: repo pins forge-scripts==2.9.0; installed is 2.8.0.
    MOCK SETUP: is_non_interactive→False; importlib.metadata.version→"2.8.0".
    EXPECTED BEHAVIOR: passed False, non_blocking True, names the pin.
    """
    monkeypatch.setattr(precommit, "is_non_interactive", lambda: False)
    _write_deps_pyproject(tmp_path, ["forge-scripts==2.9.0"])
    monkeypatch.setattr(precommit.importlib.metadata, "version", lambda _n: "2.8.0")
    result = precommit.step_env_sync(tmp_path)
    assert not result.passed
    assert result.non_blocking
    assert "forge-scripts==2.9.0" in result.output


def test_step_env_sync_no_warn_when_pin_satisfied(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No WARN when the installed forge-scripts meets or exceeds the pin."""
    monkeypatch.setattr(precommit, "is_non_interactive", lambda: False)
    _write_deps_pyproject(tmp_path, ["forge-scripts==2.8.0"])
    monkeypatch.setattr(precommit.importlib.metadata, "version", lambda _n: "2.9.0")
    result = precommit.step_env_sync(tmp_path)
    assert result.passed
    assert "behind the pin" not in result.output


def test_step_env_sync_no_warn_on_non_exact_pin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-``==`` specifier (range / channel) is not treated as a pin."""
    monkeypatch.setattr(precommit, "is_non_interactive", lambda: False)
    _write_deps_pyproject(tmp_path, ["forge-scripts>=2.8.0"])
    monkeypatch.setattr(precommit.importlib.metadata, "version", lambda _n: "2.0.0")
    result = precommit.step_env_sync(tmp_path)
    assert result.passed
    assert "behind the pin" not in result.output


def test_step_env_sync_no_warn_on_editable_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An editable / setuptools-scm dev build is not compared against the pin."""
    monkeypatch.setattr(precommit, "is_non_interactive", lambda: False)
    _write_deps_pyproject(tmp_path, ["forge-scripts==2.9.0"])
    monkeypatch.setattr(
        precommit.importlib.metadata, "version", lambda _n: "2.8.0.dev1+gabc1234"
    )
    result = precommit.step_env_sync(tmp_path)
    assert result.passed
    assert "behind the pin" not in result.output


# ---------------------------------------------------------------------------
# env_sync — registry position
# ---------------------------------------------------------------------------


def test_step_env_sync_is_first_default_step(tmp_path: Path) -> None:
    """env_sync is the first step in the default resolved sequence."""
    resolved = precommit._resolve_steps(tmp_path)
    assert resolved[0].name == "env_sync"
