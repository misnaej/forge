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

import contextlib
import datetime as _dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple
from unittest.mock import patch

import pytest

from forge import (
    config,
    emergency,
    gen_common,
    git_utils,
    install_claudemd,
    pip_audit_json,
    precommit,
    regen_docs,
    verify_test_naming,
)
from forge.audit import common as audit_common
from forge.pip_audit_json import AuditRun
from forge.run_context import _CI_MARKERS
from forge.smart_test import lifecycle as _lifecycle
from tests.conftest import (
    GIT_ENV,
    PRODUCED_AT_RE,
    _detach_head,
    commit_all,
    git_stdout,
    init_git_repo,
    init_single_track_repo,
    log_body,
    timing_log,
    write_install,
)


if TYPE_CHECKING:
    from collections.abc import Callable


@pytest.fixture(autouse=True)
def _clear_wip_sync_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip FORGE_WIP_SYNC from the environment before every test.

    main()'s wip-sync short-circuit (#404 sync ladder) reads this var
    directly from os.environ, so an ambient value left over from a real
    wip-sync checkpoint commit in the developer's shell would silently
    short-circuit every main()-calling test in this module. Tests that
    exercise the short-circuit itself set it explicitly via
    monkeypatch.setenv.
    """
    monkeypatch.delenv("FORGE_WIP_SYNC", raising=False)


@pytest.fixture(autouse=True)
def _clear_pip_audit_force_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip FORGE_PIP_AUDIT_FORCE from the environment before every test.

    main()'s ``--only pip_audit`` handling sets this var directly on
    ``os.environ`` (not via monkeypatch), so a run that names
    ``pip_audit`` explicitly would otherwise leave it set for every
    later test in the same process — including cadence tests that
    depend on the gate actually running. Tests that exercise the
    override itself set it explicitly via monkeypatch.setenv.
    """
    monkeypatch.delenv(precommit._PIP_AUDIT_FORCE_ENV, raising=False)


# ---------------------------------------------------------------------------
# Null Objects — reused across env_sync test groups
# ---------------------------------------------------------------------------


class FakeEP(NamedTuple):
    """Null-object entry point for installed_console_scripts tests.

    Attributes:
        name: Entry-point name (e.g. ``"mycli"``).
        group: Entry-point group (e.g. ``"console_scripts"``).
        value: Entry-point target (``"module:attr"``); the shared
            entry-point walk reads the module half.
    """

    name: str
    group: str
    value: str = "pkg.cli:main"


class FakeDist:
    """Null-object distribution for installed_console_scripts tests.

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


def _write_intake_entry(root: Path, text: str) -> None:
    """Write one lesson file under the default intake directory.

    Args:
        root: Repo root holding ``docs/intake``.
        text: Entry body.
    """
    lessons = root / "docs/intake/lessons"
    lessons.mkdir(parents=True)
    (lessons / "trap.md").write_text(text, encoding="utf-8")


def test_step_intake_check_skipped_without_intake_dir(tmp_path: Path) -> None:
    """step_intake_check is skipped and passing when there is no intake directory."""
    result = precommit.step_intake_check(tmp_path)
    assert result.skipped
    assert result.passed
    assert "skipped" in result.output


def test_step_intake_check_fails_on_bad_entry(tmp_path: Path) -> None:
    """A secret-shaped string in an entry fails the step with a problem count."""
    _write_intake_entry(
        tmp_path, "# Trap\n\n- seen: 2026-01-01\n\nAKIAABCDEFGHIJKLMNOP\n"
    )
    result = precommit.step_intake_check(tmp_path)
    assert not result.passed
    assert not result.skipped
    assert "AWS access key-shaped string" in result.output
    assert "1 intake problem(s)" in result.output


def test_step_intake_check_passes_on_valid_entries(tmp_path: Path) -> None:
    """Valid entries pass, are not skipped, and are counted."""
    _write_intake_entry(tmp_path, "# Trap\n\n- seen: 2026-01-01\n")
    result = precommit.step_intake_check(tmp_path)
    assert result.passed
    assert not result.skipped
    assert "1 entries OK" in result.output


def test_intake_check_is_a_default_on_registered_step() -> None:
    """intake_check is registered and runs without opt-in."""
    registered = {s.name: s for s in precommit._STEP_REGISTRY}
    assert registered["intake_check"].default_on


def test_step_commit_types_parity_skipped_when_hook_absent(
    tmp_path: Path,
) -> None:
    """step_commit_types_parity is skipped when the shell hook file is absent."""
    result = precommit.step_commit_types_parity(tmp_path)
    assert result.skipped
    assert result.passed


@pytest.mark.parametrize(
    ("project_name", "doc_text"),
    [
        pytest.param(
            "someone-else", f"{gen_common.CONFIG_DOC_START}\n", id="not-forge-repo"
        ),
        pytest.param(git_utils.FORGE_DIST_NAME, "no table here\n", id="no-marker"),
    ],
)
def test_step_config_doc_check_skips_when_not_forge_repo_or_no_marker(
    tmp_path: Path,
    project_name: str,
    doc_text: str,
) -> None:
    """step_config_doc_check skips outside forge's repo or without the marker.

    Args:
        project_name: Value written as `[project].name` in pyproject.toml.
        doc_text: Content written to the config doc file.
    """
    (tmp_path / "pyproject.toml").write_text(f'[project]\nname = "{project_name}"\n')
    doc = tmp_path / gen_common.CONFIG_DOC
    doc.parent.mkdir(parents=True)
    doc.write_text(doc_text)
    result = precommit.step_config_doc_check(tmp_path)
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
    The three new default-on steps self-skip in tmp_path (auto_rebuild:
    no rebuild_command configured; regen_docs: no docs dir present;
    vendored_integrity: no data dir with *.js + VENDORED.md) but still
    write their log files before returning.
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
    log_dir = git_utils.code_health_dir(tmp_path)
    assert log_dir.is_dir()
    expected = {
        "auto_rebuild.log",
        "env_sync.log",
        "regen_docs.log",
        "ruff.log",
        "docstring_verification.log",
        "docstring_coverage.log",
        "test_naming_check.log",
        "repo_structure_check.log",
        "intake_check.log",
        "manifest_json.log",
        "plugin_version.log",
        "vendored_integrity.log",
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
        "auto_rebuild",
        "regen_docs",
        "ruff",
        "docstring_verification",
        "test_naming_check",
        "repo_structure_check",
        "manifest_json",
        "plugin_version",
        "vendored_integrity",
        "pip_audit",
    }


# ---------------------------------------------------------------------------
# Per-step timing (#417): _step_marker / _format_timing_log / run_all's
# elapsed_s stamping / the timing surfaced by main() (JSON + human output).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("skipped", "passed", "non_blocking", "expected"),
    [
        (True, True, False, "SKIP"),
        (False, True, False, "PASS"),
        (False, False, True, "WARN"),
        (False, False, False, "FAIL"),
        # Precedence: a skipped non-blocking step is SKIP, never WARN.
        (True, False, True, "SKIP"),
    ],
)
def test_step_marker_maps_skip_pass_warn_fail(
    *,
    skipped: bool,
    passed: bool,
    non_blocking: bool,
    expected: str,
) -> None:
    """`_step_marker` maps every StepResult flag combination to its marker.

    Args:
        skipped: The `StepResult.skipped` flag under test.
        passed: The `StepResult.passed` flag under test.
        non_blocking: The `StepResult.non_blocking` flag under test.
        expected: The marker `_step_marker` must return for this
            combination.

    SCENARIO: the colored progress line and the timing log both render
    from this one mapping, so every reachable flag combination must
    resolve deterministically — skipped beats passed/failed, and a
    non-blocking failure renders distinctly from a blocking one.
    EXPECTED BEHAVIOR: `_step_marker` returns *expected* for the given
    flag combination.
    """
    result = precommit.StepResult(
        name="x", passed=passed, output="", skipped=skipped, non_blocking=non_blocking
    )
    assert precommit._step_marker(result) == expected


def test_format_timing_log_renders_header_rows_and_total() -> None:
    """`_format_timing_log` renders a header, one row per step, and an exact total.

    SCENARIO: `code_health/precommit_timing.log` is read by a human
    scanning for the slow step, so each row must show the step name, a
    one-decimal elapsed time, and its marker — and the trailing total
    must be the exact sum of the per-step values, not an approximation.
    EXPECTED BEHAVIOR: the rendered text opens with the fixed banner
    line, has one row per `StepResult` in order (name + 1-decimal
    elapsed + marker), and ends with a `total` row whose value equals
    `sum(r.elapsed_s for r in results)`.
    """
    results = [
        precommit.StepResult(name="ruff", passed=True, output="", elapsed_s=1.2),
        precommit.StepResult(
            name="env_sync", passed=True, output="", skipped=True, elapsed_s=0.5
        ),
        precommit.StepResult(
            name="pip_audit",
            passed=False,
            output="",
            non_blocking=True,
            elapsed_s=2.3,
        ),
    ]

    rendered = precommit._format_timing_log(results)
    lines = rendered.splitlines()

    assert lines[0] == "forge-precommit per-step timing (newest run overwrites)"
    assert lines[1] == ""

    ruff_line, env_sync_line, pip_audit_line = lines[2:5]
    assert "ruff" in ruff_line
    assert "1.2s" in ruff_line
    assert "PASS" in ruff_line
    assert "env_sync" in env_sync_line
    assert "0.5s" in env_sync_line
    assert "SKIP" in env_sync_line
    assert "pip_audit" in pip_audit_line
    assert "2.3s" in pip_audit_line
    assert "WARN" in pip_audit_line

    assert lines[5] == ""
    assert len(lines) == 7
    total = sum(r.elapsed_s for r in results)
    assert lines[6].startswith("total")
    assert f"{total:.1f}s" in lines[6]


def test_timing_markers_reads_rows_skips_stamp_header_and_total() -> None:
    """`timing_markers` returns name→marker for step rows only, in order.

    `pr_wrapup_compose`'s `_code_quality_rows` and `pr_evidence`'s
    `_timing_snapshot` both exercise this reader only through a rendered
    string comparison, never against its own return value — this pins the
    parser's own contract (a name→marker dict, insertion-ordered) directly,
    including that the `# produced-at:` stamp, the banner line, the blank
    separator, and the trailing `total` row are all skipped, not misread
    as steps.
    """
    log = timing_log("ruff PASS", "typecheck FAIL")

    markers = precommit.timing_markers(log)

    assert markers == {"ruff": "PASS", "typecheck": "FAIL"}
    assert list(markers) == ["ruff", "typecheck"]


def test_run_all_stamps_elapsed_s_from_monotonic_delta(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run_all stamps each StepResult.elapsed_s from a real monotonic delta.

    SCENARIO: mirrors test_run_all_only_dispatches_monkeypatched_steps —
    two monkeypatched steps run via `only=`. Here the assertion is on
    timing, not dispatch: elapsed_s must come from an actual per-step
    (end - start) delta rather than being left at its 0.0 default or
    shared across steps.
    MOCK SETUP: step_ruff / step_pip_audit replaced with canned passing
    stubs; precommit.time.monotonic is replaced with a scripted clock
    yielding fixed, strictly increasing timestamps (0.0, 1.0, 2.0, 4.0)
    so the two steps land on distinct, easy-to-verify deltas.
    EXPECTED BEHAVIOR: results[0].elapsed_s == 1.0 (from 1.0 - 0.0) and
    results[1].elapsed_s == 2.0 (from 4.0 - 2.0), exactly.
    """

    def _ruff(_root: object) -> precommit.StepResult:
        return precommit.StepResult(name="ruff", passed=True, output="x")

    def _audit(_root: object) -> precommit.StepResult:
        return precommit.StepResult(name="pip_audit", passed=True, output="x")

    monkeypatch.setattr(precommit, "step_ruff", _ruff)
    monkeypatch.setattr(precommit, "step_pip_audit", _audit)
    scripted_monotonic = iter([0.0, 1.0, 2.0, 4.0]).__next__
    monkeypatch.setattr(precommit.time, "monotonic", scripted_monotonic)

    results = precommit.run_all(
        tmp_path, print_progress=False, only=["ruff", "pip_audit"]
    )

    assert results[0].elapsed_s == 1.0
    assert results[1].elapsed_s == 2.0


def test_run_all_writes_precommit_timing_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run_all persists `code_health/precommit_timing.log` with consistent format.

    SCENARIO: `run_all` must write the same rendering `_format_timing_log`
    produces from its own returned results — the log is a downstream
    consumer's ground truth, so a self-consistency check catches drift
    between the write and the render, not the render's format itself
    (covered by test_format_timing_log_renders_header_rows_and_total).
    MOCK SETUP: reuses the `_stub_*` passing/skipping helpers from
    test_run_all_writes_code_health_logs so no real CLI / network call
    fires.
    EXPECTED BEHAVIOR: `code_health/precommit_timing.log`'s line 1
    actually matches the ``# produced-at:`` provenance stamp shape
    (FOUNDATION §13) — asserted on the raw text before ``log_body``
    strips it, so a writer that dropped the stamp line would fail here
    instead of `log_body`'s graceful no-stamp fallback silently
    absorbing it. The remaining contents equal
    `precommit._format_timing_log(results)` for the results `run_all`
    actually returned — the log is a downstream consumer's ground
    truth, so a self-consistency check catches drift between the write
    and the render, not the render's format itself (covered by
    test_format_timing_log_renders_header_rows_and_total).
    *tmp_path* is deliberately left a non-git directory, matching this
    test's established baseline (the un-stamped steps under test already
    tolerate that); the stamp itself must degrade gracefully
    (``tree=unknown``) rather than require a real repo.
    """
    _stub_env_sync_skipped(monkeypatch)
    _stub_docstrings_passing(monkeypatch)
    _stub_test_naming_passing(monkeypatch)
    _stub_repo_structure_passing(monkeypatch)
    _stub_pip_audit_skipped(monkeypatch)
    _stub_docstring_coverage_skipped(monkeypatch)
    results = precommit.run_all(repo_root=tmp_path, print_progress=False)

    log_path = git_utils.code_health_dir(tmp_path) / "precommit_timing.log"
    first_line = log_path.read_text(encoding="utf-8").splitlines()[0]
    assert PRODUCED_AT_RE.fullmatch(first_line)
    assert log_body(log_path).rstrip("\n") == precommit._format_timing_log(
        results
    ).rstrip("\n")


def test_main_json_includes_elapsed_s(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--json output carries a valid elapsed_s on every step, including a failing one.

    SCENARIO: a JSON consumer must be able to read per-step timing even
    when a step fails — elapsed_s should never be omitted or left
    non-numeric on the failure path.
    MOCK SETUP: get_repo_root pinned to tmp_path; step_docstrings is
    replaced with a blocking FAIL (passed=False, non_blocking=False);
    step_test_naming, step_repo_structure, step_pip_audit, and
    step_docstring_coverage are stubbed to pass/skip as usual; argv
    drives `forge-precommit --json`.
    EXPECTED BEHAVIOR: every parsed element has a float elapsed_s >= 0.0,
    and the failed docstring_verification element in particular reports
    passed=False alongside a valid elapsed_s.
    """
    monkeypatch.setattr(precommit, "get_repo_root", lambda: tmp_path)

    def _failing_docstrings(_root: object) -> precommit.StepResult:
        return precommit.StepResult(
            name="docstring_verification",
            passed=False,
            output="(simulated docstring error)",
        )

    monkeypatch.setattr(precommit, "step_docstrings", _failing_docstrings)
    _stub_test_naming_passing(monkeypatch)
    _stub_repo_structure_passing(monkeypatch)
    _stub_pip_audit_skipped(monkeypatch)
    _stub_docstring_coverage_skipped(monkeypatch)
    with patch.object(precommit.sys, "argv", ["forge-precommit", "--json"]):
        precommit.main()
    parsed = json.loads(capsys.readouterr().out)

    for entry in parsed:
        assert isinstance(entry["elapsed_s"], float)
        assert entry["elapsed_s"] >= 0.0

    failed = next(r for r in parsed if r["name"] == "docstring_verification")
    assert failed["passed"] is False
    assert isinstance(failed["elapsed_s"], float)
    assert failed["elapsed_s"] >= 0.0


