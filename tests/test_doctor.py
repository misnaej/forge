"""Tests for forge-doctor diagnostic CLI."""

# MOCKING STRATEGY: forge-doctor only probes the environment; every probe is
# monkeypatched so no real tools, subprocesses, or cwd are touched.
#   - shutil.which: stub which CLIs resolve on PATH.
#   - subprocess.run (via make_fake_run): stub gh / version probes.
#   - Path.cwd: pin the working directory.

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import patch

from forge import doctor, precommit
from tests.conftest import make_fake_run


if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_check_clis_returns_one_result_per_expected_cli() -> None:
    """All expected CLIs produce a CheckResult."""
    results = doctor._check_clis()
    names = {r.name for r in results}
    assert names == {f"cli:{c}" for c in doctor._expected_clis()}


def test_expected_clis_derives_from_installed_metadata() -> None:
    """The expected list is derived from forge-scripts' entry points.

    Regression guard: if a new CLI is added to ``pyproject.toml`` but
    ``EXPECTED_CLIS`` was missing it (the old hand-maintained list), the
    doctor check would silently stop covering it. The current
    implementation derives via ``importlib.metadata`` so this can't
    drift.
    """
    clis = doctor._expected_clis()
    assert "forge-doctor" in clis
    assert "forge-precommit" in clis
    assert "install-forge-githooks" in clis
    assert "install-forge-claude-md" in clis


def test_check_clis_pass_when_all_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """When shutil.which finds every CLI, all checks pass."""
    monkeypatch.setattr(doctor.shutil, "which", lambda _name: "/usr/bin/found")
    results = doctor._check_clis()
    assert all(r.passed for r in results)


def test_check_clis_fail_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """When shutil.which returns None, checks fail."""
    monkeypatch.setattr(doctor.shutil, "which", lambda _name: None)
    results = doctor._check_clis()
    assert all(not r.passed for r in results)
    assert all("not found" in r.detail for r in results)


def test_check_gh_missing_short_circuits(monkeypatch: pytest.MonkeyPatch) -> None:
    """If gh is missing, both checks fail without running gh."""
    monkeypatch.setattr(doctor.shutil, "which", lambda _name: None)
    results = doctor._check_gh()
    assert len(results) == 2
    assert not results[0].passed
    assert not results[1].passed
    assert "skipped" in results[1].detail


def test_check_gh_authenticated(monkeypatch: pytest.MonkeyPatch) -> None:
    """When gh auth status returns 0, the auth check passes."""
    monkeypatch.setattr(doctor.shutil, "which", lambda _name: "/usr/bin/gh")
    monkeypatch.setattr(
        doctor.subprocess,
        "run",
        make_fake_run(returncode=0),
    )
    results = doctor._check_gh()
    assert results[0].passed
    assert results[1].passed


def test_read_json_missing_file(tmp_path: Path) -> None:
    """Missing manifest produces an error string."""
    data, err = doctor._read_json(tmp_path / "nope.json")
    assert data == {}
    assert err is not None
    assert "missing" in err


def test_read_json_invalid(tmp_path: Path) -> None:
    """Invalid JSON produces an error string."""
    bad = tmp_path / "bad.json"
    bad.write_text("{not-json")
    data, err = doctor._read_json(bad)
    assert data == {}
    assert err is not None
    assert "invalid JSON" in err


def test_read_json_valid(tmp_path: Path) -> None:
    """Valid JSON loads cleanly with no error."""
    good = tmp_path / "good.json"
    good.write_text('{"name": "forge"}')
    data, err = doctor._read_json(good)
    assert err is None
    assert data == {"name": "forge"}


