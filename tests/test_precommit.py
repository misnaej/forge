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
import re
import shutil
import subprocess
from typing import TYPE_CHECKING, NamedTuple
from unittest.mock import patch

import pytest

from forge import config, git_utils, precommit
from forge.pip_audit_json import AuditRun
from forge.smart_test import lifecycle as _lifecycle
from tests.conftest import GIT_ENV, _detach_head, init_git_repo, init_single_track_repo


if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


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


# ---------------------------------------------------------------------------
# Null Objects — reused across env_sync test groups
# ---------------------------------------------------------------------------


class FakeEP(NamedTuple):
    """Null-object entry point for installed_console_scripts tests.

    Attributes:
        name: Entry-point name (e.g. ``"mycli"``).
        group: Entry-point group (e.g. ``"console_scripts"``).
    """

    name: str
    group: str


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


def test_step_changelog_history_shells_out_to_verify_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """step_changelog_history always shells out; the CLI owns the skip decision."""
    monkeypatch.setattr(
        shutil,
        "which",
        lambda _name: "/usr/bin/verify-forge-changelog-history",
    )
    calls: list[list[str]] = []

    def _fake_run(cmd: list[str], **_kwargs: object) -> tuple[bool, str]:
        calls.append(cmd)
        return True, "ok"

    monkeypatch.setattr(precommit, "_run", _fake_run)
    result = precommit.step_changelog_history(tmp_path)
    assert result.passed
    assert calls
    assert calls[0] == ["verify-forge-changelog-history"]


def test_step_changelog_history_marks_skipped_from_cli_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the CLI reports it skipped, the StepResult mirrors that."""
    monkeypatch.setattr(
        shutil,
        "which",
        lambda _name: "/usr/bin/verify-forge-changelog-history",
    )
    monkeypatch.setattr(
        precommit,
        "_run",
        lambda *_a, **_kw: (
            True,
            "(origin/main is not an ancestor of HEAD — skipped)\n",
        ),
    )
    result = precommit.step_changelog_history(tmp_path)
    assert result.passed
    assert result.skipped


def test_step_changelog_history_hard_fails_when_cli_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing verify-forge-changelog-history is a loud SystemExit (FOUNDATION §2)."""
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    with pytest.raises(SystemExit):
        precommit.step_changelog_history(tmp_path)


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


def test_release_guard_failure_names_both_cures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The two-minor-gap failure names both the dev-tag cure and the re-slot cure.

    See #405.
    """
    _setup_release_guard(
        tmp_path, monkeypatch, plugin_version="1.26.0", latest_tag="v1.24.1"
    )
    result = precommit.step_release_tag_guard(tmp_path)
    assert "forge-next-prep --tag" in result.output
    assert "re-slot" in result.output


def test_release_guard_failure_names_dev_branch_cause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both causes — untagged dev release and a feature branch's stale slot.

    See #405.
    """
    _setup_release_guard(
        tmp_path, monkeypatch, plugin_version="1.26.0", latest_tag="v1.24.1"
    )
    result = precommit.step_release_tag_guard(tmp_path)
    assert "dev" in result.output
    assert "feature branch" in result.output


def test_release_guard_failure_has_agents_directive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The failure explicitly forbids an agent from self-clearing via a release action.

    See #405.
    """
    _setup_release_guard(
        tmp_path, monkeypatch, plugin_version="1.26.0", latest_tag="v1.24.1"
    )
    result = precommit.step_release_tag_guard(tmp_path)
    assert "AGENTS:" in result.output
    assert "report only" in result.output
    assert "never run" in result.output


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
    log_dir = tmp_path / "code_health"
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
    EXPECTED BEHAVIOR: `code_health/precommit_timing.log`'s contents
    equal `precommit._format_timing_log(results)` for the results
    `run_all` actually returned.
    """
    _stub_env_sync_skipped(monkeypatch)
    _stub_docstrings_passing(monkeypatch)
    _stub_test_naming_passing(monkeypatch)
    _stub_repo_structure_passing(monkeypatch)
    _stub_pip_audit_skipped(monkeypatch)
    _stub_docstring_coverage_skipped(monkeypatch)
    results = precommit.run_all(repo_root=tmp_path, print_progress=False)

    log_path = tmp_path / "code_health" / "precommit_timing.log"
    assert log_path.read_text(encoding="utf-8").rstrip(
        "\n"
    ) == precommit._format_timing_log(results).rstrip("\n")


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
    assert "api_digest_check" not in names


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
    the CLI resolves on PATH; ``_run`` captures the command and returns a
    passing canned result.
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
    assert captured["cmd"] == ["forge-gen-api-digest", "--check"]
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