def test_main_prints_per_step_elapsed_and_total_line(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Non-JSON main() prints a per-step `X.Xs` suffix and a self-consistent total line.

    SCENARIO: the human-readable run must let a contributor see per-step
    cost at a glance, and the trailing total line must actually be the
    sum of the printed per-step numbers rather than an independent
    figure that could drift from them.
    MOCK SETUP: get_repo_root pinned to tmp_path; step_ruff and
    step_pip_audit stubbed to canned passing results; argv drives
    `forge-precommit --only ruff,pip_audit` (no --json).
    EXPECTED BEHAVIOR: every step progress line ends in a `<N.N>s`
    suffix, and the final `total <N.N>s (per-step:
    code_health/precommit_timing.log)` line's value equals the sum of
    the parsed per-step values.
    """
    monkeypatch.setattr(precommit, "get_repo_root", lambda: tmp_path)

    def _ruff(_root: object) -> precommit.StepResult:
        return precommit.StepResult(name="ruff", passed=True, output="x")

    def _audit(_root: object) -> precommit.StepResult:
        return precommit.StepResult(name="pip_audit", passed=True, output="x")

    monkeypatch.setattr(precommit, "step_ruff", _ruff)
    monkeypatch.setattr(precommit, "step_pip_audit", _audit)
    with patch.object(
        precommit.sys, "argv", ["forge-precommit", "--only", "ruff,pip_audit"]
    ):
        rc = precommit.main()
    assert rc == 0
    out = capsys.readouterr().out

    per_step = [
        float(m) for m in re.findall(r"^\S+\s+\S+\s+(\d+\.\d)s$", out, re.MULTILINE)
    ]
    assert len(per_step) == 2

    total_match = re.search(
        r"^total (\d+\.\d)s \(per-step: code_health/precommit_timing\.log\)$",
        out,
        re.MULTILINE,
    )
    assert total_match is not None
    assert float(total_match.group(1)) == sum(per_step)


# ---------------------------------------------------------------------------
# FORGE_WIP_SYNC short-circuit (#404 sync ladder): main() defers the full
# battery for a wip-sync checkpoint commit, before run_all ever executes.
# ---------------------------------------------------------------------------


def _run_all_spy() -> tuple[
    Callable[..., list[precommit.StepResult]], list[tuple[object, ...]]
]:
    """Build a ``run_all`` replacement that records every call's args/kwargs.

    Returns:
        A tuple of the spy callable (returns an empty step list so
        main()'s post-processing has nothing to summarize) and the list
        it appends ``(args, kwargs)`` to on each call.
    """
    calls: list[tuple[object, ...]] = []

    def _spy(*args: object, **kwargs: object) -> list[precommit.StepResult]:
        calls.append((args, kwargs))
        return []

    return _spy, calls


def test_main_wip_sync_env_short_circuits_before_run_all(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """FORGE_WIP_SYNC=1 returns 0 with the checkpoint banner and never calls run_all.

    SCENARIO: a wip-sync checkpoint commit sets FORGE_WIP_SYNC=1 to defer
    the full battery to the next real commit (FOUNDATION §2 sync ladder).
    MOCK SETUP: get_repo_root is pinned to tmp_path; run_all is replaced
    with a spy that must never fire; sys.argv drives the bare
    `forge-precommit` invocation.
    EXPECTED BEHAVIOR: main() returns 0, stdout names the wip-sync
    checkpoint, and the run_all spy recorded zero calls.
    """
    monkeypatch.setattr(precommit, "get_repo_root", lambda: tmp_path)
    monkeypatch.setenv("FORGE_WIP_SYNC", "1")
    spy, calls = _run_all_spy()
    monkeypatch.setattr(precommit, "run_all", spy)
    with patch.object(precommit.sys, "argv", ["forge-precommit"]):
        rc = precommit.main()
    assert rc == 0
    assert "wip-sync checkpoint" in capsys.readouterr().out
    assert calls == []


def test_main_wip_sync_env_unset_runs_normal_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No FORGE_WIP_SYNC set takes the normal path — the short-circuit is opt-in.

    SCENARIO: a normal (non-checkpoint) commit runs with no FORGE_WIP_SYNC
    set, so main() must take its regular path and run the full battery.
    MOCK SETUP: get_repo_root pinned to tmp_path; FORGE_WIP_SYNC removed
    from the environment; run_all replaced with a spy recording calls.
    EXPECTED BEHAVIOR: the run_all spy fires exactly once and no wip-sync
    banner appears in stdout.
    """
    monkeypatch.setattr(precommit, "get_repo_root", lambda: tmp_path)
    monkeypatch.delenv("FORGE_WIP_SYNC", raising=False)
    spy, calls = _run_all_spy()
    monkeypatch.setattr(precommit, "run_all", spy)
    with patch.object(precommit.sys, "argv", ["forge-precommit"]):
        rc = precommit.main()
    assert rc == 0
    out = capsys.readouterr().out
    assert "wip-sync checkpoint" not in out
    assert len(calls) == 1


@pytest.mark.parametrize("value", ["true", "0"])
def test_main_wip_sync_near_miss_values_run_normal_path(
    value: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Only the exact string "1" trips the short-circuit — "true"/"0" do not.

    Args:
        value: A FORGE_WIP_SYNC value that must NOT trigger the
            short-circuit (near-miss truthy/falsy strings).

    SCENARIO: a near-miss FORGE_WIP_SYNC value ("true"/"0") looks
    truthy/falsy but must not silently skip the full battery — only the
    exact string "1" is the opt-in gate.
    MOCK SETUP: get_repo_root pinned to tmp_path; FORGE_WIP_SYNC set to
    *value*; run_all replaced with a spy recording calls.
    EXPECTED BEHAVIOR: the run_all spy fires once and no wip-sync banner
    appears.
    """
    monkeypatch.setattr(precommit, "get_repo_root", lambda: tmp_path)
    monkeypatch.setenv("FORGE_WIP_SYNC", value)
    spy, calls = _run_all_spy()
    monkeypatch.setattr(precommit, "run_all", spy)
    with patch.object(precommit.sys, "argv", ["forge-precommit"]):
        rc = precommit.main()
    assert rc == 0
    out = capsys.readouterr().out
    assert "wip-sync checkpoint" not in out
    assert len(calls) == 1


def test_main_wip_sync_short_circuit_beats_json_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """FORGE_WIP_SYNC=1 emits the plain-text banner even under --json.

    SCENARIO: a wip-sync checkpoint commit runs `forge-precommit --json`
    (e.g. a CI wrapper expecting machine-readable output), but the
    short-circuit runs before the --json branch, so it must still emit
    the human text banner, not JSON — locks the current contract so a
    future refactor doesn't silently move the check below --json.
    MOCK SETUP: get_repo_root pinned to tmp_path; run_all replaced with a
    spy that must never fire; sys.argv drives `forge-precommit --json`.
    EXPECTED BEHAVIOR: raw stdout contains the plain-text banner and does
    not parse as JSON.
    """
    monkeypatch.setattr(precommit, "get_repo_root", lambda: tmp_path)
    monkeypatch.setenv("FORGE_WIP_SYNC", "1")
    spy, calls = _run_all_spy()
    monkeypatch.setattr(precommit, "run_all", spy)
    with patch.object(precommit.sys, "argv", ["forge-precommit", "--json"]):
        rc = precommit.main()
    assert rc == 0
    out = capsys.readouterr().out
    assert "wip-sync checkpoint" in out
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)
    assert calls == []


def test_main_wip_sync_short_circuit_beats_only_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """FORGE_WIP_SYNC=1 short-circuits even when --only narrows the step set.

    SCENARIO: a wip-sync checkpoint commit runs `forge-precommit --only
    ruff` (a narrowed manual invocation), and the short-circuit must
    still fire before --only ever reaches run_all.
    MOCK SETUP: get_repo_root pinned to tmp_path; run_all replaced with a
    spy that must never fire; sys.argv drives `forge-precommit --only ruff`.
    EXPECTED BEHAVIOR: main() returns 0, the banner appears, and --only
    never reaches run_all.
    """
    monkeypatch.setattr(precommit, "get_repo_root", lambda: tmp_path)
    monkeypatch.setenv("FORGE_WIP_SYNC", "1")
    spy, calls = _run_all_spy()
    monkeypatch.setattr(precommit, "run_all", spy)
    with patch.object(precommit.sys, "argv", ["forge-precommit", "--only", "ruff"]):
        rc = precommit.main()
    assert rc == 0
    out = capsys.readouterr().out
    assert "wip-sync checkpoint" in out
    assert calls == []


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


@pytest.mark.parametrize(
    ("cli_passed", "cli_output", "expect_skipped"),
    [
        pytest.param(
            True,
            "(pip-audit not on PATH — skipped)\n"
            f"{pip_audit_json.CVE_USAGE_SKIP_MARKER}\n",
            True,
            id="marker-line-skips",
        ),
        pytest.param(
            False,
            "Checked 0 of 1 reported CVEs against cve_usage_patterns.toml — "
            "no match among the ones checked.\n\n"
            "No usable pattern for: PYSEC-1 (skipped-pkg) — NOT checked.\n",
            False,
            id="word-in-package-name-is-a-real-run",
        ),
        pytest.param(
            False,
            f"⚠️  CVE-1 (x) — src/a.py:3  {pip_audit_json.CVE_USAGE_SKIP_MARKER}\n",
            False,
            id="marker-inside-a-longer-line-is-a-real-run",
        ),
    ],
)
def test_step_cve_usage_skip_is_read_from_the_marker_line_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    cli_passed: bool,
    cli_output: str,
    expect_skipped: bool,
) -> None:
    """Only the CLI's whole marker line marks a run skipped — never a word in it.

    SCENARIO: the CLI output contains "skipped" or the marker inside other
    text (a package named ``skipped-pkg``), or the marker as its own line.
    MOCK SETUP: precommit._run returns the canned CLI result.
    EXPECTED BEHAVIOR: skipped only for the marker line; a real run that
    left a CVE unchecked stays a non-blocking WARN.

    Args:
        tmp_path: Temporary repo root holding the pattern map.
        monkeypatch: Pytest fixture for mocking.
        cli_passed: Whether the CLI exited 0.
        cli_output: The CLI's combined output.
        expect_skipped: Whether the step should report a skip.
    """
    (tmp_path / "cve_usage_patterns.toml").write_text("['CVE-1']\npackage='x'\n")
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/x")
    monkeypatch.setattr(precommit, "_run", lambda *_a, **_kw: (cli_passed, cli_output))
    result = precommit.step_cve_usage(tmp_path)
    assert result.skipped is expect_skipped
    assert result.passed is cli_passed
    assert result.non_blocking


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


# ---------------------------------------------------------------------------
# pip_audit cadence: reuse-scan sidecar and the skip gate (#511)
# ---------------------------------------------------------------------------


def test_pip_audit_scan_age_hours_future_dated_returns_none(tmp_path: Path) -> None:
    """A sidecar timestamped in the future is untrusted, not a negative age.

    Clock skew or a manually-touched stamp could otherwise report a
    negative "hours ago" that reads as impossibly fresh; treating it as
    unknown instead falls back to the safe direction — scan.
    """
    sidecar = git_utils.code_health_dir(tmp_path) / pip_audit_json.SIDECAR_NAME
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text("{}", encoding="utf-8")
    future = time.time() + 3600
    os.utime(sidecar, (future, future))
    assert precommit._pip_audit_scan_age_hours(tmp_path) is None


def test_pip_audit_scan_age_hours_fresh_stamp_computes_hours(tmp_path: Path) -> None:
    """A stamp N hours old reports age_hours ≈ N — the reuse decision's input."""
    sidecar = git_utils.code_health_dir(tmp_path) / pip_audit_json.SIDECAR_NAME
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(_scan_payload(), encoding="utf-8")
    mtime = time.time() - 2 * 3600
    os.utime(sidecar, (mtime, mtime))
    age = precommit._pip_audit_scan_age_hours(tmp_path)
    assert age == pytest.approx(2.0, abs=0.01)


def _write_scan_sidecar(repo: Path, text: str) -> Path:
    """Write raw text as the pip-audit sidecar and return its path.

    Args:
        repo: Repo root whose ``code_health/`` receives the sidecar.
        text: Exact file content (so corrupt and non-finite JSON can be written).

    Returns:
        The sidecar path.
    """
    sidecar = git_utils.code_health_dir(repo) / pip_audit_json.SIDECAR_NAME
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(text, encoding="utf-8")
    return sidecar


def _scan_payload(**extra: object) -> str:
    """Return a minimal valid pip-audit report carrying *extra* keys, as JSON.

    Args:
        **extra: Extra top-level keys such as ``scanned_at``.

    Returns:
        The JSON text.
    """
    return json.dumps({"dependencies": [], **extra})


def test_sidecar_scanned_at_payload_wins_over_mtime(tmp_path: Path) -> None:
    """The recorded scan time is used even when the file's mtime says otherwise."""
    scanned = time.time() - 5 * 3600
    sidecar = _write_scan_sidecar(tmp_path, _scan_payload(scanned_at=scanned))
    os.utime(sidecar, (time.time(), time.time()))
    assert pip_audit_json.sidecar_scanned_at(tmp_path) == pytest.approx(scanned)


def test_sidecar_scanned_at_falls_back_to_mtime_without_field(tmp_path: Path) -> None:
    """A sidecar written before ``scanned_at`` existed is dated by its mtime."""
    sidecar = _write_scan_sidecar(tmp_path, _scan_payload())
    mtime = time.time() - 3600
    os.utime(sidecar, (mtime, mtime))
    assert pip_audit_json.sidecar_scanned_at(tmp_path) == pytest.approx(mtime, abs=1)


def test_sidecar_scanned_at_missing_sidecar_is_none(tmp_path: Path) -> None:
    """No sidecar at all is unknown, which callers treat as "scan"."""
    assert pip_audit_json.sidecar_scanned_at(tmp_path) is None


@pytest.mark.parametrize("mtime_offset_s", [-600, 3600], ids=["past-mtime", "future"])
def test_sidecar_scanned_at_corrupt_payload_is_none_whatever_the_mtime(
    tmp_path: Path, mtime_offset_s: int
) -> None:
    """A corrupt payload is untrustworthy, so scan — the mtime never stands in.

    Args:
        mtime_offset_s: Seconds the sidecar's mtime sits from now.
    """
    sidecar = _write_scan_sidecar(tmp_path, "{not json")
    mtime = time.time() + mtime_offset_s
    os.utime(sidecar, (mtime, mtime))
    assert pip_audit_json.sidecar_scanned_at(tmp_path) is None


def test_sidecar_scanned_at_future_mtime_without_field_is_none(
    tmp_path: Path,
) -> None:
    """An old-format sidecar whose mtime is future-dated is rejected too."""
    sidecar = _write_scan_sidecar(tmp_path, _scan_payload())
    future = time.time() + pip_audit_json.FUTURE_TOLERANCE_S + 600
    os.utime(sidecar, (future, future))
    assert pip_audit_json.sidecar_scanned_at(tmp_path) is None


def test_sidecar_scanned_at_future_dated_payload_is_none(tmp_path: Path) -> None:
    """A payload time beyond the tolerance is rejected, not clamped or mtime-rescued.

    A future stamp would otherwise suppress scans indefinitely.
    """
    future = time.time() + pip_audit_json.FUTURE_TOLERANCE_S + 600
    _write_scan_sidecar(tmp_path, _scan_payload(scanned_at=future))
    assert pip_audit_json.sidecar_scanned_at(tmp_path) is None


def test_sidecar_scanned_at_slightly_ahead_within_tolerance_is_kept(
    tmp_path: Path,
) -> None:
    """Ordinary clock skew inside the tolerance does not discard a real scan."""
    ahead = time.time() + pip_audit_json.FUTURE_TOLERANCE_S / 2
    _write_scan_sidecar(tmp_path, _scan_payload(scanned_at=ahead))
    assert pip_audit_json.sidecar_scanned_at(tmp_path) == pytest.approx(ahead)


@pytest.mark.parametrize(
    "raw_value",
    [
        "true",
        '"yesterday"',
        "null",
        "NaN",
        "Infinity",
        "-Infinity",
        "1" + "0" * 400,
    ],
    ids=["bool", "string", "null", "nan", "inf", "neg-inf", "huge-int"],
)
def test_sidecar_scanned_at_unusable_present_value_is_none_not_mtime(
    tmp_path: Path, raw_value: str
) -> None:
    """A present-but-junk ``scanned_at`` means scan; a fresh mtime must not rescue it.

    ``true`` must not read as 1970-01-01T00:00:01, and an integer too large
    for a float must not crash with OverflowError.

    Args:
        tmp_path: Pytest temporary directory holding the sidecar.
        raw_value: Raw JSON literal written as the ``scanned_at`` value.
    """
    sidecar = _write_scan_sidecar(
        tmp_path, '{"dependencies": [], "scanned_at": ' + raw_value + "}"
    )
    mtime = time.time() - 1800
    os.utime(sidecar, (mtime, mtime))
    assert pip_audit_json.sidecar_scanned_at(tmp_path) is None


def test_pip_audit_scan_age_hours_uses_payload_time_after_mtime_reset(
    tmp_path: Path,
) -> None:
    """A copied or cache-restored sidecar keeps its real age.

    SCENARIO: the scan ran 48h ago; the file is then copied, which resets
    its mtime to now.
    EXPECTED BEHAVIOR: the reported age is still ~48h, not ~0 — the
    manufactured-fresh-scan bug.
    """
    sidecar = _write_scan_sidecar(
        tmp_path, _scan_payload(scanned_at=time.time() - 48 * 3600)
    )
    os.utime(sidecar, (time.time(), time.time()))
    assert precommit._pip_audit_scan_age_hours(tmp_path) == pytest.approx(
        48.0, abs=0.01
    )


def test_pip_audit_reuse_reason_hours_old_payload_with_fresh_mtime_scans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end: a copied sidecar (fresh mtime) of an old scan is not reused.

    SCENARIO: ``cadence = "hours"`` (24h); the payload says the scan ran 48h
    ago, but a copy gave the file an mtime of now.
    EXPECTED BEHAVIOR: ``None`` (scan), where the mtime alone would have reused.
    """
    monkeypatch.delenv(precommit._PIP_AUDIT_FORCE_ENV, raising=False)
    (tmp_path / "pyproject.toml").write_text(
        '[tool.forge.pip_audit]\ncadence = "hours"\nmax_age_hours = 24\n'
    )
    sidecar = _write_scan_sidecar(
        tmp_path, _scan_payload(scanned_at=time.time() - 48 * 3600)
    )
    os.utime(sidecar, (time.time(), time.time()))
    assert precommit._pip_audit_reuse_reason(tmp_path) is None


def test_reuse_reason_branch_without_sidecar_scans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With a resolvable fork point but no sidecar, branch cadence scans, no crash.

    SCENARIO: the fork commit resolves; nothing was ever scanned.
    MOCK SETUP: precommit.fork_point_time → a fixed commit timestamp.
    EXPECTED BEHAVIOR: ``None`` (scan) rather than a FileNotFoundError.
    """
    monkeypatch.setattr(precommit, "fork_point_time", lambda _r, _b: 1_700_000_000)
    assert precommit._reuse_reason_branch(tmp_path) is None


def test_reuse_reason_branch_uses_payload_time_not_mtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A scan older than the fork still scans though a copy gave the file a new mtime.

    MOCK SETUP: as ``test_reuse_reason_branch_without_sidecar_scans``.
    """
    fork = 1_700_000_000
    monkeypatch.setattr(precommit, "fork_point_time", lambda _r, _b: fork)
    sidecar = _write_scan_sidecar(tmp_path, _scan_payload(scanned_at=fork - 3600))
    os.utime(sidecar, (time.time(), time.time()))
    assert precommit._reuse_reason_branch(tmp_path) is None


def test_pip_audit_reuse_reason_cadence_always_never_skips(tmp_path: Path) -> None:
    """`cadence = "always"` bypasses the reuse gate even with a fresh sidecar."""
    (tmp_path / "pyproject.toml").write_text(
        '[tool.forge.pip_audit]\ncadence = "always"\n'
    )
    sidecar = git_utils.code_health_dir(tmp_path) / pip_audit_json.SIDECAR_NAME
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text("{}", encoding="utf-8")
    assert precommit._pip_audit_reuse_reason(tmp_path) is None


@pytest.mark.parametrize(
    ("max_age_hours_cfg", "sidecar_age_hours", "expected_output"),
    [
        pytest.param(
            "24",
            1.0,
            "(reused scan from 1.0h ago, under the 24h cadence — its findings "
            "follow; PR finalization forces a fresh scan, or run "
            "`forge-precommit --only pip_audit` to scan now)",
            id="fresh",
        ),
        pytest.param("24", 30.0, None, id="stale"),
        pytest.param('"bogus"', 1.0, None, id="unparseable"),
    ],
)
def test_pip_audit_reuse_reason_cadence_hours(
    tmp_path: Path,
    max_age_hours_cfg: str,
    sidecar_age_hours: float,
    expected_output: str | None,
) -> None:
    """`cadence = "hours"` skips only inside the configured window.

    Parametrizes the three branches: a sidecar younger than
    ``max_age_hours`` (reuse, with the exact notice asserted), one
    older (scan), and a non-numeric ``max_age_hours`` (scan — an
    uncertain case, per the "every uncertain case scans" contract).

    Args:
        tmp_path: Temporary directory with pyproject.toml config.
        max_age_hours_cfg: Config value for `max_age_hours` (as string).
        sidecar_age_hours: Age of the pip_audit sidecar in hours.
        expected_output: Expected reuse notice, or None to expect a scan.
    """
    (tmp_path / "pyproject.toml").write_text(
        f'[tool.forge.pip_audit]\ncadence = "hours"\n'
        f"max_age_hours = {max_age_hours_cfg}\n"
    )
    sidecar = git_utils.code_health_dir(tmp_path) / pip_audit_json.SIDECAR_NAME
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(_scan_payload(), encoding="utf-8")
    mtime = time.time() - sidecar_age_hours * 3600
    os.utime(sidecar, (mtime, mtime))

    result = precommit._pip_audit_reuse_reason(tmp_path)
    if expected_output is None:
        assert result is None
    else:
        assert result == expected_output


@pytest.mark.parametrize(
    ("sidecar_offset_s", "expect_skip"),
    [
        pytest.param(-3600, False, id="older-than-fork-scans"),
        pytest.param(0, False, id="fork-second-scans"),
        pytest.param(3600, True, id="newer-than-fork-skips"),
    ],
)
def test_pip_audit_reuse_reason_cadence_branch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    sidecar_offset_s: int,
    expect_skip: bool,
) -> None:
    """`cadence = "branch"` (the default) reuses a scan taken since the branch forked.

    SCENARIO: the sidecar's mtime straddles the fork-point commit's
    timestamp.
    MOCK SETUP: precommit.fork_point_time → a fixed fork-commit timestamp
    (its git lookup has its own tests in test_git_utils), so no real git
    repo is needed.
    EXPECTED BEHAVIOR: a sidecar older than the fork commit means no scan
    has happened on this branch yet (scan); newer means one already has
    (skip).

    Args:
        tmp_path: Temporary directory with pyproject.toml config.
        monkeypatch: Pytest fixture for mocking.
        sidecar_offset_s: Offset in seconds from fork commit timestamp.
        expect_skip: Whether to expect the scan to be skipped.
    """
    fork_committed_at = 1_700_000_000
    monkeypatch.setattr(
        precommit, "fork_point_time", lambda _root, _base: fork_committed_at
    )

    sidecar = git_utils.code_health_dir(tmp_path) / pip_audit_json.SIDECAR_NAME
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(_scan_payload(), encoding="utf-8")
    sidecar_mtime = fork_committed_at + sidecar_offset_s
    os.utime(sidecar, (sidecar_mtime, sidecar_mtime))

    result = precommit._pip_audit_reuse_reason(tmp_path)
    if expect_skip:
        assert result is not None
        assert "already scanned on this branch" in result
        assert "its findings follow" in result
    else:
        assert result is None


def test_pip_audit_reuse_reason_unknown_cadence_scans(tmp_path: Path) -> None:
    """An unrecognised `cadence` value is an uncertain case — it always scans."""
    (tmp_path / "pyproject.toml").write_text(
        '[tool.forge.pip_audit]\ncadence = "fortnightly"\n'
    )
    sidecar = git_utils.code_health_dir(tmp_path) / pip_audit_json.SIDECAR_NAME
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text("{}", encoding="utf-8")
    assert precommit._pip_audit_reuse_reason(tmp_path) is None


def test_pip_audit_reuse_reason_env_override_forces_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FORGE_PIP_AUDIT_FORCE=1 always scans, even with a fresh sidecar.

    This is the escape hatch `main()` sets for `--only pip_audit` (see
    test_main_only_pip_audit_sets_force_env) — checked before any
    cadence config is even read.
    """
    monkeypatch.setenv(precommit._PIP_AUDIT_FORCE_ENV, "1")
    sidecar = git_utils.code_health_dir(tmp_path) / pip_audit_json.SIDECAR_NAME
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text("{}", encoding="utf-8")
    assert precommit._pip_audit_reuse_reason(tmp_path) is None


def _seed_reusable_audit_sidecar(
    tmp_path: Path, data: dict, *, blocking: bool = False
) -> None:
    """Write a fresh sidecar plus an hours-cadence config that reuses it.

    Args:
        tmp_path: Temporary directory path.
        data: JSON-serializable sidecar data.
        blocking: Whether pip_audit failures block the step.
    """
    (tmp_path / "pyproject.toml").write_text(
        '[tool.forge.pip_audit]\ncadence = "hours"\nmax_age_hours = 24\n'
        f"blocking = {str(blocking).lower()}\n"
    )
    sidecar = git_utils.code_health_dir(tmp_path) / pip_audit_json.SIDECAR_NAME
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(json.dumps(data), encoding="utf-8")


def _boom(_root: object) -> None:
    """Raise AssertionError when run_json is unexpectedly called.

    Args:
        _root: Unused argument passed by the mocked function.
    """
    msg = "run_json must not be called when reusing a scan"
    raise AssertionError(msg)


def test_step_pip_audit_reuse_never_calls_run_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reuse short-circuits before the network call and reports the clean scan.

    SCENARIO: `cadence = "hours"` with a wide window and a fresh, clean
    sidecar — the reuse path.
    MOCK SETUP: precommit.pip_audit_json.run_json raises if called at
    all, proving the gate short-circuits before reaching it.
    EXPECTED BEHAVIOR: a passing, non-skipped StepResult whose output
    opens with the reuse notice followed by the clean report.
    """
    _seed_reusable_audit_sidecar(tmp_path, _audit_run(0).data)
    monkeypatch.setattr(precommit.pip_audit_json, "run_json", _boom)
    result = precommit.step_pip_audit(tmp_path)
    assert not result.skipped
    assert result.passed
    assert result.output.startswith("(reused scan from")
    assert "No known vulnerabilities" in result.output


@pytest.mark.parametrize(
    "n_vulns",
    [2, precommit._PIP_AUDIT_LOUDNESS_THRESHOLD + 5],
    ids=["few", "over-threshold"],
)
def test_step_pip_audit_reused_findings_are_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, n_vulns: int
) -> None:
    """A reused scan with findings reports them exactly like a fresh scan.

    SCENARIO: fresh sidecar holding N advisories, non-blocking repo.
    MOCK SETUP: run_json raises if called (reuse must not rescan).
    EXPECTED BEHAVIOR: failing non-blocking, non-skipped result; the
    notice precedes the report, the advisory id is present, and the
    loudness banner appears only above the threshold.

    Args:
        tmp_path: Temporary repo root.
        monkeypatch: Pytest fixture for mocking.
        n_vulns: Number of advisories in the seeded sidecar.
    """
    _seed_reusable_audit_sidecar(tmp_path, _audit_run(n_vulns).data)
    monkeypatch.setattr(precommit.pip_audit_json, "run_json", _boom)
    result = precommit.step_pip_audit(tmp_path)
    assert not result.passed
    assert result.non_blocking
    assert not result.skipped
    assert "PYSEC-2024-0" in result.output
    assert result.output.index("(reused scan") < result.output.index("PYSEC-2024-0")
    large = n_vulns > precommit._PIP_AUDIT_LOUDNESS_THRESHOLD
    assert ("Consider filing a tracking issue" in result.output) is large


def test_step_pip_audit_blocking_reused_findings_rescan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A blocking repo never fails a commit on a reused scan with findings.

    SCENARIO: blocking = true, fresh sidecar with a finding.
    MOCK SETUP: run_json counts calls and returns _audit_run(1).
    EXPECTED BEHAVIOR: the scan runs; the result is a real blocking
    failure with no reuse notice.
    """
    _seed_reusable_audit_sidecar(tmp_path, _audit_run(1).data, blocking=True)
    calls: list[object] = []

    def _counting(root: object) -> AuditRun:
        calls.append(root)
        return _audit_run(1)

    monkeypatch.setattr(precommit.pip_audit_json, "run_json", _counting)
    result = precommit.step_pip_audit(tmp_path)
    assert len(calls) == 1
    assert not result.passed
    assert not result.non_blocking
    assert "reused scan" not in result.output


_UNUSABLE_SIDECAR_TEXT = {"malformed": "{not json", "non-dict": "[]", "skeletal": "{}"}


@pytest.mark.parametrize(
    "sidecar_kind", ["malformed", "non-dict", "skeletal", "unreadable"]
)
def test_step_pip_audit_unusable_sidecar_scans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sidecar_kind: str
) -> None:
    """A sidecar that cannot be trusted falls back to a real scan.

    SCENARIO: the cadence gate says reuse, but the sidecar is malformed
    JSON, a JSON non-object, an object with no ``dependencies`` list (a
    skeletal ``{}`` would otherwise render as a clean scan), or
    unreadable (a directory at its path).
    MOCK SETUP: _pip_audit_reuse_reason is pinned to a fixed notice so
    the gate's mtime logic is out of play; run_json counts calls and
    returns a clean run.
    EXPECTED BEHAVIOR: run_json is called once and no reuse notice
    appears in the output.

    Args:
        tmp_path: Temporary repo root.
        monkeypatch: Pytest fixture for mocking.
        sidecar_kind: Which kind of unusable sidecar to create.
    """
    sidecar = git_utils.code_health_dir(tmp_path) / pip_audit_json.SIDECAR_NAME
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    if sidecar_kind == "unreadable":
        sidecar.mkdir()
    else:
        sidecar.write_text(_UNUSABLE_SIDECAR_TEXT[sidecar_kind], encoding="utf-8")
    monkeypatch.setattr(
        precommit, "_pip_audit_reuse_reason", lambda _root: "(reused scan from 1h)"
    )
    calls: list[object] = []

    def _counting(root: object) -> AuditRun:
        calls.append(root)
        return _audit_run(0)

    monkeypatch.setattr(precommit.pip_audit_json, "run_json", _counting)
    result = precommit.step_pip_audit(tmp_path)
    assert len(calls) == 1
    assert "reused scan" not in result.output


def test_run_all_reused_pip_audit_output_carries_advisory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The step result run_all hands to the log writer names the reused advisory.

    SCENARIO: `run_all(only=["pip_audit"])` over a repo with a fresh
    sidecar holding a finding (run_all itself does not set the force env;
    only main() does).
    MOCK SETUP: run_json raises if called.
    EXPECTED BEHAVIOR: the pip_audit result's output contains the id.
    """
    _seed_reusable_audit_sidecar(tmp_path, _audit_run(1).data)
    monkeypatch.delenv(precommit._PIP_AUDIT_FORCE_ENV, raising=False)
    monkeypatch.setattr(precommit.pip_audit_json, "run_json", _boom)
    results = precommit.run_all(
        repo_root=tmp_path, print_progress=False, only=["pip_audit"]
    )
    assert [r.name for r in results] == ["pip_audit"]
    assert "PYSEC-2024-0" in results[0].output


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
    non_blocking StepResult; is_ci is pinned to True (deterministic —
    the echo-in-CI branch must not fire for a non-blocking warning
    regardless of the ambient environment); sys.argv is patched to the
    bare `forge-precommit` invocation.
    EXPECTED BEHAVIOR: main() returns 0; stdout prints WARN, the
    all-blocking-passed summary, and names pip_audit with its log path.
    """
    monkeypatch.setattr(precommit, "get_repo_root", lambda: tmp_path)
    _stub_docstrings_passing(monkeypatch)
    _stub_test_naming_passing(monkeypatch)
    _stub_repo_structure_passing(monkeypatch)
    _stub_docstring_coverage_skipped(monkeypatch)
    monkeypatch.setattr(precommit, "is_ci", lambda: True)

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


def test_main_does_not_echo_non_blocking_output_in_ci(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A non-blocking step's output is never echoed, even in CI.

    SCENARIO: the CI output-echo added for blocking failures must not
    also fire for a non-blocking (advisory) warning — echoing is bounded
    to blocking failures only.
    MOCK SETUP: same harness as
    ``test_non_blocking_warning_does_not_fail_main``, with is_ci pinned
    to True.
    EXPECTED BEHAVIOR: main() returns 0; the failing non-blocking step's
    own output text is absent from stdout.
    """
    monkeypatch.setattr(precommit, "get_repo_root", lambda: tmp_path)
    _stub_docstrings_passing(monkeypatch)
    _stub_test_naming_passing(monkeypatch)
    _stub_repo_structure_passing(monkeypatch)
    _stub_docstring_coverage_skipped(monkeypatch)
    monkeypatch.setattr(precommit, "is_ci", lambda: True)

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
    assert "(simulated CVE finding)" not in out


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
    StepResults; is_ci is pinned to False (deterministic — this test
    only checks the pointer lines, not the CI output echo, so it must
    not depend on the ambient environment); sys.argv is patched to the
    bare `forge-precommit` invocation.
    EXPECTED BEHAVIOR: main() returns 1; stdout prints the failure
    header and one "<step>: see code_health/<step>.log" line per
    failed step.
    """
    monkeypatch.setattr(precommit, "get_repo_root", lambda: tmp_path)
    _stub_repo_structure_passing(monkeypatch)
    _stub_pip_audit_skipped(monkeypatch)
    _stub_docstring_coverage_skipped(monkeypatch)
    monkeypatch.setattr(precommit, "is_ci", lambda: False)

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


def test_main_echoes_blocking_step_output_in_ci(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """In CI, a blocking failure's own output is echoed under its log pointer.

    SCENARIO: `code_health/<step>.log` lives on a CI runner that no
    longer exists by the time anyone reads the run, so the failure must
    name what was wrong inline rather than only pointing at a log.
    MOCK SETUP: same harness as
    ``test_main_lists_failed_steps_with_log_paths``, with is_ci pinned
    to True instead of False.
    EXPECTED BEHAVIOR: main() returns 1; the failing step's own output
    text appears in stdout under its "see code_health/…" line.
    """
    monkeypatch.setattr(precommit, "get_repo_root", lambda: tmp_path)
    _stub_repo_structure_passing(monkeypatch)
    _stub_pip_audit_skipped(monkeypatch)
    _stub_docstring_coverage_skipped(monkeypatch)
    monkeypatch.setattr(precommit, "is_ci", lambda: True)

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
    assert "docstring_verification: see code_health/docstring_verification.log" in out
    assert "(simulated docstring error)" in out
    assert "test_naming_check: see code_health/test_naming_check.log" in out
    assert "(simulated naming violation)" in out


@pytest.mark.parametrize(
    ("setting", "expected"),
    [
        ("", True),
        ("echo_ci_output = false", False),
        ("echo_ci_output = true", True),
        ('echo_ci_output = "false"', True),
        ("echo_ci_output = 0", True),
    ],
)
def test_echo_ci_output_enabled_only_boolean_false_disables(
    tmp_path: Path, setting: str, *, expected: bool
) -> None:
    """Only the boolean ``false`` turns the CI echo off; anything else keeps it.

    BEHAVIOR: a mistyped string ``"false"`` must not be read as truthy-off
    and silently hide the failure text that makes a CI failure readable.

    Args:
        setting: Line written under ``[tool.forge.precommit]`` ("" = absent).
        expected: Expected result of the helper.
    """
    _write_pyproject(tmp_path, f"[tool.forge.precommit]\n{setting}\n")
    assert precommit._echo_ci_output_enabled(tmp_path) is expected


@pytest.mark.parametrize(("echo", "shown"), [(True, True), (False, False)])
def test_emit_human_summary_echo_ci_output_gates_step_output_in_ci(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *,
    echo: bool,
    shown: bool,
) -> None:
    """In CI the failing step's output appears only when echo is enabled.

    BEHAVIOR: the log pointer is always printed; ``echo_ci_output=False``
    drops just the step's own output.

    Args:
        echo: Value passed as ``echo_ci_output``.
        shown: Whether the output text is expected in stdout.
    """
    monkeypatch.setattr(precommit, "is_ci", lambda: True)
    failing = precommit.StepResult(name="ruff", passed=False, output="SECRET-DETAIL")

    precommit._emit_human_summary([failing], [failing], [], echo_ci_output=echo)

    out = capsys.readouterr().out
    assert "ruff: see code_health/ruff.log" in out
    assert ("SECRET-DETAIL" in out) is shown


def test_main_config_echo_ci_output_false_keeps_only_the_pointer_in_ci(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`[tool.forge.precommit] echo_ci_output = false` suppresses the CI echo.

    BEHAVIOR: the config key reaches the summary through `main`.
    MOCK SETUP: same harness as ``test_main_echoes_blocking_step_output_in_ci``,
    one failing step, with the key set in the repo's pyproject.
    """
    monkeypatch.setattr(precommit, "get_repo_root", lambda: tmp_path)
    _write_pyproject(tmp_path, "[tool.forge.precommit]\necho_ci_output = false\n")
    _stub_repo_structure_passing(monkeypatch)
    _stub_pip_audit_skipped(monkeypatch)
    _stub_docstring_coverage_skipped(monkeypatch)
    monkeypatch.setattr(precommit, "is_ci", lambda: True)
    monkeypatch.setattr(
        precommit,
        "step_docstrings",
        lambda _root: precommit.StepResult(
            name="docstring_verification", passed=False, output="(simulated error)"
        ),
    )
    with patch.object(precommit.sys, "argv", ["forge-precommit"]):
        rc = precommit.main()
    out = capsys.readouterr().out
    assert rc == 1
    assert "docstring_verification: see code_health/docstring_verification.log" in out
    assert "(simulated error)" not in out


def test_main_does_not_echo_blocking_step_output_locally(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Locally, a blocking failure's output is NOT echoed — only the pointer.

    SCENARIO: the CI output echo must not fire outside CI, where the
    log file is right there on disk and dumping output into every local
    commit would be noise.
    MOCK SETUP: same harness as
    ``test_main_lists_failed_steps_with_log_paths``, with is_ci pinned
    to False.
    EXPECTED BEHAVIOR: main() returns 1; the pointer line is present but
    the failing step's own output text is absent from stdout.
    """
    monkeypatch.setattr(precommit, "get_repo_root", lambda: tmp_path)
    _stub_repo_structure_passing(monkeypatch)
    _stub_pip_audit_skipped(monkeypatch)
    _stub_docstring_coverage_skipped(monkeypatch)
    monkeypatch.setattr(precommit, "is_ci", lambda: False)

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
    assert "docstring_verification: see code_health/docstring_verification.log" in out
    assert "(simulated docstring error)" not in out
    assert "test_naming_check: see code_health/test_naming_check.log" in out
    assert "(simulated naming violation)" not in out


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


def test_step_registry_checks_files_false_only_for_environment_steps() -> None:
    """Exactly `auto_rebuild`, `env_sync`, `plugin_sync` opt out of `checks_files`.

    These three steps judge the installed environment rather than the
    tracked tree, so a tree-freshness verdict is meaningless for them (see
    the `--freshness` `n/a` tests below). Every other registry entry keeps
    the `checks_files=True` default — pinning the exact opt-out set here
    means a future environment-only step must choose it deliberately
    instead of silently inheriting a tree-based verdict.
    """
    opted_out = {
        step.name for step in precommit._STEP_REGISTRY if not step.checks_files
    }
    assert opted_out == {"auto_rebuild", "env_sync", "plugin_sync"}


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
    names = _names(precommit.resolve_steps(tmp_path))
    assert "ruff" in names
    assert "doctest" not in names
    assert "typecheck" not in names
    assert "doc_consistency" not in names
    assert "api_digest_check" not in names


def test_resolve_steps_enable_adds_opt_in(tmp_path: Path) -> None:
    """`[tool.forge.precommit] enable` opts a normally-off step in."""
    _write_pyproject(tmp_path, '[tool.forge.precommit]\nenable = ["doctest"]\n')
    assert "doctest" in _names(precommit.resolve_steps(tmp_path))


def test_resolve_steps_disable_removes_default(tmp_path: Path) -> None:
    """`[tool.forge.precommit] disable` force-skips a default step."""
    _write_pyproject(tmp_path, '[tool.forge.precommit]\ndisable = ["pip_audit"]\n')
    assert "pip_audit" not in _names(precommit.resolve_steps(tmp_path))


def test_resolve_steps_disable_beats_enable(tmp_path: Path) -> None:
    """When a name is in both `enable` and `disable`, `disable` wins."""
    _write_pyproject(
        tmp_path,
        '[tool.forge.precommit]\nenable = ["doctest"]\ndisable = ["doctest"]\n',
    )
    assert "doctest" not in _names(precommit.resolve_steps(tmp_path))


def test_resolve_steps_skip_removes(tmp_path: Path) -> None:
    """The `skip` argument removes a step for this run only."""
    assert "ruff" not in _names(precommit.resolve_steps(tmp_path, skip=["ruff"]))


def test_resolve_steps_only_overrides_in_registry_order(tmp_path: Path) -> None:
    """`only=[...]` runs exactly those steps, ordered by the registry not the arg."""
    resolved = _names(precommit.resolve_steps(tmp_path, only=["pip_audit", "ruff"]))
    assert resolved == ["ruff", "pip_audit"]


def test_resolve_steps_only_still_honors_skip(tmp_path: Path) -> None:
    """`skip` subtracts from the `only` set too — it is never silently ignored."""
    resolved = _names(
        precommit.resolve_steps(
            tmp_path, only=["ruff", "pip_audit"], skip=["pip_audit"]
        )
    )
    assert resolved == ["ruff"]


def test_resolve_steps_unknown_name_raises(tmp_path: Path) -> None:
    """An unknown name in config / skip / only raises ValueError."""
    with pytest.raises(ValueError, match="unknown step name"):
        precommit.resolve_steps(tmp_path, only=["bogus"])


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


def test_main_only_pip_audit_sets_force_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--only pip_audit` forces a scan for the duration of that run.

    SCENARIO: naming the step explicitly means the cadence gate
    must not silently reuse a stale scan — but a step function receives
    only `repo_root`, so main() communicates the override via env var
    rather than a parameter. The other `--only` tests above stub the
    step and never observe this, so it needs its own case.

    MOCK SETUP: step_pip_audit is stubbed and records what the env var
    held while it ran.

    EXPECTED BEHAVIOR: the step sees the override, and the environment
    is left as it was found — the flag belongs to the run, not to the
    process that happened to start it.
    """
    monkeypatch.setattr(precommit, "get_repo_root", lambda: tmp_path)
    monkeypatch.setenv(precommit._PIP_AUDIT_FORCE_ENV, "0")
    seen: list[str | None] = []

    def _audit(_root: object) -> precommit.StepResult:
        seen.append(os.environ.get(precommit._PIP_AUDIT_FORCE_ENV))
        return precommit.StepResult(name="pip_audit", passed=True, output="x")

    monkeypatch.setattr(precommit, "step_pip_audit", _audit)
    with patch.object(
        precommit.sys, "argv", ["forge-precommit", "--only", "pip_audit"]
    ):
        rc = precommit.main()
    assert rc == 0
    assert seen == ["1"]
    assert os.environ.get(precommit._PIP_AUDIT_FORCE_ENV) == "0"


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
    assert captured["cmd"][:3] == ["pyrefly", "check", "--"]
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


def test_step_typecheck_diff_scope_builds_prefix_from_resolved_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Diff scope forwards a `(root/,)` prefix and the resolved repo_root.

    MOCK SETUP: `[tool.forge.typecheck].paths = ["src"]` resolves the scan
    root; `get_modified_files` is stubbed to capture its kwargs instead of
    shelling out to git, returning one file under that root.
    """
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
    _write_pyproject(
        tmp_path,
        '[tool.forge.precommit]\nscope = "diff"\n\n'
        '[tool.forge.typecheck]\npaths = ["src"]\n',
    )
    _present(monkeypatch)
    captured: dict[str, object] = {}

    def _fake_get_modified_files(**kwargs: object) -> list[str]:
        captured.update(kwargs)
        return ["src/a.py"]

    monkeypatch.setattr(config, "get_modified_files", _fake_get_modified_files)
    run_captured: dict[str, list[str]] = {}

    def _run(cmd: list[str], **_kw: object) -> tuple[bool, str]:
        run_captured["cmd"] = cmd
        return True, "0 errors"

    monkeypatch.setattr(precommit, "_run", _run)
    precommit.step_typecheck(tmp_path)
    assert captured["prefix"] == ("src/",)
    assert captured["repo_root"] == tmp_path
    assert run_captured["cmd"] == ["pyrefly", "check", "--", "src/a.py"]


def test_step_typecheck_diff_scope_root_dot_disables_prefix_filter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `paths = ["."]` root disables the prefix filter (whole diff eligible)."""
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    _write_pyproject(
        tmp_path,
        '[tool.forge.precommit]\nscope = "diff"\n\n'
        '[tool.forge.typecheck]\npaths = ["."]\n',
    )
    _present(monkeypatch)
    captured: dict[str, object] = {}

    def _fake_get_modified_files(**kwargs: object) -> list[str]:
        captured.update(kwargs)
        return ["a.py"]

    monkeypatch.setattr(config, "get_modified_files", _fake_get_modified_files)
    monkeypatch.setattr(precommit, "_run", lambda _cmd, **_kw: (True, "0 errors"))
    precommit.step_typecheck(tmp_path)
    assert captured["prefix"] is None


def test_step_typecheck_diff_scope_multi_root_prefixes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Multiple `paths` roots each become their own `root/` prefix entry."""
    (tmp_path / "src").mkdir()
    (tmp_path / "lib").mkdir()
    _write_pyproject(
        tmp_path,
        '[tool.forge.precommit]\nscope = "diff"\n\n'
        '[tool.forge.typecheck]\npaths = ["src", "lib"]\n',
    )
    _present(monkeypatch)
    captured: dict[str, object] = {}

    def _fake_get_modified_files(**kwargs: object) -> list[str]:
        captured.update(kwargs)
        return []

    monkeypatch.setattr(config, "get_modified_files", _fake_get_modified_files)
    precommit.step_typecheck(tmp_path)
    assert captured["prefix"] == ("src/", "lib/")


def test_step_typecheck_diff_scope_skips_when_no_modified_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Diff scope skips cleanly before `require_cli` when nothing modified.

    MOCK SETUP: pyrefly is absent from PATH — proves the skip fires before
    the CLI presence check, so an empty diff never demands pyrefly be
    installed.
    """
    (tmp_path / "src").mkdir()
    _write_pyproject(
        tmp_path,
        '[tool.forge.precommit]\nscope = "diff"\n\n'
        '[tool.forge.typecheck]\npaths = ["src"]\n',
    )
    monkeypatch.setattr(config, "get_modified_files", lambda **_kw: [])
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    result = precommit.step_typecheck(tmp_path)
    assert result.skipped
    assert result.passed
    assert "(no modified files in scope — skipped)" in result.output


def test_step_typecheck_diff_scope_filters_deleted_files_from_disk(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deleted-on-disk modified file is dropped before the pyrefly invocation."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "kept.py").write_text("x = 1\n", encoding="utf-8")
    _write_pyproject(
        tmp_path,
        '[tool.forge.precommit]\nscope = "diff"\n\n'
        '[tool.forge.typecheck]\npaths = ["src"]\n',
    )
    _present(monkeypatch)
    monkeypatch.setattr(
        config,
        "get_modified_files",
        lambda **_kw: ["src/deleted.py", "src/kept.py"],
    )
    run_captured: dict[str, list[str]] = {}

    def _run(cmd: list[str], **_kw: object) -> tuple[bool, str]:
        run_captured["cmd"] = cmd
        return True, "0 errors"

    monkeypatch.setattr(precommit, "_run", _run)
    precommit.step_typecheck(tmp_path)
    assert run_captured["cmd"][3:] == ["src/kept.py"]


def test_step_typecheck_diff_scope_skips_when_all_modified_files_deleted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Skip when every modified file in scope was deleted, not just when none matched.

    MOCK SETUP: pyrefly is absent from PATH — same skip-before-require_cli
    contract as the empty-diff case, exercised here via a diff that names
    one file which no longer exists on disk.
    """
    (tmp_path / "src").mkdir()
    _write_pyproject(
        tmp_path,
        '[tool.forge.precommit]\nscope = "diff"\n\n'
        '[tool.forge.typecheck]\npaths = ["src"]\n',
    )
    monkeypatch.setattr(config, "get_modified_files", lambda **_kw: ["src/gone.py"])
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    result = precommit.step_typecheck(tmp_path)
    assert result.skipped
    assert result.passed
    assert "(no modified files in scope — skipped)" in result.output


# ---------------------------------------------------------------------------
# step_layering
# ---------------------------------------------------------------------------


def test_step_layering_no_config_skips_before_require_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No `[tool.forge.layering]` layers skips cleanly without reaching `require_cli`.

    MOCK SETUP: the CLI is absent from PATH — asserting no `SystemExit` is
    raised proves the skip check runs before `require_cli`.
    """
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    result = precommit.step_layering(tmp_path)
    assert result.skipped
    assert result.passed
    assert "(no [tool.forge.layering] layers — skipped)" in result.output


def test_step_layering_configured_clean_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A configured layer runs `forge-audit-layering --scope changed`."""
    _write_pyproject(
        tmp_path,
        '[[tool.forge.layering.layer]]\nname = "domain"\npackage = "myproj.domain"\n',
    )
    _present(monkeypatch)
    captured: dict[str, list[str]] = {}

    def _run(cmd: list[str], **_kw: object) -> tuple[bool, str]:
        captured["cmd"] = cmd
        return True, "clean"

    monkeypatch.setattr(precommit, "_run", _run)
    result = precommit.step_layering(tmp_path)
    assert captured["cmd"] == ["forge-audit-layering", "--scope", "changed"]
    assert result.passed
    assert not result.skipped
    assert not result.non_blocking


def test_step_layering_violation_fails_blocking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A HIGH violation fails the step; layering has no non-blocking knob."""
    _write_pyproject(
        tmp_path,
        '[[tool.forge.layering.layer]]\nname = "domain"\npackage = "myproj.domain"\n',
    )
    _present(monkeypatch)
    monkeypatch.setattr(
        precommit, "_run", lambda _cmd, **_kw: (False, "HIGH violation")
    )
    result = precommit.step_layering(tmp_path)
    assert not result.passed
    assert not result.non_blocking


def test_step_layering_configured_missing_cli_exits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An opted-in-but-absent `forge-audit-layering` binary fails loudly."""
    _write_pyproject(
        tmp_path,
        '[[tool.forge.layering.layer]]\nname = "domain"\npackage = "myproj.domain"\n',
    )
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    with pytest.raises(SystemExit):
        precommit.step_layering(tmp_path)


def test_step_layering_require_all_classified_without_layer_table_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`require_all_classified = true` alone (no `[[layer]]` table) still runs the CLI.

    The flag-without-layers case is a config error the CLI itself must
    report — the step's skip check only fires when BOTH `layer` and
    `require_all_classified` are absent.
    """
    _write_pyproject(
        tmp_path,
        "[tool.forge.layering]\nrequire_all_classified = true\n",
    )
    _present(monkeypatch)
    captured: dict[str, list[str]] = {}

    def _run(cmd: list[str], **_kw: object) -> tuple[bool, str]:
        captured["cmd"] = cmd
        return False, "require_all_classified = true needs at least one"

    monkeypatch.setattr(precommit, "_run", _run)
    result = precommit.step_layering(tmp_path)
    assert not result.skipped
    assert not result.passed
    assert captured["cmd"] == ["forge-audit-layering", "--scope", "changed"]


# ---------------------------------------------------------------------------
# step_api_digest_check
# ---------------------------------------------------------------------------


def test_step_api_digest_check_passes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runs `forge-gen-api-digest --check` and mirrors a passing exit.

    MOCK SETUP: `docs/api-digest.md` present (so the step doesn't skip);
    ``_run`` captures the command and returns a passing canned result.
    """
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "api-digest.md").write_text("# API Digest\n", encoding="utf-8")
    _present(monkeypatch)
    captured: dict[str, list[str]] = {}

    def _run(cmd: list[str], **_kw: object) -> tuple[bool, str]:
        captured["cmd"] = cmd
        return True, "up to date"

    monkeypatch.setattr(precommit, "_run", _run)
    result = precommit.step_api_digest_check(tmp_path)
    assert captured["cmd"] == [sys.executable, "-m", "forge.gen_api_digest", "--check"]
    assert result.passed
    assert not result.non_blocking
    assert not result.skipped


def test_step_api_digest_check_drift_is_blocking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A drifted digest fails the step and stays blocking (refuses commit)."""
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "api-digest.md").write_text("# API Digest\n", encoding="utf-8")
    _present(monkeypatch)
    monkeypatch.setattr(precommit, "_run", lambda _cmd, **_kw: (False, "out of sync"))
    result = precommit.step_api_digest_check(tmp_path)
    assert not result.passed
    assert not result.non_blocking


def test_step_api_digest_check_no_doc_is_skipped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No `docs/api-digest.md` skips cleanly without reaching `require_cli`.

    MOCK SETUP: the CLI is absent from PATH — asserting no `SystemExit` is
    raised proves the skip check runs before `require_cli`.
    """
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    result = precommit.step_api_digest_check(tmp_path)
    assert result.skipped
    assert result.passed
    assert "(no docs/api-digest.md — skipped)" in result.output


# ---------------------------------------------------------------------------
# step_cli_reference_check
# ---------------------------------------------------------------------------


def test_step_cli_reference_check_passes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runs `forge-gen-cli-reference --check` and mirrors a passing exit.

    MOCK SETUP: `docs/cli-reference.md` present (so the step doesn't skip);
    ``_run`` captures the command and returns a passing canned result.
    """
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "cli-reference.md").write_text(
        "# CLI Reference\n", encoding="utf-8"
    )
    _present(monkeypatch)
    captured: dict[str, list[str]] = {}

    def _run(cmd: list[str], **_kw: object) -> tuple[bool, str]:
        captured["cmd"] = cmd
        return True, "up to date"

    monkeypatch.setattr(precommit, "_run", _run)
    result = precommit.step_cli_reference_check(tmp_path)
    assert captured["cmd"] == [
        sys.executable,
        "-m",
        "forge.gen_cli_reference",
        "--check",
    ]
    assert result.passed
    assert not result.non_blocking
    assert not result.skipped


def test_step_cli_reference_check_drift_is_blocking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A drifted reference doc fails the step and stays blocking (refuses commit)."""
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "cli-reference.md").write_text(
        "# CLI Reference\n", encoding="utf-8"
    )
    _present(monkeypatch)
    monkeypatch.setattr(precommit, "_run", lambda _cmd, **_kw: (False, "out of sync"))
    result = precommit.step_cli_reference_check(tmp_path)
    assert not result.passed
    assert not result.non_blocking


def test_step_cli_reference_check_no_doc_is_skipped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No `docs/cli-reference.md` skips cleanly without reaching `require_cli`.

    MOCK SETUP: the CLI is absent from PATH — asserting no `SystemExit` is
    raised proves the skip check runs before `require_cli`.
    """
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    result = precommit.step_cli_reference_check(tmp_path)
    assert result.skipped
    assert result.passed
    assert "(no docs/cli-reference.md — skipped)" in result.output


@pytest.mark.parametrize(
    ("step", "doc", "module"),
    [
        ("step_api_digest_check", "docs/api-digest.md", "forge.gen_api_digest"),
        (
            "step_cli_reference_check",
            "docs/cli-reference.md",
            "forge.gen_cli_reference",
        ),
        ("step_c4", "docs/architecture.dsl", "forge.gen_c4"),
    ],
)
def test_generator_check_steps_launch_running_install_not_path_decoy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    step: str,
    doc: str,
    module: str,
) -> None:
    """The generator is launched as ``python -m <module>``, never by bare name.

    BEHAVIOR: a stale forge checkout earlier on PATH must not judge the
    committed doc; the argv names the running interpreter and module.
    MOCK SETUP: ``which`` resolves the bare CLI name to a decoy path and
    ``_run`` captures the argv; c4 is opted in by stubbing its model lookup.

    Args:
        step: Name of the step function under test.
        doc: Repo-relative generated doc the step guards.
        module: Module the running install declares for the generator.
    """
    (tmp_path / "docs").mkdir()
    (tmp_path / doc).write_text("x\n", encoding="utf-8")
    monkeypatch.setattr(shutil, "which", lambda name: f"/decoy/{name}")
    monkeypatch.setattr(precommit, "resolve_model_section", lambda _root: object())
    captured: dict[str, list[str]] = {}

    def _run(cmd: list[str], **_kw: object) -> tuple[bool, str]:
        captured["cmd"] = cmd
        return True, ""

    monkeypatch.setattr(precommit, "_run", _run)
    getattr(precommit, step)(tmp_path)

    assert captured["cmd"] == [sys.executable, "-m", module, "--check"]


# ---------------------------------------------------------------------------
# step_foundation_md_check
# ---------------------------------------------------------------------------


def _patch_resources_ref(monkeypatch: pytest.MonkeyPatch, ref_path: Path) -> None:
    """Stub `precommit.resources` so `step_foundation_md_check` sees *ref_path*.

    Args:
        monkeypatch: Pytest fixture.
        ref_path: Path the stubbed `resources.as_file` context manager yields
            as the "installed" `FOUNDATION.md` reference.
    """

    class _FakeRef:
        """Mock resources.files return value for testing."""

        def joinpath(self, *_parts: str) -> Path:
            """Join path parts.

            Args:
                *_parts: Path parts (unused; always returns ref_path).

            Returns:
                The mocked ref_path.
            """
            return ref_path

    monkeypatch.setattr(precommit.resources, "files", lambda _pkg: _FakeRef())
    monkeypatch.setattr(
        precommit.resources, "as_file", lambda _ref: contextlib.nullcontext(ref_path)
    )


def test_step_foundation_md_check_no_foundation_is_skipped(
    tmp_path: Path,
) -> None:
    """No `FOUNDATION.md` in the repo skips cleanly — nothing to verify."""
    result = precommit.step_foundation_md_check(tmp_path)
    assert result.skipped
    assert result.passed
    assert "(no FOUNDATION.md — skipped)" in result.output


def test_step_foundation_md_check_self_referential_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The editable-install self-reference case FAILs even if content matches.

    MOCK SETUP: the "installed" reference resolves to the very
    `FOUNDATION.md` under review (as in forge's own editable install,
    where the packaged copy is a symlink to the tracked file).
    `foundation_matches_installed` is stubbed `True` to prove
    self-reference is checked first and wins regardless.
    """
    target = tmp_path / "FOUNDATION.md"
    target.write_text("# FOUNDATION.md\n", encoding="utf-8")
    _patch_resources_ref(monkeypatch, target)
    monkeypatch.setattr(precommit, "foundation_matches_installed", lambda _p: True)
    result = precommit.step_foundation_md_check(tmp_path)
    assert not result.passed
    assert not result.skipped
    assert "editable install" in result.output


def test_step_foundation_md_check_resources_unavailable_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unresolvable installed reference FAILs — provenance unverifiable."""
    target = tmp_path / "FOUNDATION.md"
    target.write_text("# FOUNDATION.md\n", encoding="utf-8")

    def _raise(_pkg: str) -> None:
        msg = "forge"
        raise ModuleNotFoundError(msg)

    monkeypatch.setattr(precommit.resources, "files", _raise)
    result = precommit.step_foundation_md_check(tmp_path)
    assert not result.passed
    assert "provenance unverifiable" in result.output


def test_step_foundation_md_check_missing_resource_during_match_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resource that vanishes mid-compare FAILs cleanly, not a crash.

    MOCK SETUP: the installed reference resolves to a *different*,
    existing file (so `self_referential` is False and the self-reference
    branch is skipped), but `foundation_matches_installed` — called
    inside the same `try` as the self-reference check — raises
    `FileNotFoundError` (a subclass of `OSError`), simulating the
    resource disappearing between resolving the reference and reading
    its content.
    """
    target = tmp_path / "FOUNDATION.md"
    target.write_text("# FOUNDATION.md\n", encoding="utf-8")
    ref = tmp_path / "installed-FOUNDATION.md"
    ref.write_text("# FOUNDATION.md\n", encoding="utf-8")
    _patch_resources_ref(monkeypatch, ref)

    def _raise(_target: Path) -> bool:
        msg = "gone"
        raise FileNotFoundError(msg)

    monkeypatch.setattr(precommit, "foundation_matches_installed", _raise)
    result = precommit.step_foundation_md_check(tmp_path)
    assert not result.passed
    assert "provenance unverifiable" in result.output


def test_step_foundation_md_check_passes_when_matches_installed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `FOUNDATION.md` byte-matching the installed copy PASSes."""
    target = tmp_path / "FOUNDATION.md"
    target.write_text("# FOUNDATION.md\n", encoding="utf-8")
    ref = tmp_path / "installed-FOUNDATION.md"
    ref.write_text("# FOUNDATION.md\n", encoding="utf-8")
    _patch_resources_ref(monkeypatch, ref)
    monkeypatch.setattr(precommit, "foundation_matches_installed", lambda _p: True)
    result = precommit.step_foundation_md_check(tmp_path)
    assert result.passed
    assert "reproduces the installed" in result.output


def test_step_foundation_md_check_passes_on_synced_file_unstubbed(
    tmp_path: Path,
) -> None:
    """A FOUNDATION.md written by `sync_foundation` passes the real step.

    No stubs: the installed foundation is the real packaged one, so this
    pins that the writer and the checker agree on "in sync" (a version in
    the banner once made them disagree across installs).
    """
    install_claudemd.sync_foundation(tmp_path / "FOUNDATION.md")
    result = precommit.step_foundation_md_check(tmp_path)
    assert result.passed, result.output
    assert not result.skipped


def test_step_foundation_md_check_fails_when_divergent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `FOUNDATION.md` diverging from the installed copy FAILs."""
    target = tmp_path / "FOUNDATION.md"
    target.write_text("# FOUNDATION.md\n", encoding="utf-8")
    ref = tmp_path / "installed-FOUNDATION.md"
    ref.write_text("# FOUNDATION.md\n", encoding="utf-8")
    _patch_resources_ref(monkeypatch, ref)
    monkeypatch.setattr(precommit, "foundation_matches_installed", lambda _p: False)
    result = precommit.step_foundation_md_check(tmp_path)
    assert not result.passed
    assert "hand-edited, stale, or unmanaged" in result.output


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
    EXPECTED BEHAVIOR: code_health/pip_audit.json created; its contents are
        the same data dict plus a ``scanned_at`` stamp of the current time.
    """
    run = _audit_run(1)
    monkeypatch.setattr(precommit.pip_audit_json, "run_json", lambda _root: run)
    before = time.time()
    precommit.step_pip_audit(tmp_path)
    sidecar = git_utils.code_health_dir(tmp_path) / "pip_audit.json"
    assert sidecar.exists()
    written = json.loads(sidecar.read_text())
    scanned_at = written.pop("scanned_at")
    assert written == run.data
    assert before <= scanned_at <= time.time()


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
    assert not (git_utils.code_health_dir(tmp_path) / "pip_audit.json").exists()
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
    sidecar = git_utils.code_health_dir(tmp_path) / "pip_audit.json"
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/x")
    captured_argv: list[str] = []

    def _fake_run(cmd: list[str], **_kw: object) -> tuple[bool, str]:
        captured_argv.extend(cmd)
        return True, "clean"

    monkeypatch.setattr(precommit, "_run", _fake_run)
    precommit.step_cve_usage(tmp_path)
    assert "--audit-json" in captured_argv
    assert str(sidecar) in captured_argv


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


# installed_console_scripts itself now lives in forge.config — see
# tests/test_config.py for its unit tests. FakeEP/FakeDist stay here
# because the step_env_sync integration tests below still patch
# precommit.importlib.metadata.distribution directly (the real singleton
# module object config.installed_console_scripts also reads from).


# ---------------------------------------------------------------------------
# step_env_sync (integration — all patch is_ci)
# ---------------------------------------------------------------------------


def test_step_env_sync_skips_in_ci_non_interactive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """step_env_sync skips in CI without touching distribution metadata.

    SCENARIO: is_ci returns True — the step must short-circuit
    immediately without inspecting pyproject.toml or importlib.metadata.
    MOCK SETUP: is_ci stubbed to True.
    EXPECTED BEHAVIOR: passed True, skipped True, "CI" in output.
    """
    monkeypatch.setattr(precommit, "is_ci", lambda: True)
    result = precommit.step_env_sync(tmp_path)
    assert result.passed
    assert result.skipped
    assert "CI" in result.output


def test_step_env_sync_does_not_skip_when_non_interactive_but_not_ci(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An agent session (non-interactive, not CI) is not the CI self-skip.

    A Claude Code agent session has no controlling terminal, so
    ``is_non_interactive()`` is unconditionally True there, but it is
    not CI. Gating on the tty-inclusive predicate would skip this step
    on every agent-driven commit — the exact environment the
    entry-point check most needs to cover. This predicate confusion
    recurs elsewhere in this codebase, so the case is pinned directly
    rather than left to follow only from the CI-skip tests above.

    SCENARIO: is_non_interactive→True (simulating an agent session's
    absent tty) but is_ci→False — the precheck must not treat this as CI.
    MOCK SETUP: is_ci stubbed to False; is_non_interactive stubbed to
    True; no pyproject written, so the step falls through to the
    "no declared scripts" skip rather than the CI skip.
    EXPECTED BEHAVIOR: the step proceeds past the CI precheck — its
    output is not the "(CI — skipped)" text, even though it still skips
    for the unrelated reason of having no [project.scripts] to check.
    """
    monkeypatch.setattr(precommit, "is_ci", lambda: False)
    monkeypatch.setattr(precommit, "is_non_interactive", lambda: True)
    result = precommit.step_env_sync(tmp_path)
    assert result.passed
    assert result.skipped
    assert "(CI — skipped)" not in result.output
    assert "[project.scripts]" in result.output


def test_step_env_sync_skips_when_no_declared_scripts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """step_env_sync skips when pyproject has no usable [project.scripts].

    SCENARIO: no pyproject.toml in tmp_path — _declared_scripts returns None.
    MOCK SETUP: is_ci stubbed to False; no pyproject written.
    EXPECTED BEHAVIOR: passed True, skipped True, "[project.scripts]" in output.
    """
    monkeypatch.setattr(precommit, "is_ci", lambda: False)
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
    MOCK SETUP: is_ci→False; pyproject written; distribution
    stubbed to raise PackageNotFoundError.
    EXPECTED BEHAVIOR: passed True, skipped True, "not installed" in output.
    """
    monkeypatch.setattr(precommit, "is_ci", lambda: False)
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
    MOCK SETUP: is_ci→False; pyproject written; distribution→
    FakeDist([FakeEP("mycli","console_scripts"), FakeEP("helper","console_scripts")]).
    EXPECTED BEHAVIOR: passed True, skipped False, "installed" in output,
    non_blocking False.
    """
    monkeypatch.setattr(precommit, "is_ci", lambda: False)
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
    MOCK SETUP: is_ci→False; pyproject written without env_sync config;
    distribution→FakeDist([FakeEP("mycli","console_scripts")]).
    EXPECTED BEHAVIOR: passed False, skipped False, non_blocking False,
    "new-cli" in output, the consumer refresh phrase (not setup.sh, as the
    repo is not forge's own) in output.
    """
    monkeypatch.setattr(precommit, "is_ci", lambda: False)
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
    assert "reinstall forge-scripts the way this repo installs it" in result.output
    assert "setup.sh" not in result.output


def test_step_env_sync_warns_not_blocks_when_blocking_false(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """step_env_sync warns when [tool.forge.env_sync].blocking=false.

    SCENARIO: same missing-script situation as blocks_by_default, but
    [tool.forge.env_sync] blocking=false downgrades the result to WARN.
    MOCK SETUP: is_ci→False; pyproject written with blocking=false;
    distribution→FakeDist([FakeEP("mycli","console_scripts")]).
    EXPECTED BEHAVIOR: passed False, skipped False, non_blocking True,
    "new-cli" in output.
    """
    monkeypatch.setattr(precommit, "is_ci", lambda: False)
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
    MOCK SETUP: is_ci→False; importlib.metadata.version→"2.8.0".
    EXPECTED BEHAVIOR: passed False, non_blocking True, names the pin.
    """
    monkeypatch.setattr(precommit, "is_ci", lambda: False)
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
    monkeypatch.setattr(precommit, "is_ci", lambda: False)
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
    monkeypatch.setattr(precommit, "is_ci", lambda: False)
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
    monkeypatch.setattr(precommit, "is_ci", lambda: False)
    _write_deps_pyproject(tmp_path, ["forge-scripts==2.9.0"])
    monkeypatch.setattr(
        precommit.importlib.metadata, "version", lambda _n: "2.8.0.dev1+gabc1234"
    )
    result = precommit.step_env_sync(tmp_path)
    assert result.passed
    assert "behind the pin" not in result.output


def test_step_env_sync_missing_script_beats_pin_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A blocking missing entry point wins over a forge-scripts pin WARN.

    SCENARIO: the repo both has a missing declared script AND pins
    forge-scripts ahead of the install. The blocking entry-point failure
    must take priority over the non-blocking pin advisory.
    MOCK SETUP: is_ci→False; pyproject declares mypkg with two
    scripts + forge-scripts==2.9.0; only one script installed; forge-scripts
    version→2.8.0.
    EXPECTED BEHAVIOR: passed False, blocking (non_blocking False), the
    ⛔ stale-install message — NOT the pin WARN.
    """
    monkeypatch.setattr(precommit, "is_ci", lambda: False)
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "mypkg"\ndependencies = ["forge-scripts==2.9.0"]\n'
        '\n[project.scripts]\nmycli = "pkg:main"\nnew-cli = "pkg:main"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(precommit, "installed_console_scripts", lambda _n: {"mycli"})
    monkeypatch.setattr(precommit.importlib.metadata, "version", lambda _n: "2.8.0")
    result = precommit.step_env_sync(tmp_path)
    assert not result.passed
    assert not result.non_blocking
    assert "⛔ Stale install" in result.output
    assert "behind the pin" not in result.output


# ---------------------------------------------------------------------------
# env_sync — registry position
# ---------------------------------------------------------------------------


def test_step_auto_rebuild_precedes_env_sync_in_registry(tmp_path: Path) -> None:
    """auto_rebuild, env_sync, plugin_sync run first in that order by default.

    auto_rebuild heals a stale editable install before env_sync can block on
    it, and plugin_sync (a sibling install-surface gate) follows immediately
    after — the ordering is the design contract, not an implementation detail.
    """
    resolved = precommit.resolve_steps(tmp_path)
    names = [d.name for d in resolved]
    assert names[0] == "auto_rebuild"
    assert names[1] == "env_sync"
    assert names[2] == "plugin_sync"


# ---------------------------------------------------------------------------
# _check_clone_identity — env_sync's parallel-clone guard
# ---------------------------------------------------------------------------


def test_check_clone_identity_none_for_consumer_repo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A repo whose [project.name] isn't forge-scripts has nothing to compare.

    MOCK SETUP: _declared_scripts→("mypkg", ...); editable_install_origin
    stubbed to raise, proving it's never reached once the name check fails.
    """
    monkeypatch.setattr(precommit, "_declared_scripts", lambda _root: ("mypkg", {"x"}))

    def _fail() -> None:
        msg = "editable_install_origin should not be called"
        raise AssertionError(msg)

    monkeypatch.setattr(precommit, "editable_install_origin", _fail)
    assert precommit._check_clone_identity(tmp_path) is None


def test_check_clone_identity_none_when_no_declared_scripts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No usable [project.scripts] table → nothing to compare."""
    monkeypatch.setattr(precommit, "_declared_scripts", lambda _root: None)
    assert precommit._check_clone_identity(tmp_path) is None


def test_check_clone_identity_none_when_origin_is_this_clone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The editable install's origin matching repo_root is not a mismatch."""
    monkeypatch.setattr(
        precommit, "_declared_scripts", lambda _root: (precommit.DIST_NAME, {"x"})
    )
    monkeypatch.setattr(precommit, "editable_install_origin", tmp_path.resolve)
    assert precommit._check_clone_identity(tmp_path) is None


def test_check_clone_identity_none_when_origin_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-editable (or uninstalled) forge-scripts has no origin to compare."""
    monkeypatch.setattr(
        precommit, "_declared_scripts", lambda _root: (precommit.DIST_NAME, {"x"})
    )
    monkeypatch.setattr(precommit, "editable_install_origin", lambda: None)
    assert precommit._check_clone_identity(tmp_path) is None


def test_check_clone_identity_blocks_on_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A different clone's origin produces a blocking result naming both paths."""
    other_clone = tmp_path.parent / "dev-1"
    monkeypatch.setattr(
        precommit, "_declared_scripts", lambda _root: (precommit.DIST_NAME, {"x"})
    )
    monkeypatch.setattr(precommit, "editable_install_origin", lambda: other_clone)
    result = precommit._check_clone_identity(tmp_path)
    assert result is not None
    assert not result.passed
    assert str(other_clone) in result.output
    assert str(tmp_path.resolve()) in result.output


def test_check_clone_identity_redacts_home_when_both_clones_under_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test that clone mismatch message redacts home paths for privacy.

    SCENARIO: the editable-install origin and this repo both sit under
    the user's home directory when a clone mismatch fires.

    MOCK SETUP: _declared_scripts→(DIST_NAME, ...); editable_install_origin
    stubbed to a sibling clone; Path.home patched to a tmp_path-based fake
    home containing both clones.

    EXPECTED BEHAVIOR: the block message renders both paths as ``~/<rest>``
    and never leaks the fake home's raw absolute string — the redaction
    that keeps an account name out of code_health/env_sync.log and any
    public issue a contributor pastes the block into.
    """
    fake_home = (tmp_path / "home").resolve()
    fake_home.mkdir()
    other_clone = fake_home / "dev-1"
    this_clone = fake_home / "dev-0"
    other_clone.mkdir()
    this_clone.mkdir()
    monkeypatch.setattr(precommit.Path, "home", staticmethod(lambda: fake_home))
    monkeypatch.setattr(
        precommit, "_declared_scripts", lambda _root: (precommit.DIST_NAME, {"x"})
    )
    monkeypatch.setattr(precommit, "editable_install_origin", lambda: other_clone)

    result = precommit._check_clone_identity(this_clone)

    assert result is not None
    assert not result.passed
    assert "~/" in result.output
    assert str(fake_home) not in result.output


# ---------------------------------------------------------------------------
# _check_hook_sidecar — env_sync's stale-hooks guard
# ---------------------------------------------------------------------------


def test_check_hook_sidecar_none_when_sidecar_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No sidecar → nothing to compare, and pip_version is never consulted.

    MOCK SETUP: hook_sidecar_version→None; pip_version stubbed to raise,
    proving the short-circuit happens before any pip lookup.
    """
    monkeypatch.setattr(precommit, "hook_sidecar_version", lambda _root: None)

    def _fail() -> str:
        msg = "pip_version should not be called"
        raise AssertionError(msg)

    monkeypatch.setattr(precommit, "pip_version", _fail)
    assert precommit._check_hook_sidecar(tmp_path) is None


def test_check_hook_sidecar_none_when_pip_version_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A recorded sidecar with no installed pip version has nothing to compare."""
    monkeypatch.setattr(precommit, "hook_sidecar_version", lambda _root: "2.8.0")
    monkeypatch.setattr(precommit, "pip_version", lambda: None)
    assert precommit._check_hook_sidecar(tmp_path) is None


def test_check_hook_sidecar_none_when_current(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sidecar at or ahead of the installed pip version is not stale."""
    monkeypatch.setattr(precommit, "hook_sidecar_version", lambda _root: "2.9.0")
    monkeypatch.setattr(precommit, "pip_version", lambda: "2.9.0")
    assert precommit._check_hook_sidecar(tmp_path) is None


def test_check_hook_sidecar_blocks_when_behind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sidecar behind the installed pip version blocks, naming both + remediation."""
    monkeypatch.setattr(precommit, "hook_sidecar_version", lambda _root: "2.8.0")
    monkeypatch.setattr(precommit, "pip_version", lambda: "2.9.0")
    result = precommit._check_hook_sidecar(tmp_path)
    assert result is not None
    assert not result.passed
    assert "2.8.0" in result.output
    assert "2.9.0" in result.output
    assert precommit.SKEW_REMEDIATION["git hooks"] in result.output


# ---------------------------------------------------------------------------
# step_env_sync — precheck wiring
# ---------------------------------------------------------------------------


def test_step_env_sync_returns_precheck_block_without_reading_declared_scripts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A blocking precheck result short-circuits before the entry-point lookup.

    SCENARIO: _check_clone_identity finds a mismatch.
    MOCK SETUP: is_ci→False; _check_clone_identity stubbed to
    return a sentinel StepResult; _declared_scripts spied to fail the test
    if invoked — the normal entry-point/pin logic must never run once the
    precheck already decided the outcome.
    EXPECTED BEHAVIOR: step_env_sync returns exactly the sentinel result.
    """
    monkeypatch.setattr(precommit, "is_ci", lambda: False)
    sentinel = precommit.StepResult(name="env_sync", passed=False, output="⛔ sentinel")
    monkeypatch.setattr(precommit, "_check_clone_identity", lambda _root: sentinel)

    def _fail(_root: Path) -> None:
        msg = "_declared_scripts should not be called"
        raise AssertionError(msg)

    monkeypatch.setattr(precommit, "_declared_scripts", _fail)
    result = precommit.step_env_sync(tmp_path)
    assert result is sentinel


# ---------------------------------------------------------------------------
# step_plugin_sync
# ---------------------------------------------------------------------------


def _write_plugin_manifest(
    repo_root: Path,
    version: str,
    *,
    name: str = "forge",
) -> None:
    """Write a ``.claude-plugin/plugin.json`` manifest for step_plugin_sync tests.

    Args:
        repo_root: Directory to place ``.claude-plugin/`` under.
        version: Value for the manifest's ``version`` field.
        name: Value for the manifest's ``name`` field.
    """
    manifest_dir = repo_root / ".claude-plugin"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    (manifest_dir / "plugin.json").write_text(
        json.dumps({"name": name, "version": version}), encoding="utf-8"
    )


def test_step_plugin_sync_skips_when_no_manifest(tmp_path: Path) -> None:
    """A repo that ships no plugin has nothing for plugin_sync to check."""
    result = precommit.step_plugin_sync(tmp_path)
    assert result.passed
    assert result.skipped
    assert "no plugin shipped" in result.output


def test_step_plugin_sync_skips_non_interactive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CI contexts self-skip (FOUNDATION §15)."""
    _write_plugin_manifest(tmp_path, "2.9.0")
    monkeypatch.setattr(precommit, "is_ci", lambda: True)
    result = precommit.step_plugin_sync(tmp_path)
    assert result.passed
    assert result.skipped
    assert "CI" in result.output


def test_step_plugin_sync_does_not_skip_when_non_interactive_but_not_ci(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An agent session (non-interactive, not CI) is not the CI self-skip.

    Sibling of the same-named ``step_env_sync`` test: the CI-vs-non-
    interactive distinction applies here too. A Claude Code agent
    session has no controlling terminal, so ``is_non_interactive()``
    reads True unconditionally there even though it is not CI — the
    case the install-currency check most needs to run in, since the
    install it guards is what the agent session itself reads.

    SCENARIO: is_non_interactive→True (simulating an agent session's
    absent tty) but is_ci→False, with ``claude`` installed and no install
    recorded for the checkout — the precheck must not treat this as CI.
    MOCK SETUP: is_ci stubbed to False; is_non_interactive stubbed to
    True; ``claude`` found on PATH; a plugin manifest is written so the
    "no plugin shipped" skip doesn't fire first.
    EXPECTED BEHAVIOR: the step proceeds past the CI precheck and reaches
    its verdict — a non-blocking WARN, not the "(CI — skipped)" text.
    """
    _write_plugin_manifest(tmp_path, "2.9.0")
    monkeypatch.setattr(precommit, "is_ci", lambda: False)
    monkeypatch.setattr(precommit, "is_non_interactive", lambda: True)
    monkeypatch.setattr(precommit.shutil, "which", lambda _n: "/bin/claude")
    result = precommit.step_plugin_sync(tmp_path)
    assert not result.skipped
    assert not result.passed
    assert result.non_blocking
    assert "(CI — skipped)" not in result.output


def test_step_plugin_sync_skips_when_claude_not_installed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without Claude Code there is no plugin to be current or stale."""
    _write_plugin_manifest(tmp_path, "2.9.0")
    monkeypatch.setattr(precommit, "is_ci", lambda: False)
    monkeypatch.setattr(precommit.shutil, "which", lambda _n: None)
    result = precommit.step_plugin_sync(tmp_path)
    assert result.passed
    assert result.skipped
    assert "not installed" in result.output


def test_step_plugin_sync_passes_when_checkout_has_its_own_install(
    tmp_path: Path,
    claude_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A recorded install of the manifest's release for this checkout passes."""
    repo = tmp_path / "repo"
    _write_plugin_manifest(repo, "2.9.0")
    write_install(claude_home, repo, "2.9.0")
    monkeypatch.setattr(precommit, "is_ci", lambda: False)
    monkeypatch.setattr(precommit.shutil, "which", lambda _n: "/bin/claude")
    result = precommit.step_plugin_sync(repo)
    assert result.passed
    assert not result.skipped
    assert "2.9.0" in result.output


def test_step_plugin_sync_names_an_unparsed_verdict_truthfully(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A manifest version that is no release passes but is never called a match.

    MOCK SETUP: manifest version ``banana``; ``claude`` on PATH.
    EXPECTED BEHAVIOR: passes (nothing can be compared), and the line says
    the version was not compared instead of claiming a plugin match.
    """
    _write_plugin_manifest(tmp_path, "banana")
    monkeypatch.setattr(precommit, "is_ci", lambda: False)
    monkeypatch.setattr(precommit.shutil, "which", lambda _n: "/bin/claude")
    result = precommit.step_plugin_sync(tmp_path)
    assert result.passed
    assert "not compared" in result.output
    assert "is this checkout's plugin" not in result.output


@pytest.mark.parametrize(
    "other_repo_has_install", [False, True], ids=["no-record", "only-another-repos"]
)
def test_step_plugin_sync_warns_when_checkout_has_no_install(
    tmp_path: Path,
    claude_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    other_repo_has_install: bool,
) -> None:
    """No install for this checkout is WARN, however many other repos have one.

    Args:
        other_repo_has_install: Whether another repo has an install record.
    """
    repo = tmp_path / "repo"
    _write_plugin_manifest(repo, "2.9.0")
    if other_repo_has_install:
        write_install(claude_home, tmp_path / "other", "2.9.0")
    monkeypatch.setattr(precommit, "is_ci", lambda: False)
    monkeypatch.setattr(precommit.shutil, "which", lambda _n: "/bin/claude")
    result = precommit.step_plugin_sync(repo)
    assert not result.passed
    assert result.non_blocking
    assert "⚠️" in result.output
    assert "forge-plugin-sync" in result.output


def test_step_plugin_sync_blocks_when_behind_and_configured_blocking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``[tool.forge.plugin_sync].blocking = true`` config escalates to a block."""
    _write_plugin_manifest(tmp_path, "2.9.0")
    _write_pyproject(tmp_path, "[tool.forge.plugin_sync]\nblocking = true\n")
    monkeypatch.setattr(precommit, "is_ci", lambda: False)
    monkeypatch.setattr(precommit.shutil, "which", lambda _n: "/bin/claude")
    result = precommit.step_plugin_sync(tmp_path)
    assert not result.passed
    assert not result.non_blocking
    assert "⛔" in result.output


# ---------------------------------------------------------------------------
# _emergency_skip — env_sync / plugin_sync stand down while a sentinel is armed
# ---------------------------------------------------------------------------


def _write_emergency_sentinel(
    repo_root: Path, *, hours: float, spent: bool = False
) -> None:
    """Write a real ``.forge-emergency`` sentinel via ``emergency.write_state``.

    Reuses the CLI's own writer (as ``tests/test_emergency.py`` does)
    instead of hand-rolling the sentinel JSON, so these tests exercise
    the real ``active_state``/``armed_state`` read path, not a mocked
    stand-in for it.

    Args:
        repo_root: Directory to write the sentinel under.
        hours: Offset from now for ``expires_at`` — negative writes an
            already-expired sentinel.
        spent: Whether the sentinel's one allowed publication has
            already been consumed.
    """
    expires_at = (_dt.datetime.now(tz=_dt.UTC) + _dt.timedelta(hours=hours)).isoformat()
    emergency.write_state(
        repo_root,
        emergency.EmergencyState(
            ledger_issue=99,
            reason="prod is down",
            expires_at=expires_at,
            spent=spent,
        ),
    )


def test_step_env_sync_skips_when_emergency_armed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An armed emergency sentinel skips env_sync where it would otherwise block.

    SCENARIO: the same missing-script setup as
    ``test_step_env_sync_blocks_by_default_when_script_missing`` (blocking
    defaults to True), plus a freshly armed sentinel — proving the skip
    fires on the gate that would otherwise fail, not one that already
    passes.
    MOCK SETUP: is_ci→False; pyproject declares {mycli, new-cli};
    distribution→FakeDist with only mycli installed; a real armed
    sentinel written via ``emergency.write_state``.
    EXPECTED BEHAVIOR: passed True, skipped True, ledger number in output.
    """
    monkeypatch.setattr(precommit, "is_ci", lambda: False)
    _write_project_scripts_pyproject(tmp_path, "mypkg", {"mycli": "", "new-cli": ""})
    eps = [FakeEP("mycli", "console_scripts")]
    monkeypatch.setattr(
        precommit.importlib.metadata, "distribution", lambda _n: FakeDist(eps)
    )
    _write_emergency_sentinel(tmp_path, hours=1)

    result = precommit.step_env_sync(tmp_path)

    assert result.passed
    assert result.skipped
    assert "#99" in result.output


def test_step_plugin_sync_skips_when_emergency_armed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An armed emergency sentinel skips plugin_sync where it would otherwise block.

    SCENARIO: the same missing-install-plus-blocking setup as
    ``test_step_plugin_sync_blocks_when_behind_and_configured_blocking``,
    plus a freshly armed sentinel — proving the skip fires on the gate
    that would otherwise hard-fail, not one that already passes.
    MOCK SETUP: is_ci→False; ``claude`` on PATH; manifest at 2.9.0;
    [tool.forge.plugin_sync] blocking=true; no install recorded for the
    checkout; a real armed sentinel written via ``emergency.write_state``.
    EXPECTED BEHAVIOR: passed True, skipped True, ledger number in output.
    """
    _write_plugin_manifest(tmp_path, "2.9.0")
    _write_pyproject(tmp_path, "[tool.forge.plugin_sync]\nblocking = true\n")
    monkeypatch.setattr(precommit, "is_ci", lambda: False)
    monkeypatch.setattr(precommit.shutil, "which", lambda _n: "/bin/claude")
    _write_emergency_sentinel(tmp_path, hours=1)

    result = precommit.step_plugin_sync(tmp_path)

    assert result.passed
    assert result.skipped
    assert "#99" in result.output


def test_step_plugin_sync_skips_when_emergency_spent_but_unexpired(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A spent-but-unexpired sentinel still stands plugin_sync down.

    SCENARIO: the same missing-install-plus-blocking setup as
    ``test_step_plugin_sync_skips_when_emergency_armed``, but the
    sentinel's one allowed publication has already been consumed
    (``spent=True``) while it is still unexpired — the exact shape a
    real emergency PR is in the moment after ``gh pr create`` runs.
    Pins the ``armed_state``→``active_state`` split this PR makes:
    before it, a spent sentinel read as "not armed" and this fixture
    would fail closed with ⛔, defeating the emergency for the
    conflicts and follow-up commits that arrive after publication.
    MOCK SETUP: is_ci→False; ``claude`` on PATH; manifest at 2.9.0;
    [tool.forge.plugin_sync] blocking=true; no install recorded for the
    checkout; a real spent-but-unexpired sentinel written via
    ``emergency.write_state``.
    EXPECTED BEHAVIOR: passed True, skipped True, ledger number in output.
    """
    _write_plugin_manifest(tmp_path, "2.9.0")
    _write_pyproject(tmp_path, "[tool.forge.plugin_sync]\nblocking = true\n")
    monkeypatch.setattr(precommit, "is_ci", lambda: False)
    monkeypatch.setattr(precommit.shutil, "which", lambda _n: "/bin/claude")
    _write_emergency_sentinel(tmp_path, hours=1, spent=True)

    result = precommit.step_plugin_sync(tmp_path)

    assert result.passed
    assert result.skipped
    assert "#99" in result.output


def test_emergency_skip_returns_none_when_sentinel_expired(tmp_path: Path) -> None:
    """An expired sentinel does not stand the gate down — fails closed.

    Pins the discipline `forge-emergency` is built on: a bypass that
    could be revived by a stale sentinel would not be one-shot. Exercises
    the shared ``_emergency_skip`` helper directly since both
    ``step_env_sync`` and ``step_plugin_sync`` delegate to it
    unconditionally — the positive (armed) path is already pinned per
    step above, so this covers the negative path once rather than twice.
    """
    _write_emergency_sentinel(tmp_path, hours=-1)

    assert precommit._emergency_skip(tmp_path, "env_sync") is None


# ---------------------------------------------------------------------------
# Helpers for vendored-integrity + regen-docs test groups
# ---------------------------------------------------------------------------


def _write_vendored_md(repo_root: Path, entries: dict[str, str]) -> None:
    """Write ``src/forge/data/VENDORED.md`` with ``## <name>`` + SHA-256 sections.

    Creates ``src/forge/data/`` when absent. Each entry becomes a ``## <name>``
    header followed by a ``- **SHA-256:** `<hash>``` line as the parser expects.

    Args:
        repo_root: Git repo root; ``src/forge/data/`` is created under it.
        entries: Mapping of vendored filename to 64-char lowercase SHA-256 hex string.
    """
    data_dir = repo_root / "src" / "forge" / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for name, sha in entries.items():
        lines.append(f"## {name}")
        lines.append(f"- **SHA-256:** `{sha}`")
        lines.append("")
    (data_dir / "VENDORED.md").write_text("\n".join(lines), encoding="utf-8")


def _write_data_js(
    repo_root: Path,
    filename: str,
    content: bytes = b"fake js",
) -> Path:
    """Create ``src/forge/data/<filename>`` with *content* and return its path.

    Creates ``src/forge/data/`` when absent so callers need not mkdir first.

    Args:
        repo_root: Git repo root; ``src/forge/data/`` is created under it.
        filename: Basename of the vendored asset (e.g. ``"mermaid.min.js"``).
        content: Raw bytes to write. Defaults to ``b"fake js"``.

    Returns:
        Absolute path to the written file.
    """
    data_dir = repo_root / "src" / "forge" / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / filename
    path.write_bytes(content)
    return path


# ---------------------------------------------------------------------------
# Group 2: step_regen_docs
# MOCKING STRATEGY: precommit.forge_cli_argv → controls the FOUNDATION §2 loud
# fail on a CLI the running forge does not declare (the real helper resolves
# against the installed forge-scripts otherwise); precommit._run → controls
# generator success/failure without
# invoking real CLIs; precommit.stage_modified_paths → controls restaged-files
# list without touching a real git index. No real generators or git ops fire.
# ---------------------------------------------------------------------------


def test_step_regen_docs_skips_when_no_docs_exist(tmp_path: Path) -> None:
    """No docs present → skipped=True, passed=True, 'skipped' in output."""
    result = precommit.step_regen_docs(tmp_path)
    assert result.skipped
    assert result.passed
    assert "skipped" in result.output


def test_step_regen_docs_runs_generator_for_present_doc(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """api-digest.md present → generator is invoked; step is non-blocking and passed.

    SCENARIO: only docs/api-digest.md exists; forge-gen-api-digest exits 0.
    MOCK SETUP: shutil.which → returns a valid path so require_cli passes;
        precommit._run → (True, "generated"); stage_modified_paths → [].
    EXPECTED BEHAVIOR: passed=True, non_blocking=True, skipped=False;
        "forge-gen-api-digest" appears in output.
    """
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "api-digest.md").write_text("old\n")
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/x")
    monkeypatch.setattr(precommit, "_run", lambda _cmd, **_kw: (True, "generated"))
    monkeypatch.setattr(precommit, "stage_modified_paths", lambda *_a: [])
    result = precommit.step_regen_docs(tmp_path)
    assert result.passed
    assert result.non_blocking
    assert not result.skipped
    assert "forge-gen-api-digest" in result.output


def test_step_regen_docs_is_non_blocking_on_generator_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Generator error → passed=False, non_blocking (warn, don't block commit).

    SCENARIO: docs/api-digest.md present; forge-gen-api-digest exits non-zero.
    MOCK SETUP: shutil.which → valid path; precommit._run → (False, "crash");
        stage_modified_paths → [].
    EXPECTED BEHAVIOR: passed=False, non_blocking=True, skipped=False.
    """
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "api-digest.md").write_text("old\n")
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/x")
    monkeypatch.setattr(precommit, "_run", lambda _cmd, **_kw: (False, "crash"))
    monkeypatch.setattr(precommit, "stage_modified_paths", lambda *_a: [])
    result = precommit.step_regen_docs(tmp_path)
    assert not result.passed
    assert result.non_blocking
    assert not result.skipped


def test_step_regen_docs_includes_restaged_files_in_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Re-staged paths from stage_modified_paths appear in the step output.

    SCENARIO: api-digest.md present; stage_modified_paths returns the doc path.
    MOCK SETUP: shutil.which → valid path; precommit._run → (True, "");
        stage_modified_paths → ["docs/api-digest.md"].
    EXPECTED BEHAVIOR: "Re-staged:" and "docs/api-digest.md" both in output.
    """
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "api-digest.md").write_text("old\n")
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/x")
    monkeypatch.setattr(precommit, "_run", lambda _cmd, **_kw: (True, ""))
    monkeypatch.setattr(
        precommit, "stage_modified_paths", lambda *_a: ["docs/api-digest.md"]
    )
    result = precommit.step_regen_docs(tmp_path)
    assert "Re-staged:" in result.output
    assert "docs/api-digest.md" in result.output


def test_step_regen_docs_runs_both_generators_when_both_docs_exist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both generated docs present → both generator modules launched via -m.

    SCENARIO: docs/api-digest.md and docs/cli-reference.md both exist.
    MOCK SETUP: precommit._run captures each argv and returns (True, "");
        stage_modified_paths → [].
    EXPECTED BEHAVIOR: captured _run argvs start with
        ``[sys.executable, "-m", "forge.gen_api_digest"]`` and
        ``[sys.executable, "-m", "forge.gen_cli_reference"]`` — the running
        install's own entry points, never a bare PATH name.
    """
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "api-digest.md").write_text("old\n")
    (docs_dir / "cli-reference.md").write_text("old\n")
    run_cmds: list[list[str]] = []

    def _capture_run(cmd: list[str], **_kw: object) -> tuple[bool, str]:
        run_cmds.append(cmd)
        return True, ""

    monkeypatch.setattr(precommit, "_run", _capture_run)
    monkeypatch.setattr(precommit, "stage_modified_paths", lambda *_a: [])
    precommit.step_regen_docs(tmp_path)
    assert [sys.executable, "-m", "forge.gen_api_digest"] in run_cmds
    assert [sys.executable, "-m", "forge.gen_cli_reference"] in run_cmds


def test_step_regen_docs_cli_missing_exits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing forge-gen-* CLI raises SystemExit(2) (FOUNDATION §2 loud-fail)."""
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "api-digest.md").write_text("old\n")

    def _raise_missing(_name: str, **_kw: object) -> list[str]:
        raise SystemExit(2)

    monkeypatch.setattr(precommit, "forge_cli_argv", _raise_missing)
    with pytest.raises(SystemExit) as exc_info:
        precommit.step_regen_docs(tmp_path)
    assert exc_info.value.code == 2


def test_step_regen_docs_partial_failure_when_second_generator_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Loop continues past a first-generator success when the second generator fails.

    SCENARIO: both docs/api-digest.md and docs/cli-reference.md exist; the
    first generator (forge-gen-api-digest) exits 0 but the second
    (forge-gen-cli-reference) exits non-zero. The loop must not abort after
    the success — it accumulates the failure flag and continues, so both
    generator names appear in output and the caller can see the full picture.
    MOCK SETUP: shutil.which → valid path so require_cli passes for both
    CLIs; precommit._run dispatches on cmd[0] — "forge-gen-api-digest"
    returns (True, "ok") and "forge-gen-cli-reference" returns (False,
    "crash"); precommit.stage_modified_paths → [].
    EXPECTED BEHAVIOR: passed=False, non_blocking=True, skipped=False;
    "forge-gen-api-digest" and "forge-gen-cli-reference" both in output.
    """
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "api-digest.md").write_text("old\n")
    (docs_dir / "cli-reference.md").write_text("old\n")
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/x")

    def _partial_run(cmd: list[str], **_kw: object) -> tuple[bool, str]:
        if cmd[0] == "forge-gen-api-digest":
            return True, "ok"
        return False, "crash"

    monkeypatch.setattr(precommit, "_run", _partial_run)
    monkeypatch.setattr(precommit, "stage_modified_paths", lambda *_a: [])
    result = precommit.step_regen_docs(tmp_path)
    assert not result.passed
    assert result.non_blocking
    assert not result.skipped
    assert "forge-gen-api-digest" in result.output
    assert "forge-gen-cli-reference" in result.output


def _raise_if_missing_console_scripts_called(_repo_root: Path) -> list[str]:
    """A `missing_console_scripts` stand-in that fails the test if invoked.

    Args:
        _repo_root: Ignored (signature compatibility).

    Raises:
        AssertionError: Always — the stale-install check must be scoped to
            targets that include ``docs/cli-reference.md``.
    """
    msg = "missing_console_scripts must not be called without cli-reference.md"
    raise AssertionError(msg)


def test_step_regen_docs_warns_when_console_scripts_stale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A declared-but-uninstalled console script → non-blocking WARN (#436).

    SCENARIO: docs/cli-reference.md exists; the editable install is stale
    (a declared [project.scripts] entry is absent from the install the
    generator reads), so the regenerated doc would silently omit it.
    MOCK SETUP: shutil.which → valid path; precommit._run → (True, "");
        precommit.stage_modified_paths → []; precommit.missing_console_scripts
        → ["forge-foo"].
    EXPECTED BEHAVIOR: passed=False, non_blocking=True; output carries the
        "⚠️" marker and the missing script name.
    """
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "cli-reference.md").write_text("old\n")
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/x")
    monkeypatch.setattr(precommit, "_run", lambda _cmd, **_kw: (True, ""))
    monkeypatch.setattr(precommit, "stage_modified_paths", lambda *_a: [])
    monkeypatch.setattr(
        precommit, "missing_console_scripts", lambda _root: ["forge-foo"]
    )
    result = precommit.step_regen_docs(tmp_path)
    assert not result.passed
    assert result.non_blocking
    assert "⚠️" in result.output
    assert "forge-foo" in result.output


def test_step_regen_docs_no_warning_when_console_scripts_fresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fully-installed editable install → no stale-install WARN.

    SCENARIO: docs/cli-reference.md exists; every declared console script
    is already installed.
    MOCK SETUP: shutil.which → valid path; precommit._run → (True, "");
        precommit.stage_modified_paths → []; precommit.missing_console_scripts
        → [] (nothing missing).
    EXPECTED BEHAVIOR: passed=True; no "⚠️" marker in output.
    """
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "cli-reference.md").write_text("old\n")
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/x")
    monkeypatch.setattr(precommit, "_run", lambda _cmd, **_kw: (True, ""))
    monkeypatch.setattr(precommit, "stage_modified_paths", lambda *_a: [])
    monkeypatch.setattr(precommit, "missing_console_scripts", lambda _root: [])
    result = precommit.step_regen_docs(tmp_path)
    assert result.passed
    assert "⚠️" not in result.output


def test_step_regen_docs_stale_check_scoped_to_cli_reference_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stale-install check never fires when cli-reference.md is absent.

    SCENARIO: only docs/api-digest.md exists — cli-reference.md is not
    among the regeneration targets, so the generator that would go stale
    from a missing console script never runs.
    MOCK SETUP: shutil.which → valid path; precommit._run → (True, "");
        precommit.stage_modified_paths → []; precommit.missing_console_scripts
        → a stub that raises AssertionError if called at all.
    EXPECTED BEHAVIOR: passed=True (no AssertionError propagates), proving
        the check is scoped to targets containing cli-reference.md.
    """
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "api-digest.md").write_text("old\n")
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/x")
    monkeypatch.setattr(precommit, "_run", lambda _cmd, **_kw: (True, ""))
    monkeypatch.setattr(precommit, "stage_modified_paths", lambda *_a: [])
    monkeypatch.setattr(
        precommit, "missing_console_scripts", _raise_if_missing_console_scripts_called
    )
    result = precommit.step_regen_docs(tmp_path)
    assert result.passed


def test_step_regen_docs_warns_even_when_non_interactive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stale-install WARN fires in CI too — it is not gated on interactivity.

    SCENARIO: docs/cli-reference.md exists; is_non_interactive() reports
    True (CI-like environment), same as the healing steps (auto_rebuild,
    env_sync) that self-skip non-interactively. Unlike those, this WARN
    is unconditional on interactivity — it is detection-only, never an
    auto-heal, so there is no unattended-prompt risk to guard against
    and it must fire in CI too.
    MOCK SETUP: precommit.is_non_interactive → lambda: True; shutil.which
        → valid path; precommit._run → (True, ""); stage_modified_paths →
        []; precommit.missing_console_scripts → ["forge-foo"].
    EXPECTED BEHAVIOR: passed=False; "⚠️" and "forge-foo" still in output.
    """
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "cli-reference.md").write_text("old\n")
    monkeypatch.setattr(precommit, "is_non_interactive", lambda: True)
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/x")
    monkeypatch.setattr(precommit, "_run", lambda _cmd, **_kw: (True, ""))
    monkeypatch.setattr(precommit, "stage_modified_paths", lambda *_a: [])
    monkeypatch.setattr(
        precommit, "missing_console_scripts", lambda _root: ["forge-foo"]
    )
    result = precommit.step_regen_docs(tmp_path)
    assert not result.passed
    assert "⚠️" in result.output
    assert "forge-foo" in result.output


def test_env_sync_and_regen_docs_agree_on_stale_install_non_interactive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both steps flag the same stale install in an agent-style session.

    An agent session is non-interactive but not CI. env_sync once gated on
    the tty predicate and stood down there while regen_docs warned, so the
    two disagreed about the same install.

    SCENARIO: ``[project.scripts]`` declares ``mycli`` and ``new-cli``; the
        install registers only ``mycli``; no CI marker; no tty.
    MOCK SETUP: ``forge.config.console_script_modules`` → ``{"mycli": …}``
        (the one seam both steps read through); ``is_non_interactive`` →
        True; generators stubbed via ``precommit._run``.
    EXPECTED BEHAVIOR: env_sync fails naming ``new-cli``, and regen_docs
        warns naming ``new-cli`` too.
    """
    for name in _CI_MARKERS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(precommit, "is_non_interactive", lambda: True)
    _write_project_scripts_pyproject(tmp_path, "consumer", {"mycli": "", "new-cli": ""})
    monkeypatch.setattr(
        config, "console_script_modules", lambda _name=None: {"mycli": "pkg"}
    )
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "cli-reference.md").write_text("old\n")
    monkeypatch.setattr(precommit, "_run", lambda _cmd, **_kw: (True, ""))
    monkeypatch.setattr(precommit, "stage_modified_paths", lambda *_a: [])

    env_sync = precommit.step_env_sync(tmp_path)
    regen = precommit.step_regen_docs(tmp_path)

    assert not env_sync.passed
    assert "new-cli" in env_sync.output
    assert not regen.passed
    assert "install is stale" in regen.output
    assert "new-cli" in regen.output


# ---------------------------------------------------------------------------
# Group 2b: step_regen_docs partial-commit guard (real git repos — the
# guard reads the actual worktree via `git diff --name-only`, so a fake
# `run_git` would not exercise it).
# ---------------------------------------------------------------------------


def _raise_if_called(cmd: list[str], **_kw: object) -> tuple[bool, str]:
    """A `precommit._run` stand-in that fails the test if ever invoked.

    Args:
        cmd: The command argv the caller would have run.
        **_kw: Ignored keyword arguments (signature compatibility).

    Raises:
        AssertionError: Always — the partial-commit guard must short-circuit
            before any generator runs.
    """
    msg = f"_run must not be called when unstaged changes exist: {cmd}"
    raise AssertionError(msg)


def test_step_regen_docs_skips_on_unstaged_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dirty worktree (unrelated tracked file) skips regeneration entirely.

    SCENARIO: docs/api-digest.md exists and is committed; a separate
    tracked file (other.txt) has an unstaged edit. The guard reads
    `git diff --name-only` — any non-empty unstaged diff anywhere in the
    tree is the signal, not just the generated doc's own file.
    MOCK SETUP: `precommit._run` → `_raise_if_called`, proving the guard
    short-circuits before any generator would run.
    EXPECTED BEHAVIOR: skipped=True, passed=True, output mentions the
    partial commit and that regeneration was skipped.
    """
    init_git_repo(tmp_path)
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "api-digest.md").write_text("v1\n")
    (tmp_path / "other.txt").write_text("a\n")
    subprocess.run(
        ["git", "add", "docs/api-digest.md", "other.txt"],
        cwd=tmp_path,
        env=GIT_ENV,
        check=True,
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "add tracked files"],
        cwd=tmp_path,
        env=GIT_ENV,
        check=True,
    )
    (tmp_path / "other.txt").write_text("b\n")
    monkeypatch.setattr(precommit, "_run", _raise_if_called)
    result = precommit.step_regen_docs(tmp_path)
    assert result.skipped
    assert result.passed
    assert "partial commit" in result.output
    assert "regeneration skipped" in result.output


def test_step_regen_docs_partial_commit_guard_catches_same_file_staged_and_dirty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `git add -p`-style same-file partial stage still trips the guard.

    SCENARIO: docs/api-digest.md is committed at v1, staged at v2, then
    edited again to v3 without staging — the exact case the guard's
    docstring calls out (#363): a staged-vs-dirty *set* comparison would
    miss this because the file is staged, but its unstaged hunk still
    means the tree doesn't match what the commit would record.
    MOCK SETUP: `precommit._run` → `_raise_if_called`.
    EXPECTED BEHAVIOR: skipped=True — still skipped despite the file
    having a staged change too.
    """
    init_git_repo(tmp_path)
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "api-digest.md").write_text("v1\n")
    subprocess.run(
        ["git", "add", "docs/api-digest.md"], cwd=tmp_path, env=GIT_ENV, check=True
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "add doc"], cwd=tmp_path, env=GIT_ENV, check=True
    )
    (tmp_path / "docs" / "api-digest.md").write_text("v2\n")
    subprocess.run(
        ["git", "add", "docs/api-digest.md"], cwd=tmp_path, env=GIT_ENV, check=True
    )
    (tmp_path / "docs" / "api-digest.md").write_text("v3\n")
    monkeypatch.setattr(precommit, "_run", _raise_if_called)
    result = precommit.step_regen_docs(tmp_path)
    assert result.skipped


def test_step_regen_docs_runs_when_tree_is_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A clean real repo (no unstaged changes) lets the generator run.

    MOCK SETUP: shutil.which → valid path (require_cli passes);
        precommit._run → (True, "generated"); stage_modified_paths → [].
    EXPECTED BEHAVIOR: skipped=False, passed=True — the guard stands down.
    """
    init_git_repo(tmp_path)
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "api-digest.md").write_text("v1\n")
    subprocess.run(
        ["git", "add", "docs/api-digest.md"], cwd=tmp_path, env=GIT_ENV, check=True
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "add doc"], cwd=tmp_path, env=GIT_ENV, check=True
    )
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/x")
    monkeypatch.setattr(precommit, "_run", lambda _cmd, **_kw: (True, "generated"))
    monkeypatch.setattr(precommit, "stage_modified_paths", lambda *_a: [])
    result = precommit.step_regen_docs(tmp_path)
    assert not result.skipped
    assert result.passed


# ---------------------------------------------------------------------------
# Group 2c: regen_docs incremental — decide_regen / _regen_inputs / the
# per-worktree record / step_regen_docs skip-vs-regenerate behavior.
# MOCKING STRATEGY: real git repos in tmp_path (inputs and the record are
# derived from the index and `git rev-parse --git-path`, so a fake git would
# test nothing); only the generators are stubbed, via precommit._run (the
# seam the existing regen_docs groups use) — the stub records which
# generator modules were launched and never writes a doc.
# ---------------------------------------------------------------------------

_API_DOC = "docs/api-digest.md"
_CLI_DOC = "docs/cli-reference.md"
_API_MODULE = "forge.gen_api_digest"
_CLI_MODULE = "forge.gen_cli_reference"


def _rg_repo(tmp_path: Path, *, name: str = "consumer", commit: bool = True) -> Path:
    """Build a repo with a source root, both generated docs and a pyproject.

    Args:
        tmp_path: Directory to create the repo under.
        name: Project name written to the pyproject.
        commit: Whether to commit the files (otherwise only staged).

    Returns:
        The repo root.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    if commit:
        init_git_repo(repo)
    else:
        for cmd in (
            ["git", "init", "-q", "-b", "main"],
            ["git", "config", "user.name", "t"],
            ["git", "config", "user.email", "t@t"],
        ):
            subprocess.run(cmd, cwd=repo, env=GIT_ENV, check=True)
    (repo / "pyproject.toml").write_text(
        f'[project]\nname = "{name}"\nversion = "0"\n\n'
        '[tool.forge]\nsource_dirs = ["src"]\n'
    )
    (repo / "src" / "pkg").mkdir(parents=True)
    (repo / "src" / "pkg" / "a.py").write_text(
        '"""A."""\n\n\ndef f() -> int:\n    """F."""\n    return 1\n'
    )
    (repo / "scripts").mkdir()
    (repo / "scripts" / "tool.py").write_text("x = 1\n")
    (repo / "docs").mkdir()
    (repo / _API_DOC).write_text("api\n")
    (repo / _CLI_DOC).write_text("cli\n")
    git_stdout(repo, "add", "-A")
    if commit:
        git_stdout(repo, "commit", "-q", "-m", "seed")
    return repo


def _rg_stub_generators(
    monkeypatch: pytest.MonkeyPatch,
    *,
    ok: bool = True,
    check_ok: bool | None = None,
    check_output: str = "out of sync",
) -> list[str]:
    """Stub the generators, in both write and ``--check`` mode.

    Args:
        monkeypatch: Fixture used to patch the generator launcher.
        ok: Whether the stubbed generators succeed.
        check_ok: Whether a ``--check`` run passes; defaults to *ok*.
        check_output: What a failing ``--check`` run prints.

    Returns:
        The live list of launches: the module name for a write run,
        ``"<module> --check"`` for a check run.
    """
    launched: list[str] = []
    check_passes = ok if check_ok is None else check_ok

    def _fake_run(cmd: list[str], **_kw: object) -> tuple[bool, str]:
        if cmd[-1] == "--check":
            launched.append(f"{cmd[-2]} --check")
            return check_passes, "" if check_passes else check_output
        launched.append(cmd[-1])
        return ok, "" if ok else "crash"

    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/x")
    monkeypatch.setattr(precommit, "_run", _fake_run)
    monkeypatch.setattr(precommit, "missing_console_scripts", lambda _root: [])
    return launched


def _rg_record_bytes(repo: Path) -> bytes | None:
    """Return the raw bytes of the regen record file, or None if absent.

    Args:
        repo: Repository to read the record from.

    Returns:
        Raw bytes of the record file, or ``None`` if it doesn't exist.
    """
    path = regen_docs.record_path(repo)
    assert path is not None
    return path.read_bytes() if path.exists() else None


_RG_CURRENT = {"forge_version": "1.0", "inputs": "aaa"}
_RG_RECORDED = {"forge_version": "1.0", "inputs": "aaa", "doc_blob": "b1"}


def _rg_decide(**overrides: object) -> regen_docs.RegenDecision:
    """Call decide_regen with test defaults and optional overrides.

    Args:
        **overrides: Keyword arguments overriding test defaults.

    Returns:
        A RegenDecision from the test scenario.
    """
    kwargs: dict[str, object] = {
        "current": _RG_CURRENT,
        "recorded": _RG_RECORDED,
        "doc_blob": "b1",
        "merge": False,
        "doc_staged": False,
        "inputs_staged": True,
    }
    kwargs.update(overrides)
    return regen_docs.decide_regen(
        _API_DOC,
        "forge-gen-api-digest",
        regen_docs.RegenSignals(**kwargs),  # type: ignore[arg-type]
    )


def test_decide_regen_skips_when_everything_matches() -> None:
    """All signals agree with the record → skip, with the documented reason."""
    decision = _rg_decide()
    assert decision.action == "skip"
    assert decision.reason == "inputs unchanged since last build"
    assert decision.rel == _API_DOC
    assert decision.cli == "forge-gen-api-digest"


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"recorded": None}, "no record"),
        ({"merge": True}, "merge in progress"),
        ({"current": {**_RG_CURRENT, "forge_version": "2.0"}}, "forge"),
        ({"doc_staged": True}, "doc edited outside generator"),
        ({"doc_blob": "b2"}, "doc edited outside generator"),
        ({"current": {**_RG_CURRENT, "inputs": "bbb"}}, "changed"),
    ],
)
def test_decide_regen_each_signal_regenerates(
    overrides: dict[str, object], fragment: str
) -> None:
    """Each single signal, alone, forces a regenerate naming its reason.

    Args:
        overrides: Decision inputs overriding the unchanged baseline.
        fragment: Text the decision reason must contain.
    """
    decision = _rg_decide(**overrides)
    assert decision.action == "generate"
    assert fragment in decision.reason


@pytest.mark.parametrize(
    ("overrides", "action"),
    [
        ({"recorded": None}, "check"),
        ({"current": {**_RG_CURRENT, "forge_version": "2.0"}}, "check"),
        ({"doc_blob": "b2"}, "check"),
        ({"current": {**_RG_CURRENT, "inputs": "bbb"}}, "check"),
        # The doc itself staged, or a merge, is the commit feeding the doc.
        ({"doc_staged": True}, "generate"),
        ({"merge": True}, "generate"),
        # No signal at all: inputs unchanged since the last build.
        ({}, "skip"),
    ],
)
def test_decide_regen_rebuild_without_staged_inputs_only_checks(
    overrides: dict[str, object], action: str
) -> None:
    """A rebuild the commit does not feed is a check, never a regeneration.

    Args:
        overrides: Decision inputs overriding the unchanged baseline.
        action: The action expected with no staged input.
    """
    assert _rg_decide(inputs_staged=False, **overrides).action == action


def test_decide_regen_first_match_wins_ordering() -> None:
    """Signals are checked in order: record, merge, forge, doc edit, inputs."""
    everything = {
        "current": {"forge_version": "2.0", "inputs": "bbb"},
        "doc_staged": True,
        "merge": True,
    }
    assert "no record" in _rg_decide(recorded=None, **everything).reason
    assert "merge in progress" in _rg_decide(**everything).reason
    no_merge = {**everything, "merge": False}
    assert "forge" in _rg_decide(**no_merge).reason
    same_version = {**no_merge, "current": {"forge_version": "1.0", "inputs": "bbb"}}
    assert "doc edited outside generator" in _rg_decide(**same_version).reason
    only_inputs = {**same_version, "doc_staged": False}
    assert "changed" in _rg_decide(**only_inputs).reason
    assert "doc edited" not in _rg_decide(**only_inputs).reason


def test_regen_inputs_is_stable_sha256_hex(tmp_path: Path) -> None:
    """Inputs are a deterministic sha256 hex digest per doc."""
    repo = _rg_repo(tmp_path)
    first = regen_docs.regen_inputs(repo, _API_DOC)
    assert re.fullmatch(r"[0-9a-f]{64}", first)
    assert regen_docs.regen_inputs(repo, _API_DOC) == first


def test_regen_inputs_api_digest_ignores_file_outside_roots(tmp_path: Path) -> None:
    """A tracked file the digest would not scan does not move the inputs."""
    repo = _rg_repo(tmp_path)
    before = regen_docs.regen_inputs(repo, _API_DOC)
    (repo / "scripts" / "other.py").write_text("y = 2\n")
    (repo / "README.md").write_text("hi\n")
    git_stdout(repo, "add", "-A")
    assert regen_docs.regen_inputs(repo, _API_DOC) == before


@pytest.mark.parametrize("change", ["edit", "add", "delete", "rename", "pyproject"])
def test_regen_inputs_api_digest_tracks_root_and_pyproject_changes(
    tmp_path: Path, change: str
) -> None:
    """Edit / add / delete / rename under the roots, or a pyproject edit, moves it.

    Args:
        change: Kind of change applied to the repo.
    """
    repo = _rg_repo(tmp_path)
    before = regen_docs.regen_inputs(repo, _API_DOC)
    src = repo / "src" / "pkg"
    if change == "edit":
        (src / "a.py").write_text('"""A2."""\n')
    elif change == "add":
        (src / "b.py").write_text('"""B."""\n')
    elif change == "delete":
        git_stdout(repo, "rm", "-q", "-f", "src/pkg/a.py")
    elif change == "rename":
        git_stdout(repo, "mv", "src/pkg/a.py", "src/pkg/renamed.py")
    else:
        (repo / "pyproject.toml").write_text(
            (repo / "pyproject.toml").read_text() + "\n# touched\n"
        )
    git_stdout(repo, "add", "-A")
    assert regen_docs.regen_inputs(repo, _API_DOC) != before


def test_regen_inputs_cli_reference_ignores_consumer_sources(tmp_path: Path) -> None:
    """In a consumer repo the CLI reference depends on installed scripts only."""
    repo = _rg_repo(tmp_path, name="consumer")
    (repo / "src" / "forge").mkdir()
    (repo / "src" / "forge" / "m.py").write_text("a = 1\n")
    git_stdout(repo, "add", "-A")
    before = regen_docs.regen_inputs(repo, _CLI_DOC)
    (repo / "src" / "forge" / "m.py").write_text("a = 2\n")
    (repo / "src" / "pkg" / "a.py").write_text("z = 1\n")
    (repo / "pyproject.toml").write_text(
        (repo / "pyproject.toml").read_text() + "\n# touched\n"
    )
    git_stdout(repo, "add", "-A")
    assert regen_docs.regen_inputs(repo, _CLI_DOC) == before


def test_regen_inputs_cli_reference_tracks_src_forge_in_forge_scripts_repo(
    tmp_path: Path,
) -> None:
    """Forge's own repo: a src/forge .py (or pyproject) edit moves the inputs."""
    repo = _rg_repo(tmp_path, name="forge-scripts")
    (repo / "src" / "forge").mkdir()
    (repo / "src" / "forge" / "m.py").write_text("a = 1\n")
    git_stdout(repo, "add", "-A")
    before = regen_docs.regen_inputs(repo, _CLI_DOC)
    (repo / "src" / "forge" / "data").mkdir()
    (repo / "src" / "forge" / "data" / "note.md").write_text("not python\n")
    git_stdout(repo, "add", "-A")
    assert regen_docs.regen_inputs(repo, _CLI_DOC) == before
    (repo / "src" / "forge" / "m.py").write_text("a = 2\n")
    git_stdout(repo, "add", "-A")
    after_edit = regen_docs.regen_inputs(repo, _CLI_DOC)
    assert after_edit != before
    (repo / "pyproject.toml").write_text(
        (repo / "pyproject.toml").read_text() + "\n# touched\n"
    )
    git_stdout(repo, "add", "-A")
    assert regen_docs.regen_inputs(repo, _CLI_DOC) != after_edit


def test_regen_record_path_follows_git_path(tmp_path: Path) -> None:
    """The record lives at `git rev-parse --git-path forge/regen_docs.json`."""
    repo = _rg_repo(tmp_path)
    expected = git_stdout(
        repo, "rev-parse", "--git-path", "forge/regen_docs.json"
    ).strip()
    path = regen_docs.record_path(repo)
    assert path is not None
    assert path.resolve() == (repo / expected).resolve()


def test_regen_record_path_is_none_outside_git(tmp_path: Path) -> None:
    """No repository → no record path, and saving is a harmless no-op."""
    plain = tmp_path / "plain"
    plain.mkdir()
    assert regen_docs.record_path(plain) is None
    assert regen_docs.load_record(plain) == {}
    regen_docs.save_record(plain, {_API_DOC: {"inputs": "x"}})
    assert regen_docs.load_record(plain) == {}


def test_regen_record_round_trips_and_is_per_worktree(tmp_path: Path) -> None:
    """A saved record loads back; a linked worktree has its own, separate record."""
    repo = _rg_repo(tmp_path)
    record = {_API_DOC: {"forge_version": "1", "inputs": "i", "doc_blob": "b"}}
    regen_docs.save_record(repo, record)
    assert regen_docs.load_record(repo) == record
    linked = tmp_path / "linked"
    git_stdout(repo, "worktree", "add", "-q", "-b", "other", str(linked))
    main_path = regen_docs.record_path(repo)
    linked_path = regen_docs.record_path(linked)
    assert linked_path is not None
    assert main_path != linked_path
    assert regen_docs.load_record(linked) == {}


@pytest.mark.parametrize("content", ["{not json", "[1, 2]", '{"docs/x.md": "str"}'])
def test_load_regen_record_corrupt_or_wrong_schema_is_empty(
    tmp_path: Path, content: str
) -> None:
    """Garbage, a non-mapping, or a mapping of non-records loads as {}.

    Args:
        content: Raw text written to the record file.
    """
    repo = _rg_repo(tmp_path)
    path = regen_docs.record_path(repo)
    assert path is not None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    assert regen_docs.load_record(repo) == {}


_CLI_AT_HEAD = "# CLI\n\n## forge-foo\nfoo\n\n## forge-bar\nbar\n"


def _rg_stale_cli_repo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    regenerated: str,
    *,
    missing: tuple[str, ...] = ("forge-new",),
    documented_at_head: bool = True,
) -> Path:
    """Build a repo whose generator rewrites the cli-reference.

    Args:
        tmp_path: Directory to create the repo under.
        monkeypatch: Used to stub the generators and the stale-install probe.
        regenerated: What the cli-reference generator writes to the worktree.
        missing: Console scripts the install probe reports absent (empty =
            a fresh install).
        documented_at_head: Whether ``_CLI_AT_HEAD`` is committed as the doc;
            otherwise the doc does not exist at ``HEAD``.

    Returns:
        The repo root. It is forge's own repo with a ``src/forge`` edit
        staged, so the commit feeds the cli-reference and it regenerates.
    """
    repo = _rg_repo(tmp_path, name=regen_docs.FORGE_DIST_NAME)
    if documented_at_head:
        (repo / _CLI_DOC).write_text(_CLI_AT_HEAD)
    else:
        (repo / _CLI_DOC).unlink()
    git_stdout(repo, "add", "-A")
    git_stdout(repo, "commit", "-q", "-m", "document clis")
    (repo / "src" / "forge").mkdir()
    (repo / "src" / "forge" / "m.py").write_text("a = 1\n")
    git_stdout(repo, "add", "-A")

    def _fake_run(cmd: list[str], **_kw: object) -> tuple[bool, str]:
        if cmd[-1] == _CLI_MODULE:
            (repo / _CLI_DOC).write_text(regenerated)
        return True, ""

    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/x")
    monkeypatch.setattr(precommit, "_run", _fake_run)
    monkeypatch.setattr(precommit, "missing_console_scripts", lambda _r: list(missing))
    return repo


def test_step_regen_docs_stale_install_dropping_sections_blocks_and_holds_doc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale install that would delete documented CLIs blocks and is not staged.

    SCENARIO: HEAD documents forge-foo and forge-bar; the install is stale
    and the regenerated doc keeps only forge-foo.
    MOCK SETUP: a real git repo; precommit._run → writes the shortened doc
    for the cli-reference generator; missing_console_scripts → one script.
    EXPECTED BEHAVIOR: result blocks (not non_blocking); the doc is not
    staged; the worktree file stays modified; the message names the lost
    section, the generator and the refresh hint.
    """
    repo = _rg_stale_cli_repo(tmp_path, monkeypatch, "# CLI\n\n## forge-foo\nfoo\n")
    result = precommit.step_regen_docs(repo)
    assert not result.non_blocking
    assert not result.passed
    assert _CLI_DOC not in git_stdout(repo, "diff", "--cached", "--name-only")
    assert _CLI_DOC in git_stdout(repo, "diff", "--name-only")
    assert (repo / _CLI_DOC).read_text() == "# CLI\n\n## forge-foo\nfoo\n"
    assert "forge-bar" in result.output
    assert "forge-gen-cli-reference" in result.output
    assert "Refresh the install" in result.output
    assert f"git add {_CLI_DOC}" in result.output
    # Held docs keep no record entry, so the next commit retries them.
    assert _CLI_DOC not in regen_docs.load_record(repo)


def test_step_regen_docs_fresh_install_dropping_a_section_is_not_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A current install may legitimately remove a CLI, so nothing is held.

    MOCK SETUP: as the blocking case, but missing_console_scripts → [].
    EXPECTED BEHAVIOR: non_blocking, and the shortened doc is staged.
    """
    repo = _rg_stale_cli_repo(
        tmp_path, monkeypatch, "# CLI\n\n## forge-foo\nfoo\n", missing=()
    )
    result = precommit.step_regen_docs(repo)
    assert result.non_blocking
    assert "NOT staged" not in result.output
    assert _CLI_DOC in git_stdout(repo, "diff", "--cached", "--name-only")


def test_step_regen_docs_stale_install_without_doc_at_head_is_not_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A doc that does not exist at HEAD cannot lose sections, so it is staged.

    MOCK SETUP: as the blocking case, with the doc deleted at HEAD.
    """
    repo = _rg_stale_cli_repo(
        tmp_path,
        monkeypatch,
        "# CLI\n\n## forge-foo\nfoo\n",
        documented_at_head=False,
    )
    result = precommit.step_regen_docs(repo)
    assert result.non_blocking
    assert "NOT staged" not in result.output


def test_step_regen_docs_stale_install_without_lost_section_only_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale install that drops nothing stays the non-blocking WARN it was.

    MOCK SETUP: as the blocking case, but the regenerated doc keeps both
    sections and adds a third.
    EXPECTED BEHAVIOR: non_blocking, not passed, the stale warning present,
    and the doc re-staged as usual.
    """
    repo = _rg_stale_cli_repo(
        tmp_path, monkeypatch, _CLI_AT_HEAD + "\n## forge-baz\nbaz\n"
    )
    result = precommit.step_regen_docs(repo)
    assert result.non_blocking
    assert not result.passed
    assert "install is stale" in result.output
    assert _CLI_DOC in git_stdout(repo, "diff", "--cached", "--name-only")


def test_install_refresh_hint_names_dev_setup_only_in_forge_repo(
    tmp_path: Path,
) -> None:
    """Forge's repo is told its one bootstrap; any other repo a generic phrase.

    A consumer has no ``dev/setup.sh``, and neither answer may recommend a
    raw ``pip install`` (FOUNDATION §2).
    """
    forge_repo = tmp_path / "forge"
    consumer = tmp_path / "consumer"
    for repo, name in ((forge_repo, regen_docs.FORGE_DIST_NAME), (consumer, "other")):
        repo.mkdir()
        (repo / "pyproject.toml").write_text(f'[project]\nname = "{name}"\n')
    assert "./dev/setup.sh" in precommit.install_refresh_hint(forge_repo)
    other = precommit.install_refresh_hint(consumer)
    assert "./dev/setup.sh" not in other
    assert "pip install" not in other
    assert "pip install" not in precommit.install_refresh_hint(forge_repo)


def _stale_install_message(
    kind: str, repo: Path, monkeypatch: pytest.MonkeyPatch, *, forge_own: bool
) -> str:
    """Produce one of the three stale-install remediation messages.

    Args:
        kind: ``clone``, ``scripts`` or ``pin``.
        repo: Directory to write the repo's pyproject into.
        monkeypatch: Used to stub the install probes.
        forge_own: Whether the repo is forge's own distribution.

    Returns:
        The message the step emitted.
    """
    name = regen_docs.FORGE_DIST_NAME if forge_own else "consumer"
    monkeypatch.setattr(precommit, "is_ci", lambda: False)
    if kind == "clone":
        (repo / "pyproject.toml").write_text(f'[project]\nname = "{name}"\n')
        monkeypatch.setattr(
            precommit, "_declared_scripts", lambda _r: (precommit.DIST_NAME, {"x"})
        )
        monkeypatch.setattr(
            precommit, "editable_install_origin", lambda: repo.parent / "other-clone"
        )
        result = precommit._check_clone_identity(repo)
        assert result is not None
        return result.output
    if kind == "scripts":
        _write_project_scripts_pyproject(repo, name, {"mycli": "", "new-cli": ""})
        monkeypatch.setattr(
            precommit, "installed_console_scripts", lambda _n: {"mycli"}
        )
    else:
        (repo / "pyproject.toml").write_text(
            f'[project]\nname = "{name}"\ndependencies = ["forge-scripts==2.9.0"]\n'
        )
        monkeypatch.setattr(precommit.importlib.metadata, "version", lambda _n: "2.8.0")
    return precommit.step_env_sync(repo).output


@pytest.mark.parametrize("kind", ["clone", "scripts", "pin"])
@pytest.mark.parametrize("forge_own", [True, False], ids=["forge-repo", "consumer"])
def test_stale_install_messages_name_refresh_command_by_repo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    *,
    forge_own: bool,
) -> None:
    """Each stale-install message names forge's bootstrap only in forge's own repo.

    A consumer has no ``dev/setup.sh``, and no message may recommend a raw
    ``pip install`` (FOUNDATION §2).

    Args:
        kind: Which of the three messages is produced.
        forge_own: Whether the repo is forge's own distribution.
    """
    output = _stale_install_message(kind, tmp_path, monkeypatch, forge_own=forge_own)
    assert "pip install" not in output
    if forge_own:
        assert "./dev/setup.sh" in output
    else:
        assert "./dev/setup.sh" not in output
        assert "reinstall forge-scripts the way this repo installs it" in output


def test_cli_reference_heading_regex_matches_generator_shape() -> None:
    """The hold's heading regex matches the ``## <cli>`` lines the generator emits.

    A heading shape the generator emits but the regex misses would make the
    held-doc check silently blind, so the committed doc must yield headings.
    """
    pattern = precommit._CLI_REFERENCE_HEADING_RE
    assert pattern.findall("# CLI\n\n## forge-foo\nbody\n\n## fix-bar \n") == [
        "forge-foo",
        "fix-bar",
    ]
    committed = (Path(__file__).parent.parent / _CLI_DOC).read_text(encoding="utf-8")
    headings = pattern.findall(committed)
    assert headings
    assert "forge-precommit" in headings


def test_step_regen_docs_first_run_unfed_checks_both_and_records_in_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No record, nothing staged → both docs only checked; in sync, a record lands."""
    repo = _rg_repo(tmp_path)
    launched = _rg_stub_generators(monkeypatch)
    result = precommit.step_regen_docs(repo)
    assert sorted(launched) == [f"{_API_MODULE} --check", f"{_CLI_MODULE} --check"]
    assert not result.skipped
    assert result.passed
    assert result.output.count("checked only (this commit does not feed it)") == 2
    assert "no record" in result.output
    assert set(regen_docs.load_record(repo)) == {_API_DOC, _CLI_DOC}


def test_step_regen_docs_first_run_drifted_unfed_doc_warns_and_touches_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No record, only an unrelated file staged, docs out of date → WARN.

    SCENARIO: a committed repo with no build record and an unrelated
        ``notes.txt`` staged; both checks fail.
    MOCK SETUP: generators stubbed via ``precommit._run``; ``--check`` fails.
    EXPECTED BEHAVIOR: non-blocking, not passed; each doc's advice names its
        generator and says it was not regenerated; no write run, the index
        holds only ``notes.txt``, no record.
    """
    repo = _rg_repo(tmp_path)
    launched = _rg_stub_generators(monkeypatch, check_ok=False)
    (repo / "notes.txt").write_text("hello\n")
    git_stdout(repo, "add", "notes.txt")
    result = precommit.step_regen_docs(repo)
    assert not result.passed
    assert result.non_blocking
    assert all(launch.endswith("--check") for launch in launched)
    assert result.output.count("not regenerated because nothing in this commit") == 2
    assert "run `forge-gen-api-digest` and commit it on its own" in result.output
    assert sorted(launched) == [f"{_API_MODULE} --check", f"{_CLI_MODULE} --check"]
    assert git_stdout(repo, "status", "--porcelain").split() == ["A", "notes.txt"]
    assert git_stdout(repo, "diff", "--cached", "--name-only").split() == ["notes.txt"]
    assert _rg_record_bytes(repo) is None


@pytest.mark.parametrize("change", ["delete", "rename-away"])
def test_step_regen_docs_staged_removal_of_an_input_feeds_the_doc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    """Deleting an input, or renaming it out of the roots, regenerates the digest.

    Neither path is in the index any more, so only the staged path itself
    can show the commit feeds the doc.

    Args:
        change: How the input leaves the doc's roots.
    """
    repo = _rg_repo(tmp_path)
    # A second module keeps the source root in existence once a.py leaves.
    (repo / "src" / "pkg" / "keep.py").write_text('"""Keep."""\n')
    git_stdout(repo, "add", "-A")
    git_stdout(repo, "commit", "-q", "-m", "second module")
    launched = _rg_stub_generators(monkeypatch)
    precommit.step_regen_docs(repo)
    launched.clear()
    if change == "delete":
        git_stdout(repo, "rm", "-q", "src/pkg/a.py")
    else:
        git_stdout(repo, "mv", "src/pkg/a.py", "scripts/a.py")
    result = precommit.step_regen_docs(repo)
    assert launched == [_API_MODULE]
    assert "regenerated — sources changed" in result.output


def test_step_regen_docs_cli_reference_check_on_stale_install_advises_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A check that fails on a stale install says refresh, never regenerate.

    Regenerating the CLI reference from a stale install drops sections, so
    the "regenerate and commit it on its own" advice would be harmful there.
    """
    repo = _rg_repo(tmp_path)

    def _cli_advice() -> str:
        output = precommit.step_regen_docs(repo).output
        return next(s for s in output.split("\n\n") if s.startswith(f"⚠️ {_CLI_DOC}"))

    # The generator itself reports a help it could not capture.
    _rg_stub_generators(
        monkeypatch,
        check_ok=False,
        check_output=gen_common.HELP_UNAVAILABLE_MARKER,
    )
    advice = _cli_advice()
    assert "refresh the install" in advice
    assert "reinstall forge-scripts" in advice
    assert "commit it on its own" not in advice
    # Or the install is missing a declared console script.
    _rg_stub_generators(monkeypatch, check_ok=False)
    monkeypatch.setattr(precommit, "missing_console_scripts", lambda _r: ["forge-new"])
    advice = _cli_advice()
    assert "refresh the install" in advice
    assert "commit it on its own" not in advice


def test_step_regen_docs_unchanged_second_run_skips_everything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With nothing changed since the first run, no generator launches."""
    repo = _rg_repo(tmp_path)
    launched = _rg_stub_generators(monkeypatch)
    precommit.step_regen_docs(repo)
    launched.clear()
    result = precommit.step_regen_docs(repo)
    assert launched == []
    assert result.skipped
    assert result.passed
    assert result.output.count("skipped — inputs unchanged since last build") == 2


def test_step_regen_docs_unrelated_staged_file_skips_both(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Staging a file outside every generator's inputs costs nothing."""
    repo = _rg_repo(tmp_path)
    launched = _rg_stub_generators(monkeypatch)
    precommit.step_regen_docs(repo)
    launched.clear()
    (repo / "notes.txt").write_text("hello\n")
    git_stdout(repo, "add", "notes.txt")
    result = precommit.step_regen_docs(repo)
    assert launched == []
    assert result.skipped


def test_step_regen_docs_staged_source_runs_api_digest_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A staged edit under the roots rebuilds the digest, not the CLI reference."""
    repo = _rg_repo(tmp_path)
    launched = _rg_stub_generators(monkeypatch)
    precommit.step_regen_docs(repo)
    launched.clear()
    (repo / "src" / "pkg" / "a.py").write_text('"""A2."""\n')
    git_stdout(repo, "add", "src/pkg/a.py")
    result = precommit.step_regen_docs(repo)
    assert launched == [_API_MODULE]
    assert not result.skipped
    assert result.output.count("regenerated — ") == 1
    assert result.output.count("skipped — ") == 1


def test_step_regen_docs_unborn_head_regenerates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The very first commit (no HEAD yet) works and regenerates both docs."""
    repo = _rg_repo(tmp_path, commit=False)
    launched = _rg_stub_generators(monkeypatch)
    result = precommit.step_regen_docs(repo)
    assert sorted(launched) == [_API_MODULE, _CLI_MODULE]
    assert not result.skipped
    assert result.passed


def test_step_regen_docs_detects_source_committed_bypassing_the_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A change that landed without the step running is caught next time.

    The record names the inputs at the last build, not the last commit, so a
    commit made with the step disabled still reads as "changed". This commit
    does not feed the digest, so it is checked and warned about, not
    regenerated into an unrelated commit.
    """
    repo = _rg_repo(tmp_path)
    launched = _rg_stub_generators(monkeypatch)
    precommit.step_regen_docs(repo)
    (repo / "src" / "pkg" / "a.py").write_text('"""Bypassed."""\n')
    git_stdout(repo, "add", "-A")
    git_stdout(repo, "commit", "-q", "-m", "bypass the step")
    launched = _rg_stub_generators(monkeypatch, check_ok=False)
    result = precommit.step_regen_docs(repo)
    assert launched == [f"{_API_MODULE} --check"]
    assert "out of date (sources changed)" in result.output
    assert result.non_blocking
    assert not result.passed
    assert git_stdout(repo, "status", "--porcelain") == ""


def test_step_regen_docs_hand_edited_staged_doc_is_regenerated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A doc edited outside the generator is rebuilt rather than trusted."""
    repo = _rg_repo(tmp_path)
    launched = _rg_stub_generators(monkeypatch)
    precommit.step_regen_docs(repo)
    launched.clear()
    (repo / _API_DOC).write_text("hand edited\n")
    git_stdout(repo, "add", _API_DOC)
    result = precommit.step_regen_docs(repo)
    assert launched == [_API_MODULE]
    assert "doc edited outside generator" in result.output


def test_step_regen_docs_merge_in_progress_regenerates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """While a merge is in progress nothing is skipped."""
    repo = _rg_repo(tmp_path)
    launched = _rg_stub_generators(monkeypatch)
    precommit.step_regen_docs(repo)
    launched.clear()
    merge_head = git_stdout(repo, "rev-parse", "--git-path", "MERGE_HEAD").strip()
    (repo / merge_head).write_text(git_stdout(repo, "rev-parse", "HEAD"))
    result = precommit.step_regen_docs(repo)
    assert sorted(launched) == [_API_MODULE, _CLI_MODULE]
    assert "merge in progress" in result.output


def test_step_regen_docs_forge_version_change_checks_and_rerecords(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A record from another forge version is not trusted: both docs re-verified.

    Nothing staged feeds either doc, so both are checked rather than
    regenerated; in sync, the record is rewritten for the current version.
    """
    repo = _rg_repo(tmp_path)
    launched = _rg_stub_generators(monkeypatch)
    precommit.step_regen_docs(repo)
    record = regen_docs.load_record(repo)
    assert record
    for entry in record.values():
        entry["forge_version"] = "0.0.0-other"
    regen_docs.save_record(repo, record)
    launched.clear()
    result = precommit.step_regen_docs(repo)
    assert sorted(launched) == [f"{_API_MODULE} --check", f"{_CLI_MODULE} --check"]
    assert "forge 0.0.0-other → " in result.output
    assert all(
        e["forge_version"] != "0.0.0-other"
        for e in regen_docs.load_record(repo).values()
    )


def test_step_regen_docs_generator_failure_leaves_record_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed rebuild must not be recorded as done."""
    repo = _rg_repo(tmp_path)
    launched = _rg_stub_generators(monkeypatch, ok=False)
    first = precommit.step_regen_docs(repo)
    assert not first.passed
    assert _rg_record_bytes(repo) is None
    monkeypatch.undo()
    _rg_stub_generators(monkeypatch)
    precommit.step_regen_docs(repo)
    recorded = _rg_record_bytes(repo)
    assert recorded is not None
    (repo / "src" / "pkg" / "a.py").write_text('"""A3."""\n')
    git_stdout(repo, "add", "src/pkg/a.py")
    monkeypatch.undo()
    launched = _rg_stub_generators(monkeypatch, ok=False)
    result = precommit.step_regen_docs(repo)
    assert launched == [_API_MODULE]
    assert not result.passed
    assert _rg_record_bytes(repo) == recorded


def test_step_regen_docs_partial_commit_guard_leaves_record_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Skipping for unstaged changes neither creates nor rewrites the record."""
    repo = _rg_repo(tmp_path)
    launched = _rg_stub_generators(monkeypatch)
    (repo / "scripts" / "tool.py").write_text("x = 2\n")
    result = precommit.step_regen_docs(repo)
    assert result.skipped
    assert launched == []
    assert _rg_record_bytes(repo) is None
    (repo / "scripts" / "tool.py").write_text("x = 1\n")
    precommit.step_regen_docs(repo)
    recorded = _rg_record_bytes(repo)
    assert recorded is not None
    (repo / "scripts" / "tool.py").write_text("x = 3\n")
    (repo / "src" / "pkg" / "a.py").write_text('"""Dirty."""\n')
    launched.clear()
    precommit.step_regen_docs(repo)
    assert launched == []
    assert _rg_record_bytes(repo) == recorded


def test_step_regen_docs_corrupt_record_rebuilds_like_no_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreadable record behaves like no record at all."""
    repo = _rg_repo(tmp_path)
    launched = _rg_stub_generators(monkeypatch)
    precommit.step_regen_docs(repo)
    path = regen_docs.record_path(repo)
    assert path is not None
    path.write_text("{corrupt")
    launched.clear()
    result = precommit.step_regen_docs(repo)
    assert sorted(launched) == [f"{_API_MODULE} --check", f"{_CLI_MODULE} --check"]
    assert "no record" in result.output


def test_step_regen_docs_staged_input_regenerates_and_stages_the_doc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A staged input regenerates the doc, and the rewritten doc is staged.

    MOCK SETUP: ``precommit._run`` → the api-digest generator rewrites the
        doc; ``--check`` runs are recorded but never expected.
    """
    repo = _rg_repo(tmp_path)
    _rg_stub_generators(monkeypatch)
    precommit.step_regen_docs(repo)

    def _writing_run(cmd: list[str], **_kw: object) -> tuple[bool, str]:
        assert cmd[-1] != "--check", cmd
        if cmd[-1] == _API_MODULE:
            (repo / _API_DOC).write_text("api v2\n")
        return True, ""

    monkeypatch.setattr(precommit, "_run", _writing_run)
    (repo / "src" / "pkg" / "a.py").write_text('"""A2."""\n')
    git_stdout(repo, "add", "src/pkg/a.py")
    result = precommit.step_regen_docs(repo)
    assert "Re-staged: docs/api-digest.md" in result.output
    staged = git_stdout(repo, "diff", "--cached", "--name-only").split()
    assert sorted(staged) == [_API_DOC, "src/pkg/a.py"]
    assert _API_DOC in regen_docs.load_record(repo)


def test_step_regen_docs_failed_help_capture_stages_nothing_and_hints_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cli-reference generator that cannot capture some help changes nothing.

    SCENARIO: forge's own repo, a ``src/forge`` edit staged (the commit
        feeds the CLI reference); the generator exits 1 reporting a help it
        could not capture, writing nothing.
    MOCK SETUP: ``precommit._run`` → api-digest succeeds, cli-reference
        fails with the generator's "--help unavailable" line.
    EXPECTED BEHAVIOR: non-blocking WARN; the CLI reference is neither
        rewritten nor staged nor recorded; the warning names the refresh.
    """
    repo = _rg_stale_cli_repo(tmp_path, monkeypatch, "unused", missing=())

    def _failing_cli(cmd: list[str], **_kw: object) -> tuple[bool, str]:
        if cmd[-1] == _CLI_MODULE:
            return False, f"{gen_common.HELP_UNAVAILABLE_MARKER} for forge-foo (exit 1)"
        return True, ""

    monkeypatch.setattr(precommit, "_run", _failing_cli)
    result = precommit.step_regen_docs(repo)
    assert result.non_blocking
    assert not result.passed
    assert (repo / _CLI_DOC).read_text() == _CLI_AT_HEAD
    assert _CLI_DOC not in git_stdout(repo, "diff", "--cached", "--name-only")
    assert _CLI_DOC not in regen_docs.load_record(repo)
    assert "./dev/setup.sh" in result.output
    assert "some CLI help could not be captured" in result.output


# ---------------------------------------------------------------------------
# Group 3: _vendored_documented_hashes + _sha256_file
# ---------------------------------------------------------------------------


def test_vendored_documented_hashes_returns_empty_when_md_absent(
    tmp_path: Path,
) -> None:
    """No VENDORED.md at src/forge/data/ → empty dict."""
    assert precommit._vendored_documented_hashes(tmp_path) == {}


def test_vendored_documented_hashes_parses_single_entry(tmp_path: Path) -> None:
    """A single ## header + SHA-256 line is parsed into {filename: hash}."""
    _write_vendored_md(tmp_path, {"mermaid.min.js": "a" * 64})
    result = precommit._vendored_documented_hashes(tmp_path)
    assert result == {"mermaid.min.js": "a" * 64}


def test_vendored_documented_hashes_parses_multiple_entries(tmp_path: Path) -> None:
    """Multiple ## sections are all parsed and returned in the dict."""
    entries = {"mermaid.min.js": "a" * 64, "elk.bundled.js": "b" * 64}
    _write_vendored_md(tmp_path, entries)
    result = precommit._vendored_documented_hashes(tmp_path)
    assert result == entries


def test_vendored_documented_hashes_skips_header_without_hash_line(
    tmp_path: Path,
) -> None:
    """A ## header with no SHA-256 line beneath it is absent from the result."""
    data_dir = tmp_path / "src" / "forge" / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "VENDORED.md").write_text(
        "## mermaid.min.js\n\nNo hash documented here.\n",
        encoding="utf-8",
    )
    result = precommit._vendored_documented_hashes(tmp_path)
    assert "mermaid.min.js" not in result


def test_sha256_file_returns_correct_digest(tmp_path: Path) -> None:
    """_sha256_file output matches hashlib.sha256 computed over the same bytes."""
    content = b"test content for sha256"
    target = tmp_path / "blob.js"
    target.write_bytes(content)
    expected = hashlib.sha256(content).hexdigest()
    assert precommit._sha256_file(target) == expected


# ---------------------------------------------------------------------------
# Group 4: step_vendored_integrity (pure FS — no subprocess mocking)
# ---------------------------------------------------------------------------


def test_step_vendored_integrity_skips_when_data_dir_absent(tmp_path: Path) -> None:
    """No src/forge/data/ directory → skipped, passed."""
    result = precommit.step_vendored_integrity(tmp_path)
    assert result.skipped
    assert result.passed


def test_step_vendored_integrity_skips_when_no_js_files(tmp_path: Path) -> None:
    """VENDORED.md present but no *.js in data dir → skipped, passed."""
    _write_vendored_md(tmp_path, {"mermaid.min.js": "a" * 64})
    result = precommit.step_vendored_integrity(tmp_path)
    assert result.skipped
    assert result.passed


def test_step_vendored_integrity_skips_when_no_vendored_md(tmp_path: Path) -> None:
    """A *.js present but no VENDORED.md → skipped, passed."""
    _write_data_js(tmp_path, "mermaid.min.js")
    result = precommit.step_vendored_integrity(tmp_path)
    assert result.skipped
    assert result.passed


def test_step_vendored_integrity_passes_when_all_hashes_match(
    tmp_path: Path,
) -> None:
    """Digest matches VENDORED.md → passed=True, skipped=False, count in output."""
    content = b"fake js"
    real_sha = hashlib.sha256(content).hexdigest()
    _write_data_js(tmp_path, "mermaid.min.js", content)
    _write_vendored_md(tmp_path, {"mermaid.min.js": real_sha})
    result = precommit.step_vendored_integrity(tmp_path)
    assert result.passed
    assert not result.skipped
    assert "Verified 1 vendored asset(s)" in result.output


def test_step_vendored_integrity_fails_blocking_on_hash_mismatch(
    tmp_path: Path,
) -> None:
    """Documented hash differs from actual digest → passed=False, non_blocking=False."""
    _write_data_js(tmp_path, "mermaid.min.js", b"real content")
    _write_vendored_md(tmp_path, {"mermaid.min.js": "0" * 64})
    result = precommit.step_vendored_integrity(tmp_path)
    assert not result.passed
    assert not result.non_blocking
    assert "mismatch" in result.output
    assert "mermaid.min.js" in result.output


def test_step_vendored_integrity_fails_blocking_on_undocumented_blob(
    tmp_path: Path,
) -> None:
    """A *.js file with no entry in VENDORED.md → passed=False, non_blocking=False."""
    _write_data_js(tmp_path, "mermaid.min.js", b"content")
    # VENDORED.md documents only an absent file — not the actual blob present.
    _write_vendored_md(tmp_path, {"other.js": "a" * 64})
    result = precommit.step_vendored_integrity(tmp_path)
    assert not result.passed
    assert not result.non_blocking
    assert "no SHA-256 entry" in result.output
    assert "mermaid.min.js" in result.output


def test_step_vendored_integrity_notes_orphan_nonfatally(tmp_path: Path) -> None:
    """Documented-but-absent file is noted in output without failing the step.

    ghost.js appears in VENDORED.md but has no corresponding file on disk;
    mermaid.min.js is present with its correct hash. The step passes overall
    (correct blob verified) but surfaces ghost.js as "documented but absent".
    """
    content = b"real blob"
    real_sha = hashlib.sha256(content).hexdigest()
    _write_data_js(tmp_path, "mermaid.min.js", content)
    _write_vendored_md(tmp_path, {"mermaid.min.js": real_sha, "ghost.js": "c" * 64})
    result = precommit.step_vendored_integrity(tmp_path)
    assert result.passed
    assert "documented but absent" in result.output
    assert "ghost.js" in result.output


def test_step_vendored_integrity_fails_with_orphan_and_mismatch_combined(
    tmp_path: Path,
) -> None:
    """Orphaned VENDORED.md entry plus a hash mismatch both surface in one result.

    Two blobs on disk: mermaid.min.js has a correct documented hash;
    elk.bundled.js has a wrong documented hash (FAIL branch). VENDORED.md also
    documents ghost.js which has no file on disk (orphan branch). Confirms
    that neither branch silences the other — both are present in the output
    and the step fails blocking.
    """
    good_content = b"good blob"
    good_sha = hashlib.sha256(good_content).hexdigest()
    _write_data_js(tmp_path, "mermaid.min.js", good_content)
    _write_data_js(tmp_path, "elk.bundled.js", b"real elk content")
    _write_vendored_md(
        tmp_path,
        {
            "mermaid.min.js": good_sha,
            "elk.bundled.js": "0" * 64,  # documented hash is wrong
            "ghost.js": "c" * 64,  # documented but no file on disk
        },
    )
    result = precommit.step_vendored_integrity(tmp_path)
    assert result.passed is False
    assert result.non_blocking is False
    assert "Note (documented but absent):" in result.output
    assert "FAIL:" in result.output
    assert "mismatch" in result.output


# ---------------------------------------------------------------------------
# missing_console_scripts — shared staleness signal for env_sync + auto_rebuild
# ---------------------------------------------------------------------------


def test_missing_console_scripts_empty_when_no_scripts_declared(
    tmp_path: Path,
) -> None:
    """Returns [] when pyproject has no [project.scripts] table."""
    result = precommit.missing_console_scripts(tmp_path)
    assert result == []


def test_missing_console_scripts_empty_when_dist_not_installed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Return [] when [project.scripts] declared but distribution is not installed.

    SCENARIO: pyproject declares mypkg with one script; installed_console_scripts
        returns None (distribution absent from the environment).
    MOCK SETUP: installed_console_scripts patched to always return None.
    EXPECTED BEHAVIOR: [] — nothing to compare against; a fresh checkout that
        predates install is not reported as stale.
    """
    _write_project_scripts_pyproject(tmp_path, "mypkg", {"mycli": ""})
    monkeypatch.setattr(precommit, "installed_console_scripts", lambda _n: None)
    assert precommit.missing_console_scripts(tmp_path) == []


def test_missing_console_scripts_lists_missing_sorted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Returns a sorted list of declared scripts absent from the installed set.

    SCENARIO: pyproject declares {a, b, c}; installed reports only {b}.
    MOCK SETUP: installed_console_scripts patched to return {"b"}.
    EXPECTED BEHAVIOR: ["a", "c"] — only the two missing names, sorted.
    """
    _write_project_scripts_pyproject(tmp_path, "mypkg", {"a": "", "b": "", "c": ""})
    monkeypatch.setattr(precommit, "installed_console_scripts", lambda _n: {"b"})
    assert precommit.missing_console_scripts(tmp_path) == ["a", "c"]


def test_missing_console_scripts_empty_when_all_installed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Returns [] when every declared script is present in the installed set."""
    _write_project_scripts_pyproject(tmp_path, "mypkg", {"mycli": "", "helper": ""})
    monkeypatch.setattr(
        precommit,
        "installed_console_scripts",
        lambda _n: {"mycli", "helper", "extra"},
    )
    assert precommit.missing_console_scripts(tmp_path) == []


# ---------------------------------------------------------------------------
# step_auto_rebuild (#128) — heal stale install before env_sync blocks
# MOCKING STRATEGY: precommit.is_non_interactive controls the CI short-circuit;
#   FORGE_NO_AUTO_REBUILD (monkeypatch.setenv/delenv) controls the opt-out path;
#   [tool.forge.env_sync].rebuild_command in a tmp pyproject.toml drives the
#   _forge_step_config lookup; precommit.missing_console_scripts controls the
#   staleness signal; precommit._run captures or stubs the subprocess without
#   spawning a real process. All tests except test_step_auto_rebuild_skips_in_ci
#   force the interactive path via is_non_interactive → lambda: False and
#   delete FORGE_NO_AUTO_REBUILD from the environment.
# ---------------------------------------------------------------------------


def test_step_auto_rebuild_skips_in_ci(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """step_auto_rebuild skips immediately when is_non_interactive returns True.

    SCENARIO: CI environment — is_non_interactive signals non-interactive.
    MOCK SETUP: precommit.is_non_interactive → lambda: True;
        FORGE_NO_AUTO_REBUILD removed to ensure the CI short-circuit is the
        sole reason for the skip (not the env opt-out).
    EXPECTED BEHAVIOR: passed True, skipped True.
    """
    monkeypatch.setattr(precommit, "is_non_interactive", lambda: True)
    monkeypatch.delenv("FORGE_NO_AUTO_REBUILD", raising=False)
    result = precommit.step_auto_rebuild(tmp_path)
    assert result.passed
    assert result.skipped


def test_step_auto_rebuild_skips_when_opted_out(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """step_auto_rebuild skips when FORGE_NO_AUTO_REBUILD is set.

    SCENARIO: interactive session but the env opt-out is active.
    MOCK SETUP: precommit.is_non_interactive → lambda: False;
        FORGE_NO_AUTO_REBUILD set to "1".
    EXPECTED BEHAVIOR: passed True, skipped True.
    """
    monkeypatch.setattr(precommit, "is_non_interactive", lambda: False)
    monkeypatch.setenv("FORGE_NO_AUTO_REBUILD", "1")
    result = precommit.step_auto_rebuild(tmp_path)
    assert result.passed
    assert result.skipped


def test_step_auto_rebuild_skips_when_no_rebuild_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """step_auto_rebuild skips when [tool.forge.env_sync].rebuild_command is absent.

    SCENARIO: interactive; no FORGE_NO_AUTO_REBUILD; no rebuild_command in
        pyproject — the step never auto-installs without an explicit command.
    MOCK SETUP: precommit.is_non_interactive → lambda: False;
        FORGE_NO_AUTO_REBUILD removed; no pyproject.toml written (no config).
    EXPECTED BEHAVIOR: passed True, skipped True; "rebuild_command configured"
        in output (specific phrase rather than bare "no", to catch message
        drift); "env_sync" in output (confirms the config path is named).
    """
    monkeypatch.setattr(precommit, "is_non_interactive", lambda: False)
    monkeypatch.delenv("FORGE_NO_AUTO_REBUILD", raising=False)
    result = precommit.step_auto_rebuild(tmp_path)
    assert result.passed
    assert result.skipped
    assert "rebuild_command configured" in result.output
    assert "env_sync" in result.output


def test_step_auto_rebuild_skips_when_install_fresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """step_auto_rebuild skips when no console scripts are missing.

    SCENARIO: interactive; rebuild_command configured; missing_console_scripts
        returns [] — install is current, nothing to do.
    MOCK SETUP: precommit.is_non_interactive → lambda: False;
        FORGE_NO_AUTO_REBUILD removed; pyproject carries rebuild_command;
        precommit.missing_console_scripts → lambda _: [].
    EXPECTED BEHAVIOR: passed True, skipped True; "nothing to rebuild" in output.
    """
    monkeypatch.setattr(precommit, "is_non_interactive", lambda: False)
    monkeypatch.delenv("FORGE_NO_AUTO_REBUILD", raising=False)
    (tmp_path / "pyproject.toml").write_text(
        '[tool.forge.env_sync]\nrebuild_command = "./dev/setup.sh"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(precommit, "missing_console_scripts", lambda _root: [])
    result = precommit.step_auto_rebuild(tmp_path)
    assert result.passed
    assert result.skipped
    assert "nothing to rebuild" in result.output


def test_step_auto_rebuild_runs_command_when_stale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """step_auto_rebuild runs the rebuild command when a declared script is missing.

    SCENARIO: interactive; rebuild_command="./dev/setup.sh"; missing_console_scripts
        returns ["forge-foo"] — one stale script triggers the rebuild.
    MOCK SETUP: precommit.is_non_interactive → lambda: False;
        FORGE_NO_AUTO_REBUILD removed; pyproject carries rebuild_command;
        precommit.missing_console_scripts → lambda _: ["forge-foo"];
        precommit._run captures argv and returns (True, "ok").
    EXPECTED BEHAVIOR: passed True, non_blocking True, skipped False;
        _run called with ["./dev/setup.sh"] (shlex.split of the command);
        "rebuilt" in output.
    """
    monkeypatch.setattr(precommit, "is_non_interactive", lambda: False)
    monkeypatch.delenv("FORGE_NO_AUTO_REBUILD", raising=False)
    (tmp_path / "pyproject.toml").write_text(
        '[tool.forge.env_sync]\nrebuild_command = "./dev/setup.sh"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        precommit, "missing_console_scripts", lambda _root: ["forge-foo"]
    )
    captured_argv: list[list[str]] = []

    def _fake_run(cmd: list[str], **_kw: object) -> tuple[bool, str]:
        captured_argv.append(cmd)
        return True, "ok"

    monkeypatch.setattr(precommit, "_run", _fake_run)
    result = precommit.step_auto_rebuild(tmp_path)
    assert result.passed
    assert result.non_blocking
    assert not result.skipped
    assert captured_argv == [["./dev/setup.sh"]]
    assert "rebuilt" in result.output


def test_step_auto_rebuild_nonblocking_when_rebuild_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """step_auto_rebuild is non-blocking when the rebuild command exits non-zero.

    SCENARIO: same stale setup as test_step_auto_rebuild_runs_command_when_stale
        but _run returns (False, "boom") — the rebuild command failed.
    MOCK SETUP: precommit.is_non_interactive → lambda: False;
        FORGE_NO_AUTO_REBUILD removed; pyproject carries rebuild_command;
        precommit.missing_console_scripts → lambda _: ["forge-foo"];
        precommit._run → lambda _cmd, **_kw: (False, "boom").
    EXPECTED BEHAVIOR: passed False, non_blocking True; "FAILED" in output.
    """
    monkeypatch.setattr(precommit, "is_non_interactive", lambda: False)
    monkeypatch.delenv("FORGE_NO_AUTO_REBUILD", raising=False)
    (tmp_path / "pyproject.toml").write_text(
        '[tool.forge.env_sync]\nrebuild_command = "./dev/setup.sh"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        precommit, "missing_console_scripts", lambda _root: ["forge-foo"]
    )
    monkeypatch.setattr(precommit, "_run", lambda _cmd, **_kw: (False, "boom"))
    result = precommit.step_auto_rebuild(tmp_path)
    assert not result.passed
    assert result.non_blocking
    assert "FAILED" in result.output


# ---------------------------------------------------------------------------
# smart_test step
# ---------------------------------------------------------------------------


def test_step_smart_test_skips_without_config(tmp_path: Path) -> None:
    """step_smart_test skips when smart_test config is absent."""
    result = precommit.step_smart_test(tmp_path)
    assert result.skipped
    assert result.passed
    assert "skipped" in result.output


def _fake_run_capturing(
    calls: list[list[str]],
    *,
    expected_results: dict[str, tuple[bool, str]] | None = None,
) -> Callable[..., tuple[bool, str]]:
    """Build a ``precommit._run`` replacement that records argv and stamps results.

    Args:
        calls: List appended with each invocation's argv, in call order.
        expected_results: Optional ``{argv_key: (passed, output)}`` map, keyed by the
            joined argv string, for tests needing per-command results.
            Unmatched argvs default to ``(False, "ERR")``.

    Returns:
        A callable compatible with ``precommit._run``'s ``(cmd, cwd)``
        signature.
    """

    def _fake(cmd: list[str], **_kw: object) -> tuple[bool, str]:
        calls.append(list(cmd))
        if expected_results is not None:
            key = " ".join(cmd)
            if key in expected_results:
                return expected_results[key]
        return False, "ERR"

    return _fake


def test_step_smart_test_non_blocking_by_default_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing smart-test run is non-blocking (WARN) when blocking is not opted in.

    SCENARIO: ``[tool.forge.smart_test] precommit_depth = 1``; no
        ``.forge-full-run`` stamp exists, so the run escalates to a full,
        ``--all-tests`` run (missing stamp always escalates); that run fails;
        no ``blocking = true`` key.
    MOCK SETUP: precommit.require_cli → no-op; precommit._run → records argv,
        returns (False, "ERR") for the escalated full-suite command.
    EXPECTED BEHAVIOR: passed=False, non_blocking=True; the escalated
        ``forge-smart-test --depth full --all-tests`` argv was used, not the
        plain ``--depth 1`` command.
    """
    (tmp_path / "pyproject.toml").write_text(
        "[tool.forge.smart_test]\nprecommit_depth = 1\n", encoding="utf-8"
    )
    monkeypatch.setattr(precommit, "require_cli", lambda *_a, **_kw: None)
    calls: list[list[str]] = []
    monkeypatch.setattr(precommit, "_run", _fake_run_capturing(calls))
    result = precommit.step_smart_test(tmp_path)
    assert not result.passed
    assert result.non_blocking
    assert calls == [["forge-smart-test", "--depth", "full", "--all-tests"]]


def test_step_smart_test_blocking_when_opted_in(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``[tool.forge.smart_test].blocking = true`` makes a failure a hard FAIL.

    SCENARIO: same missing-stamp escalation as the default case, but
        ``blocking = true`` is set.
    MOCK SETUP: precommit.require_cli → no-op; precommit._run → records argv,
        returns (False, "ERR").
    EXPECTED BEHAVIOR: passed=False, non_blocking=False; the escalated
        full-suite argv was used.
    """
    (tmp_path / "pyproject.toml").write_text(
        "[tool.forge.smart_test]\nprecommit_depth = 1\nblocking = true\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(precommit, "require_cli", lambda *_a, **_kw: None)
    calls: list[list[str]] = []
    monkeypatch.setattr(precommit, "_run", _fake_run_capturing(calls))
    result = precommit.step_smart_test(tmp_path)
    assert not result.passed
    assert not result.non_blocking
    assert calls == [["forge-smart-test", "--depth", "full", "--all-tests"]]


def test_step_smart_test_fresh_stamp_runs_normal_depth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh (well within max-age) stamp runs the plain configured depth.

    SCENARIO: ``precommit_depth = "2"``; a stamp written just now (age ≈0h,
        well under the 48h default).
    MOCK SETUP: precommit.require_cli → no-op; precommit._run → records argv,
        returns (True, "ok").
    EXPECTED BEHAVIOR: the normal ``forge-smart-test --depth 2`` argv runs —
        no escalation, no stamp rewrite.
    """
    (tmp_path / "pyproject.toml").write_text(
        '[tool.forge.smart_test]\nprecommit_depth = "2"\n', encoding="utf-8"
    )
    _lifecycle.write_stamp(tmp_path)
    monkeypatch.setattr(precommit, "require_cli", lambda *_a, **_kw: None)
    calls: list[list[str]] = []
    monkeypatch.setattr(
        precommit,
        "_run",
        _fake_run_capturing(
            calls, expected_results={"forge-smart-test --depth 2": (True, "ok")}
        ),
    )
    result = precommit.step_smart_test(tmp_path)
    assert result.passed
    assert calls == [["forge-smart-test", "--depth", "2"]]


def test_step_smart_test_stale_stamp_escalates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stamp older than ``full_run_max_age_hours`` escalates to a full run.

    SCENARIO: default 48h max age; stamp written 50 hours ago.
    MOCK SETUP: precommit.require_cli → no-op; precommit._run → records argv.
    EXPECTED BEHAVIOR: the escalated ``--depth full --all-tests`` argv runs,
        not the configured ``--depth 1``.
    """
    (tmp_path / "pyproject.toml").write_text(
        "[tool.forge.smart_test]\nprecommit_depth = 1\n", encoding="utf-8"
    )
    stale = _dt.datetime.now(tz=_dt.UTC) - _dt.timedelta(hours=50)
    (tmp_path / _lifecycle.STAMP_RELPATH).write_text(
        stale.isoformat() + "\n", encoding="utf-8"
    )
    monkeypatch.setattr(precommit, "require_cli", lambda *_a, **_kw: None)
    calls: list[list[str]] = []
    monkeypatch.setattr(
        precommit,
        "_run",
        _fake_run_capturing(
            calls,
            expected_results={
                "forge-smart-test --depth full --all-tests": (True, "ok")
            },
        ),
    )
    result = precommit.step_smart_test(tmp_path)
    assert result.passed
    assert calls[0] == ["forge-smart-test", "--depth", "full", "--all-tests"]


def test_step_smart_test_escalated_pass_rewrites_stamp_and_stages_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An escalated run that passes rewrites the stamp and stages it via git.

    SCENARIO: no stamp (missing → escalate); the escalated run passes.
    MOCK SETUP: precommit.require_cli → no-op; precommit._run → records argv,
        returns (True, "ok") for the escalated command and (True, "") for the
        ``git add`` call.
    EXPECTED BEHAVIOR: a ``git add .forge-full-run`` call is captured after
        the escalated run; the output carries both cadence lines (the
        escalation notice and the stamp-refreshed confirmation).
    """
    (tmp_path / "pyproject.toml").write_text(
        "[tool.forge.smart_test]\nprecommit_depth = 1\n", encoding="utf-8"
    )
    monkeypatch.setattr(precommit, "require_cli", lambda *_a, **_kw: None)
    calls: list[list[str]] = []
    monkeypatch.setattr(
        precommit,
        "_run",
        _fake_run_capturing(
            calls,
            expected_results={
                "forge-smart-test --depth full --all-tests": (True, "ok"),
            },
        ),
    )
    result = precommit.step_smart_test(tmp_path)
    assert result.passed
    assert ["git", "add", str(_lifecycle.STAMP_RELPATH)] in calls
    assert "missing or invalid" in result.output
    assert "ran the full suite with --all-tests" in result.output
    assert "stamp refreshed and staged into this commit" in result.output


def test_step_smart_test_escalated_fail_does_not_rewrite_stamp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An escalated run that fails leaves the stamp untouched and never stages it.

    SCENARIO: no stamp (missing → escalate); the escalated run fails.
    MOCK SETUP: precommit.require_cli → no-op; precommit._run → records argv,
        returns (False, "ERR").
    EXPECTED BEHAVIOR: no ``git add`` call is captured; the stamp file still
        does not exist.
    """
    (tmp_path / "pyproject.toml").write_text(
        "[tool.forge.smart_test]\nprecommit_depth = 1\n", encoding="utf-8"
    )
    monkeypatch.setattr(precommit, "require_cli", lambda *_a, **_kw: None)
    calls: list[list[str]] = []
    monkeypatch.setattr(precommit, "_run", _fake_run_capturing(calls))
    result = precommit.step_smart_test(tmp_path)
    assert not result.passed
    assert not any(cmd[:2] == ["git", "add"] for cmd in calls)
    assert not (tmp_path / _lifecycle.STAMP_RELPATH).exists()


def test_step_smart_test_full_run_max_age_hours_config_honored(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tightened ``full_run_max_age_hours`` escalates on stale stamps.

    SCENARIO: ``full_run_max_age_hours = 1``; stamp written 2 hours ago.
    MOCK SETUP: precommit.require_cli → no-op; precommit._run → records argv.
    EXPECTED BEHAVIOR: the escalated ``--depth full --all-tests`` argv runs.
    """
    (tmp_path / "pyproject.toml").write_text(
        "[tool.forge.smart_test]\nprecommit_depth = 1\nfull_run_max_age_hours = 1\n",
        encoding="utf-8",
    )
    two_hours_ago = _dt.datetime.now(tz=_dt.UTC) - _dt.timedelta(hours=2)
    (tmp_path / _lifecycle.STAMP_RELPATH).write_text(
        two_hours_ago.isoformat() + "\n", encoding="utf-8"
    )
    monkeypatch.setattr(precommit, "require_cli", lambda *_a, **_kw: None)
    calls: list[list[str]] = []
    monkeypatch.setattr(
        precommit,
        "_run",
        _fake_run_capturing(
            calls,
            expected_results={
                "forge-smart-test --depth full --all-tests": (True, "ok")
            },
        ),
    )
    result = precommit.step_smart_test(tmp_path)
    assert result.passed
    assert calls[0] == ["forge-smart-test", "--depth", "full", "--all-tests"]


def test_step_smart_test_full_run_max_age_hours_non_numeric_falls_back_to_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-numeric ``full_run_max_age_hours`` falls back to the 48h default.

    SCENARIO: ``full_run_max_age_hours = "not-a-number"`` (a malformed
        config value); a stamp written just now (age ~0h, well under the
        48h default).
    MOCK SETUP: precommit.require_cli → no-op; precommit._run → records argv.
    EXPECTED BEHAVIOR: the fresh stamp does not escalate under the 48h
        fallback — the normal ``forge-smart-test --depth 1`` argv runs, not
        ``--depth full``.
    """
    (tmp_path / "pyproject.toml").write_text(
        "[tool.forge.smart_test]\nprecommit_depth = 1\n"
        'full_run_max_age_hours = "not-a-number"\n',
        encoding="utf-8",
    )
    _lifecycle.write_stamp(tmp_path)
    monkeypatch.setattr(precommit, "require_cli", lambda *_a, **_kw: None)
    calls: list[list[str]] = []
    monkeypatch.setattr(
        precommit,
        "_run",
        _fake_run_capturing(
            calls, expected_results={"forge-smart-test --depth 1": (True, "ok")}
        ),
    )
    result = precommit.step_smart_test(tmp_path)
    assert result.passed
    assert calls == [["forge-smart-test", "--depth", "1"]]


def test_step_smart_test_advisory_missing_stamp_never_escalates_or_blocks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``cadence_mode = "advisory"`` never escalates; blocking survives.

    SCENARIO: ``cadence_mode = "advisory"``, ``precommit_depth = 1``,
        ``blocking = true``; no ``.forge-full-run`` stamp exists; the
        configured-depth run FAILS.
    MOCK SETUP: precommit.require_cli → no-op; precommit._run → records argv,
        returns (False, "boom") for the plain configured-depth command.
    EXPECTED BEHAVIOR: only the configured-depth argv runs — no escalated
        full-suite command, no ``git add`` of the stamp; the output carries
        the advisory cadence line; the REAL failure still gates —
        ``non_blocking`` stays False because the repo opted into
        ``blocking = true`` (stamp staleness must never disable it).
    """
    (tmp_path / "pyproject.toml").write_text(
        '[tool.forge.smart_test]\ncadence_mode = "advisory"\n'
        "precommit_depth = 1\nblocking = true\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(precommit, "require_cli", lambda *_a, **_kw: None)
    calls: list[list[str]] = []
    monkeypatch.setattr(
        precommit,
        "_run",
        _fake_run_capturing(
            calls, expected_results={"forge-smart-test --depth 1": (False, "boom")}
        ),
    )
    result = precommit.step_smart_test(tmp_path)
    assert calls == [["forge-smart-test", "--depth", "1"]]
    assert "cadence: advisory" in result.output
    assert result.passed is False
    assert result.non_blocking is False


def test_step_smart_test_advisory_fresh_stamp_no_cadence_line(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``cadence_mode = "advisory"`` with a fresh stamp adds no cadence line.

    SCENARIO: ``cadence_mode = "advisory"``, ``precommit_depth = "2"``; a
        stamp written just now (age ≈0h, well under the 48h default).
    MOCK SETUP: precommit.require_cli → no-op; precommit._run → records argv,
        returns (True, "ok") for the plain configured-depth command.
    EXPECTED BEHAVIOR: only the configured-depth argv runs; the output
        carries no ``cadence:`` line at all — the stamp isn't stale, so
        advisory has nothing to warn about.
    """
    (tmp_path / "pyproject.toml").write_text(
        '[tool.forge.smart_test]\ncadence_mode = "advisory"\nprecommit_depth = "2"\n',
        encoding="utf-8",
    )
    _lifecycle.write_stamp(tmp_path)
    monkeypatch.setattr(precommit, "require_cli", lambda *_a, **_kw: None)
    calls: list[list[str]] = []
    monkeypatch.setattr(
        precommit,
        "_run",
        _fake_run_capturing(
            calls, expected_results={"forge-smart-test --depth 2": (True, "ok")}
        ),
    )
    result = precommit.step_smart_test(tmp_path)
    assert calls == [["forge-smart-test", "--depth", "2"]]
    assert "cadence:" not in result.output


def test_step_smart_test_external_stamp_past_2x_window_warns_broken(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``cadence_mode = "external"`` warns once a stamp is stale past 2x the window.

    SCENARIO: ``cadence_mode = "external"``, ``precommit_depth = 1``, default
        48h window; stamp written 100 hours ago (> 2 * 48h).
    MOCK SETUP: precommit.require_cli → no-op; precommit._run → records argv,
        returns (True, "ok") for the plain configured-depth command.
    EXPECTED BEHAVIOR: only the configured-depth argv runs — external mode
        never escalates; the output warns the CI cadence job may be broken.
    """
    (tmp_path / "pyproject.toml").write_text(
        '[tool.forge.smart_test]\ncadence_mode = "external"\nprecommit_depth = 1\n',
        encoding="utf-8",
    )
    stale = _dt.datetime.now(tz=_dt.UTC) - _dt.timedelta(hours=100)
    (tmp_path / _lifecycle.STAMP_RELPATH).write_text(
        stale.isoformat() + "\n", encoding="utf-8"
    )
    monkeypatch.setattr(precommit, "require_cli", lambda *_a, **_kw: None)
    calls: list[list[str]] = []
    monkeypatch.setattr(
        precommit,
        "_run",
        _fake_run_capturing(
            calls, expected_results={"forge-smart-test --depth 1": (True, "ok")}
        ),
    )
    result = precommit.step_smart_test(tmp_path)
    assert calls == [["forge-smart-test", "--depth", "1"]]
    assert "may be broken" in result.output


def test_step_smart_test_external_stamp_stale_under_2x_no_cadence_line(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``cadence_mode = "external"`` stays silent for a stamp stale but under 2x.

    Regression guard for the 2x threshold: a stamp past the plain 48h
    window but short of the 96h (2x) broken-pipeline threshold must not
    warn — external mode only detects a *broken* cadence job, not routine
    staleness the scheduled job hasn't refreshed yet.

    SCENARIO: ``cadence_mode = "external"``, ``precommit_depth = 1``,
        default 48h window; stamp written 60 hours ago (stale, but
        < 2 * 48h = 96h).
    MOCK SETUP: precommit.require_cli → no-op; precommit._run → records argv,
        returns (True, "ok") for the plain configured-depth command.
    EXPECTED BEHAVIOR: only the configured-depth argv runs; the output
        carries no ``cadence:`` line.
    """
    (tmp_path / "pyproject.toml").write_text(
        '[tool.forge.smart_test]\ncadence_mode = "external"\nprecommit_depth = 1\n',
        encoding="utf-8",
    )
    stale = _dt.datetime.now(tz=_dt.UTC) - _dt.timedelta(hours=60)
    (tmp_path / _lifecycle.STAMP_RELPATH).write_text(
        stale.isoformat() + "\n", encoding="utf-8"
    )
    monkeypatch.setattr(precommit, "require_cli", lambda *_a, **_kw: None)
    calls: list[list[str]] = []
    monkeypatch.setattr(
        precommit,
        "_run",
        _fake_run_capturing(
            calls, expected_results={"forge-smart-test --depth 1": (True, "ok")}
        ),
    )
    result = precommit.step_smart_test(tmp_path)
    assert calls == [["forge-smart-test", "--depth", "1"]]
    assert "cadence:" not in result.output


def test_step_smart_test_external_missing_stamp_warns_broken(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``cadence_mode = "external"`` treats a missing stamp as past the 2x threshold.

    SCENARIO: ``cadence_mode = "external"``, ``precommit_depth = 1``; no
        ``.forge-full-run`` stamp exists.
    MOCK SETUP: precommit.require_cli → no-op; precommit._run → records argv,
        returns (True, "ok") for the plain configured-depth command.
    EXPECTED BEHAVIOR: only the configured-depth argv runs; the output
        warns the CI cadence job may be broken and reports the stamp as
        missing or invalid.
    """
    (tmp_path / "pyproject.toml").write_text(
        '[tool.forge.smart_test]\ncadence_mode = "external"\nprecommit_depth = 1\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(precommit, "require_cli", lambda *_a, **_kw: None)
    calls: list[list[str]] = []
    monkeypatch.setattr(
        precommit,
        "_run",
        _fake_run_capturing(
            calls, expected_results={"forge-smart-test --depth 1": (True, "ok")}
        ),
    )
    result = precommit.step_smart_test(tmp_path)
    assert calls == [["forge-smart-test", "--depth", "1"]]
    assert "may be broken" in result.output
    assert "missing or invalid" in result.output


def test_step_smart_test_unknown_cadence_mode_falls_back_to_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unrecognized ``cadence_mode`` falls back to ``"commit"`` behavior.

    SCENARIO: ``cadence_mode = "cron"`` (not one of ``commit`` /
        ``advisory`` / ``external``); no ``.forge-full-run`` stamp exists.
    MOCK SETUP: precommit.require_cli → no-op; precommit._run → records argv,
        returns (True, "ok") for the escalated full-suite command and
        (True, "") for the ``git add`` call.
    EXPECTED BEHAVIOR: escalates exactly like the ``"commit"`` default —
        the escalated ``--depth full --all-tests`` argv runs first, and on
        success the stamp is rewritten on disk and staged via ``git add``.
    """
    (tmp_path / "pyproject.toml").write_text(
        '[tool.forge.smart_test]\ncadence_mode = "cron"\nprecommit_depth = 1\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(precommit, "require_cli", lambda *_a, **_kw: None)
    calls: list[list[str]] = []
    monkeypatch.setattr(
        precommit,
        "_run",
        _fake_run_capturing(
            calls,
            expected_results={
                "forge-smart-test --depth full --all-tests": (True, "ok"),
            },
        ),
    )
    result = precommit.step_smart_test(tmp_path)
    assert result.passed
    assert calls[0] == ["forge-smart-test", "--depth", "full", "--all-tests"]
    assert ["git", "add", str(_lifecycle.STAMP_RELPATH)] in calls
    assert (tmp_path / _lifecycle.STAMP_RELPATH).exists()


# ---------------------------------------------------------------------------
# step_changelog_version / step_changelog_updated
# ---------------------------------------------------------------------------


def _fake_run_git_dispatch(*, base_changelog: str = "") -> Callable[..., str]:
    """Build a ``run_git`` fake dispatching on the git subcommand.

    Branch resolution is not dispatched here — ``step_changelog_version``
    resolves the current branch via ``git_utils.resolve_current_branch``
    (patched separately with ``_fake_resolve_current_branch``), which calls
    its own module-internal ``run_git`` rather than the one patched on
    ``precommit``. ``merge-base`` is likewise not dispatched here —
    ``step_changelog_version`` resolves the merge-base via
    ``git_utils.merge_base_with_head``, not a raw ``run_git("merge-base",
    ...)`` call, so callers patch that function directly instead of
    feeding this fake a ``merge_base=`` value.

    Args:
        base_changelog: What ``show <rev>:CHANGELOG.md`` reports — the
            old-side contents the stranded membership comparison reads.

    Returns:
        A callable with ``run_git``'s signature.
    """

    def _fake(*args: str, **_kw: object) -> str:
        if args[0] == "show":
            return base_changelog
        return ""

    return _fake


def _fake_resolve_current_branch(
    branch: str | None, source: str = "local"
) -> Callable[[Path], tuple[str, str] | None]:
    """Build a `resolve_current_branch` fake returning a fixed `(branch, source)`.

    Args:
        branch: Branch name to report, or `None` to simulate the guard
            seeing no current branch (detached HEAD, no GITHUB_HEAD_REF).
        source: `"local"` or `"GITHUB_HEAD_REF"`.

    Returns:
        A callable with `resolve_current_branch`'s signature.
    """

    def _fake(_repo_root: Path) -> tuple[str, str] | None:
        return None if branch is None else (branch, source)

    return _fake


def test_step_changelog_version_skips_without_changelog(tmp_path: Path) -> None:
    """No CHANGELOG.md → self-skip."""
    result = precommit.step_changelog_version(tmp_path)
    assert result.skipped


def test_step_changelog_version_skips_manifest_repo(tmp_path: Path) -> None:
    """Plugin-manifest repo → verify-forge-plugin-version owns it; skip."""
    (tmp_path / "CHANGELOG.md").write_text("## v1.0.0\n")
    (tmp_path / ".claude-plugin").mkdir()
    (tmp_path / ".claude-plugin" / "plugin.json").write_text("{}")
    result = precommit.step_changelog_version(tmp_path)
    assert result.skipped


def test_step_changelog_version_fails_on_invalid_heading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`## Unreleased` heading fails the gate, blocking by default."""
    (tmp_path / "CHANGELOG.md").write_text("## Unreleased\n\n## v1.0.0\n")
    monkeypatch.setattr(precommit, "latest_v_tag", lambda _r: "v1.0.0")
    monkeypatch.setattr(
        precommit, "resolve_current_branch", _fake_resolve_current_branch("feat/x")
    )
    monkeypatch.setattr(precommit, "fetch_tags_best_effort", lambda _r, **_kw: [])
    monkeypatch.setattr(precommit, "run_git", _fake_run_git_dispatch())
    result = precommit.step_changelog_version(tmp_path)
    assert not result.passed
    assert not result.non_blocking
    assert "## Unreleased" in result.output


def test_step_changelog_version_passes_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Consistent headings and no stranded entries → pass."""
    (tmp_path / "CHANGELOG.md").write_text("## v1.1.0\n\n- a\n\n## v1.0.0\n")
    monkeypatch.setattr(precommit, "latest_v_tag", lambda _r: "v1.0.0")
    monkeypatch.setattr(
        precommit, "resolve_current_branch", _fake_resolve_current_branch("feat/x")
    )
    monkeypatch.setattr(precommit, "fetch_tags_best_effort", lambda _r, **_kw: [])
    monkeypatch.setattr(precommit, "run_git", _fake_run_git_dispatch())
    result = precommit.step_changelog_version(tmp_path)
    assert result.passed


def test_step_changelog_version_detects_stranded_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Branch adds a bullet under the already-tagged top heading → fail.

    MOCK SETUP: `merge_base_with_head` is patched directly (the step now
    resolves the merge-base via that helper rather than composing
    `run_git("merge-base", ...)` itself) to return a fixed SHA; `run_git`
    handles `branch --show-current` and the `show <rev>:CHANGELOG.md`
    old-side read for the membership comparison.
    """
    text = "## v1.0.0\n\n- new bullet\n"
    base_changelog = "## v1.0.0\n"
    (tmp_path / "CHANGELOG.md").write_text(text)
    monkeypatch.setattr(precommit, "latest_v_tag", lambda _r: "v1.0.0")
    monkeypatch.setattr(
        precommit, "resolve_current_branch", _fake_resolve_current_branch("feat/x")
    )
    monkeypatch.setattr(precommit, "fetch_tags_best_effort", lambda _r, **_kw: [])
    merge_base_calls: list[tuple[object, object]] = []

    def _fake_merge_base_with_head(root: object, base: object) -> str:
        merge_base_calls.append((root, base))
        return "abc123"

    monkeypatch.setattr(precommit, "merge_base_with_head", _fake_merge_base_with_head)
    monkeypatch.setattr(
        precommit, "run_git", _fake_run_git_dispatch(base_changelog=base_changelog)
    )
    result = precommit.step_changelog_version(tmp_path)
    assert not result.passed
    assert "stranded" in result.output
    assert "merge in progress" not in result.output
    cfg = config.load_config(tmp_path)
    assert merge_base_calls == [(tmp_path, cfg.base_branch)]


def test_step_changelog_version_detects_deleted_released_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Branch removes a bullet under the already-tagged top heading → fail.

    MOCK SETUP: same shape as
    `test_step_changelog_version_detects_stranded_entries`, but the
    base-side changelog carries a bullet the branch's current changelog no
    longer has — a released-history deletion (#363), not a strand.
    """
    text = "## v1.0.0\n"
    base_changelog = "## v1.0.0\n\n- old bullet\n"
    (tmp_path / "CHANGELOG.md").write_text(text)
    monkeypatch.setattr(precommit, "latest_v_tag", lambda _r: "v1.0.0")
    monkeypatch.setattr(
        precommit, "resolve_current_branch", _fake_resolve_current_branch("feat/x")
    )
    monkeypatch.setattr(precommit, "fetch_tags_best_effort", lambda _r, **_kw: [])
    monkeypatch.setattr(precommit, "merge_base_with_head", lambda *_a: "abc123")
    monkeypatch.setattr(
        precommit, "run_git", _fake_run_git_dispatch(base_changelog=base_changelog)
    )
    result = precommit.step_changelog_version(tmp_path)
    assert result.passed is False
    assert "deleted" in result.output


def test_step_changelog_version_deleted_and_stranded_both_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A branch that both drops one bullet and adds another under v1.0.0 reports both.

    MOCK SETUP: base-side changelog has `- keep` and `- old bullet`; the
    branch's changelog has `- keep` and `- new bullet` — one entry lost,
    one gained, same released heading.
    """
    text = "## v1.0.0\n\n- keep\n- new bullet\n"
    base_changelog = "## v1.0.0\n\n- keep\n- old bullet\n"
    (tmp_path / "CHANGELOG.md").write_text(text)
    monkeypatch.setattr(precommit, "latest_v_tag", lambda _r: "v1.0.0")
    monkeypatch.setattr(
        precommit, "resolve_current_branch", _fake_resolve_current_branch("feat/x")
    )
    monkeypatch.setattr(precommit, "fetch_tags_best_effort", lambda _r, **_kw: [])
    monkeypatch.setattr(precommit, "merge_base_with_head", lambda *_a: "abc123")
    monkeypatch.setattr(
        precommit, "run_git", _fake_run_git_dispatch(base_changelog=base_changelog)
    )
    result = precommit.step_changelog_version(tmp_path)
    assert result.passed is False
    assert "stranded" in result.output
    assert "deleted" in result.output


def _setup_tagged_repo_mid_merge(base: Path, feat_changelog: str) -> Path:
    """Build a tagged single-track repo mid `--no-ff --no-commit` merge on `feat/x`.

    Args:
        base: Base directory for the test repo.
        feat_changelog: Changelog content for the feature branch.

    Returns:
        Path to the work repository.
    """
    work, _bare = init_single_track_repo(base)
    (work / "CHANGELOG.md").write_text("## v1.0.0\n")
    subprocess.run(["git", "add", "CHANGELOG.md"], cwd=work, env=GIT_ENV, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "chore: add changelog"],
        cwd=work,
        env=GIT_ENV,
        check=True,
    )
    subprocess.run(
        ["git", "push", "-q", "origin", "main"], cwd=work, env=GIT_ENV, check=True
    )
    subprocess.run(
        ["git", "tag", "-a", "v1.0.0", "-m", "v1.0.0"],
        cwd=work,
        env=GIT_ENV,
        check=True,
    )
    subprocess.run(
        ["git", "push", "-q", "origin", "--tags"], cwd=work, env=GIT_ENV, check=True
    )

    subprocess.run(
        ["git", "checkout", "-q", "-b", "other"], cwd=work, env=GIT_ENV, check=True
    )
    (work / "other.txt").write_text("other\n")
    subprocess.run(["git", "add", "other.txt"], cwd=work, env=GIT_ENV, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "other work"], cwd=work, env=GIT_ENV, check=True
    )

    subprocess.run(["git", "checkout", "-q", "main"], cwd=work, env=GIT_ENV, check=True)
    subprocess.run(
        ["git", "checkout", "-q", "-b", "feat/x"], cwd=work, env=GIT_ENV, check=True
    )
    (work / "CHANGELOG.md").write_text(feat_changelog)
    subprocess.run(["git", "add", "CHANGELOG.md"], cwd=work, env=GIT_ENV, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "docs: feat/x changelog"],
        cwd=work,
        env=GIT_ENV,
        check=True,
    )

    subprocess.run(
        ["git", "merge", "--no-ff", "--no-commit", "other"],
        cwd=work,
        env=GIT_ENV,
        check=True,
    )
    return work


def test_step_changelog_version_skips_stranded_diff_during_merge(
    tmp_path: Path,
) -> None:
    """A merge in progress suppresses the stranded-entries diff, not the gate.

    SCENARIO: `feat/x` carries a genuinely stranded-shaped bullet (added
    under the already-tagged `v1.0.0` heading) — outside a merge this
    would fail. But `feat/x` is mid `git merge --no-ff --no-commit
    other`: `HEAD` still predates the merge commit, so the merge-base
    would be the stale fork point and misattribute `other`'s changes.
    The step must recognize the in-progress merge and skip only the
    stranded diff, still passing since no structural finding fires.
    """
    work = _setup_tagged_repo_mid_merge(tmp_path, "## v1.0.0\n\n- new bullet\n")
    result = precommit.step_changelog_version(work)
    assert result.passed is True
    assert "Entries added under released heading" not in result.output
    assert "merge in progress" in result.output


def test_step_changelog_version_structural_findings_still_fire_during_merge(
    tmp_path: Path,
) -> None:
    """A merge in progress only suppresses the stranded diff, not structural findings.

    SCENARIO: same mid-merge setup as
    `test_step_changelog_version_skips_stranded_diff_during_merge`, but
    `feat/x`'s CHANGELOG carries an invalid `## Unreleased` heading. The
    stranded-diff suppression must not blanket-pass the step — the
    heading-validity finding still fires.
    """
    work = _setup_tagged_repo_mid_merge(tmp_path, "## Unreleased\n\n## v1.0.0\n")
    result = precommit.step_changelog_version(work)
    assert result.passed is False
    assert "## Unreleased" in result.output
    assert "merge in progress" in result.output


def test_step_changelog_version_nonblocking_when_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[tool.forge.changelog].blocking=false downgrades a finding to WARN."""
    (tmp_path / "CHANGELOG.md").write_text("## Unreleased\n")
    (tmp_path / "pyproject.toml").write_text(
        "[tool.forge.changelog]\nblocking = false\n"
    )
    monkeypatch.setattr(precommit, "latest_v_tag", lambda _r: None)
    monkeypatch.setattr(
        precommit, "resolve_current_branch", _fake_resolve_current_branch("feat/x")
    )
    monkeypatch.setattr(precommit, "fetch_tags_best_effort", lambda _r, **_kw: [])
    monkeypatch.setattr(precommit, "run_git", _fake_run_git_dispatch())
    result = precommit.step_changelog_version(tmp_path)
    assert not result.passed
    assert result.non_blocking


def test_step_changelog_version_stale_local_base_no_false_positive(
    tmp_path: Path,
) -> None:
    """A stale local `main` must not manufacture a stranded-entry false positive.

    SCENARIO: `work`'s local `main` stays on the pre-release commit while
    `origin/main` advances with the tagged release entry. A feature
    branch cut straight from `origin/main` is byte-identical to it, so
    the gate must diff against `origin/main` (the correct merge-base) —
    diffing against the stale local `main` would show the released
    bullet as freshly added under the already-tagged heading, the false
    positive this PR fixes.
    """
    work, bare = init_single_track_repo(tmp_path)
    (work / "CHANGELOG.md").write_text("## v1.0.0\n")
    subprocess.run(["git", "add", "CHANGELOG.md"], cwd=work, env=GIT_ENV, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "chore: add changelog"],
        cwd=work,
        env=GIT_ENV,
        check=True,
    )
    subprocess.run(
        ["git", "push", "-q", "origin", "main"], cwd=work, env=GIT_ENV, check=True
    )

    # A separate clone stands in for "upstream": it finalizes the release
    # (appends the bullet, tags, pushes) while `work`'s local `main` never
    # moves — mirroring a developer who branched before pulling the latest
    # `main`.
    upstream = tmp_path / "upstream"
    subprocess.run(
        ["git", "clone", "-q", str(bare), str(upstream)], env=GIT_ENV, check=True
    )
    (upstream / "CHANGELOG.md").write_text("## v1.0.0\n\n- released thing\n")
    subprocess.run(
        ["git", "add", "CHANGELOG.md"], cwd=upstream, env=GIT_ENV, check=True
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "release: v1.0.0"],
        cwd=upstream,
        env=GIT_ENV,
        check=True,
    )
    subprocess.run(
        ["git", "tag", "-a", "v1.0.0", "-m", "v1.0.0"],
        cwd=upstream,
        env=GIT_ENV,
        check=True,
    )
    subprocess.run(
        ["git", "push", "-q", "origin", "main", "--tags"],
        cwd=upstream,
        env=GIT_ENV,
        check=True,
    )

    # `work` fetches the tag and branches from `origin/main` — local `main`
    # is never updated, so it stays stranded on the pre-release commit.
    subprocess.run(
        ["git", "fetch", "-q", "origin", "--tags"], cwd=work, env=GIT_ENV, check=True
    )
    subprocess.run(
        ["git", "checkout", "-q", "-b", "feat/x", "origin/main"],
        cwd=work,
        env=GIT_ENV,
        check=True,
    )

    result = precommit.step_changelog_version(work)
    assert result.passed is True
    assert "stranded" not in result.output


# ---------------------------------------------------------------------------
# step_changelog_version — stale-branch guidance (_tag_only_on_base)
# ---------------------------------------------------------------------------


def test_step_changelog_version_stale_branch_guidance_when_tag_only_on_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tag-shaped finding + `_tag_only_on_base` True appends the stale-branch cure.

    SCENARIO: CHANGELOG.md is missing the latest tag's heading (a
    tag-shaped finding — "has no `## v1.0.0` heading"), and the tag lives
    on the base branch but not on this one. The gate must name the one
    real cure (merging the base in) rather than let the author flail at
    hand-editing headings.
    """
    (tmp_path / "CHANGELOG.md").write_text("## v1.1.0\n")
    monkeypatch.setattr(precommit, "latest_v_tag", lambda _r: "v1.0.0")
    monkeypatch.setattr(
        precommit, "resolve_current_branch", _fake_resolve_current_branch("feat/x")
    )
    monkeypatch.setattr(precommit, "fetch_tags_best_effort", lambda _r, **_kw: [])
    monkeypatch.setattr(precommit, "run_git", _fake_run_git_dispatch())
    monkeypatch.setattr(precommit, "_tag_only_on_base", lambda *_a, **_kw: True)
    result = precommit.step_changelog_version(tmp_path)
    assert not result.passed
    assert not result.non_blocking
    assert "Stale branch:" in result.output
    assert "git merge origin/main" in result.output
    assert "will not work" in result.output


def test_step_changelog_version_no_stale_guidance_when_tag_not_only_on_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same tag-shaped finding but `_tag_only_on_base` False → plain finding only.

    SCENARIO: identical CHANGELOG/tag setup to the stale-branch case, but
    the tag is reachable from HEAD too (or from neither ref) — not the
    stale-branch signature. The finding still fails the gate; only the
    extra "Stale branch:" guidance is withheld.
    """
    (tmp_path / "CHANGELOG.md").write_text("## v1.1.0\n")
    monkeypatch.setattr(precommit, "latest_v_tag", lambda _r: "v1.0.0")
    monkeypatch.setattr(
        precommit, "resolve_current_branch", _fake_resolve_current_branch("feat/x")
    )
    monkeypatch.setattr(precommit, "fetch_tags_best_effort", lambda _r, **_kw: [])
    monkeypatch.setattr(precommit, "run_git", _fake_run_git_dispatch())
    monkeypatch.setattr(precommit, "_tag_only_on_base", lambda *_a, **_kw: False)
    result = precommit.step_changelog_version(tmp_path)
    assert not result.passed
    assert "Stale branch:" not in result.output
    assert "v1.0.0" in result.output


def test_step_changelog_version_no_stale_guidance_for_non_tag_shaped_finding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-tag-shaped finding never gets stale-branch guidance.

    Even when `_tag_only_on_base` is True.

    SCENARIO: the latest tag's heading IS present (no "has no" finding)
    and the top heading is not behind the tag, but headings are
    non-decreasing (a duplicate heading) — a structural finding unrelated
    to tag reachability. `_tag_only_on_base` is stubbed True to prove the
    guidance is gated on `tag_shaped`, not merely called unconditionally.
    """
    (tmp_path / "CHANGELOG.md").write_text("## v1.0.0\n\n## v1.0.0\n")
    monkeypatch.setattr(precommit, "latest_v_tag", lambda _r: "v1.0.0")
    monkeypatch.setattr(
        precommit, "resolve_current_branch", _fake_resolve_current_branch("feat/x")
    )
    monkeypatch.setattr(precommit, "fetch_tags_best_effort", lambda _r, **_kw: [])
    monkeypatch.setattr(precommit, "run_git", _fake_run_git_dispatch())
    monkeypatch.setattr(precommit, "_tag_only_on_base", lambda *_a, **_kw: True)
    result = precommit.step_changelog_version(tmp_path)
    assert not result.passed
    assert "not strictly decreasing" in result.output
    assert "Stale branch:" not in result.output


# ---------------------------------------------------------------------------
# _tag_only_on_base
# ---------------------------------------------------------------------------


def test_tag_only_on_base_false_when_base_ref_unresolvable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No resolvable base ref → False, without calling `is_ancestor` at all."""
    monkeypatch.setattr(precommit, "resolve_base_branch_ref", lambda *_a, **_kw: None)

    def _unexpected_is_ancestor(*_a: object, **_kw: object) -> bool:
        msg = "is_ancestor must not be called when the base ref is unresolvable"
        raise AssertionError(msg)

    monkeypatch.setattr(precommit, "is_ancestor", _unexpected_is_ancestor)
    assert precommit._tag_only_on_base(tmp_path, "v1.0.0", "main") is False


def test_tag_only_on_base_true_when_tag_on_base_but_not_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tag reachable from base but not HEAD → True (the stale-branch signature)."""
    monkeypatch.setattr(
        precommit, "resolve_base_branch_ref", lambda *_a, **_kw: "origin/main"
    )

    def _fake_is_ancestor(_root: object, _tag: str, descendant_ref: str) -> bool:
        return descendant_ref == "origin/main"

    monkeypatch.setattr(precommit, "is_ancestor", _fake_is_ancestor)
    assert precommit._tag_only_on_base(tmp_path, "v1.0.0", "main") is True


def test_tag_only_on_base_false_when_tag_on_both_base_and_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tag reachable from both base and HEAD → False (branch is up to date)."""
    monkeypatch.setattr(
        precommit, "resolve_base_branch_ref", lambda *_a, **_kw: "origin/main"
    )
    monkeypatch.setattr(precommit, "is_ancestor", lambda *_a, **_kw: True)
    assert precommit._tag_only_on_base(tmp_path, "v1.0.0", "main") is False


def test_step_changelog_updated_env_escape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SKIP_CHANGELOG_CHECK=1 skips the gate with the no-version opt-out wording."""
    (tmp_path / "CHANGELOG.md").write_text("## v1.0.0\n")
    monkeypatch.delenv("NO_VERSION", raising=False)
    monkeypatch.setenv("SKIP_CHANGELOG_CHECK", "1")
    monkeypatch.setattr(
        precommit, "resolve_current_branch", _fake_resolve_current_branch("feat/x")
    )
    result = precommit.step_changelog_updated(tmp_path)
    assert result.skipped
    assert "no-version opt-out" in result.output
    assert "SKIP_CHANGELOG_CHECK" in result.output


def test_step_changelog_updated_skips_on_base_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Per-PR guard — on the base branch it self-skips."""
    (tmp_path / "CHANGELOG.md").write_text("## v1.0.0\n")
    monkeypatch.setattr(
        precommit, "resolve_current_branch", _fake_resolve_current_branch("main")
    )
    result = precommit.step_changelog_updated(tmp_path)
    assert result.skipped


def test_step_changelog_updated_fails_without_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Code changed, CHANGELOG untouched → fail with the opt-out hint."""
    (tmp_path / "CHANGELOG.md").write_text("## v1.0.0\n")
    monkeypatch.delenv("NO_VERSION", raising=False)
    monkeypatch.delenv("SKIP_CHANGELOG_CHECK", raising=False)
    monkeypatch.delenv("GITHUB_HEAD_REF", raising=False)
    monkeypatch.setattr(
        precommit, "resolve_current_branch", _fake_resolve_current_branch("feat/x")
    )
    monkeypatch.setattr(
        precommit.config, "select_diff_files", lambda *_a, **_kw: ["src/pkg/mod.py"]
    )
    result = precommit.step_changelog_updated(tmp_path)
    assert not result.passed
    assert "NO_VERSION=1" in result.output
    assert "[no-version]" in result.output


def test_step_changelog_updated_passes_with_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Change set includes CHANGELOG.md → pass."""
    (tmp_path / "CHANGELOG.md").write_text("## v1.0.0\n")
    monkeypatch.delenv("NO_VERSION", raising=False)
    monkeypatch.delenv("SKIP_CHANGELOG_CHECK", raising=False)
    monkeypatch.delenv("GITHUB_HEAD_REF", raising=False)
    monkeypatch.setattr(
        precommit, "resolve_current_branch", _fake_resolve_current_branch("feat/x")
    )
    monkeypatch.setattr(
        precommit.config,
        "select_diff_files",
        lambda *_a, **_kw: ["src/pkg/mod.py", "CHANGELOG.md"],
    )
    result = precommit.step_changelog_updated(tmp_path)
    assert result.passed


def test_step_changelog_updated_honors_exempt_and_require_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """exempt_paths silences a subtree; require_paths re-includes inside it."""
    (tmp_path / "CHANGELOG.md").write_text("## v1.0.0\n")
    (tmp_path / "pyproject.toml").write_text(
        "[tool.forge.changelog]\n"
        'exempt_paths = ["projects/"]\n'
        'require_paths = ["projects/shipped/"]\n'
    )
    monkeypatch.delenv("NO_VERSION", raising=False)
    monkeypatch.delenv("SKIP_CHANGELOG_CHECK", raising=False)
    monkeypatch.delenv("GITHUB_HEAD_REF", raising=False)
    monkeypatch.setattr(
        precommit, "resolve_current_branch", _fake_resolve_current_branch("feat/x")
    )
    monkeypatch.setattr(
        precommit.config,
        "select_diff_files",
        lambda *_a, **_kw: ["projects/scratch/x.py"],
    )
    assert precommit.step_changelog_updated(tmp_path).passed
    monkeypatch.setattr(
        precommit.config,
        "select_diff_files",
        lambda *_a, **_kw: ["projects/shipped/x.py"],
    )
    assert not precommit.step_changelog_updated(tmp_path).passed


def test_step_changelog_updated_skips_with_branch_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `no-version` branch token skips the gate; output names the signal.

    SCENARIO: wants_no_version fires on a branch-token signal.
    MOCK SETUP: precommit.wants_no_version → a canned branch-token signal,
    isolating the step's dispatch from wants_no_version's own git-based
    detection (covered directly with real repos in tests/test_changelog.py).
    No select_diff_files mock — the step returns on the signal before
    ever calling it.
    EXPECTED BEHAVIOR: the step short-circuits skipped, and the signal's
    branch name surfaces verbatim in the output.
    """
    (tmp_path / "CHANGELOG.md").write_text("## v1.0.0\n")
    monkeypatch.setattr(
        precommit,
        "resolve_current_branch",
        _fake_resolve_current_branch("chore/x-no-version"),
    )
    monkeypatch.setattr(
        precommit,
        "wants_no_version",
        lambda _r: "`no-version` token in branch name 'chore/x-no-version'",
    )
    result = precommit.step_changelog_updated(tmp_path)
    assert result.skipped
    assert "chore/x-no-version" in result.output


def test_step_changelog_updated_skips_with_commit_tag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `[no-version]` commit tag skips the gate; output names the signal.

    SCENARIO: wants_no_version fires on a commit-tag signal.
    MOCK SETUP: precommit.wants_no_version → a canned commit-tag signal,
    same isolation rationale as the branch-token test above. No
    select_diff_files mock — the step returns on the signal before ever
    calling it.
    EXPECTED BEHAVIOR: the step short-circuits skipped, and the signal's
    tag text surfaces verbatim in the output.
    """
    (tmp_path / "CHANGELOG.md").write_text("## v1.0.0\n")
    monkeypatch.setattr(
        precommit, "resolve_current_branch", _fake_resolve_current_branch("feat/x")
    )
    monkeypatch.setattr(
        precommit,
        "wants_no_version",
        lambda _r: "[no-version] tag in a commit message (main..HEAD)",
    )
    result = precommit.step_changelog_updated(tmp_path)
    assert result.skipped
    assert "[no-version]" in result.output


def test_step_changelog_updated_no_signal_gate_still_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No no-version signal fired → the gate stays intact and still fails.

    SCENARIO: wants_no_version reports no signal, so the gate must still
    evaluate the diff and fail on an untouched CHANGELOG.md.
    MOCK SETUP: precommit.wants_no_version → None (no opt-out); this is
    the one sibling where select_diff_files is load-bearing — with no
    signal, the step falls through to it to build the trigger set.
    EXPECTED BEHAVIOR: the step does not skip and fails the gate.
    """
    (tmp_path / "CHANGELOG.md").write_text("## v1.0.0\n")
    monkeypatch.setattr(
        precommit, "resolve_current_branch", _fake_resolve_current_branch("feat/x")
    )
    monkeypatch.setattr(precommit, "wants_no_version", lambda _r: None)
    monkeypatch.setattr(
        precommit.config, "select_diff_files", lambda *_a, **_kw: ["src/pkg/mod.py"]
    )
    result = precommit.step_changelog_updated(tmp_path)
    assert not result.passed
    assert not result.skipped


def test_step_changelog_updated_deferred_mode_skips_local(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deferred mode self-skips a local (non-CI) run regardless of the diff."""
    (tmp_path / "CHANGELOG.md").write_text("## v1.0.0\n")
    (tmp_path / "pyproject.toml").write_text(
        "[tool.forge.changelog]\nprecommit_enforce = false\n"
    )
    monkeypatch.setattr(
        precommit, "resolve_current_branch", _fake_resolve_current_branch("feat/x")
    )
    monkeypatch.setattr(precommit, "wants_no_version", lambda _r: None)
    monkeypatch.setattr(precommit, "is_ci", lambda: False)
    monkeypatch.setattr(
        precommit.config, "select_diff_files", lambda *_a, **_kw: ["src/pkg/mod.py"]
    )
    result = precommit.step_changelog_updated(tmp_path)
    assert result.skipped is True
    assert "Deferred mode" in result.output
    assert "at PR wrap-up" in result.output


def test_step_changelog_updated_deferred_mode_ci_fails_with_deferred_tone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deferred mode still fails on genuine CI, with deferred-tone wording.

    SCENARIO: ``precommit_enforce = false`` defers the gate locally, but
    CI must keep failing until the entry lands at PR wrap-up.
    MOCK SETUP: ``is_ci`` → True (genuine CI, not just non-interactive);
    ``wants_no_version`` → None (no opt-out); ``select_diff_files`` →
    a src change with no matching CHANGELOG.md entry.
    EXPECTED BEHAVIOR: the gate fails (not skipped), the output uses the
    deferred-mode wording ("deferred mode" / "expected") rather than the
    enforce-mode opt-out hint ("NO_VERSION=1"), and stays blocking by
    default.
    """
    (tmp_path / "CHANGELOG.md").write_text("## v1.0.0\n")
    (tmp_path / "pyproject.toml").write_text(
        "[tool.forge.changelog]\nprecommit_enforce = false\n"
    )
    monkeypatch.setattr(
        precommit, "resolve_current_branch", _fake_resolve_current_branch("feat/x")
    )
    monkeypatch.setattr(precommit, "wants_no_version", lambda _r: None)
    monkeypatch.setattr(precommit, "is_ci", lambda: True)
    monkeypatch.setattr(
        precommit.config, "select_diff_files", lambda *_a, **_kw: ["src/pkg/mod.py"]
    )
    result = precommit.step_changelog_updated(tmp_path)
    assert result.passed is False
    assert result.skipped is False
    assert "deferred mode" in result.output
    assert "expected" in result.output
    assert "NO_VERSION=1" not in result.output
    assert result.non_blocking is False


def test_step_changelog_updated_deferred_mode_ci_nonblocking_when_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deferred-mode CI failure is non-blocking when ``blocking = false``."""
    (tmp_path / "CHANGELOG.md").write_text("## v1.0.0\n")
    (tmp_path / "pyproject.toml").write_text(
        "[tool.forge.changelog]\nprecommit_enforce = false\nblocking = false\n"
    )
    monkeypatch.setattr(
        precommit, "resolve_current_branch", _fake_resolve_current_branch("feat/x")
    )
    monkeypatch.setattr(precommit, "wants_no_version", lambda _r: None)
    monkeypatch.setattr(precommit, "is_ci", lambda: True)
    monkeypatch.setattr(
        precommit.config, "select_diff_files", lambda *_a, **_kw: ["src/pkg/mod.py"]
    )
    result = precommit.step_changelog_updated(tmp_path)
    assert result.passed is False
    assert result.non_blocking is True


def test_step_changelog_updated_deferred_warns_when_blocking_also_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both keys false → the skip and the CI WARN both carry the caveat.

    Deferred mode's guarantee is CI staying red until the entry lands;
    blocking=false degrades that to a WARN, so the combination must be
    surfaced rather than silently voiding the guarantee.
    """
    (tmp_path / "CHANGELOG.md").write_text("## v1.0.0\n")
    (tmp_path / "pyproject.toml").write_text(
        "[tool.forge.changelog]\nprecommit_enforce = false\nblocking = false\n"
    )
    monkeypatch.setattr(
        precommit, "resolve_current_branch", _fake_resolve_current_branch("feat/x")
    )
    monkeypatch.setattr(
        precommit.config, "select_diff_files", lambda *_a, **_kw: ["src/pkg/mod.py"]
    )
    monkeypatch.setattr(precommit, "is_ci", lambda: False)
    local = precommit.step_changelog_updated(tmp_path)
    assert local.skipped
    assert "does NOT hold" in local.output
    monkeypatch.setattr(precommit, "is_ci", lambda: True)
    ci = precommit.step_changelog_updated(tmp_path)
    assert not ci.passed
    assert ci.non_blocking
    assert "does NOT hold" in ci.output


def test_step_changelog_updated_deferred_no_warning_when_blocking_true(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deferred mode with default blocking carries no guarantee caveat."""
    (tmp_path / "CHANGELOG.md").write_text("## v1.0.0\n")
    (tmp_path / "pyproject.toml").write_text(
        "[tool.forge.changelog]\nprecommit_enforce = false\n"
    )
    monkeypatch.setattr(
        precommit, "resolve_current_branch", _fake_resolve_current_branch("feat/x")
    )
    monkeypatch.setattr(
        precommit.config, "select_diff_files", lambda *_a, **_kw: ["src/pkg/mod.py"]
    )
    monkeypatch.setattr(precommit, "is_ci", lambda: False)
    assert "does NOT hold" not in precommit.step_changelog_updated(tmp_path).output


def test_step_changelog_updated_no_version_optout_wins_over_deferred(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The no-version opt-out is checked before the deferred-mode branch."""
    (tmp_path / "CHANGELOG.md").write_text("## v1.0.0\n")
    (tmp_path / "pyproject.toml").write_text(
        "[tool.forge.changelog]\nprecommit_enforce = false\n"
    )
    monkeypatch.setattr(
        precommit, "resolve_current_branch", _fake_resolve_current_branch("feat/x")
    )
    monkeypatch.setattr(
        precommit,
        "wants_no_version",
        lambda _r: "[no-version] tag in a commit message (main..HEAD)",
    )
    monkeypatch.setattr(precommit, "is_ci", lambda: True)
    result = precommit.step_changelog_updated(tmp_path)
    assert result.skipped is True
    assert "no-version opt-out" in result.output


def test_step_changelog_updated_deferred_mode_gated_on_is_ci_not_non_interactive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deferred mode's local/CI split is gated on ``is_ci()``, not non-interactivity.

    SCENARIO: an agent-driven local run has a non-tty stdin but is not
    genuine CI, so the deferred skip must key off ``is_ci()`` alone —
    the same distinction ``test_step_changelog_version_fetches_tags_in_every_context``
    pins for the tag-refresh gate.
    MOCK SETUP: ``precommit.is_non_interactive`` is monkeypatched to raise
    if called at all, so any accidental read of it fails the test loudly
    instead of silently passing on either backend.
    EXPECTED BEHAVIOR: ``is_ci() == False`` skips (deferred, local);
    ``is_ci() == True`` runs the gate and fails on the missing entry.
    """
    (tmp_path / "CHANGELOG.md").write_text("## v1.0.0\n")
    (tmp_path / "pyproject.toml").write_text(
        "[tool.forge.changelog]\nprecommit_enforce = false\n"
    )
    monkeypatch.setattr(
        precommit, "resolve_current_branch", _fake_resolve_current_branch("feat/x")
    )
    monkeypatch.setattr(precommit, "wants_no_version", lambda _r: None)
    monkeypatch.setattr(
        precommit.config, "select_diff_files", lambda *_a, **_kw: ["src/pkg/mod.py"]
    )

    def _unexpected_is_non_interactive() -> bool:
        msg = "must not be called"
        raise AssertionError(msg)

    monkeypatch.setattr(precommit, "is_non_interactive", _unexpected_is_non_interactive)

    monkeypatch.setattr(precommit, "is_ci", lambda: False)
    result = precommit.step_changelog_updated(tmp_path)
    assert result.skipped is True

    monkeypatch.setattr(precommit, "is_ci", lambda: True)
    result = precommit.step_changelog_updated(tmp_path)
    assert result.skipped is False
    assert result.passed is False


def _repo_with_undocumented_src_change(tmp_path: Path) -> Path:
    """Build a repo with a real, undocumented src change on a feature branch.

    ``main`` carries a committed ``CHANGELOG.md``; a ``feat/x`` branch adds
    a real source file without touching the changelog, then HEAD is
    detached at that commit — the CI ``pull_request`` checkout shape the
    detached-HEAD ``step_changelog_updated`` tests below share.

    Returns:
        The work-tree path, HEAD detached on the feature-branch commit.
    """
    work, _bare = init_single_track_repo(tmp_path)
    (work / "CHANGELOG.md").write_text("## v1.0.0\n")
    subprocess.run(["git", "add", "CHANGELOG.md"], cwd=work, env=GIT_ENV, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "chore: add changelog"],
        cwd=work,
        env=GIT_ENV,
        check=True,
    )
    subprocess.run(
        ["git", "checkout", "-q", "-b", "feat/x"], cwd=work, env=GIT_ENV, check=True
    )
    (work / "src_change.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "src_change.py"], cwd=work, env=GIT_ENV, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "feat: add thing"],
        cwd=work,
        env=GIT_ENV,
        check=True,
    )
    _detach_head(work)
    return work


def test_step_changelog_updated_runs_gate_on_detached_head_with_no_version_head_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A detached HEAD's `GITHUB_HEAD_REF` carrying a no-version token skips the gate.

    CI's `pull_request` checkout of `refs/pull/N/merge` detaches HEAD, so
    `git branch --show-current` is empty; `GITHUB_HEAD_REF` stands in for
    the PR source branch, and its `no-version` token fires the same
    opt-out a local checkout of that branch would.
    """
    monkeypatch.delenv("NO_VERSION", raising=False)
    monkeypatch.delenv("SKIP_CHANGELOG_CHECK", raising=False)
    monkeypatch.delenv("GITHUB_HEAD_REF", raising=False)
    work = _repo_with_undocumented_src_change(tmp_path)
    monkeypatch.setenv("GITHUB_HEAD_REF", "chore/x-no-version")
    result = precommit.step_changelog_updated(work)
    assert result.skipped is True
    assert "GITHUB_HEAD_REF" in result.output
    assert "chore/x-no-version" in result.output


def test_step_changelog_updated_runs_gate_on_detached_head_without_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Detached HEAD's `GITHUB_HEAD_REF` without no-version token runs the gate.

    No opt-out signal fires, so the step falls through to the real diff
    against the previous commit — which shows the feature branch's src
    change without a matching `CHANGELOG.md` entry — and the gate fails
    exactly as it would for a live local branch.
    """
    monkeypatch.delenv("NO_VERSION", raising=False)
    monkeypatch.delenv("SKIP_CHANGELOG_CHECK", raising=False)
    monkeypatch.delenv("GITHUB_HEAD_REF", raising=False)
    work = _repo_with_undocumented_src_change(tmp_path)
    monkeypatch.setenv("GITHUB_HEAD_REF", "chore/plain")
    result = precommit.step_changelog_updated(work)
    assert result.skipped is False
    assert result.passed is False


def test_step_changelog_updated_skips_detached_head_with_no_head_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A detached HEAD with no `GITHUB_HEAD_REF` at all self-skips as a per-PR guard.

    Neither `git branch --show-current` nor the `GITHUB_HEAD_REF`
    fallback yields a branch name, so the step cannot tell which PR this
    diff belongs to and skips rather than risk a false failure.
    """
    monkeypatch.delenv("NO_VERSION", raising=False)
    monkeypatch.delenv("SKIP_CHANGELOG_CHECK", raising=False)
    monkeypatch.delenv("GITHUB_HEAD_REF", raising=False)
    work = _repo_with_undocumented_src_change(tmp_path)
    result = precommit.step_changelog_updated(work)
    assert result.skipped is True
    assert "detached HEAD" in result.output


def test_step_changelog_version_fetches_tags_in_every_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tags are refreshed best-effort regardless of `is_ci()`.

    SCENARIO: a CI `pull_request` checkout may start with no tags at all,
    so the refresh must not be gated on `is_ci()` — it runs whether the
    caller is a human, an agent, or genuine CI.
    MOCK SETUP: `git_utils.subprocess.run` recorded (the fetch is a direct
    bounded call inside `fetch_tags_best_effort`, which `precommit` imports
    by name); run_git faked for the `show` old-side read; resolve_current_branch
    faked so the branch resolution does not depend on the real (non-git)
    `tmp_path`. `merge_base_with_head` is left real — against a non-git
    `tmp_path` it resolves no base ref and short-circuits to `""`, so no
    stranded-entries diff is attempted.
    EXPECTED BEHAVIOR: fetch argv appears whether is_ci() is False or True.
    """
    (tmp_path / "CHANGELOG.md").write_text("## v1.0.0\n")
    monkeypatch.setattr(precommit, "latest_v_tag", lambda _r: "v1.0.0")
    monkeypatch.setattr(
        precommit, "resolve_current_branch", _fake_resolve_current_branch("feat/x")
    )
    monkeypatch.setattr(precommit, "run_git", _fake_run_git_dispatch())
    fetches: list[list[str]] = []

    def _fake_subprocess_run(cmd: list[str], **_kw: object) -> object:
        fetches.append(list(cmd))
        return type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(git_utils.subprocess, "run", _fake_subprocess_run)
    monkeypatch.setattr(precommit, "is_ci", lambda: False)
    assert precommit.step_changelog_version(tmp_path).passed
    assert ["git", "fetch", "--tags", "--quiet", "origin"] in fetches

    fetches.clear()
    monkeypatch.setattr(precommit, "is_ci", lambda: True)
    assert precommit.step_changelog_version(tmp_path).passed
    assert ["git", "fetch", "--tags", "--quiet", "origin"] in fetches


def _setup_tagged_repo_stranded_detached(base: Path) -> Path:
    """Build a tagged single-track repo with a stranded bullet, HEAD detached.

    Mirrors a CI ``pull_request`` checkout of ``refs/pull/N/merge``:
    ``main`` carries a tagged ``CHANGELOG.md``; ``feat/x`` adds a bullet
    under the already-tagged heading (the stranded shape) and HEAD is then
    detached at that commit, so ``git branch --show-current`` is empty and
    the ``GITHUB_HEAD_REF`` fallback must carry the branch name instead.

    Args:
        base: Base directory for the test repo.

    Returns:
        Path to the work repository, HEAD detached on the feature-branch
        commit.
    """
    work, _bare = init_single_track_repo(base)
    (work / "CHANGELOG.md").write_text("## v1.0.0\n")
    subprocess.run(["git", "add", "CHANGELOG.md"], cwd=work, env=GIT_ENV, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "chore: add changelog"],
        cwd=work,
        env=GIT_ENV,
        check=True,
    )
    subprocess.run(
        ["git", "push", "-q", "origin", "main"], cwd=work, env=GIT_ENV, check=True
    )
    subprocess.run(
        ["git", "tag", "-a", "v1.0.0", "-m", "v1.0.0"],
        cwd=work,
        env=GIT_ENV,
        check=True,
    )
    subprocess.run(
        ["git", "push", "-q", "origin", "--tags"], cwd=work, env=GIT_ENV, check=True
    )
    subprocess.run(
        ["git", "checkout", "-q", "-b", "feat/x"], cwd=work, env=GIT_ENV, check=True
    )
    (work / "CHANGELOG.md").write_text("## v1.0.0\n\n- new bullet\n")
    subprocess.run(["git", "add", "CHANGELOG.md"], cwd=work, env=GIT_ENV, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "docs: feat/x changelog"],
        cwd=work,
        env=GIT_ENV,
        check=True,
    )
    _detach_head(work)
    return work


def _setup_tagged_repo_deleted_detached(base: Path) -> Path:
    """Build a tagged single-track repo with a deleted released bullet, HEAD detached.

    Mirrors a CI ``pull_request`` checkout of ``refs/pull/N/merge``:
    ``main`` carries a tagged ``CHANGELOG.md`` with a bullet under the
    released heading; ``feat/x`` removes that bullet (the deleted-history
    shape) and HEAD is then detached at that commit, so ``git branch
    --show-current`` is empty and the ``GITHUB_HEAD_REF`` fallback must
    carry the branch name instead.

    Args:
        base: Base directory for the test repo.

    Returns:
        Path to the work repository, HEAD detached on the feature-branch
        commit.
    """
    work, _bare = init_single_track_repo(base)
    (work / "CHANGELOG.md").write_text("## v1.0.0\n\n- original bullet\n")
    subprocess.run(["git", "add", "CHANGELOG.md"], cwd=work, env=GIT_ENV, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "chore: add changelog"],
        cwd=work,
        env=GIT_ENV,
        check=True,
    )
    subprocess.run(
        ["git", "push", "-q", "origin", "main"], cwd=work, env=GIT_ENV, check=True
    )
    subprocess.run(
        ["git", "tag", "-a", "v1.0.0", "-m", "v1.0.0"],
        cwd=work,
        env=GIT_ENV,
        check=True,
    )
    subprocess.run(
        ["git", "push", "-q", "origin", "--tags"], cwd=work, env=GIT_ENV, check=True
    )
    subprocess.run(
        ["git", "checkout", "-q", "-b", "feat/x"], cwd=work, env=GIT_ENV, check=True
    )
    (work / "CHANGELOG.md").write_text("## v1.0.0\n")
    subprocess.run(["git", "add", "CHANGELOG.md"], cwd=work, env=GIT_ENV, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "docs: drop released bullet"],
        cwd=work,
        env=GIT_ENV,
        check=True,
    )
    _detach_head(work)
    return work


def test_step_changelog_version_detects_deleted_entry_on_detached_pr_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deleted-entries check stays live on a detached CI `pull_request` checkout.

    SCENARIO: HEAD is detached (as in a CI `refs/pull/N/merge` checkout),
    so `git branch --show-current` is empty; branch resolution must fall
    back to `GITHUB_HEAD_REF` to keep the deleted-entries check live
    rather than silently skipping it (the `current == ""` short-circuit
    that would otherwise hide the finding). Real git —
    `resolve_current_branch` is not mocked here; the `GITHUB_HEAD_REF`
    fallback wiring is the thing under test.
    """
    work = _setup_tagged_repo_deleted_detached(tmp_path)
    monkeypatch.setenv("GITHUB_HEAD_REF", "feat/x")
    result = precommit.step_changelog_version(work)
    assert not result.passed
    assert "deleted" in result.output


def test_step_changelog_version_detects_stranded_entries_on_detached_pr_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stranded-entries check stays live on a detached CI `pull_request` checkout.

    SCENARIO: HEAD is detached (as in a CI `refs/pull/N/merge` checkout),
    so `git branch --show-current` is empty; branch resolution must fall
    back to `GITHUB_HEAD_REF` to keep the stranded-entries check live
    rather than silently skipping it (the `current == ""` short-circuit
    that would otherwise hide the finding). Real git —
    `resolve_current_branch` is not mocked here; the `GITHUB_HEAD_REF`
    fallback wiring is the thing under test.
    """
    work = _setup_tagged_repo_stranded_detached(tmp_path)
    monkeypatch.setenv("GITHUB_HEAD_REF", "feat/x")
    result = precommit.step_changelog_version(work)
    assert not result.passed
    assert "stranded" in result.output


def test_step_changelog_version_fetch_failure_falls_back_to_local_tags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed `git fetch --tags` degrades to local tags with a visible note.

    MOCK SETUP: `git_utils.subprocess.run` faked to report `returncode=1`
    for the best-effort tag refresh (the fetch is a direct bounded call
    inside `fetch_tags_best_effort`, which `precommit` imports by name);
    `run_git` / `resolve_current_branch` faked so the rest of the step
    reads a consistent, tag-having changelog.
    EXPECTED BEHAVIOR: the step still passes (local tags are stale but
    present) and the output carries the degradation note.
    """
    (tmp_path / "CHANGELOG.md").write_text("## v1.1.0\n\n- a\n\n## v1.0.0\n")
    monkeypatch.setattr(precommit, "latest_v_tag", lambda _r: "v1.0.0")
    monkeypatch.setattr(
        precommit, "resolve_current_branch", _fake_resolve_current_branch("feat/x")
    )
    monkeypatch.setattr(precommit, "run_git", _fake_run_git_dispatch())

    def _fake_subprocess_run(*_a: object, **_kw: object) -> object:
        return type("P", (), {"returncode": 1, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(git_utils.subprocess, "run", _fake_subprocess_run)
    result = precommit.step_changelog_version(tmp_path)
    assert result.passed is True
    assert "validating against local tags, which may be stale." in result.output


def test_step_changelog_version_no_tag_visible_warns_structural_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No `v*` tag visible after the refresh → structural-only note, not a failure.

    MOCK SETUP: `latest_v_tag` faked to `None` (no tag visible, e.g. a
    fresh CI checkout with no tags fetched); `git_utils.subprocess.run`
    faked to report a successful (`returncode=0`) refresh (the fetch is
    a direct bounded call inside `fetch_tags_best_effort`, which
    `precommit` imports by name) so the "no tag visible" note is
    attributable to `latest_v_tag`, not a fetch failure.
    EXPECTED BEHAVIOR: the step passes (no tag-relative check can fire)
    and the output names the missing reference tag.
    """
    (tmp_path / "CHANGELOG.md").write_text("## v1.0.0\n")
    monkeypatch.setattr(precommit, "latest_v_tag", lambda _r: None)
    monkeypatch.setattr(
        precommit, "resolve_current_branch", _fake_resolve_current_branch("feat/x")
    )
    monkeypatch.setattr(precommit, "run_git", _fake_run_git_dispatch())

    def _fake_subprocess_run(*_a: object, **_kw: object) -> object:
        return type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(git_utils.subprocess, "run", _fake_subprocess_run)
    result = precommit.step_changelog_version(tmp_path)
    assert result.passed is True
    assert "no `v*` tag visible" in result.output


# ---------------------------------------------------------------------------
# step_changelog_version / step_changelog_updated — fragments mode
# ---------------------------------------------------------------------------
#
# Shared fixture recipe: CHANGELOG.md present (so the steps don't self-skip),
# no `.claude-plugin/plugin.json` (not manifest-versioned), `pyproject.toml`
# opting into `[tool.forge.changelog] mode = "fragments"`.


def _write_fragments_pyproject(tmp_path: Path) -> None:
    """Write `pyproject.toml` opting into fragments mode."""
    (tmp_path / "pyproject.toml").write_text(
        '[tool.forge.changelog]\nmode = "fragments"\n'
    )


def _write_pending_fragment(tmp_path: Path, name: str, body: str) -> None:
    """Write a `changelog.d/<name>` pending fragment with *body*.

    Args:
        tmp_path: Repo root.
        name: Fragment filename.
        body: Fragment file contents, written verbatim.
    """
    directory = tmp_path / "changelog.d"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(body)


def test_step_changelog_version_fragments_mode_valid_pending_passes(
    tmp_path: Path,
) -> None:
    """Fragment mode with a valid pending fragment passes, naming "Fragment mode"."""
    (tmp_path / "CHANGELOG.md").write_text("# Changelog\n")
    _write_fragments_pyproject(tmp_path)
    _write_pending_fragment(tmp_path, "a.added.md", "bump: minor\n- x\n")
    result = precommit.step_changelog_version(tmp_path)
    assert result.passed
    assert "Fragment mode" in result.output


def test_step_changelog_version_fragments_mode_invalid_pending_fails(
    tmp_path: Path,
) -> None:
    """Fragment mode with an invalid pending fragment fails, blocking by default."""
    (tmp_path / "CHANGELOG.md").write_text("# Changelog\n")
    _write_fragments_pyproject(tmp_path)
    _write_pending_fragment(tmp_path, "a.bogus.md", "bump: minor\n- x\n")
    result = precommit.step_changelog_version(tmp_path)
    assert not result.passed
    assert not result.non_blocking
    assert "unknown type 'bogus'" in result.output


def test_step_changelog_version_manifest_and_fragments_invalid_fragment_fails(
    tmp_path: Path,
) -> None:
    """A manifest repo in fragments mode still fails on an invalid fragment.

    The fragments-mode gate must be judged BEFORE the
    ``.claude-plugin/plugin.json`` short-circuit: a repo carrying both a
    manifest and fragments needs the fragment gate's per-branch checks
    (PR hygiene) to run regardless of who owns the declared-version
    invariant. Before the fix, the manifest short-circuit fired first and
    this repo passed+skipped with the "Manifest-versioned repo" message
    instead of failing on the invalid fragment.
    """
    (tmp_path / "CHANGELOG.md").write_text("# Changelog\n")
    _write_fragments_pyproject(tmp_path)
    (tmp_path / ".claude-plugin").mkdir()
    (tmp_path / ".claude-plugin" / "plugin.json").write_text("{}")
    _write_pending_fragment(tmp_path, "a.bogus.md", "bump: minor\n- x\n")
    result = precommit.step_changelog_version(tmp_path)
    assert not result.passed
    assert not result.skipped
    assert "unknown type 'bogus'" in result.output


def test_step_changelog_version_fragments_mode_no_pending_passes(
    tmp_path: Path,
) -> None:
    """Fragment mode with no `changelog.d/` at all passes cleanly."""
    (tmp_path / "CHANGELOG.md").write_text("# Changelog\n")
    _write_fragments_pyproject(tmp_path)
    result = precommit.step_changelog_version(tmp_path)
    assert result.passed
    assert "Fragment mode" in result.output


def _init_fragments_mode_repo(base: Path) -> Path:
    """Build a single-track fragments-mode repo, checked out on ``feat/x``.

    Real-git counterpart to the synthetic fixtures above: a
    ``base_branch = "main"`` + fragments-mode ``pyproject.toml`` and a
    ``CHANGELOG.md`` are committed and pushed to ``origin/main`` before
    branching, so ``branch_added_fragments`` has a real fork point to
    diff against. Callers add their own fragment(s) on ``feat/x``.

    Args:
        base: Base directory for the test repo.

    Returns:
        Path to the work repository, checked out on ``feat/x``.
    """
    work, _bare = init_single_track_repo(base)
    (work / "pyproject.toml").write_text(
        '[tool.forge]\nbase_branch = "main"\n\n'
        '[tool.forge.changelog]\nmode = "fragments"\n'
    )
    (work / "CHANGELOG.md").write_text("# Changelog\n")
    commit_all(work, "chore: fragments mode setup")
    subprocess.run(
        ["git", "push", "-q", "origin", "main"], cwd=work, env=GIT_ENV, check=True
    )
    subprocess.run(
        ["git", "checkout", "-q", "-b", "feat/x"], cwd=work, env=GIT_ENV, check=True
    )
    return work


def test_fragment_gate_blocks_second_branch_added_fragment(tmp_path: Path) -> None:
    """Two fragments added on one branch trip the one-fragment-per-PR gate.

    Cited by ``docs/release-process.md``'s invariant table as the
    enforcing test for "a branch adds at most ONE fragment" — do not
    rename without updating that reference.
    """
    work = _init_fragments_mode_repo(tmp_path)
    (work / "changelog.d").mkdir()
    (work / "changelog.d" / "a.added.md").write_text("bump: minor\n- a\n")
    (work / "changelog.d" / "b.added.md").write_text("bump: patch\n- b\n")
    commit_all(work, "feat: two fragments")
    result = precommit.step_changelog_version(work)
    assert not result.passed
    assert "a PR carries ONE fragment" in result.output


def test_fragment_gate_message_names_stacked_pr_remedy(tmp_path: Path) -> None:
    """The two-fragments message points at the stacked-PR remedy, not a merge.

    Guards the #473 wording fix: a naive "merge the bullets" remedy once
    led the fixer to propose deleting a parent PR's own fragment on a
    stacked branch. The message must name that scenario explicitly.
    """
    work = _init_fragments_mode_repo(tmp_path)
    (work / "changelog.d").mkdir()
    (work / "changelog.d" / "a.added.md").write_text("bump: minor\n- a\n")
    (work / "changelog.d" / "b.added.md").write_text("bump: patch\n- b\n")
    commit_all(work, "feat: two fragments")
    result = precommit.step_changelog_version(work)
    assert not result.passed
    assert "stacked" in result.output


def test_fragment_gate_passes_single_branch_added_fragment(tmp_path: Path) -> None:
    """A single fragment added on the branch passes the fragment gate."""
    work = _init_fragments_mode_repo(tmp_path)
    (work / "changelog.d").mkdir()
    (work / "changelog.d" / "a.added.md").write_text("bump: minor\n- a\n")
    commit_all(work, "feat: one fragment")
    result = precommit.step_changelog_version(work)
    assert result.passed


def _changelog_updated_on_branch_with_source_change(
    work: Path, monkeypatch: pytest.MonkeyPatch
) -> precommit.StepResult:
    """Commit a triggering source change on ``feat/x`` and run the step.

    Args:
        work: Fragments-mode repo from ``_init_fragments_mode_repo``.
        monkeypatch: Clears the no-version opt-out environment.

    Returns:
        The ``changelog_updated`` step result.
    """
    for var in ("NO_VERSION", "SKIP_CHANGELOG_CHECK", "GITHUB_HEAD_REF"):
        monkeypatch.delenv(var, raising=False)
    (work / "src" / "pkg").mkdir(parents=True)
    (work / "src" / "pkg" / "mod.py").write_text("X = 1\n")
    commit_all(work, "feat: source change")
    return precommit.step_changelog_updated(work)


def test_changelog_updated_untracked_valid_fragment_not_counted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BEHAVIOR: a fragment written but never added does not satisfy the gate.

    It is not in the commit the gate guards, so the gate fails and names
    the fragment as not staged rather than counting it.
    """
    work = _init_fragments_mode_repo(tmp_path)
    _changelog_updated_on_branch_with_source_change(work, monkeypatch)
    _write_pending_fragment(work, "a.added.md", "bump: minor\n- x\n")
    result = precommit.step_changelog_updated(work)
    assert not result.passed
    assert "require a changelog fragment" in result.output
    assert "Not staged, so not counted: `changelog.d/a.added.md`" in result.output


def test_changelog_updated_unstaged_intent_to_add_fragment_not_counted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BEHAVIOR: a fragment known to git but with nothing staged does not count.

    ``git add -N`` records the path without its content: the file is no
    longer untracked, yet the commit would still carry no fragment.
    """
    work = _init_fragments_mode_repo(tmp_path)
    _changelog_updated_on_branch_with_source_change(work, monkeypatch)
    _write_pending_fragment(work, "a.added.md", "bump: minor\n- x\n")
    git_stdout(work, "add", "-N", "changelog.d/a.added.md")
    result = precommit.step_changelog_updated(work)
    assert not result.passed
    assert "Not staged, so not counted: `changelog.d/a.added.md`" in result.output


def test_changelog_updated_staged_valid_fragment_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BEHAVIOR: a staged, not yet committed fragment satisfies the gate."""
    work = _init_fragments_mode_repo(tmp_path)
    _changelog_updated_on_branch_with_source_change(work, monkeypatch)
    _write_pending_fragment(work, "a.added.md", "bump: minor\n- x\n")
    git_stdout(work, "add", "changelog.d/a.added.md")
    result = precommit.step_changelog_updated(work)
    assert result.passed


def test_changelog_updated_validates_the_staged_copy_not_the_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BEHAVIOR: a placeholder staged, then fixed only on disk, still fails.

    The commit ships the index, so the staged copy is what must be valid;
    a disk-only check would pass while the placeholder lands.
    """
    work = _init_fragments_mode_repo(tmp_path)
    _changelog_updated_on_branch_with_source_change(work, monkeypatch)
    _write_pending_fragment(work, "a.added.md", "placeholder\n")
    git_stdout(work, "add", "changelog.d/a.added.md")
    _write_pending_fragment(work, "a.added.md", "bump: minor\n- x\n")
    result = precommit.step_changelog_updated(work)
    assert not result.passed
    assert "first line must be 'bump: patch|minor|major' (staged copy)" in (
        result.output
    )


def test_changelog_updated_untracked_gitignored_fragment_not_counted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BEHAVIOR: gitignored fragments are out of scope; gate fails."""
    work = _init_fragments_mode_repo(tmp_path)
    (work / ".gitignore").write_text("changelog.d/ignored/\n")
    commit_all(work, "chore: ignore")
    (work / "changelog.d" / "ignored").mkdir(parents=True)
    (work / "changelog.d" / "ignored" / "a.added.md").write_text("bump: minor\n- x\n")
    result = _changelog_updated_on_branch_with_source_change(work, monkeypatch)
    assert not result.passed
    assert "require a changelog fragment" in result.output


def test_changelog_updated_staged_invalid_fragment_still_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BEHAVIOR: presence of a staged fragment does not waive validation."""
    work = _init_fragments_mode_repo(tmp_path)
    _changelog_updated_on_branch_with_source_change(work, monkeypatch)
    _write_pending_fragment(work, "a.bogus.md", "bump: minor\n- x\n")
    git_stdout(work, "add", "changelog.d/a.bogus.md")
    result = precommit.step_changelog_updated(work)
    assert not result.passed
    assert "Invalid changelog.d/" in result.output


def test_step_changelog_updated_fragments_mode_trigger_without_fragment_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A change requiring a changelog entry, with no fragment anywhere, fails."""
    (tmp_path / "CHANGELOG.md").write_text("# Changelog\n")
    _write_fragments_pyproject(tmp_path)
    monkeypatch.delenv("NO_VERSION", raising=False)
    monkeypatch.delenv("SKIP_CHANGELOG_CHECK", raising=False)
    monkeypatch.delenv("GITHUB_HEAD_REF", raising=False)
    monkeypatch.setattr(
        precommit, "resolve_current_branch", _fake_resolve_current_branch("feat/x")
    )
    monkeypatch.setattr(
        precommit.config, "select_diff_files", lambda *_a, **_kw: ["src/pkg/mod.py"]
    )
    result = precommit.step_changelog_updated(tmp_path)
    assert not result.passed
    assert "changelog.d/" in result.output


def test_step_changelog_updated_fragments_mode_message_names_fragment_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fragments-mode failure names whose job the fragment is.

    SCENARIO: this gate fires inside the `git commit` that
    `forge:git-commit-push` runs, so a commit agent is its usual reader.
    A message that states the requirement and hands over the authoring
    recipe — path template, slug/type placeholders, exact first line —
    without naming an owner is one that reader will satisfy itself,
    choosing a `bump:` level and with it the released version.

    MOCK SETUP: fragments mode with one changelog-requiring source file
    in the diff and no fragment anywhere.

    EXPECTED BEHAVIOR: the output names the PR author as the writer and
    tells a commit agent to report and stop instead of authoring one.
    """
    (tmp_path / "CHANGELOG.md").write_text("# Changelog\n")
    _write_fragments_pyproject(tmp_path)
    monkeypatch.delenv("NO_VERSION", raising=False)
    monkeypatch.delenv("SKIP_CHANGELOG_CHECK", raising=False)
    monkeypatch.delenv("GITHUB_HEAD_REF", raising=False)
    monkeypatch.setattr(
        precommit, "resolve_current_branch", _fake_resolve_current_branch("feat/x")
    )
    monkeypatch.setattr(
        precommit.config, "select_diff_files", lambda *_a, **_kw: ["src/pkg/mod.py"]
    )
    result = precommit.step_changelog_updated(tmp_path)
    assert not result.passed
    assert "the PR author's to write" in result.output
    assert "reports this gate and stops instead of authoring one" in result.output


def test_step_changelog_updated_fragments_mode_trigger_with_valid_fragment_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A trigger plus a valid fragment in the commit and on disk passes."""
    (tmp_path / "CHANGELOG.md").write_text("# Changelog\n")
    _write_fragments_pyproject(tmp_path)
    _write_pending_fragment(tmp_path, "a.added.md", "bump: minor\n- x\n")
    monkeypatch.delenv("NO_VERSION", raising=False)
    monkeypatch.delenv("SKIP_CHANGELOG_CHECK", raising=False)
    monkeypatch.delenv("GITHUB_HEAD_REF", raising=False)
    monkeypatch.setattr(
        precommit, "resolve_current_branch", _fake_resolve_current_branch("feat/x")
    )
    monkeypatch.setattr(
        precommit.config,
        "select_diff_files",
        lambda *_a, **_kw: ["src/pkg/mod.py", "changelog.d/a.added.md"],
    )
    monkeypatch.setattr(
        precommit, "get_modified_files", lambda **_kw: ["changelog.d/a.added.md"]
    )
    result = precommit.step_changelog_updated(tmp_path)
    assert result.passed
    assert "fragment present" in result.output


def test_step_changelog_updated_fragments_mode_invalid_on_disk_fragment_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A trigger with a fragment in the diff that fails validation still fails."""
    (tmp_path / "CHANGELOG.md").write_text("# Changelog\n")
    _write_fragments_pyproject(tmp_path)
    _write_pending_fragment(tmp_path, "a.bogus.md", "bump: minor\n- x\n")
    monkeypatch.delenv("NO_VERSION", raising=False)
    monkeypatch.delenv("SKIP_CHANGELOG_CHECK", raising=False)
    monkeypatch.delenv("GITHUB_HEAD_REF", raising=False)
    monkeypatch.setattr(
        precommit, "resolve_current_branch", _fake_resolve_current_branch("feat/x")
    )
    monkeypatch.setattr(
        precommit.config,
        "select_diff_files",
        lambda *_a, **_kw: ["src/pkg/mod.py", "changelog.d/a.bogus.md"],
    )
    monkeypatch.setattr(
        precommit, "get_modified_files", lambda **_kw: ["changelog.d/a.bogus.md"]
    )
    result = precommit.step_changelog_updated(tmp_path)
    assert not result.passed
    assert "Invalid changelog.d/" in result.output


def test_step_changelog_updated_fragments_mode_fragment_only_diff_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A diff touching only a fragment carries no changelog-requiring trigger.

    Fragments live under the excluded `changelog.d/` prefix, so a
    fragment-only diff never itself counts as a trigger — the gate is
    satisfied by having nothing left to require an entry for.
    """
    (tmp_path / "CHANGELOG.md").write_text("# Changelog\n")
    _write_fragments_pyproject(tmp_path)
    _write_pending_fragment(tmp_path, "a.added.md", "bump: minor\n- x\n")
    monkeypatch.delenv("NO_VERSION", raising=False)
    monkeypatch.delenv("SKIP_CHANGELOG_CHECK", raising=False)
    monkeypatch.delenv("GITHUB_HEAD_REF", raising=False)
    monkeypatch.setattr(
        precommit, "resolve_current_branch", _fake_resolve_current_branch("feat/x")
    )
    monkeypatch.setattr(
        precommit.config,
        "select_diff_files",
        lambda *_a, **_kw: ["changelog.d/a.added.md"],
    )
    result = precommit.step_changelog_updated(tmp_path)
    assert result.passed
    assert "No changelog-requiring changes." in result.output


def test_step_changelog_updated_fragments_mode_validates_even_without_triggers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An invalid on-disk fragment fails the gate even with an empty diff.

    Locks that fragment validation is unconditional: `check_pending_fragments`
    runs regardless of whether `triggers` is empty, so a stray invalid
    fragment left behind by an earlier commit still blocks — it is not
    gated on there being a changelog-requiring change in this diff.
    """
    (tmp_path / "CHANGELOG.md").write_text("# Changelog\n")
    _write_fragments_pyproject(tmp_path)
    _write_pending_fragment(tmp_path, "a.bogus.md", "bump: minor\n- x\n")
    monkeypatch.delenv("NO_VERSION", raising=False)
    monkeypatch.delenv("SKIP_CHANGELOG_CHECK", raising=False)
    monkeypatch.delenv("GITHUB_HEAD_REF", raising=False)
    monkeypatch.setattr(
        precommit, "resolve_current_branch", _fake_resolve_current_branch("feat/x")
    )
    monkeypatch.setattr(precommit.config, "select_diff_files", lambda *_a, **_kw: [])
    result = precommit.step_changelog_updated(tmp_path)
    assert not result.passed
    assert "Invalid changelog.d/" in result.output


# ---------------------------------------------------------------------------
# run_all step invocation
# ---------------------------------------------------------------------------


def _fake_step(name: str, called: list[str]) -> Callable[[Path], precommit.StepResult]:
    """Return a step fn that records its invocation and returns a canned pass.

    Fake (not ``Mock``) closure over *called* — the run_all invocation
    tests only need to observe whether the step ran, not stub a complex
    interface.

    Args:
        name: Step name stamped onto the returned ``StepResult``.
        called: List the fn appends *name* to when invoked — the spy.

    Returns:
        A step function usable as a ``monkeypatch.setattr(precommit,
        "step_<x>", ...)`` replacement.
    """

    def _step(_root: Path) -> precommit.StepResult:
        called.append(name)
        return precommit.StepResult(name=name, passed=True, output="ran")

    return _step


def test_run_all_invokes_selected_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``run_all`` invokes a step selected via ``only`` unconditionally."""
    init_git_repo(tmp_path)
    called: list[str] = []
    monkeypatch.setattr(precommit, "step_ruff", _fake_step("ruff", called))
    precommit.run_all(repo_root=tmp_path, only=["ruff"], print_progress=False)
    assert "ruff" in called


# ---------------------------------------------------------------------------
# main() --freshness (#538)
#
# All behavior tests: pin the read-only freshness reporter's documented
# contract (runs no steps, one verdict line per non-history log, --only
# filters by log basename without step-registry validation, --json emits
# a name->verdict map, always exits 0, a checks_files=False step's log
# always reads n/a, and --only against a *_history.log reads history
# rather than missing). Real git via init_git_repo / commit_all; the sole
# mock is the run_all spy the wip-sync tests above already establish as
# sanctioned.
# ---------------------------------------------------------------------------


def _write_log_with_stamp(
    repo: Path, name: str, *, tree: str, body: str = "log body\n"
) -> Path:
    """Write ``code_health/<name>.log`` with a produced-at stamp for *tree*.

    Args:
        repo: Repo root.
        name: Log basename (no extension).
        tree: Value for the stamp's ``tree=`` field — a 40-hex sha,
            ``"unknown"``, or any other string (the reader treats
            anything but a match to the current tree as stale).
        body: Content following the stamp line.

    Returns:
        The written log path.
    """
    log_dir = git_utils.code_health_dir(repo)
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"{name}.log"
    path.write_text(
        f"# produced-at: tree={tree} head=abc1234 2024-01-01T00:00:00Z\n{body}"
    )
    return path


def test_main_freshness_never_invokes_run_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--freshness` runs no steps at all — the run_all spy is never called.

    BEHAVIOR: pins the plan's "runs no steps" contract for the read-only
    freshness reporter, so a future refactor cannot silently route it
    through the step-execution path (which would run ruff, docstrings,
    etc. just to answer a staleness question).
    MOCK SETUP: `run_all` replaced with the same spy the wip-sync
    short-circuit tests above use — the sanctioned mock.
    """
    init_git_repo(tmp_path)
    monkeypatch.setattr(precommit, "get_repo_root", lambda: tmp_path)
    spy, calls = _run_all_spy()
    monkeypatch.setattr(precommit, "run_all", spy)
    with patch.object(precommit.sys, "argv", ["forge-precommit", "--freshness"]):
        rc = precommit.main()
    assert rc == 0
    assert calls == []


def test_main_freshness_human_output_reports_verdict_per_log_and_skips_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Human `--freshness` output covers all four verdicts and filters sidecars.

    Each `code_health/*.log` file gets one `<verdict> <name>.log` line (non-history
    logs only), and non-`.log` sidecars (`.json`, `*_history.log`) are excluded.

    SCENARIO: four `code_health/*.log` files stamped fresh / stale /
    unknown / (no stamp at all), plus a `*_history.log` and a `.json`
    sidecar that must both be excluded from the listing.
    """
    init_git_repo(tmp_path)
    (tmp_path / "tracked.txt").write_text("v1\n")
    commit_all(tmp_path, "seed")
    monkeypatch.setattr(precommit, "get_repo_root", lambda: tmp_path)
    head_tree = git_utils.get_tree_sha(tmp_path, "HEAD")

    _write_log_with_stamp(tmp_path, "audit_fresh", tree=head_tree)
    _write_log_with_stamp(tmp_path, "audit_stale", tree="0" * 40)
    _write_log_with_stamp(tmp_path, "audit_unknown", tree="unknown")
    (git_utils.code_health_dir(tmp_path) / "audit_unstamped.log").write_text(
        "no stamp here\n"
    )
    (git_utils.code_health_dir(tmp_path) / "smart_test_history.log").write_text(
        f"# produced-at: tree={head_tree} head=abc1234 2024-01-01T00:00:00Z\nhistory\n"
    )
    (git_utils.code_health_dir(tmp_path) / "pip_audit.json").write_text("{}\n")

    with patch.object(precommit.sys, "argv", ["forge-precommit", "--freshness"]):
        rc = precommit.main()

    assert rc == 0
    # Each line is `<verdict> <name>.log`, column-padded — split() rather
    # than an exact-spacing match so the assertion survives a padding-width
    # tweak.
    reported = {
        tuple(line.split())
        for line in capsys.readouterr().out.splitlines()
        if line.strip()
    }
    assert ("fresh", "audit_fresh.log") in reported
    assert ("stale", "audit_stale.log") in reported
    assert ("unknown", "audit_unknown.log") in reported
    assert ("unstamped", "audit_unstamped.log") in reported
    assert not any("history" in name for _verdict, name in reported)
    assert not any("pip_audit" in name for _verdict, name in reported)


def test_main_freshness_json_emits_name_to_verdict_map(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`--freshness --json` emits `{name: verdict}`, keyed by log stem (no `.log`)."""
    init_git_repo(tmp_path)
    (tmp_path / "tracked.txt").write_text("v1\n")
    commit_all(tmp_path, "seed")
    monkeypatch.setattr(precommit, "get_repo_root", lambda: tmp_path)
    head_tree = git_utils.get_tree_sha(tmp_path, "HEAD")
    _write_log_with_stamp(tmp_path, "audit_fresh", tree=head_tree)
    _write_log_with_stamp(tmp_path, "audit_stale", tree="0" * 40)

    with patch.object(
        precommit.sys, "argv", ["forge-precommit", "--freshness", "--json"]
    ):
        rc = precommit.main()

    payload = json.loads(capsys.readouterr().out)
    assert payload == {"audit_fresh": "fresh", "audit_stale": "stale"}
    assert rc == 0


def test_freshness_verdicts_marks_log_without_producer_orphaned(
    tmp_path: Path,
) -> None:
    """A log no step or known writer produces reads orphaned, even with a fresh stamp.

    BEHAVIOR: a fresh stamp on a removed step's leftover log would read as
    current evidence for a check that no longer runs.
    """
    init_git_repo(tmp_path)
    (tmp_path / "tracked.txt").write_text("v1\n")
    commit_all(tmp_path, "seed")
    head_tree = git_utils.get_tree_sha(tmp_path, "HEAD")
    _write_log_with_stamp(tmp_path, "removed_step", tree=head_tree)
    _write_log_with_stamp(tmp_path, "audit_summary", tree=head_tree)

    verdicts = precommit.freshness_verdicts(tmp_path)

    assert verdicts["removed_step"] == "orphaned"
    assert verdicts["audit_summary"] == "fresh"


@pytest.mark.parametrize(
    "name",
    [
        "precommit_timing",
        "precommit_only_timing",
        "pr_evidence",
        "pytest",
        "smart_test",
        "audit_deps_tree",
        "telemetry",
        "telemetry_build",
        "ruff",
    ],
)
def test_freshness_verdicts_known_producers_are_not_orphaned(
    tmp_path: Path, name: str
) -> None:
    """Logs written by non-step producers (and real steps) are never orphaned.

    Args:
        name: Log stem a forge component still writes.
    """
    init_git_repo(tmp_path)
    (tmp_path / "tracked.txt").write_text("v1\n")
    commit_all(tmp_path, "seed")
    _write_log_with_stamp(tmp_path, name, tree="0" * 40)

    assert precommit.freshness_verdicts(tmp_path)[name] != "orphaned"


def test_every_literal_step_log_name_in_src_has_a_producer() -> None:
    """Each log name src/forge writes literally is recognised as having a producer.

    Covers literal positional log names only: a name built dynamically or
    passed by keyword is invisible to the pattern.

    BEHAVIOR: guards the orphan rule against a new component writing a log
    the rule would brand orphaned (and `--freshness` then misreport).
    """
    src = Path(precommit.__file__).parent
    pattern = re.compile(
        r'(?:write_step_log|capturing_to_step_log)\(\s*\w+\s*,\s*"([A-Za-z0-9_]+)"'
    )
    names = {
        name
        for path in src.rglob("*.py")
        for name in pattern.findall(path.read_text(encoding="utf-8"))
    }
    step_names = frozenset(step.name for step in precommit._STEP_REGISTRY)

    assert names, "regex found no literal log names; the pattern has rotted"
    unowned = sorted(n for n in names if not precommit._log_has_producer(n, step_names))
    assert unowned == []


def test_main_freshness_only_filters_by_log_basename_without_step_registry_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`--freshness --only audit_summary,not_a_step` selects by log basename.

    `audit_summary` names no real precommit step (it is the
    `forge-audit-all` orchestrator's own summary log), and `not_a_step`
    names nothing at all — the ordinary `--only` path raises `ValueError`
    for an unknown step name via `resolve_steps`, but `--freshness`
    filters logs directly and must not reach that validation.
    """
    init_git_repo(tmp_path)
    (tmp_path / "tracked.txt").write_text("v1\n")
    commit_all(tmp_path, "seed")
    monkeypatch.setattr(precommit, "get_repo_root", lambda: tmp_path)
    head_tree = git_utils.get_tree_sha(tmp_path, "HEAD")
    _write_log_with_stamp(tmp_path, "ruff", tree=head_tree)
    _write_log_with_stamp(tmp_path, "audit_summary", tree=head_tree)

    with patch.object(
        precommit.sys,
        "argv",
        ["forge-precommit", "--freshness", "--only", "audit_summary,not_a_step"],
    ):
        rc = precommit.main()

    out = capsys.readouterr().out
    assert rc == 0
    assert "audit_summary.log" in out
    assert "ruff.log" not in out
    assert "unknown step" not in out.lower()


def test_main_freshness_only_missing_log_reports_missing_verdict_alongside_real_ones(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A requested log name with no `code_health/<name>.log` reports `missing`.

    `--only ruff,ghost_step` names one real, fresh log and one that was
    never written — the caller checking a specific step must see
    `missing` for it rather than silence, alongside `ruff`'s real
    verdict in the same run.
    """
    init_git_repo(tmp_path)
    (tmp_path / "tracked.txt").write_text("v1\n")
    commit_all(tmp_path, "seed")
    monkeypatch.setattr(precommit, "get_repo_root", lambda: tmp_path)
    head_tree = git_utils.get_tree_sha(tmp_path, "HEAD")
    _write_log_with_stamp(tmp_path, "ruff", tree=head_tree)

    with patch.object(
        precommit.sys,
        "argv",
        ["forge-precommit", "--freshness", "--only", "ruff,ghost_step"],
    ):
        rc = precommit.main()

    reported = {
        tuple(line.split())
        for line in capsys.readouterr().out.splitlines()
        if line.strip()
    }
    assert rc == 0
    assert ("fresh", "ruff.log") in reported
    assert ("missing", "ghost_step.log") in reported


def test_main_freshness_only_missing_log_reports_missing_in_json_map(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`--freshness --json` carries `"missing"` for a requested absent log too."""
    init_git_repo(tmp_path)
    (tmp_path / "tracked.txt").write_text("v1\n")
    commit_all(tmp_path, "seed")
    monkeypatch.setattr(precommit, "get_repo_root", lambda: tmp_path)
    head_tree = git_utils.get_tree_sha(tmp_path, "HEAD")
    _write_log_with_stamp(tmp_path, "ruff", tree=head_tree)

    with patch.object(
        precommit.sys,
        "argv",
        ["forge-precommit", "--freshness", "--json", "--only", "ruff,ghost_step"],
    ):
        rc = precommit.main()

    payload = json.loads(capsys.readouterr().out)
    assert payload == {"ruff": "fresh", "ghost_step": "missing"}
    assert rc == 0


def test_main_freshness_exits_zero_regardless_of_stale_or_unstamped_verdicts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--freshness` always exits 0 — a reporter, never a second gate.

    A stale or unstamped log is exactly the finding the flag exists to
    surface; failing the process on it would make the reporter a second
    gate instead of a read-only advisory.
    """
    init_git_repo(tmp_path)
    monkeypatch.setattr(precommit, "get_repo_root", lambda: tmp_path)
    _write_log_with_stamp(tmp_path, "ruff", tree="0" * 40)
    (git_utils.code_health_dir(tmp_path) / "unstamped.log").write_text(
        "plain text, no stamp\n"
    )

    with patch.object(precommit.sys, "argv", ["forge-precommit", "--freshness"]):
        rc = precommit.main()
    assert rc == 0


def test_main_freshness_human_output_drops_non_printable_chars_from_log_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A log stem containing an ESC byte prints with the byte stripped.

    Defends stdout from terminal control-sequence injection via a
    crafted `code_health/` filename: `_report_freshness`'s human-output
    path filters each name through `str.isprintable()` before formatting
    the line, since a log name is a file name — untrusted relative to
    what a reporter should echo verbatim to a terminal.
    """
    init_git_repo(tmp_path)
    monkeypatch.setattr(precommit, "get_repo_root", lambda: tmp_path)
    log_dir = git_utils.code_health_dir(tmp_path)
    log_dir.mkdir(exist_ok=True)
    # `audit_` prefix gives the log a producer, so it reads unknown, not orphaned.
    name = "audit_\x1b[31mruff"
    try:
        (log_dir / f"{name}.log").write_text(
            "# produced-at: tree=unknown head=abc1234 2024-01-01T00:00:00Z\nbody\n"
        )
    except OSError as exc:
        pytest.skip(f"filesystem rejects an ESC byte in a filename: {exc}")

    with patch.object(precommit.sys, "argv", ["forge-precommit", "--freshness"]):
        rc = precommit.main()

    out = capsys.readouterr().out
    expected_shown = "".join(ch for ch in name if ch.isprintable())
    reported = {tuple(line.split()) for line in out.splitlines() if line.strip()}
    assert rc == 0
    assert "\x1b" not in out
    assert ("unknown", f"{expected_shown}.log") in reported


def test_main_freshness_environment_step_log_reports_na_regardless_of_stale_stamp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A `checks_files=False` step's log reads `n/a` even with a stale stamp.

    `env_sync` judges the installed environment, not the tree on disk — a
    stamp mismatch says nothing about whether the environment is stale, so
    the reporter overrides its verdict to `n/a` regardless of what the
    stamp says. `ruff.log`, an ordinary `checks_files=True` step, still
    reports its real stamp-derived `stale` verdict in the same run — the
    override is per-step, not global.
    """
    init_git_repo(tmp_path)
    (tmp_path / "tracked.txt").write_text("v1\n")
    commit_all(tmp_path, "seed")
    monkeypatch.setattr(precommit, "get_repo_root", lambda: tmp_path)
    _write_log_with_stamp(tmp_path, "env_sync", tree="0" * 40)
    _write_log_with_stamp(tmp_path, "ruff", tree="0" * 40)

    with patch.object(precommit.sys, "argv", ["forge-precommit", "--freshness"]):
        rc = precommit.main()

    reported = {
        tuple(line.split())
        for line in capsys.readouterr().out.splitlines()
        if line.strip()
    }
    assert rc == 0
    assert ("n/a", "env_sync.log") in reported
    assert ("stale", "ruff.log") in reported


def test_main_freshness_json_reports_na_for_environment_step_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`--freshness --json` carries `"n/a"` for a `checks_files=False` step's log too.

    Same override as the human-output test above, exercised through the
    JSON rendering path instead.
    """
    init_git_repo(tmp_path)
    (tmp_path / "tracked.txt").write_text("v1\n")
    commit_all(tmp_path, "seed")
    monkeypatch.setattr(precommit, "get_repo_root", lambda: tmp_path)
    _write_log_with_stamp(tmp_path, "env_sync", tree="0" * 40)
    _write_log_with_stamp(tmp_path, "ruff", tree="0" * 40)

    with patch.object(
        precommit.sys, "argv", ["forge-precommit", "--freshness", "--json"]
    ):
        rc = precommit.main()

    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["env_sync"] == "n/a"
    assert payload["ruff"] == "stale"


def test_main_freshness_only_history_log_reports_history_not_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`--only <history-log-name>` reports `history`, not `missing`.

    An append-only `*_history.log` stays unlisted from a bare `--freshness`
    (covered above) because one stamp cannot describe the many trees it
    accumulates records from — but a caller naming it explicitly via
    `--only` must not read `missing`, which would wrongly claim the log
    was never written at all. A genuinely absent name in the same `--only`
    list still reads `missing` (already pinned above for a non-history
    name); this test only adds the history case.
    """
    init_git_repo(tmp_path)
    monkeypatch.setattr(precommit, "get_repo_root", lambda: tmp_path)
    git_utils.code_health_dir(tmp_path).mkdir(exist_ok=True)
    (git_utils.code_health_dir(tmp_path) / "smart_test_history.log").write_text(
        "# produced-at: tree=unknown head=abc1234 2024-01-01T00:00:00Z\nhistory\n"
    )

    with patch.object(
        precommit.sys,
        "argv",
        ["forge-precommit", "--freshness", "--only", "smart_test_history,ghost_step"],
    ):
        rc = precommit.main()

    reported = {
        tuple(line.split())
        for line in capsys.readouterr().out.splitlines()
        if line.strip()
    }
    assert rc == 0
    assert ("history", "smart_test_history.log") in reported
    assert ("missing", "ghost_step.log") in reported


def _verdict_repo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    rows: dict[str, str],
    timing_tree: str | None = None,
) -> list[str]:
    """Seed a git repo whose timing log carries *rows*; return enabled step names.

    Args:
        tmp_path: Repo root to initialise.
        monkeypatch: Points ``get_repo_root`` at *tmp_path*.
        rows: Step name -> marker written into ``precommit_timing.log``.
        timing_tree: ``tree=`` stamp for the timing log; defaults to the
            current tree (fresh).

    Returns:
        The enabled step names, in registry order.
    """
    init_git_repo(tmp_path)
    (tmp_path / "tracked.txt").write_text("v1\n")
    commit_all(tmp_path, "seed")
    monkeypatch.setattr(precommit, "get_repo_root", lambda: tmp_path)
    head_tree = git_utils.get_tree_sha(tmp_path, "HEAD")
    body = timing_log(*(f"{name} {mark}" for name, mark in rows.items()), stamp=None)
    _write_log_with_stamp(
        tmp_path, "precommit_timing", tree=timing_tree or head_tree, body=body
    )
    return [d.name for d in precommit.resolve_steps(tmp_path)]


def _all_rows(tmp_path: Path, marker: str = "PASS") -> dict[str, str]:
    """Return a dict of all enabled step names mapped to a status marker.

    Args:
        tmp_path: Repository to read enabled steps from.
        marker: Status marker value for each step.

    Returns:
        Mapping of step name to marker.
    """
    return {d.name: marker for d in precommit.resolve_steps(tmp_path)}


def test_verdict_all_enabled_steps_pass_is_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every enabled step has a PASS row in a fresh timing log -> PASS."""
    init_git_repo(tmp_path)
    rows = _all_rows(tmp_path)
    _verdict_repo(tmp_path, monkeypatch, rows=rows)

    ok, lines = precommit.verdict(tmp_path)

    assert ok is True
    assert lines[-1] == "verdict: PASS"
    assert all(f"PASS {name}" in lines for name in rows)


def test_verdict_fail_row_fails_and_names_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A FAIL marker flips the verdict and emits `FAIL <step>`."""
    init_git_repo(tmp_path)
    rows = _all_rows(tmp_path)
    rows["ruff"] = "FAIL"
    _verdict_repo(tmp_path, monkeypatch, rows=rows)

    ok, lines = precommit.verdict(tmp_path)

    assert ok is False
    assert "FAIL ruff" in lines
    assert lines[-1].startswith("verdict: FAIL — failing:")
    assert "ruff" in lines[-1]


def test_verdict_missing_enabled_step_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An enabled step with no timing row is reported `MISSING <step>`."""
    init_git_repo(tmp_path)
    rows = _all_rows(tmp_path)
    del rows["ruff"]
    _verdict_repo(tmp_path, monkeypatch, rows=rows)

    ok, lines = precommit.verdict(tmp_path)

    assert ok is False
    assert "MISSING ruff" in lines
    assert lines[-1].startswith("verdict: FAIL — missing:")
    assert "ruff" in lines[-1]


def test_verdict_stale_timing_log_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A timing log stamped for another tree fails even with all PASS rows."""
    init_git_repo(tmp_path)
    rows = _all_rows(tmp_path)
    _verdict_repo(tmp_path, monkeypatch, rows=rows, timing_tree="0" * 40)

    ok, lines = precommit.verdict(tmp_path)

    assert ok is False
    assert "STALE precommit_timing (last full run describes another tree)" in lines
    assert lines[-1].startswith("verdict: FAIL — evidence describes another tree")


def test_verdict_stale_evidence_takes_precedence_over_failing_cause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BEHAVIOR: a FAIL row from a stale run is not blamed; the re-run advice wins."""
    init_git_repo(tmp_path)
    rows = _all_rows(tmp_path)
    rows["ruff"] = "FAIL"
    _verdict_repo(tmp_path, monkeypatch, rows=rows, timing_tree="0" * 40)

    ok, lines = precommit.verdict(tmp_path)

    assert ok is False
    assert lines[-1].startswith("verdict: FAIL — evidence describes another tree")
    assert "failing:" not in lines[-1]


def test_verdict_warn_and_skip_count_as_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WARN and SKIP rows are echoed as `<MARKER> <step>` and do not fail."""
    init_git_repo(tmp_path)
    rows = _all_rows(tmp_path)
    rows["ruff"] = "WARN"
    skipped = next(name for name in rows if name != "ruff")
    rows[skipped] = "SKIP"
    _verdict_repo(tmp_path, monkeypatch, rows=rows)

    ok, lines = precommit.verdict(tmp_path)

    assert ok is True
    assert "WARN ruff" in lines
    assert f"SKIP {skipped}" in lines
    assert lines[-1] == "verdict: PASS"


def test_verdict_stale_step_log_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A passing step whose own log is stamped for another tree -> `STALE <step>`."""
    init_git_repo(tmp_path)
    rows = _all_rows(tmp_path)
    _verdict_repo(tmp_path, monkeypatch, rows=rows)
    _write_log_with_stamp(tmp_path, "ruff", tree="0" * 40)

    ok, lines = precommit.verdict(tmp_path)

    assert ok is False
    assert "STALE ruff" in lines
    assert lines[-1].startswith("verdict: FAIL — evidence describes another tree")


def test_verdict_unstamped_step_log_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A passing step whose own log has no produced-at stamp is not verified."""
    init_git_repo(tmp_path)
    rows = _all_rows(tmp_path)
    _verdict_repo(tmp_path, monkeypatch, rows=rows)
    (git_utils.code_health_dir(tmp_path) / "ruff.log").write_text("no stamp here\n")

    ok, lines = precommit.verdict(tmp_path)

    assert ok is False
    assert "STALE ruff" in lines
    assert lines[-1].startswith("verdict: FAIL — evidence describes another tree")


def test_verdict_skipped_step_with_old_log_is_not_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A SKIP row never fails on its own old log (partial commit leaves it behind)."""
    init_git_repo(tmp_path)
    rows = _all_rows(tmp_path)
    step = next(iter(rows))
    rows[step] = "SKIP"
    _verdict_repo(tmp_path, monkeypatch, rows=rows)
    _write_log_with_stamp(tmp_path, step, tree="0" * 40)

    ok, lines = precommit.verdict(tmp_path)

    assert ok is True
    assert f"SKIP {step}" in lines
    assert f"STALE {step}" not in lines
    assert lines[-1] == "verdict: PASS"


@pytest.mark.parametrize(("marker", "rc"), [("PASS", 0), ("FAIL", 1)])
def test_main_verdict_prints_lines_exits_by_verdict_and_runs_no_steps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    marker: str,
    rc: int,
) -> None:
    """`--verdict` prints the verdict lines, exits 0/1, and never calls run_all.

    Args:
        marker: Verdict marker (PASS or FAIL) on every row.
        rc: Expected exit code for that marker.
    """
    init_git_repo(tmp_path)
    rows = _all_rows(tmp_path, marker)
    _verdict_repo(tmp_path, monkeypatch, rows=rows)
    spy, calls = _run_all_spy()
    monkeypatch.setattr(precommit, "run_all", spy)

    with patch.object(precommit.sys, "argv", ["forge-precommit", "--verdict"]):
        got = precommit.main()

    assert got == rc
    out = capsys.readouterr().out.splitlines()
    assert out[-1].startswith(f"verdict: {marker}")
    assert calls == []


def test_run_all_only_writes_only_log_and_leaves_full_log_untouched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `--only` run writes `precommit_only_timing.log`, never the full log.

    BEHAVIOR: the full-run log describes the last full run; a partial run
    must not overwrite it. A run without `only` still writes it.
    MOCK SETUP: `step_ruff` stubbed to a passing result.
    """

    def _ruff(_root: object) -> precommit.StepResult:
        return precommit.StepResult(name="ruff", passed=True, output="x")

    monkeypatch.setattr(precommit, "step_ruff", _ruff)
    log_dir = git_utils.code_health_dir(tmp_path)
    log_dir.mkdir(exist_ok=True)
    full = log_dir / "precommit_timing.log"
    full.write_bytes(b"# produced-at: tree=abc head=x\nfull run body\n")
    before = full.read_bytes()

    precommit.run_all(tmp_path, print_progress=False, only=["ruff"])

    assert (log_dir / "precommit_only_timing.log").is_file()
    assert full.read_bytes() == before


def test_run_all_skip_writes_only_log_and_leaves_full_log_untouched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `--skip` run is partial too: it writes the only-log, not the full log.

    BEHAVIOR: a skipped battery is not the whole battery's result, so it must
    not replace the full-run record `--verdict` and wrap-ups read.
    MOCK SETUP: `step_ruff` stubbed passing; every other step is skipped.
    """

    def _ruff(_root: object) -> precommit.StepResult:
        return precommit.StepResult(name="ruff", passed=True, output="x")

    monkeypatch.setattr(precommit, "step_ruff", _ruff)
    log_dir = git_utils.code_health_dir(tmp_path)
    log_dir.mkdir(exist_ok=True)
    full = log_dir / "precommit_timing.log"
    full.write_bytes(b"# produced-at: tree=abc head=x\nfull run body\n")
    before = full.read_bytes()
    others = [d.name for d in precommit.resolve_steps(tmp_path) if d.name != "ruff"]

    precommit.run_all(tmp_path, print_progress=False, skip=others)

    assert (log_dir / "precommit_only_timing.log").is_file()
    assert full.read_bytes() == before


def test_run_all_without_only_still_writes_full_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A full run writes `precommit_timing.log` (and not the only-log)."""
    _stub_env_sync_skipped(monkeypatch)
    _stub_docstrings_passing(monkeypatch)
    _stub_test_naming_passing(monkeypatch)
    _stub_repo_structure_passing(monkeypatch)
    _stub_pip_audit_skipped(monkeypatch)
    _stub_docstring_coverage_skipped(monkeypatch)

    precommit.run_all(repo_root=tmp_path, print_progress=False)

    assert (git_utils.code_health_dir(tmp_path) / "precommit_timing.log").is_file()
    assert not (
        git_utils.code_health_dir(tmp_path) / "precommit_only_timing.log"
    ).exists()


def _staged_doc_repo(tmp_path: Path, rel: str) -> Path:
    """Init a repo with *rel* written and staged.

    Args:
        tmp_path: Repo root to initialise.
        rel: Repo-relative doc path.

    Returns:
        The repo root.
    """
    init_git_repo(tmp_path)
    (tmp_path / rel).write_text("generated v1\n")
    subprocess.run(["git", "add", rel], cwd=tmp_path, check=True)
    return tmp_path


def test_record_regenerated_skips_doc_not_restaged(tmp_path: Path) -> None:
    """A doc whose worktree differs from its index blob is not recorded."""
    rel = "api.md"
    repo = _staged_doc_repo(tmp_path, rel)
    (repo / rel).write_text("generated v2, never staged\n")

    regen_docs.record_regenerated(
        repo, [rel], {rel: {"forge_version": "1", "inputs": "x"}}
    )

    assert rel not in regen_docs.load_record(repo)


def test_record_regenerated_records_doc_matching_index_blob(tmp_path: Path) -> None:
    """A doc whose worktree equals its index blob is recorded with that blob."""
    rel = "api.md"
    repo = _staged_doc_repo(tmp_path, rel)

    regen_docs.record_regenerated(
        repo, [rel], {rel: {"forge_version": "1", "inputs": "x"}}
    )

    entry = regen_docs.load_record(repo)[rel]
    assert entry["doc_blob"] == regen_docs.index_blobs(repo)[rel]
    assert entry["inputs"] == "x"


# ---------------------------------------------------------------------------
# continuation status panel written by main()
# ---------------------------------------------------------------------------


@pytest.fixture
def panel_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A real tmp repo with ``.plan/`` ignored, pinned as pre-commit's root.

    Returns:
        The repo root; CI markers are cleared so the panel is written.
    """
    for name in _CI_MARKERS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("FORGE_WIP_SYNC", raising=False)
    init_git_repo(tmp_path)
    (tmp_path / ".gitignore").write_text(".plan/\n", encoding="utf-8")
    commit_all(tmp_path, "ignore plan")
    monkeypatch.setattr(precommit, "get_repo_root", lambda: tmp_path)
    return tmp_path


def _panel_text(root: Path) -> str:
    """Return the handoff note under *root*.

    Args:
        root: Repository root directory.

    Returns:
        Contents of CONTINUATION.md.
    """
    return (root / ".plan" / "CONTINUATION.md").read_text(encoding="utf-8")


def _drive_main(monkeypatch: pytest.MonkeyPatch, run_all: object, *argv: str) -> int:
    """Run ``precommit.main`` with *run_all* stubbed and *argv* as arguments.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        run_all: Mock for precommit.run_all.
        *argv: Command-line arguments for precommit.main.

    Returns:
        Exit code from precommit.main().
    """
    monkeypatch.setattr(precommit, "run_all", run_all)
    with patch.object(precommit.sys, "argv", ["forge-precommit", *argv]):
        return precommit.main()


def test_main_writes_the_panel_naming_the_blocking_step(
    panel_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A blocked commit leaves its blocking step in the note, exit stays 1."""

    def _blocked(**_kw: object) -> list[precommit.StepResult]:
        return [precommit.StepResult(name="ruff_check", passed=False, output="x")]

    assert _drive_main(monkeypatch, _blocked) == 1
    assert "- last-attempt: blocked:ruff_check" in _panel_text(panel_repo)


def test_main_writes_passed_on_a_clean_run(
    panel_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A clean run records ``passed``."""
    assert _drive_main(monkeypatch, lambda **_kw: []) == 0
    assert "- last-attempt: passed" in _panel_text(panel_repo)


def test_main_writes_error_when_run_all_raises_value_error(
    panel_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A usage error (unknown step) is recorded as ``error`` and still exits 1."""

    def _bad(**_kw: object) -> list[precommit.StepResult]:
        msg = "unknown step"
        raise ValueError(msg)

    assert _drive_main(monkeypatch, _bad) == 1
    assert "- last-attempt: error" in _panel_text(panel_repo)


def test_main_writes_interrupted_when_the_run_is_cut_short(
    panel_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A KeyboardInterrupt still reaches the ``finally`` and is recorded."""

    def _interrupt(**_kw: object) -> list[precommit.StepResult]:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        _drive_main(monkeypatch, _interrupt)
    assert "- last-attempt: interrupted" in _panel_text(panel_repo)


def test_main_never_fails_the_commit_when_the_panel_write_errors(
    panel_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A broken panel write is logged, and pre-commit's exit code is unchanged."""

    def _boom(*_a: object, **_kw: object) -> str:
        msg = "disk full"
        raise OSError(msg)

    monkeypatch.setattr(precommit, "write_state", _boom)
    assert _drive_main(monkeypatch, lambda **_kw: []) == 0


@pytest.mark.parametrize("scenario", ["only", "ci", "wip-sync"])
def test_main_skips_the_panel_for_partial_ci_and_wip_runs(
    panel_repo: Path, monkeypatch: pytest.MonkeyPatch, scenario: str
) -> None:
    """``--only`` is not a commit attempt, CI has no reader, wip-sync runs nothing.

    Args:
        scenario: Which skip condition to set up (``only``, ``ci`` or ``wip-sync``).
    """
    argv: tuple[str, ...] = ()
    if scenario == "only":
        argv = ("--only", "ruff_check")
    elif scenario == "ci":
        monkeypatch.setenv("CI", "1")
    else:
        monkeypatch.setenv("FORGE_WIP_SYNC", "1")
    _drive_main(monkeypatch, lambda **_kw: [], *argv)
    assert not (panel_repo / ".plan" / "CONTINUATION.md").exists()


def test_main_skip_run_leaves_continuation_panel_unwritten(
    panel_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``--skip`` run omits steps, so it is not a commit attempt: no panel."""
    _drive_main(monkeypatch, lambda **_kw: [], "--skip", "pip_audit")
    assert not (panel_repo / ".plan" / "CONTINUATION.md").exists()


# ---------------------------------------------------------------------------
# Untracked files: some steps skip them, and the run always says which
# ---------------------------------------------------------------------------

_GOOD_MODULE = (
    '"""A module."""\n\n\n'
    "def f(x: int) -> int:\n"
    '    """Return *x*.\n\n'
    "    Args:\n"
    "        x: A value.\n\n"
    "    Returns:\n"
    "        The value.\n"
    '    """\n'
    "    return x\n"
)
# Args names a parameter the signature does not have: a blocking
# docstring-verification error whenever the file is checked.
_BAD_MODULE = _GOOD_MODULE.replace("        x: A value.", "        y: A value.")


@pytest.fixture
def untracked_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A real repo on ``feat/x`` off ``main`` with one tracked, clean module.

    Returns:
        The repo root, pinned as pre-commit's root, with CI markers
        cleared and ``src/pkg/good.py`` committed on ``main``.
    """
    for name in _CI_MARKERS:
        monkeypatch.delenv(name, raising=False)
    init_git_repo(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        '[tool.forge]\nsource_dirs = ["src"]\ntest_dirs = ["tests"]\n'
    )
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "good.py").write_text(_GOOD_MODULE)
    commit_all(tmp_path, "seed")
    git_stdout(tmp_path, "checkout", "-q", "-b", "feat/x")
    monkeypatch.setattr(precommit, "get_repo_root", lambda: tmp_path)
    return tmp_path


def _set_scope(repo: Path, scope: str) -> None:
    """Append a ``[tool.forge.precommit]`` scope to *repo*'s pyproject.

    Args:
        repo: Repo root.
        scope: ``all`` or ``diff``.
    """
    with (repo / "pyproject.toml").open("a") as fh:
        fh.write(f'\n[tool.forge.precommit]\nscope = "{scope}"\n')


@pytest.mark.parametrize("scope", ["all", "diff"])
def test_untracked_module_with_docstring_error_is_named_not_checked(
    untracked_repo: Path, monkeypatch: pytest.MonkeyPatch, scope: str
) -> None:
    """The step passes over an untracked module and its log says so.

    The module carries a blocking docstring error; the step still passes
    because it never read the file, and the log names the file rather
    than letting the PASS read as covering it.

    Args:
        scope: The docstring step's file-selection scope.
    """
    monkeypatch.chdir(untracked_repo)
    _set_scope(untracked_repo, scope)
    commit_all(untracked_repo, "scope")
    (untracked_repo / "src" / "pkg" / "new.py").write_text(_BAD_MODULE)

    [result] = precommit.run_all(
        untracked_repo, print_progress=False, only=["docstring_verification"]
    )

    assert result.passed
    log = (
        git_utils.code_health_dir(untracked_repo) / "docstring_verification.log"
    ).read_text()
    assert "NOTE: 1 untracked file(s) not checked by this step: `src/pkg/new.py`" in log


@pytest.mark.parametrize("scope", ["all", "diff"])
def test_tracked_unstaged_edit_is_still_checked(
    untracked_repo: Path, monkeypatch: pytest.MonkeyPatch, scope: str
) -> None:
    """An unstaged edit to a tracked file is checked in both scopes.

    Args:
        scope: The docstring step's file-selection scope.
    """
    monkeypatch.chdir(untracked_repo)
    _set_scope(untracked_repo, scope)
    commit_all(untracked_repo, "scope")
    (untracked_repo / "src" / "pkg" / "good.py").write_text(_BAD_MODULE)

    [result] = precommit.run_all(
        untracked_repo, print_progress=False, only=["docstring_verification"]
    )

    assert not result.passed
    assert "untracked" not in result.output


def test_relevant_untracked_files_lists_only_what_a_blind_step_would_check(
    untracked_repo: Path,
) -> None:
    """Scratch text and gitignored files are never listed; a module is.

    The ``.py`` under the source root is one docstring verification would
    have read; the ``.txt`` is one no enabled step reads, and the ignored
    module is declared out of scope by the repo itself.
    """
    (untracked_repo / ".gitignore").write_text("src/pkg/ignored.py\n")
    commit_all(untracked_repo, "ignore")
    (untracked_repo / "src" / "pkg" / "new.py").write_text(_GOOD_MODULE)
    (untracked_repo / "src" / "pkg" / "ignored.py").write_text(_GOOD_MODULE)
    (untracked_repo / "notes.txt").write_text("scratch\n")

    listed = precommit.relevant_untracked_files(untracked_repo)

    assert [path for path, _ in listed] == ["src/pkg/new.py"]
    assert "docstring_verification" in listed[0][1]


def test_relevant_untracked_files_omits_a_step_that_reads_untracked_files(
    untracked_repo: Path,
) -> None:
    """Ruff in ``all`` scope walks the disk, so it is never named; in ``diff`` it is.

    ``all`` hands ruff the source roots and ruff reads every file under
    them, untracked ones included — naming it would claim it skipped a
    file it checked. ``diff`` hands it git's diff, which has no untracked
    file in it.
    """
    (untracked_repo / "src" / "pkg" / "new.py").write_text(_GOOD_MODULE)

    assert precommit.relevant_untracked_files(untracked_repo, only=["ruff"]) == []
    _set_scope(untracked_repo, "diff")
    assert precommit.relevant_untracked_files(untracked_repo, only=["ruff"]) == [
        ("src/pkg/new.py", ("ruff",))
    ]


def test_relevant_untracked_files_clean_tree_is_empty(untracked_repo: Path) -> None:
    """A tree with nothing untracked produces no note anywhere."""
    assert precommit.relevant_untracked_files(untracked_repo) == []


def test_relevant_untracked_files_outside_git_is_empty(tmp_path: Path) -> None:
    """Outside a git work tree there is nothing to list, and nothing raises."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("X = 1\n")
    assert precommit.relevant_untracked_files(tmp_path) == []


def _untracked_verdict(
    untracked_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    marker_for: str | None,
    stamp_tree: str | None = None,
) -> tuple[bool, list[str]]:
    """Run ``verdict`` over a repo holding one untracked bad module.

    Args:
        untracked_repo: Repo fixture with docstring verification enabled.
        monkeypatch: Pytest monkeypatch fixture.
        marker_for: Step to record as ``FAIL`` in the timing log, or
            ``None`` for an all-PASS run.
        stamp_tree: Tree to stamp the timing log with, or ``None`` for the
            current working tree.

    Returns:
        ``verdict``'s ``(ok, lines)``.
    """
    rows = _all_rows(untracked_repo)
    if marker_for is not None:
        rows[marker_for] = "FAIL"
    (untracked_repo / "src" / "pkg" / "new.py").write_text(_BAD_MODULE)
    body = timing_log(*(f"{name} {mark}" for name, mark in rows.items()), stamp=None)
    tree = stamp_tree or git_utils.working_tree_sha(untracked_repo)
    assert tree is not None
    _write_log_with_stamp(untracked_repo, "precommit_timing", tree=tree, body=body)
    monkeypatch.chdir(untracked_repo)
    return precommit.verdict(untracked_repo)


def test_verdict_untracked_unchecked_files_fail_with_hook_safe_line(
    untracked_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Untracked files no step checked fail the verdict, naming the steps.

    BEHAVIOR: unknown is never a pass (FOUNDATION section 13); the closing
    line keeps the literal ``verdict: FAIL`` the require_fixer_verdict hook
    matches, with no leading FAIL/MISSING/STALE row for it to mistake.
    """
    ok, lines = _untracked_verdict(untracked_repo, monkeypatch, marker_for=None)

    assert ok is False
    assert lines[-1].startswith("verdict: FAIL — untracked files not checked by ")
    assert "docstring_verification" in lines[-1]
    assert lines[-1].endswith("gitignore them if not")
    [note] = [line for line in lines if line.startswith("NOTE ")]
    assert "`src/pkg/new.py`" in note
    assert not [line for line in lines if line.startswith(("FAIL", "MISSING", "STALE"))]


def test_verdict_failing_step_outranks_unchecked_untracked_files(
    untracked_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing step is the named cause even when untracked files exist too.

    BEHAVIOR: precedence stale > failing/missing > unchecked, so the advice
    shown first is the one that must be acted on first.
    """
    ok, lines = _untracked_verdict(
        untracked_repo, monkeypatch, marker_for="docstring_verification"
    )

    assert ok is False
    assert lines[-1].startswith("verdict: FAIL")
    assert "untracked" not in lines[-1]
    assert "docstring_verification" in lines[-1]


def test_verdict_stale_evidence_outranks_unchecked_untracked_files(
    untracked_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Evidence for another tree is the named cause, not the untracked files.

    BEHAVIOR: precedence stale > failing/missing > unchecked; a stale log
    says nothing about the code, so the untracked advice would be premature.
    """
    ok, lines = _untracked_verdict(
        untracked_repo, monkeypatch, marker_for=None, stamp_tree="0" * 40
    )

    assert ok is False
    assert lines[-1].startswith("verdict: FAIL — evidence describes another tree")
    assert "untracked" not in lines[-1]


@pytest.mark.parametrize(
    "step_fn",
    [
        precommit.step_api_digest_check,
        precommit.step_cli_reference_check,
        precommit.step_c4,
    ],
    ids=["api_digest_check", "cli_reference_check", "c4"],
)
def test_gen_check_step_propagates_systemexit_when_cli_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    step_fn: Callable[[Path], precommit.StepResult],
) -> None:
    """A --check step lets the loud-fail SystemExit from forge_cli_argv escape.

    BEHAVIOR: FOUNDATION section 2 -- a missing forge CLI must never degrade
    into a failed or skipped StepResult.

    Args:
        step_fn: The ``--check`` step under test.
    """
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "api-digest.md").write_text("x\n")
    (tmp_path / "docs" / "cli-reference.md").write_text("x\n")
    monkeypatch.setattr(precommit, "resolve_model_section", lambda _root: {})

    def _raise_missing(_name: str, **_kw: object) -> list[str]:
        raise SystemExit(2)

    monkeypatch.setattr(precommit, "forge_cli_argv", _raise_missing)

    with pytest.raises(SystemExit) as exc_info:
        step_fn(tmp_path)
    assert exc_info.value.code == 2


def test_verdict_clean_tree_has_no_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing untracked → no NOTE line in the verdict."""
    init_git_repo(tmp_path)
    _verdict_repo(tmp_path, monkeypatch, rows=_all_rows(tmp_path))

    _ok, lines = precommit.verdict(tmp_path)

    assert not [line for line in lines if line.startswith("NOTE")]


def test_main_summary_names_untracked_files(
    untracked_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The closing summary carries one combined line naming the untracked files."""
    monkeypatch.chdir(untracked_repo)
    (untracked_repo / "src" / "pkg" / "new.py").write_text(_GOOD_MODULE)

    rc = _drive_main(monkeypatch, lambda **_kw: [])

    assert rc == 0
    out = capsys.readouterr().out
    assert "1 untracked file(s) skipped by some steps (not checked by" in out
    assert "`src/pkg/new.py`" in out
    assert "add them if they belong to this work, leave them out if not" in out


def _commit_on_branch(repo: Path, message: str) -> None:
    """Stage everything and commit it on the current branch.

    Args:
        repo: Repo root.
        message: Commit message.
    """
    git_stdout(repo, "add", "-A")
    git_stdout(repo, "commit", "-q", "-m", message)


# Candidate files spanning every axis a step's selection filters on:
# source vs test roots, a root no step scans, the repo-wide exclude, an
# audit-excluded directory, a fragment, and a non-Python file.
_PARITY_FILES = (
    "src/pkg/m.py",
    "tests/test_m.py",
    "scripts/tool.py",
    "src/vendor/v.py",
    "src/build/b.py",
    "changelog.d/a.added.md",
    "notes.txt",
)


def _real_selection(repo: Path, step: str, scope: str) -> set[str]:
    """Return the files *step* actually selects on *repo*, via its own selector.

    Args:
        repo: Repo root, with the candidates committed on the branch.
        step: A step in ``_UNTRACKED_BLIND_SCOPES``.
        scope: The scope the step runs at.

    Returns:
        The selected candidate paths.
    """
    if step == "ruff":
        selected = config.select_diff_files(repo)
    elif step == "docstring_verification":
        selected = (
            config.tracked_files_under_roots(
                repo,
                config.resolve_tool_roots(
                    repo, "docstring_verification", include_tests=True
                ),
            )
            if scope == "all"
            else config.select_diff_files(repo, apply_exclude=True)
        )
    elif step == "test_naming_check":
        selected = verify_test_naming._resolve_test_files(repo, None, scope)
    elif step == "typecheck":
        selected = config.select_diff_files(
            repo, roots=config.resolve_tool_roots(repo, "typecheck")
        )
    elif step == "layering":
        # The audit weighs graph modules (walked from its roots) that the
        # diff touches.
        layering_roots = config.resolve_tool_roots(repo, "layering")
        roots = [(repo / r).resolve() for r in layering_roots]
        modules = {
            p.relative_to(repo).as_posix()
            for p in audit_common.iter_files(audit_common.Scope.FULL, roots)
        }
        selected = [f for f in config.select_diff_files(repo) if f in modules]
    else:  # changelog_updated: fragments in the commit view
        selected = git_utils.get_modified_files(
            suffix="",
            prefix="changelog.d/",
            repo_root=repo,
            include_unstaged=False,
        )
    return set(selected) & set(_PARITY_FILES)


@pytest.mark.parametrize(
    ("step", "scope"),
    [
        (step, scope)
        for step, (scopes, _verb) in precommit._UNTRACKED_BLIND_SCOPES.items()
        for scope in sorted(scopes)
    ],
)
def test_untracked_note_selects_what_the_step_selects_once_tracked(
    tmp_path: Path, step: str, scope: str
) -> None:
    """The note's file choice equals the step's real selection, per step.

    The same candidates are first untracked (what the note names), then
    committed on the branch (what the step's own selector picks). Any
    drift between ``_untracked_in_reach`` and a step's selector fails
    here instead of producing a note that names the wrong files.

    Args:
        step: The step under comparison.
        scope: The scope it runs at.
    """
    init_git_repo(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        '[tool.forge]\nsource_dirs = ["src"]\ntest_dirs = ["tests"]\n'
        'exclude = ["src/vendor"]\n'
    )
    # ruff's own exclusion: fix-forge-ruff still names the file to ruff,
    # which checks a named file anyway, so it must stay in both selections.
    (tmp_path / "ruff.toml").write_text('extend-exclude = ["scripts"]\n')
    for rel in ("src/pkg/__init__.py", "tests/conftest.py"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("")
    commit_all(tmp_path, "seed")
    git_stdout(tmp_path, "checkout", "-q", "-b", "feat/x")
    for rel in _PARITY_FILES:
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        body = "bump: patch\n\n- x\n" if rel.endswith(".md") else ""
        (tmp_path / rel).write_text(body)

    reach = set(
        precommit._untracked_in_reach(tmp_path, step, scope, list(_PARITY_FILES))
    )
    _commit_on_branch(tmp_path, "candidates")

    assert reach == _real_selection(tmp_path, step, scope)
    assert reach, "the candidates must exercise the selector"


def test_relevant_untracked_files_skips_steps_that_would_self_skip(
    untracked_repo: Path,
) -> None:
    """An enabled step that would skip is never named as passing a file over.

    Layering without layers and the changelog check without a CHANGELOG
    both skip before selecting anything, so naming them would claim a
    selection neither made.
    """
    with (untracked_repo / "pyproject.toml").open("a") as fh:
        fh.write(
            '\n[tool.forge.precommit]\nenable = ["layering", "changelog_updated"]\n'
            '\n[tool.forge.changelog]\nmode = "fragments"\n'
        )
    (untracked_repo / "src" / "pkg" / "new.py").write_text(_GOOD_MODULE)
    (untracked_repo / "changelog.d").mkdir()
    (untracked_repo / "changelog.d" / "a.added.md").write_text("bump: patch\n\n- x\n")

    listed = dict(precommit.relevant_untracked_files(untracked_repo))

    assert "layering" not in listed["src/pkg/new.py"]
    assert "changelog.d/a.added.md" not in listed


def test_per_step_line_says_not_counted_for_the_changelog_check() -> None:
    """The changelog check validates fragments on disk, so it says "not counted"."""
    result = precommit.StepResult(name="changelog_updated", passed=False, output="x")
    precommit._note_untracked_in_output(
        result, [("changelog.d/a.added.md", ("changelog_updated",))]
    )
    assert result.output.endswith(
        "NOTE: 1 untracked file(s) not counted by this step: `changelog.d/a.added.md`\n"
    )
