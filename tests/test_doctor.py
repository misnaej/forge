"""Tests for forge-doctor diagnostic CLI."""

# MOCKING STRATEGY: forge-doctor only probes the environment; every probe is
# monkeypatched so no real tools, subprocesses, or cwd are touched.
#   - shutil.which: stub which CLIs resolve on PATH.
#   - subprocess.run (via make_fake_run): stub gh / version probes.
#   - Path.cwd: pin the working directory.

from __future__ import annotations

import argparse
import json
import os
import subprocess
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from forge import doctor, git_utils, precommit, version_surfaces
from tests.conftest import make_fake_run, manifest_repo, write_install


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
        artifact = doctor._artifact_path(tmp_path, relpath)
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


# --- _check_version_skew() (#184) -------------------------------------------


def test_version_skew_aligned_normalizes_dev_suffix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same X.Y.Z across pip and the git hooks reports aligned, dev-suffix and all.

    MOCK SETUP: pip reports an editable-install dev suffix
    (``2.23.1.dev2+gabc``), the hook sidecar reports bare ``2.23.1`` —
    parse_semver normalizes both to the same triple.
    """
    monkeypatch.setattr(
        version_surfaces.metadata, "version", lambda _dist: "2.23.1.dev2+gabc"
    )
    githooks = tmp_path / ".githooks"
    githooks.mkdir()
    (githooks / version_surfaces.HOOK_VERSION_SIDECAR).write_text(
        "2.23.1", encoding="utf-8"
    )

    results = doctor._check_version_skew(tmp_path)

    assert len(results) == 1
    assert results[0].name == "version_skew"
    assert results[0].passed
    assert not results[0].info
    assert "aligned at v2.23.1" in results[0].detail
    assert "plugin not compared" in results[0].detail
    assert "plugin:checkout" in results[0].detail


def test_version_skew_flags_lagging_surface_as_advisory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lagging git-hook sidecar produces an advisory version_skew result.

    A lagging surface is always reported as advisory (``info=True``,
    ``passed=False``) — it carries the remediation but never sways the exit
    code, regardless of interactive vs. CI context. The plugin is not part
    of this comparison (``plugin:checkout`` judges it against the release
    the checkout names) — only pip and the git hooks derive from the same
    installed line, so pip is the single "current" surface here.

    MOCK SETUP: pip reports v2.23.1; the git-hook sidecar reports the
    older v2.22.0.
    """
    monkeypatch.setattr(version_surfaces.metadata, "version", lambda _dist: "2.23.1")
    githooks = tmp_path / ".githooks"
    githooks.mkdir()
    (githooks / version_surfaces.HOOK_VERSION_SIDECAR).write_text(
        "2.22.0", encoding="utf-8"
    )

    results = doctor._check_version_skew(tmp_path)

    assert len(results) == 1
    assert results[0].name == "version_skew:git_hooks"
    assert not results[0].passed
    assert results[0].info  # advisory only — never sways the exit code
    assert "2.22.0" in results[0].detail
    assert "2.23.1" in results[0].detail
    assert "install-forge-githooks" in results[0].detail