def test_step_api_digest_check_missing_cli_exits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An opted-in-but-absent `forge-gen-api-digest` binary fails loudly."""
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "api-digest.md").write_text("# API Digest\n", encoding="utf-8")
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    with pytest.raises(SystemExit):
        precommit.step_api_digest_check(tmp_path)


# ---------------------------------------------------------------------------
# step_cli_reference_check
# ---------------------------------------------------------------------------


def test_step_cli_reference_check_passes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runs `forge-gen-cli-reference --check` and mirrors a passing exit.

    MOCK SETUP: `docs/cli-reference.md` present (so the step doesn't skip);
    the CLI resolves on PATH; ``_run`` captures the command and returns a
    passing canned result.
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
    assert captured["cmd"] == ["forge-gen-cli-reference", "--check"]
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


def test_step_cli_reference_check_missing_cli_exits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An opted-in-but-absent `forge-gen-cli-reference` binary fails loudly."""
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "cli-reference.md").write_text(
        "# CLI Reference\n", encoding="utf-8"
    )
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    with pytest.raises(SystemExit):
        precommit.step_cli_reference_check(tmp_path)


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


# installed_console_scripts itself now lives in forge.config — see
# tests/test_config.py for its unit tests. FakeEP/FakeDist stay here
# because the step_env_sync integration tests below still patch
# precommit.importlib.metadata.distribution directly (the real singleton
# module object config.installed_console_scripts also reads from).


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


def test_step_env_sync_missing_script_beats_pin_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A blocking missing entry point wins over a forge-scripts pin WARN.

    SCENARIO: the repo both has a missing declared script AND pins
    forge-scripts ahead of the install. The blocking entry-point failure
    must take priority over the non-blocking pin advisory.
    MOCK SETUP: is_non_interactive→False; pyproject declares mypkg with two
    scripts + forge-scripts==2.9.0; only one script installed; forge-scripts
    version→2.8.0.
    EXPECTED BEHAVIOR: passed False, blocking (non_blocking False), the
    ⛔ stale-install message — NOT the pin WARN.
    """
    monkeypatch.setattr(precommit, "is_non_interactive", lambda: False)
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
    """auto_rebuild is first and env_sync is second in the default resolved sequence.

    auto_rebuild heals a stale editable install before env_sync can block on
    it — the ordering is the design contract, not an implementation detail.
    """
    resolved = precommit._resolve_steps(tmp_path)
    names = [d.name for d in resolved]
    assert names[0] == "auto_rebuild"
    assert names[1] == "env_sync"


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
# MOCKING STRATEGY: shutil.which → controls require_cli (FOUNDATION §2 loud
# fail vs. pass); precommit._run → controls generator success/failure without
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
    """Both generated docs present → both CLI names appear in captured _run calls.

    SCENARIO: docs/api-digest.md and docs/cli-reference.md both exist.
    MOCK SETUP: shutil.which → valid path; precommit._run captures each argv
        and returns (True, ""); stage_modified_paths → [].
    EXPECTED BEHAVIOR: "forge-gen-api-digest" and "forge-gen-cli-reference"
        both appear as the first token of a captured _run invocation.
    """
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "api-digest.md").write_text("old\n")
    (docs_dir / "cli-reference.md").write_text("old\n")
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/x")
    run_cmds: list[list[str]] = []

    def _capture_run(cmd: list[str], **_kw: object) -> tuple[bool, str]:
        run_cmds.append(cmd)
        return True, ""

    monkeypatch.setattr(precommit, "_run", _capture_run)
    monkeypatch.setattr(precommit, "stage_modified_paths", lambda *_a: [])
    precommit.step_regen_docs(tmp_path)
    invoked_clis = [cmd[0] for cmd in run_cmds]
    assert "forge-gen-api-digest" in invoked_clis
    assert "forge-gen-cli-reference" in invoked_clis


def test_step_regen_docs_cli_missing_exits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing forge-gen-* CLI raises SystemExit(2) (FOUNDATION §2 loud-fail)."""
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "api-digest.md").write_text("old\n")
    monkeypatch.setattr(shutil, "which", lambda _name: None)
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


