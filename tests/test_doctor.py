"""Tests for forge-doctor diagnostic CLI."""

# MOCKING STRATEGY: forge-doctor only probes the environment; every probe is
# monkeypatched so no real tools, subprocesses, or cwd are touched.
#   - shutil.which: stub which CLIs resolve on PATH.
#   - subprocess.run (via make_fake_run): stub gh / version probes.
#   - Path.cwd: pin the working directory.

from __future__ import annotations

import argparse
import json
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from forge import doctor, precommit
from tests.conftest import make_fake_run


if TYPE_CHECKING:
    from pathlib import Path


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


# --- _validate_plugin_name() (#200) -----------------------------------------


def test_validate_plugin_name_accepts_bare_names() -> None:
    """A bare identifier matching the safe charset passes through unchanged.

    ``a..b`` is included deliberately: a ``..`` *substring* is a harmless
    literal directory name (only an exact ``..`` component escapes, and that
    is barred by the leading-alnum rule), so it must not be over-rejected.
    """
    for name in ["forge", "myrepo", "my.plugin", "a_b-c", "a..b"]:
        assert doctor._validate_plugin_name(name) == name


def test_validate_plugin_name_rejects_traversal_and_separators() -> None:
    r"""Path separators, bare '..', non-alnum leads, and trailing newlines raise.

    The trailing-newline case (``"forge\\n"``) locks in the ``\\Z`` anchor —
    a bare ``$`` would admit it (Python ``$`` matches before a final newline).
    """
    for name in ["../../etc", "a/b", "..", ".hidden", "-x", "", "foo/..", "forge\n"]:
        with pytest.raises(argparse.ArgumentTypeError):
            doctor._validate_plugin_name(name)


def test_doctor_cli_rejects_unsafe_plugin_name() -> None:
    """A traversal --plugin-name fails argparse's type= validator at parse time.

    argparse exits with code 2 (usage error) when a ``type=`` callable
    raises ``ArgumentTypeError``, before any check runs (#200).
    """
    argv = ["forge-doctor", "--plugin-name", "../../etc"]
    with (
        patch.object(doctor.sys, "argv", argv),
        pytest.raises(SystemExit) as exc_info,
    ):
        doctor.main()
    assert exc_info.value.code == 2


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


# --- _surface_*() readers (#184) -------------------------------------------


def test_surface_pip_version_reads_installed_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Returns the version string reported by importlib.metadata."""
    monkeypatch.setattr(doctor.metadata, "version", lambda _dist: "2.23.1")
    assert doctor._surface_pip_version() == "2.23.1"


def test_surface_pip_version_none_when_not_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Returns None when the forge-scripts distribution isn't installed."""

    def _raise(_dist: str) -> str:
        raise doctor.metadata.PackageNotFoundError

    monkeypatch.setattr(doctor.metadata, "version", _raise)
    assert doctor._surface_pip_version() is None


def test_surface_hook_version_none_when_sidecar_absent(tmp_path: Path) -> None:
    """No .githooks/.forge-hook-version sidecar → None, not an error."""
    assert doctor._surface_hook_version(tmp_path) is None


def test_surface_hook_version_reads_stripped_sidecar(tmp_path: Path) -> None:
    """The sidecar's version string is returned with trailing whitespace stripped."""
    githooks = tmp_path / ".githooks"
    githooks.mkdir()
    (githooks / doctor._HOOK_VERSION_SIDECAR).write_text("2.23.1\n", encoding="utf-8")
    assert doctor._surface_hook_version(tmp_path) == "2.23.1"


def test_surface_hook_version_none_when_sidecar_empty(tmp_path: Path) -> None:
    """An empty (or whitespace-only) sidecar counts as absent."""
    githooks = tmp_path / ".githooks"
    githooks.mkdir()
    (githooks / doctor._HOOK_VERSION_SIDECAR).write_text("   \n", encoding="utf-8")
    assert doctor._surface_hook_version(tmp_path) is None


def test_surface_plugin_version_none_when_plugin_root_none() -> None:
    """No cached plugin install → None, short-circuiting before any lookup."""
    assert doctor._surface_plugin_version(None) is None


def test_surface_plugin_version_reads_plugin_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prefers the plugin.json "version" field over the install dir name."""
    install_dir = tmp_path / "forge" / "2.23.1"
    (install_dir / ".claude-plugin").mkdir(parents=True)
    (install_dir / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"version": "2.23.1"}), encoding="utf-8"
    )
    monkeypatch.setattr(doctor, "_find_install_dir", lambda _root: install_dir)
    assert doctor._surface_plugin_version(tmp_path) == "2.23.1"


def test_surface_plugin_version_falls_back_to_dir_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When plugin.json is missing/unversioned, the install dir name is used."""
    install_dir = tmp_path / "forge" / "2.23.1"
    install_dir.mkdir(parents=True)
    monkeypatch.setattr(doctor, "_find_install_dir", lambda _root: install_dir)
    assert doctor._surface_plugin_version(tmp_path) == "2.23.1"