def test_version_skew_below_two_surfaces_reports_nothing_to_compare(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Single surface present (pip only) returns info result."""
    monkeypatch.setattr(version_surfaces.metadata, "version", lambda _dist: "2.23.1")

    results = doctor._check_version_skew(tmp_path)

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

    MOCK SETUP: pip reports a valid v2.23.1; the hook sidecar holds
    "garbage", which parse_semver rejects, so it is dropped — leaving one
    surface, with nothing to compare it against.
    """
    monkeypatch.setattr(version_surfaces.metadata, "version", lambda _dist: "2.23.1")
    githooks = tmp_path / ".githooks"
    githooks.mkdir()
    (githooks / version_surfaces.HOOK_VERSION_SIDECAR).write_text(
        "garbage", encoding="utf-8"
    )

    results = doctor._check_version_skew(tmp_path)

    assert len(results) == 1
    assert results[0].passed
    assert results[0].info
    assert "only the pip package surface" in results[0].detail


# --- plugin:installed and the checks that read the install ------------------


def _populate_install(install_dir: Path, *, name: str = "forge") -> None:
    """Give an install directory the manifests and content a real one carries.

    Args:
        install_dir: The directory an install record names.
        name: The plugin name both manifests declare.
    """
    meta = install_dir / ".claude-plugin"
    meta.mkdir(parents=True, exist_ok=True)
    (meta / "plugin.json").write_text(
        json.dumps({"name": name, "version": install_dir.name}), encoding="utf-8"
    )
    (meta / "marketplace.json").write_text(json.dumps({"name": name}), encoding="utf-8")
    for area in doctor.EXPECTED_PLUGIN_DIRS:
        (install_dir / area).mkdir(exist_ok=True)
        (install_dir / area / "entry").write_text("x", encoding="utf-8")


def _plugin_results(repo: Path, plugin_name: str = "forge") -> list[doctor.CheckResult]:
    """Run the install-reading plugin checks the way ``main()`` wires them.

    Args:
        repo: Checkout root.
        plugin_name: The ``--plugin-name`` value.

    Returns:
        The ``plugin:installed``, manifest and content results, in order.
    """
    install = version_surfaces.resolve_install(repo, plugin_name)
    install_dir = install.install_dir if install is not None else None
    return [
        doctor._check_plugin_install(install, plugin_name),
        *doctor._check_plugin_manifests(install_dir, plugin_name),
        *doctor._check_plugin_contents(install_dir),
    ]


def test_plugin_checks_read_this_checkouts_own_install(
    tmp_path: Path, claude_home: Path
) -> None:
    """Every plugin line describes the install recorded for this checkout.

    SCENARIO: the machine holds only a per-release install for this
    checkout — no shared slot — which once failed ``plugin:installed``.
    EXPECTED BEHAVIOR: every line passes and names that install.
    """
    repo = manifest_repo(tmp_path, "1.2.3")
    install_dir = write_install(claude_home, repo, "1.2.3")
    _populate_install(install_dir)

    results = _plugin_results(repo)

    assert all(r.passed for r in results)
    installed = results[0]
    assert installed.name == "plugin:installed"
    assert "forge@forge-v1-2-3 1.2.3" in installed.detail
    assert str(install_dir) in installed.detail
    assert results[1].detail == "name=forge, version=1.2.3"


def test_plugin_installed_fails_naming_sync_without_an_install(
    tmp_path: Path, claude_home: Path
) -> None:
    """No install for this checkout is a blocking failure with the fix named.

    SCENARIO: another checkout and the user account each hold a forge;
    neither is this checkout's.
    """
    repo = manifest_repo(tmp_path, "1.2.3")
    write_install(claude_home, tmp_path / "other", "1.2.3")
    write_install(claude_home, None, "1.2.3", key="forge@forge", scope="user")

    results = _plugin_results(repo)

    assert not any(r.passed for r in results)
    assert not any(r.info for r in results)
    assert "forge-plugin-sync" in results[0].detail
    assert results[1].detail == "plugin not installed"


def test_plugin_installed_for_another_plugin_falls_back_to_user_scope(
    tmp_path: Path, claude_home: Path
) -> None:
    """``--plugin-name`` for a non-forge plugin accepts its user-wide install."""
    repo = tmp_path / "repo"
    repo.mkdir()
    install_dir = write_install(
        claude_home, None, "0.4.0", key="other@market", scope="user"
    )
    _populate_install(install_dir, name="other")

    results = _plugin_results(repo, "other")

    assert all(r.passed for r in results)
    assert "other@market 0.4.0 (user scope)" in results[0].detail


def test_plugin_installed_for_another_plugin_without_any_install(
    tmp_path: Path,
) -> None:
    """A non-forge plugin with no install is a failure that names both scopes."""
    result = doctor._check_plugin_install(None, "other")

    assert not result.passed
    assert "this checkout or for your user account" in result.detail


def test_plugin_manifests_do_not_echo_crafted_names(tmp_path: Path) -> None:
    """A cached manifest's name with shell characters is not printed raw.

    Args:
        tmp_path: Pytest temp directory standing in for the cache slot.
    """
    manifest_dir = tmp_path / "9.1.1" / ".claude-plugin"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "plugin.json").write_text(
        json.dumps({"name": "forge\n$(id)", "version": "9.1.1"}), encoding="utf-8"
    )
    (manifest_dir / "marketplace.json").write_text(
        json.dumps({"name": "forge; rm -rf ~"}), encoding="utf-8"
    )

    plugin_json, market_json = doctor._check_plugin_manifests(
        tmp_path / "9.1.1", "forge"
    )

    assert plugin_json.detail == "name=<unprintable>, version=9.1.1"
    assert market_json.detail == "name=<unprintable>"
    assert not plugin_json.passed