def test_step_changelog_version_skips_dual_track(tmp_path: Path) -> None:
    """Dual-track repo (dev_branch != base_branch) → skip."""
    (tmp_path / "CHANGELOG.md").write_text("## v1.0.0\n")
    (tmp_path / "pyproject.toml").write_text('[tool.forge]\ndev_branch = "dev"\n')
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
# _release_merge_context + run_all era-gap promotion suppression
# ---------------------------------------------------------------------------


def _stage_release_promotion_merge(
    repo: Path, tag: str, *, extra_change: str | None = None
) -> Path:
    """Build a repo mid-merge on ``release/<tag>``, staged tree reproducing *tag*.

    Mirrors the ``/promote`` flow (docs/release-process.md §5): a trunk
    branch carries the real content and is tagged; a ``release/<tag>``
    branch forked from the earlier, empty ``main`` merges the tag in via
    ``git merge --no-ff --no-commit`` — a clean, conflict-free merge since
    ``main`` never touched the tagged paths — leaving ``MERGE_HEAD``
    present and the staged index (``git write-tree``) byte-identical to
    the tagged tree, the state :func:`forge.precommit._release_merge_context`
    must recognize.

    Args:
        repo: Directory to initialize the repo in.
        tag: Release tag to create and merge (e.g. ``"v1.0.0"``); also
            names the ``release/<tag>`` branch.
        extra_change: When given, appends and re-stages an edit to this
            path (``"CHANGELOG.md"`` or ``"a.py"``) on top of the merge —
            simulates content drift beyond the tagged tree.

    Returns:
        The repo path (for call-site chaining).
    """
    init_git_repo(repo)
    subprocess.run(
        ["git", "checkout", "-q", "-b", "dev"], cwd=repo, env=GIT_ENV, check=True
    )
    (repo / "a.py").write_text("x = 1\n")
    (repo / "CHANGELOG.md").write_text(f"## {tag}\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, env=GIT_ENV, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "trunk commits"],
        cwd=repo,
        env=GIT_ENV,
        check=True,
    )
    subprocess.run(
        ["git", "tag", "-a", tag, "-m", tag], cwd=repo, env=GIT_ENV, check=True
    )
    subprocess.run(["git", "checkout", "-q", "main"], cwd=repo, env=GIT_ENV, check=True)
    subprocess.run(
        ["git", "checkout", "-q", "-b", f"release/{tag}"],
        cwd=repo,
        env=GIT_ENV,
        check=True,
    )
    subprocess.run(
        ["git", "merge", "--no-ff", "--no-commit", tag],
        cwd=repo,
        env=GIT_ENV,
        check=True,
    )
    if extra_change is not None:
        target = repo / extra_change
        target.write_text(target.read_text() + "extra change\n")
        subprocess.run(["git", "add", extra_change], cwd=repo, env=GIT_ENV, check=True)
    return repo