def test_main_emits_json(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--json flag produces parseable JSON output."""
    monkeypatch.setattr(doctor.shutil, "which", lambda _name: None)
    with patch.object(doctor.sys, "argv", ["forge-doctor", "--json"]):
        rc = doctor.main()
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert isinstance(payload, list)
    assert all("name" in entry and "passed" in entry for entry in payload)
    assert rc != 0  # all checks fail when CLIs aren't present


def test_main_skip_plugin_checks_omits_plugin_results(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--skip-plugin-checks drops every plugin:* / plugin.json / plugin/* check."""
    monkeypatch.setattr(doctor.shutil, "which", lambda _name: "/usr/bin/found")
    monkeypatch.setattr(
        doctor.subprocess,
        "run",
        make_fake_run(returncode=0),
    )
    argv = ["forge-doctor", "--json", "--skip-plugin-checks"]
    with patch.object(doctor.sys, "argv", argv):
        doctor.main()
    payload = json.loads(capsys.readouterr().out)
    names = [entry["name"] for entry in payload]
    plugin_related = [
        n for n in names if n.startswith(("plugin:", "plugin.", "plugin/"))
    ]
    assert plugin_related == []


def test_under_used_surfaces_missing_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When CLIs are installed but artifacts absent, advisory checks fire."""
    monkeypatch.setattr(doctor.shutil, "which", lambda _name: "/usr/bin/found")
    results = doctor._check_under_used_capabilities(tmp_path)
    names = {r.name for r in results}
    assert names == {
        "underused:install-forge-githooks",
        "underused:install-forge-claude-md",
        "underused:forge-gen-api-digest",
        "underused:forge-gen-cli-reference",
        "underused:forge-audit-deps",
    }
    assert all(r.info for r in results)
    assert all(r.passed for r in results)  # info-only, never fails


def test_under_used_silent_when_artifacts_present(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When every artifact exists, no advisory results are returned."""
    monkeypatch.setattr(doctor.shutil, "which", lambda _name: "/usr/bin/found")
    for _cli, relpath, _rec in doctor._UNDERUSED_ARTIFACTS:
        artifact = tmp_path / relpath
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text("present")
    results = doctor._check_under_used_capabilities(tmp_path)
    assert results == []


def test_under_used_skipped_when_cli_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A CLI that isn't on PATH is "absent", not "under-used" — no result."""
    monkeypatch.setattr(doctor.shutil, "which", lambda _name: None)
    results = doctor._check_under_used_capabilities(tmp_path)
    assert results == []


def test_info_results_do_not_affect_exit_code(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When only `info` checks "fail", forge-doctor still exits 0."""
    monkeypatch.setattr(doctor.shutil, "which", lambda _name: "/usr/bin/found")
    monkeypatch.setattr(doctor.subprocess, "run", make_fake_run(returncode=0))
    monkeypatch.setattr(doctor.Path, "cwd", classmethod(lambda _: tmp_path))
    argv = ["forge-doctor", "--skip-plugin-checks"]
    with patch.object(doctor.sys, "argv", argv):
        rc = doctor.main()
    captured = capsys.readouterr().out
    assert "[i]" in captured  # advisory marker rendered
    assert rc == 0


def test_check_step_tools_flags_missing_tool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An enabled step whose tool is absent produces a failing result.

    MOCK SETUP: pyproject enables the typecheck step; shutil.which reports
    pyrefly missing.
    """
    (tmp_path / "pyproject.toml").write_text(
        '[tool.forge.precommit]\nenable = ["typecheck"]\n', encoding="utf-8"
    )
    monkeypatch.setattr(doctor.shutil, "which", lambda _name: None)
    results = doctor._check_step_tools(tmp_path)
    assert len(results) == 1
    assert results[0].name == "step-tool:typecheck"
    assert not results[0].passed
    assert "pyrefly" in results[0].detail


def test_check_step_tools_passes_when_tool_present(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An enabled step whose tool is on PATH passes."""
    (tmp_path / "pyproject.toml").write_text(
        '[tool.forge.precommit]\nenable = ["typecheck"]\n', encoding="utf-8"
    )
    monkeypatch.setattr(doctor.shutil, "which", lambda name: f"/usr/bin/{name}")
    results = doctor._check_step_tools(tmp_path)
    assert len(results) == 1
    assert results[0].passed


def test_check_step_tools_empty_when_no_step_enabled(tmp_path: Path) -> None:
    """No [tool.forge.precommit] enable list → nothing to check."""
    (tmp_path / "pyproject.toml").write_text("[tool.forge]\n", encoding="utf-8")
    assert doctor._check_step_tools(tmp_path) == []


def test_step_tools_keys_are_opt_in_steps() -> None:
    """Every _STEP_TOOLS key is a real opt-in (default-off) pre-commit step.

    Drift guard: forge.precommit owns the step registry; doctor's
    step→tool map must reference only steps that exist and are opt-in
    (a default-on step always runs and is covered elsewhere).
    """
    opt_in = {d.name for d in precommit._STEP_REGISTRY if not d.default_on}
    assert set(doctor._STEP_TOOLS).issubset(opt_in)