# --- _check_global_install() ---------------------------------------------


def _write_settings(path: Path, *, enabled: bool) -> None:
    """Write a Claude Code settings file setting forge's enablement.

    Args:
        path: Settings file to write (parents created).
        enabled: Value for ``enabledPlugins["forge@forge"]``.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"enabledPlugins": {"forge@forge": enabled}}), encoding="utf-8"
    )


@pytest.mark.parametrize(
    "case",
    [
        {
            "user_enabled": True,
            "user_record": False,
            "local_disabled": False,
            "expected": True,
        },
        {
            "user_enabled": False,
            "user_record": True,
            "local_disabled": False,
            "expected": True,
        },
        {
            "user_enabled": False,
            "user_record": False,
            "local_disabled": False,
            "expected": False,
        },
        {
            "user_enabled": True,
            "user_record": False,
            "local_disabled": True,
            "expected": False,
        },
    ],
    ids=["user-settings", "user-record", "no-global", "repo-opted-out"],
)
def test_global_install_advisory(
    tmp_path: Path,
    claude_home: Path,
    case: dict[str, bool],
) -> None:
    """A user-wide forge beside this repo's enabled one is reported, not judged.

    Args:
        tmp_path: Pytest temp directory.
        claude_home: Fake ``~/.claude``.
        case: Test case with user_enabled, user_record, local_disabled, expected.
    """
    repo = tmp_path / "repo"
    _write_settings(repo / ".claude" / "settings.json", enabled=True)
    if case["local_disabled"]:
        _write_settings(repo / ".claude" / "settings.local.json", enabled=False)
    if case["user_enabled"]:
        _write_settings(claude_home / "settings.json", enabled=True)
    if case["user_record"]:
        write_install(claude_home, None, "3.30.0", key="forge@forge", scope="user")

    results = doctor._check_global_install(repo)

    if not case["expected"]:
        assert results == []
        return
    assert [r.name for r in results] == ["plugin:global_install"]
    detail = results[0].detail
    assert results[0].info
    assert "`claude plugin uninstall forge@forge --scope user`" in detail
    assert "enabledPlugins false is no fix" in detail


def test_global_install_advisory_names_a_per_release_user_install(
    tmp_path: Path, claude_home: Path
) -> None:
    """A user-scope ``forge@forge-v…`` record shadows this checkout's own install.

    SCENARIO: the checkout has its own per-release install (no settings
    file enables anything — the record alone is its copy), and a release
    marketplace was also installed for the whole user account.
    EXPECTED BEHAVIOR: the advisory fires and its fix names that key.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    write_install(claude_home, repo, "1.2.3")
    write_install(claude_home, None, "1.2.0", scope="user")

    results = doctor._check_global_install(repo)

    assert [r.name for r in results] == ["plugin:global_install"]
    assert "forge@forge-v1-2-0" in results[0].detail
    assert (
        "`claude plugin uninstall forge@forge-v1-2-0 --scope user`" in results[0].detail
    )


def test_global_install_advisory_silent_without_files(tmp_path: Path) -> None:
    """No settings and no records anywhere is a no-op, never an error.

    Args:
        tmp_path: Pytest temp directory (a repo with no ``.claude/``).
    """
    assert doctor._check_global_install(tmp_path) == []


@pytest.mark.parametrize("malformed", ["repo", "user"])
def test_global_install_advisory_survives_list_typed_settings(
    tmp_path: Path, claude_home: Path, malformed: str
) -> None:
    """A settings file holding a JSON list neither crashes nor fires.

    Args:
        tmp_path: Pytest temp directory.
        claude_home: Fake ``~/.claude``.
        malformed: Which settings file holds the list — the repo's (so the
            repo does not enable forge) or the user's (so nothing global).
    """
    repo_settings = tmp_path / ".claude" / "settings.json"
    user_settings = claude_home / "settings.json"
    _write_settings(repo_settings, enabled=True)
    _write_settings(user_settings, enabled=True)
    target = repo_settings if malformed == "repo" else user_settings
    target.write_text('["forge@forge"]', encoding="utf-8")

    assert doctor._check_global_install(tmp_path) == []