def test_surface_plugin_version_none_when_no_install_found(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No recognisable install layout under plugin_root → None."""
    monkeypatch.setattr(doctor, "_find_install_dir", lambda _root: None)
    assert doctor._surface_plugin_version(tmp_path) is None


# --- _check_version_skew() (#184) -------------------------------------------


def test_version_skew_aligned_normalizes_dev_suffix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same X.Y.Z across all three surfaces reports aligned, dev-suffix and all.

    MOCK SETUP: pip reports an editable-install dev suffix
    (``2.23.1.dev2+gabc``), the hook sidecar and plugin.json report bare
    ``2.23.1`` — parse_semver normalizes all three to the same triple.
    """
    monkeypatch.setattr(doctor.metadata, "version", lambda _dist: "2.23.1.dev2+gabc")
    githooks = tmp_path / ".githooks"
    githooks.mkdir()
    (githooks / doctor._HOOK_VERSION_SIDECAR).write_text("2.23.1", encoding="utf-8")
    install_dir = tmp_path / "plugin" / "2.23.1"
    (install_dir / ".claude-plugin").mkdir(parents=True)
    (install_dir / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"version": "2.23.1"}), encoding="utf-8"
    )
    monkeypatch.setattr(doctor, "_find_install_dir", lambda _root: install_dir)

    results = doctor._check_version_skew(tmp_path, tmp_path)

    assert len(results) == 1
    assert results[0].name == "version_skew"
    assert results[0].passed
    assert not results[0].info
    assert "aligned at v2.23.1" in results[0].detail


def test_version_skew_flags_lagging_surface_as_advisory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lagging plugin cache produces an advisory version_skew result.

    A lagging surface is always reported as advisory (``info=True``,
    ``passed=False``) — it carries the remediation but never sways the exit
    code, regardless of interactive vs. CI context.

    MOCK SETUP: pip + hooks report v2.23.1; the cached plugin.json reports
    the older v2.22.0.
    """
    monkeypatch.setattr(doctor.metadata, "version", lambda _dist: "2.23.1")
    githooks = tmp_path / ".githooks"
    githooks.mkdir()
    (githooks / doctor._HOOK_VERSION_SIDECAR).write_text("2.23.1", encoding="utf-8")
    install_dir = tmp_path / "plugin" / "2.22.0"
    (install_dir / ".claude-plugin").mkdir(parents=True)
    (install_dir / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"version": "2.22.0"}), encoding="utf-8"
    )
    monkeypatch.setattr(doctor, "_find_install_dir", lambda _root: install_dir)

    results = doctor._check_version_skew(tmp_path, tmp_path)

    assert len(results) == 1
    assert results[0].name == "version_skew:plugin_cache"
    assert not results[0].passed
    assert results[0].info  # advisory only — never sways the exit code
    assert "2.22.0" in results[0].detail
    assert "2.23.1" in results[0].detail
    assert "/plugin update forge@forge" in results[0].detail


def test_version_skew_below_two_surfaces_reports_nothing_to_compare(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Single surface present (pip only) returns info result."""
    monkeypatch.setattr(doctor.metadata, "version", lambda _dist: "2.23.1")

    results = doctor._check_version_skew(tmp_path, None)

    assert len(results) == 1
    assert results[0].name == "version_skew"
    assert results[0].passed
    assert results[0].info
    assert "pip package" in results[0].detail
    assert "nothing to compare" in results[0].detail


def test_version_skew_drops_unparseable_surface(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unparseable version is excluded from comparison.

    MOCK SETUP: pip + hooks report the same valid v2.23.1; the plugin
    install dir name is "garbage" (unparseable) with no plugin.json, so
    parse_semver returns None for it and it's dropped from comparison —
    the remaining two surfaces still align.
    """
    monkeypatch.setattr(doctor.metadata, "version", lambda _dist: "2.23.1")
    githooks = tmp_path / ".githooks"
    githooks.mkdir()
    (githooks / doctor._HOOK_VERSION_SIDECAR).write_text("2.23.1", encoding="utf-8")
    install_dir = tmp_path / "plugin" / "garbage"
    install_dir.mkdir(parents=True)
    monkeypatch.setattr(doctor, "_find_install_dir", lambda _root: install_dir)

    results = doctor._check_version_skew(tmp_path, tmp_path)

    assert len(results) == 1
    assert results[0].passed
    assert not results[0].info
    assert "aligned at v2.23.1" in results[0].detail