def _stage_release_promotion_conflict(repo: Path, tag: str) -> Path:
    """Build a repo mid-merge on ``release/<tag>`` with an unresolved conflict.

    Both ``main`` (before ``release/<tag>`` forks from it) and the tagged
    trunk commit add ``a.py`` with different content, so merging the tag
    into the release branch produces a genuine "both added" conflict and
    leaves the index unresolved — the one state
    :func:`_stage_release_promotion_merge` cannot produce, since its merge
    is always conflict-free.

    Args:
        repo: Directory to initialize the repo in.
        tag: Release tag to create and merge (e.g. ``"v1.0.0"``); also
            names the ``release/<tag>`` branch.

    Returns:
        The repo path.
    """
    init_git_repo(repo)
    subprocess.run(
        ["git", "checkout", "-q", "-b", "dev"], cwd=repo, env=GIT_ENV, check=True
    )
    (repo / "a.py").write_text("dev version\n")
    subprocess.run(["git", "add", "a.py"], cwd=repo, env=GIT_ENV, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "trunk commits"],
        cwd=repo,
        env=GIT_ENV,
        check=True,
    )
    subprocess.run(
        ["git", "tag", "-a", tag, "-m", tag], cwd=repo, env=GIT_ENV, check=True
    )
    subprocess.run(["git", "checkout", "-q", "main"], cwd=repo, env=GIT_ENV, check=True)
    (repo / "a.py").write_text("main version\n")
    subprocess.run(["git", "add", "a.py"], cwd=repo, env=GIT_ENV, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "main edit"], cwd=repo, env=GIT_ENV, check=True
    )
    subprocess.run(
        ["git", "checkout", "-q", "-b", f"release/{tag}"],
        cwd=repo,
        env=GIT_ENV,
        check=True,
    )
    result = subprocess.run(
        ["git", "merge", "--no-ff", "--no-commit", tag],
        cwd=repo,
        env=GIT_ENV,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    return repo


def test_release_merge_context_returns_tag_when_all_conditions_hold(
    tmp_path: Path,
) -> None:
    """All three conditions hold → the merge context resolves to the promoted tag."""
    repo = _stage_release_promotion_merge(tmp_path, "v1.0.0")
    assert precommit._release_merge_context(repo) == "v1.0.0"


def test_release_merge_context_none_when_no_merge_in_progress(tmp_path: Path) -> None:
    """On the release branch with a matching tree but no ``MERGE_HEAD`` → ``None``."""
    repo = _stage_release_promotion_merge(tmp_path, "v1.0.0")
    subprocess.run(
        ["git", "commit", "-q", "--no-edit"], cwd=repo, env=GIT_ENV, check=True
    )
    assert precommit._release_merge_context(repo) is None


def test_release_merge_context_none_when_branch_name_not_release_pattern(
    tmp_path: Path,
) -> None:
    """Mid-merge on a branch not matching ``release/vX.Y.Z`` → ``None``.

    SETUP: reuses :func:`_stage_release_promotion_merge`'s mid-merge
    state, then renames the current branch away from the release pattern —
    ``git branch -m`` only rewrites the branch ref, so ``MERGE_HEAD`` (a
    separate file) survives the rename untouched.
    """
    repo = _stage_release_promotion_merge(tmp_path, "v1.0.0")
    subprocess.run(
        ["git", "branch", "-m", "release/v1.0.0", "promote-v1.0.0"],
        cwd=repo,
        env=GIT_ENV,
        check=True,
    )
    assert precommit.merge_in_progress(repo) is True
    assert precommit._release_merge_context(repo) is None


def test_release_merge_context_none_when_staged_tree_diverges_beyond_changelog(
    tmp_path: Path,
) -> None:
    """Extra staged edit beyond CHANGELOG.md breaks the fingerprint match."""
    repo = _stage_release_promotion_merge(tmp_path, "v1.0.0", extra_change="a.py")
    assert precommit._release_merge_context(repo) is None


def test_release_merge_context_still_matches_when_only_changelog_diverges(
    tmp_path: Path,
) -> None:
    """A staged ``CHANGELOG.md``-only edit is tolerated — the tag still matches.

    ``CHANGELOG.md`` is excluded from
    :func:`forge.git_utils.release_tree_fingerprint` by design (the
    curated-CHANGELOG divergence every real promotion carries).
    """
    repo = _stage_release_promotion_merge(
        tmp_path, "v1.0.0", extra_change="CHANGELOG.md"
    )
    assert precommit._release_merge_context(repo) == "v1.0.0"


def test_release_merge_context_none_when_write_tree_is_none(tmp_path: Path) -> None:
    """Unresolved merge conflict during release merge fails write_tree."""
    repo = _stage_release_promotion_conflict(tmp_path, "v1.0.0")
    assert precommit._release_merge_context(repo) is None


def test_release_merge_context_none_when_tag_does_not_exist(tmp_path: Path) -> None:
    """A ``release/vX.Y.Z``-named branch mid-merge, but the tag was never created.

    ``release_tree_fingerprint(repo, tag)`` resolves nothing for a
    nonexistent tag, so the fingerprint lookup itself fails closed to
    ``None`` regardless of the merge's actual state.
    """
    init_git_repo(tmp_path)
    subprocess.run(
        ["git", "checkout", "-q", "-b", "other"], cwd=tmp_path, env=GIT_ENV, check=True
    )
    (tmp_path / "other.txt").write_text("other\n")
    subprocess.run(["git", "add", "other.txt"], cwd=tmp_path, env=GIT_ENV, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "other work"],
        cwd=tmp_path,
        env=GIT_ENV,
        check=True,
    )
    subprocess.run(
        ["git", "checkout", "-q", "main"], cwd=tmp_path, env=GIT_ENV, check=True
    )
    subprocess.run(
        ["git", "checkout", "-q", "-b", "release/v9.9.9"],
        cwd=tmp_path,
        env=GIT_ENV,
        check=True,
    )
    subprocess.run(
        ["git", "merge", "--no-ff", "--no-commit", "other"],
        cwd=tmp_path,
        env=GIT_ENV,
        check=True,
    )
    assert precommit._release_merge_context(tmp_path) is None