def test_global_install_advisory_leaves_exit_code_unchanged(
    tmp_path: Path,
    claude_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The global-install advisory never fails ``forge-doctor``.

    MOCK SETUP: every other check stubbed to nothing; forge enabled both
    in the repo and in user settings; cwd is the repo.
    EXPECTED BEHAVIOR: exit 0 with the advisory present in the JSON.
    """
    _write_settings(tmp_path / ".claude" / "settings.json", enabled=True)
    _write_settings(claude_home / "settings.json", enabled=True)
    monkeypatch.chdir(tmp_path)
    for name in (
        "_check_clis",
        "_check_gh",
        "_check_step_tools",
        "_check_version_skew",
        "_check_hooks_path",
        "_surface_pin_revision",
        "_check_under_used_capabilities",
        "_check_plugin_manifests",
        "_check_plugin_contents",
        "_check_checkout_plugin",
    ):
        monkeypatch.setattr(doctor, name, lambda *_a: [])
    monkeypatch.setattr(
        doctor,
        "_check_plugin_install",
        lambda *_a: doctor.CheckResult("plugin:installed", passed=True, detail=""),
    )
    monkeypatch.setattr(doctor.sys, "argv", ["forge-doctor", "--json"])
    emitted: list[str] = []
    monkeypatch.setattr(doctor, "emit", emitted.append)

    assert doctor.main() == 0
    names = [r["name"] for r in json.loads(emitted[0])]
    assert names == ["plugin:installed", "plugin:global_install"]


# --- _check_hooks_path() ----------------------------------------------------
# Real git in a scratch repo (the one exception to the module's mocking
# strategy): what is under test is how git resolves the folder.


@pytest.fixture
def hooks_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A scratch repo tracking `.githooks/pre-commit`, with no machine git config.

    Returns:
        The repo root.
    """
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    hook = tmp_path / ".githooks" / "pre-commit"
    hook.parent.mkdir()
    hook.write_text("#!/bin/sh\n")
    hook.chmod(0o755)
    subprocess.run(["git", "add", ".githooks/pre-commit"], cwd=tmp_path, check=True)
    return tmp_path


def test_check_hooks_path_reports_a_wired_folder(hooks_repo: Path) -> None:
    """A configured folder holding an executable pre-commit passes, as advisory."""
    subprocess.run(
        ["git", "config", "core.hooksPath", ".githooks"], cwd=hooks_repo, check=True
    )
    [result] = doctor._check_hooks_path(hooks_repo)
    assert result.name == "git:hooks_path"
    assert result.info
    assert result.passed
    assert str(hooks_repo / ".githooks") in result.detail
    assert "core.hooksPath=.githooks" in result.detail


def test_check_hooks_path_flags_git_default_with_no_pre_commit(
    hooks_repo: Path,
) -> None:
    """Unwired, git uses `.git/hooks`: no gate runs, and the fix is named."""
    [result] = doctor._check_hooks_path(hooks_repo)
    assert result.info
    assert not result.passed
    assert "git's default" in result.detail
    assert "no pre-commit" in result.detail
    assert "install-forge-githooks" in result.detail


def test_check_hooks_path_flags_a_pre_commit_that_is_not_executable(
    hooks_repo: Path,
) -> None:
    """A plain file is skipped by git, so it is reported as not runnable."""
    subprocess.run(
        ["git", "config", "core.hooksPath", ".githooks"], cwd=hooks_repo, check=True
    )
    (hooks_repo / ".githooks" / "pre-commit").chmod(0o644)
    [result] = doctor._check_hooks_path(hooks_repo)
    assert not result.passed
    assert "NOT executable" in result.detail


def test_check_hooks_path_names_no_forge_fix_for_another_hook_manager(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repo not tracking forge's hook gets the folder, never forge's fix."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "core.hooksPath", ".husky"], cwd=tmp_path, check=True
    )
    [result] = doctor._check_hooks_path(tmp_path)
    assert "core.hooksPath=.husky" in result.detail
    assert "install-forge-githooks" not in result.detail


def test_check_hooks_path_silent_outside_a_repo(tmp_path: Path) -> None:
    """Outside a git repo there is no hooks folder to report."""
    assert doctor._check_hooks_path(tmp_path) == []


# --- pad_semver() -------------------------------------------------------


def test_padded_version_pads_short_components() -> None:
    """A pin's lower bound may omit trailing components; padding fills zeros.

    This is the gap that silently disabled the whole step-tool drift check
    (it compared against :func:`forge.git_utils.parse_semver`, which
    requires all three parts and returns ``None`` for a short pin like
    ``"1.2"``) — pinned directly so it can't regress unnoticed.
    """
    assert git_utils.pad_semver("1") == (1, 0, 0)
    assert git_utils.pad_semver("1.2") == (1, 2, 0)
    assert git_utils.pad_semver("1.1.1") == (1, 1, 1)


def test_padded_version_none_for_non_numeric() -> None:
    """A non-numeric leading component can't be padded into a version."""
    assert git_utils.pad_semver("abc") is None


# --- _check_step_tools() step-tool-version drift ----------------------------


def test_check_step_tools_flags_drift_when_installed_below_pin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An installed tool below this repo's pinned floor adds a second advisory.

    MOCK SETUP: pyproject pins ``pyrefly>=99.0``; pyrefly resolves on PATH
    but its installed version (1.0.0) is far below that floor.
    """
    (tmp_path / "pyproject.toml").write_text(
        '[tool.forge.precommit]\nenable = ["typecheck"]\n\n'
        '[project.optional-dependencies]\ndev = ["pyrefly>=99.0"]\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(doctor.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(doctor.importlib.metadata, "version", lambda _tool: "1.0.0")

    results = doctor._check_step_tools(tmp_path)

    assert len(results) == 2
    assert results[0].name == "step-tool:typecheck"
    assert results[0].passed
    assert results[1].name == "step-tool-version:typecheck"
    assert not results[1].passed
    assert results[1].info
    assert "1.0.0" in results[1].detail
    assert "99.0" in results[1].detail


def test_check_step_tools_no_drift_when_installed_meets_pin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An installed version equal to the pinned floor is not drift (>= boundary)."""
    (tmp_path / "pyproject.toml").write_text(
        '[tool.forge.precommit]\nenable = ["typecheck"]\n\n'
        '[project.optional-dependencies]\ndev = ["pyrefly>=99.0"]\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(doctor.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(doctor.importlib.metadata, "version", lambda _tool: "99.0.0")

    results = doctor._check_step_tools(tmp_path)

    assert len(results) == 1
    assert results[0].name == "step-tool:typecheck"


def test_surface_pin_revision_reports_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A provable pin/installed-revision mismatch surfaces one advisory result.

    MOCK SETUP: ``doctor.py`` imports ``pin_revision_mismatch`` from
    ``forge.upgrade`` into its own namespace, so the shared predicate is
    patched at ``doctor.pin_revision_mismatch`` — patching
    ``forge.upgrade.*`` would not reach this module's bound name. The
    predicate's own branch logic is unit-tested in ``test_upgrade.py``.
    """
    monkeypatch.setattr(
        doctor, "pin_revision_mismatch", lambda _root: ("v1.2.0", "v1.1.0")
    )

    results = doctor._surface_pin_revision(tmp_path)

    assert len(results) == 1
    assert results[0].name == "pin:revision"
    assert results[0].passed is True
    assert results[0].info is True
    assert "v1.2.0" in results[0].detail
    assert "v1.1.0" in results[0].detail
    assert doctor.pip_command("v1.2.0") in results[0].detail


def test_surface_pin_revision_empty_when_no_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No provable mismatch (predicate returns None) -> empty result list."""
    monkeypatch.setattr(doctor, "pin_revision_mismatch", lambda _root: None)

    assert doctor._surface_pin_revision(tmp_path) == []


def test_check_checkout_plugin_passes_when_installed(
    tmp_path: Path, claude_home: Path
) -> None:
    """A recorded install of the pinned release for this checkout is a pass."""
    repo = manifest_repo(tmp_path, "1.2.3")
    write_install(claude_home, repo, "1.2.3")

    results = doctor._check_checkout_plugin(repo)

    assert [(r.name, r.passed) for r in results] == [("plugin:checkout", True)]


def test_check_checkout_plugin_advises_forge_plugin_sync_when_missing(
    tmp_path: Path,
) -> None:
    """No install for the checkout is an advisory (not a failure) naming the fix."""
    repo = manifest_repo(tmp_path, "1.2.3")

    results = doctor._check_checkout_plugin(repo)

    assert len(results) == 1
    assert results[0].name == "plugin:checkout"
    assert results[0].passed is False
    assert results[0].info is True
    assert "forge-plugin-sync" in results[0].detail


def test_check_checkout_plugin_is_silent_without_a_pinned_release(
    tmp_path: Path,
) -> None:
    """No manifest version and no tag pin: nothing to judge, nothing reported."""
    assert doctor._check_checkout_plugin(tmp_path) == []