def _fake_step(name: str, called: list[str]) -> Callable[[Path], precommit.StepResult]:
    """Return a step fn that records its invocation and returns a canned pass.

    Fake (not ``Mock``) closure over *called* — the run_all suppression
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


def test_run_all_suppresses_tree_content_steps_during_release_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``tree_content`` step is skipped without running during a promotion merge.

    SCENARIO: the merge commit of a ``release/vX.Y.Z`` promotion branch
    runs pre-commit with its staged tree release-locked to the tag.
    MOCK SETUP: ``step_ruff`` (``tree_content=True``) and
    ``step_plugin_version`` (``tree_content=False``) are replaced by Fake
    closures recording their calls, on a repo mid a promotion merge whose
    staged tree reproduces ``v1.0.0``.
    EXPECTED BEHAVIOR: the ruff fake is never called and its result is a
    synthesized skip naming the release-locked tag; the plugin_version
    fake runs normally and its result is unmodified.
    """
    repo = _stage_release_promotion_merge(tmp_path, "v1.0.0")
    called: list[str] = []
    monkeypatch.setattr(precommit, "step_ruff", _fake_step("ruff", called))
    monkeypatch.setattr(
        precommit, "step_plugin_version", _fake_step("plugin_version", called)
    )
    results = precommit.run_all(
        repo_root=repo, only=["ruff", "plugin_version"], print_progress=False
    )
    assert "ruff" not in called
    assert "plugin_version" in called
    by_name = {r.name: r for r in results}
    ruff_result = by_name["ruff"]
    assert ruff_result.skipped is True
    assert ruff_result.passed is True
    assert "release-locked tree, content check skipped" in ruff_result.output
    assert "v1.0.0" in ruff_result.output
    assert by_name["plugin_version"].output == "ran"


def test_run_all_no_suppression_when_tree_diverges_beyond_changelog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A staged edit beyond ``CHANGELOG.md`` must not suppress content gates.

    SAFETY-INVARIANT TEST (docs/release-process.md §4): during a promotion
    merge, an auto-fixer poisoning the release fingerprint must not slip past
    unchecked.
    """
    repo = _stage_release_promotion_merge(tmp_path, "v1.0.0", extra_change="a.py")
    called: list[str] = []
    monkeypatch.setattr(precommit, "step_ruff", _fake_step("ruff", called))
    precommit.run_all(repo_root=repo, only=["ruff"], print_progress=False)
    assert "ruff" in called


def test_run_all_still_suppresses_when_only_changelog_diverges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``CHANGELOG.md``-only staged edit still engages suppression."""
    repo = _stage_release_promotion_merge(
        tmp_path, "v1.0.0", extra_change="CHANGELOG.md"
    )
    called: list[str] = []
    monkeypatch.setattr(precommit, "step_ruff", _fake_step("ruff", called))
    results = precommit.run_all(repo_root=repo, only=["ruff"], print_progress=False)
    assert "ruff" not in called
    assert results[0].skipped is True


def test_run_all_no_suppression_outside_a_promotion_merge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A normal repo with no merge in progress never engages suppression."""
    init_git_repo(tmp_path)
    called: list[str] = []
    monkeypatch.setattr(precommit, "step_ruff", _fake_step("ruff", called))
    precommit.run_all(repo_root=tmp_path, only=["ruff"], print_progress=False)
    assert "ruff" in called


def test_step_registry_tree_content_is_bool_for_every_entry() -> None:
    """Every ``StepDef.tree_content`` is an explicit bool — no default drift."""
    assert all(isinstance(d.tree_content, bool) for d in precommit._STEP_REGISTRY)
