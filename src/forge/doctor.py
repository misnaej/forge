"""forge-doctor — diagnose a forge install in the current environment.

Runs a set of checks and prints a pass/fail report. Exits non-zero if
any check fails.

Checks:
  1. ``forge-scripts`` CLI entry points on PATH (pip package installed).
  2. ``gh`` CLI installed and authenticated (needed by
     ``install-forge-labels`` and any GitHub-aware workflow).
  3. Plugin checks (``--skip-plugin-checks`` turns them off) — all read
     this checkout's own install, as Claude Code recorded it:
     a. An install of the plugin is recorded for this checkout (for a
        non-forge ``--plugin-name``, a user-scope install also counts).
     b. ``plugin.json`` and ``marketplace.json`` present and well-formed.
     c. ``agents/``, ``skills/``, ``claude-hooks/`` directories populated.
  4. Version skew across install surfaces — the pip package and the
     git-hook sidecar should share a version, and this checkout's plugin
     install (checked separately, ``plugin:checkout``) should be the
     release the checkout names; a lagging surface is reported as an
     advisory with the exact command to converge it (never fails the exit
     code — the report line is the signal).
  5. Pre-commit step tools (opt-in steps only) — the tool for each enabled
     step is on PATH, and its installed version is not below this repo's
     pinned floor; both are advisories, never a failing exit code.
  6. Global install (advisory) — forge installed or enabled for the whole
     user account beside this checkout's own copy.
  7. Git hooks folder (advisory) — where git will look for hooks in this
     checkout (``core.hooksPath`` resolved, or git's default), and whether
     a runnable ``pre-commit`` sits there: without one, git commits with
     no forge gate at all.

Usage:
    forge-doctor                              # human-readable
    forge-doctor --json                       # machine-readable
    forge-doctor --plugin-name myrepo         # check a non-forge plugin
"""

import argparse
import importlib.metadata
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from forge import config
from forge.config import installed_console_scripts
from forge.git_utils import (
    FORGE_DIST_NAME,
    code_health_dir,
    emit,
    git_path,
    pad_semver,
    parse_semver,
    run_git,
)
from forge.upgrade import pin_revision_mismatch, pip_command
from forge.version_surfaces import (
    SKEW_REMEDIATION,
    UNPRINTABLE,
    CheckoutInstall,
    checkout_installs,
    checkout_plugin_current,
    enabled_forge_keys,
    global_forge_keys,
    hook_sidecar_version,
    pinned_marketplace,
    pip_version,
    read_json,
    resolve_install,
    safe_text,
    target_plugin_version,
)


# Drift is only meaningful across at least two surfaces; a single present
# surface (e.g. a pip-only consumer with no git hooks) has nothing to
# compare against and must be skipped, not flagged.
_MIN_SURFACES_TO_COMPARE = 2


@dataclass
class CheckResult:
    """Outcome of one diagnostic check.

    Attributes:
        name: Short identifier for the check (e.g. ``"cli:forge-doctor"``).
        passed: True if the check succeeded. INFO-only checks (e.g.
            under-used capabilities) set ``passed=True`` regardless of
            their detail so they never sway the overall exit code; the
            ``info`` flag distinguishes them visually.
        detail: Human-readable explanation — path found, error message,
            recommendation.
        info: ``True`` for advisory checks that should be printed with
            an "i" marker instead of "✓"/"✗" and which never affect
            ``forge-doctor``'s exit code.
    """

    name: str
    passed: bool
    detail: str
    info: bool = False


EXPECTED_PLUGIN_DIRS = ("agents", "skills", "claude-hooks")

DIST_NAME = FORGE_DIST_NAME


def _expected_clis() -> list[str]:
    """Return the console-script names shipped by ``forge-scripts``.

    Derived at runtime from the installed distribution's entry-point
    metadata so a new CLI added to ``pyproject.toml`` is automatically
    picked up by ``forge-doctor`` — no parallel list to keep in sync.

    Returns:
        Sorted list of console-script names registered by this dist.
        Empty if ``forge-scripts`` isn't installed.
    """
    installed = installed_console_scripts(DIST_NAME)
    return sorted(installed) if installed is not None else []


def _check_clis() -> list[CheckResult]:
    """One result per expected CLI entry point on PATH."""
    results = []
    for cli in _expected_clis():
        path = shutil.which(cli)
        results.append(
            CheckResult(
                name=f"cli:{cli}",
                passed=path is not None,
                detail=path or "not found on PATH",
            )
        )
    return results


def _check_gh() -> list[CheckResult]:
    """Check `gh` is installed and authenticated."""
    if shutil.which("gh") is None:
        return [
            CheckResult(name="gh:installed", passed=False, detail="gh CLI not on PATH"),
            CheckResult(
                name="gh:authenticated", passed=False, detail="skipped — gh missing"
            ),
        ]
    auth = subprocess.run(
        ["gh", "auth", "status"],
        capture_output=True,
        text=True,
        check=False,
    )
    return [
        CheckResult(name="gh:installed", passed=True, detail=shutil.which("gh") or ""),
        CheckResult(
            name="gh:authenticated",
            passed=auth.returncode == 0,
            detail="ok" if auth.returncode == 0 else "run `gh auth login`",
        ),
    ]


# A plugin name is a bare identifier: Claude Code files installs under
# ``<name>@<marketplace>`` and names cache directories after it, and the
# report echoes it. The charset excludes path separators (``/`` / ``\``)
# and, by requiring a leading alphanumeric, a bare ``..``; a ``..``
# *substring* like ``a..b`` is a harmless literal and is allowed. ``\Z``
# (not ``$``) also rejects a trailing newline, which ``$`` would admit.
_SAFE_PLUGIN_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\Z")


def _validate_plugin_name(name: str) -> str:
    """Argparse ``type`` for ``--plugin-name`` — reject a non-identifier value.

    A value carrying a path separator, an ``@`` or a bare ``..`` is not a
    plugin name, and matching install records against it would either
    find nothing or the wrong plugin. Fail loudly at parse time rather
    than report on something the user did not ask about.

    Args:
        name: The raw ``--plugin-name`` argument.

    Returns:
        *name* unchanged when it is a safe bare plugin identifier.

    Raises:
        argparse.ArgumentTypeError: When *name* is empty, does not start with
            an alphanumeric, or contains a path separator.
    """
    if not _SAFE_PLUGIN_NAME_RE.match(name):
        msg = (
            f"invalid --plugin-name {name!r}: expected a bare plugin name "
            "(starts alphanumeric; letters, digits, '.', '_', '-'; no path "
            "separators)"
        )
        raise argparse.ArgumentTypeError(msg)
    return name


def _check_plugin_install(
    install: CheckoutInstall | None, plugin_name: str
) -> CheckResult:
    """Report the install this checkout loads, as Claude Code recorded it.

    Args:
        install: The install :func:`forge.version_surfaces.resolve_install`
            chose for this checkout, or ``None`` when there is none.
        plugin_name: Plugin identifier (e.g. ``"forge"``).

    Returns:
        A blocking ``CheckResult``: passed with the record's key, version
        and directory, failed naming the command that installs one.
    """
    if install is None:
        if plugin_name == "forge":
            detail = (
                "no forge install is recorded for this checkout — run "
                f"`{SKEW_REMEDIATION['plugin']}`"
            )
        else:
            detail = (
                f"no {plugin_name} install is recorded for this checkout or "
                "for your user account"
            )
        return CheckResult(name="plugin:installed", passed=False, detail=detail)
    return CheckResult(
        name="plugin:installed",
        passed=True,
        detail=(
            f"{safe_text(install.key)} {safe_text(install.version)} "
            f"({install.scope or 'unknown'} scope) at {install.install_dir}"
        ),
    )


def _check_checkout_plugin(repo_root: Path) -> list[CheckResult]:
    """Report whether this checkout has its own install of its forge release.

    The same judgment ``forge-plugin-sync`` acts on
    (:func:`forge.version_surfaces.checkout_plugin_current`). Always one
    line when a release is pinned, so the ``version_skew`` line can point
    here without pointing at nothing.

    Args:
        repo_root: Checkout root.

    Returns:
        One result — passed when current, an advisory naming
        ``forge-plugin-sync`` otherwise — or none when the checkout pins
        no release (no manifest version, no ``forge-scripts`` tag pin).
    """
    version = target_plugin_version(repo_root)
    if version is None:
        return []
    key = f"forge@{pinned_marketplace(version)}"
    if checkout_plugin_current(repo_root, version):
        return [
            CheckResult(
                name="plugin:checkout",
                passed=True,
                detail=f"{key} {version} installed for this checkout",
            )
        ]
    return [
        CheckResult(
            name="plugin:checkout",
            passed=False,
            info=True,
            detail=(
                f"this checkout has no install of forge {version} ({key}) — "
                f"run `{SKEW_REMEDIATION['plugin']}`"
            ),
        )
    ]


def _check_global_install(repo_root: Path) -> list[CheckResult]:
    """Flag a forge installed for the whole user account beside this checkout's own.

    A user-scope plugin loads in every repo, and Claude Code offers no way
    to keep it out of one that has its own copy, so the global copy can
    shadow the checkout's: the checkout then runs another forge while every
    update reports it current. Which copy wins is Claude Code's decision
    and not observable here, so the finding is that both are present —
    never a claim about which loads. The judgment of "global" is
    :func:`forge.version_surfaces.global_forge_keys`, the same one
    ``forge-plugin-sync`` refuses to install beside.

    Args:
        repo_root: Checkout whose own install records and
            ``.claude/settings(.local).json`` say whether it has a forge.

    Returns:
        One advisory result when forge is installed or enabled at user
        scope and this checkout has its own forge too; otherwise an empty
        list.
    """
    claude_dir = repo_root / ".claude"
    own = checkout_installs(repo_root) or enabled_forge_keys(
        claude_dir / "settings.json", claude_dir / "settings.local.json"
    )
    keys = [safe_text(key) or UNPRINTABLE for key in global_forge_keys()]
    if not own or not keys:
        return []
    commands = "; ".join(
        f"`claude plugin uninstall {key} --scope user`" for key in keys
    )
    return [
        CheckResult(
            name="plugin:global_install",
            passed=False,
            info=True,
            detail=(
                f"forge is installed or enabled for your whole user account "
                f"({', '.join(keys)}) and this checkout has its own copy too — "
                "the global copy can shadow this checkout's, so it may run "
                "another forge while every update reports it current. Fix: "
                f"{commands}, and keep forge installed per checkout "
                "(`forge-plugin-sync`). A per-repo enabledPlugins false is no "
                "fix: it turns forge off in that repo entirely."
            ),
        )
    ]


def _check_hooks_path(repo_root: Path) -> list[CheckResult]:
    """Report the hooks folder git will use here and whether it can gate a commit.

    git reads ``core.hooksPath`` — from any config scope, or a one-off
    ``-c`` — to decide where hooks live; pointed elsewhere, a commit runs
    no forge pre-commit at all and succeeds as if it had passed. The
    pre-commit hook cannot report that about itself (it is the part that
    never runs), so this check shows a human the folder git resolves and
    whether a runnable ``pre-commit`` sits in it. Advisory, like the other
    environment checks: a repo on another hook manager is a choice, not a
    broken install.

    Args:
        repo_root: Checkout to inspect.

    Returns:
        One advisory result, or an empty list outside a git repo.
    """
    hooks = git_path(repo_root, "hooks")
    if hooks is None:
        return []
    configured = run_git(
        "config", "--get", "core.hooksPath", cwd=repo_root, check=False
    )
    source = f"core.hooksPath={configured}" if configured else "git's default"
    pre_commit = hooks / "pre-commit"
    runnable = pre_commit.is_file() and os.access(pre_commit, os.X_OK)
    if runnable:
        state = "pre-commit present and executable"
    elif pre_commit.is_file():
        state = "pre-commit present but NOT executable — git skips it"
    else:
        state = "no pre-commit — git commits here run no pre-commit gate"
    detail = f"{hooks} ({source}); {state}"
    tracked = run_git(
        "ls-files", "--", ".githooks/pre-commit", cwd=repo_root, check=False
    )
    if not runnable and tracked:
        detail += ". Fix: `install-forge-githooks`"
    return [
        CheckResult(name="git:hooks_path", passed=runnable, detail=detail, info=True)
    ]


def _check_version_skew(repo_root: Path) -> list[CheckResult]:
    """Compare forge's version across its install surfaces and flag drift (#184).

    Reads the two pip and git-hook surfaces a single forge install presents —
    the pip ``forge-scripts`` package and the git-hook sidecar — normalizes
    each with :func:`parse_semver` (so a ``.devN+g<sha>`` editable version
    compares cleanly), and reports the ones lagging the highest (current) line
    with the exact command to converge them. Absent surfaces (no hooks — e.g. a
    pip-only consumer) are skipped, not failed.

    Posture: a lagging surface is reported as an **advisory** (``info``), never
    a failure — it carries the remediation but never sways ``forge-doctor``'s
    exit code, matching the ``under_used_capabilities`` advisory pattern.
    Advisory-not-failing keeps the exit contract stable across environments (no
    fail-locally/pass-in-CI split, and no hard-fail on a CI runner that
    legitimately predates an install — FOUNDATION §15); the remediation line in
    the report is the actionable signal, not the exit code.

    The plugin is judged in :func:`_check_checkout_plugin`, not here — pip and
    git hooks are one line (both derive from installed forge-scripts), while
    the plugin's version is the release the checkout names (which fragments
    mode parks at the latest tag by design). Comparing them made a warning that
    named a command, and the command answered that nothing was out of date:
    both true, and never convergent.

    Args:
        repo_root: Repo whose ``.githooks/`` sidecar is read.

    Returns:
        A single ``version_skew`` result when surfaces align (or too few to
        compare), else one advisory result per lagging surface naming its
        remediation.
    """
    raw = {
        "pip package": pip_version(),
        "git hooks": hook_sidecar_version(repo_root),
    }
    parsed = {name: parse_semver(v) for name, v in raw.items() if v is not None}
    comparable = {name: t for name, t in parsed.items() if t is not None}
    if len(comparable) < _MIN_SURFACES_TO_COMPARE:
        only = next(iter(comparable), "no")
        return [
            CheckResult(
                name="version_skew",
                passed=True,
                info=True,
                detail=f"only the {only} surface is present — nothing to compare",
            )
        ]

    current = max(comparable.values())
    behind = {name: t for name, t in comparable.items() if t < current}
    cur = ".".join(str(n) for n in current)
    if not behind:
        aligned = ", ".join(sorted(comparable))
        return [
            CheckResult(
                name="version_skew",
                passed=True,
                detail=(
                    f"aligned at v{cur} across {aligned} — "
                    "plugin not compared, see plugin:checkout"
                ),
            )
        ]

    return [
        CheckResult(
            name=f"version_skew:{name.replace(' ', '_')}",
            passed=False,
            info=True,
            detail=(
                f"{name} at v{'.'.join(str(n) for n in triple)}, behind current "
                f"v{cur} — run `{SKEW_REMEDIATION[name]}`"
            ),
        )
        for name, triple in sorted(behind.items())
    ]


def _surface_pin_revision(root: Path) -> list[CheckResult]:
    """Compare the pyproject pin's git ref against the installed build's.

    The fourth skew surface: a pin rewritten from a branch to a tag (or
    edited by hand) leaves the environment silently running the old
    build until pip is re-run — consumer refresh wrappers that only
    force-reinstall branch pins skip tag pins entirely. Advisory only:
    the absence of either side (no pin found, or a non-git install such
    as forge's own editable checkout) is not a finding.

    Args:
        root: Consumer repo root, for pin discovery.

    Returns:
        One advisory :class:`CheckResult` on a provable mismatch; empty
        list otherwise.
    """
    mismatch = pin_revision_mismatch(root)
    if mismatch is None:
        return []
    pinned_ref, installed = mismatch
    return [
        CheckResult(
            name="pin:revision",
            passed=True,
            info=True,
            detail=(
                f"pin says '{pinned_ref}' but the installed build is from "
                f"'{installed}' — run: {pip_command(pinned_ref)}"
            ),
        )
    ]


def _check_plugin_manifests(
    install_dir: Path | None,
    plugin_name: str,
) -> list[CheckResult]:
    """Validate plugin.json + marketplace.json in the install this checkout loads.

    Args:
        install_dir: The install's directory, or None when none is recorded.
        plugin_name: Expected plugin name to match against ``plugin.json`` /
            ``marketplace.json``.

    Returns:
        List of check results for plugin.json and marketplace.json validation.
    """
    if install_dir is None:
        return [
            CheckResult(
                name="plugin.json", passed=False, detail="plugin not installed"
            ),
            CheckResult(
                name="marketplace.json", passed=False, detail="plugin not installed"
            ),
        ]

    manifest_dir = install_dir / ".claude-plugin"
    plugin_data, plugin_err = read_json(manifest_dir / "plugin.json")
    market_data, market_err = read_json(manifest_dir / "marketplace.json")

    plugin_ok = plugin_err is None and plugin_data.get("name") == plugin_name
    market_ok = market_err is None and market_data.get("name") == plugin_name

    # The manifests sit in a user-writable cache; their values and the
    # read errors (which embed paths and exception text) are echoed only
    # in a form that cannot carry control characters into the report.
    plugin_detail = (
        f"name={safe_text(plugin_data.get('name'))}, "
        f"version={safe_text(plugin_data.get('version'))}"
    )
    return [
        CheckResult(
            name="plugin.json",
            passed=plugin_ok,
            detail=repr(plugin_err) if plugin_err else plugin_detail,
        ),
        CheckResult(
            name="marketplace.json",
            passed=market_ok,
            detail=(
                repr(market_err)
                if market_err
                else f"name={safe_text(market_data.get('name'))}"
            ),
        ),
    ]


def _check_plugin_contents(install_dir: Path | None) -> list[CheckResult]:
    """Verify the expected plugin sub-directories contain files.

    Args:
        install_dir: The install's directory, or None when none is recorded.

    Returns:
        List of check results for each expected plugin directory.
    """
    if install_dir is None:
        return [
            CheckResult(name=f"plugin/{d}", passed=False, detail="plugin not installed")
            for d in EXPECTED_PLUGIN_DIRS
        ]

    results = []
    for d in EXPECTED_PLUGIN_DIRS:
        sub = install_dir / d
        present = sub.is_dir() and any(sub.iterdir())
        count = sum(1 for _ in sub.iterdir()) if sub.is_dir() else 0
        results.append(
            CheckResult(
                name=f"plugin/{d}",
                passed=present,
                detail=f"{count} entries" if present else "missing or empty",
            )
        )
    return results


# Under-used capability map: each forge CLI maps to the artifact whose
# absence implies the CLI has been installed but never run. Surfaced as
# advisory INFO so consumers discover capabilities they're not yet using.
# Keep paths repo-relative; ``_check_under_used_capabilities`` resolves
# them against the repo root (a ``code_health/`` path against the
# step-log directory, see ``_artifact_path``).
_UNDERUSED_ARTIFACTS: tuple[tuple[str, str, str], ...] = (
    ("install-forge-githooks", ".githooks/pre-commit", "install-forge-bootstrap"),
    ("install-forge-claude-md", "FOUNDATION.md", "install-forge-bootstrap"),
    ("forge-gen-api-digest", "docs/api-digest.md", "install-forge-bootstrap"),
    ("forge-gen-cli-reference", "docs/cli-reference.md", "install-forge-bootstrap"),
    ("forge-audit-deps", "code_health/audit_deps_tree.log", "install-forge-bootstrap"),
)


# External tool each opt-in pre-commit step shells out to. Mirrors the
# steps in forge.precommit; a drift test
# (test_doctor.py::test_step_tools_keys_are_opt_in_steps) asserts every key
# is a real opt-in step so this map can't silently fall out of sync.
_STEP_TOOLS: dict[str, str] = {
    "typecheck": "pyrefly",
    "doctest": "pytest",
}


def _declared_lower_bound(repo_root: Path, tool: str) -> str | None:
    """Return the lower bound *tool* is pinned to in an extras group.

    Args:
        repo_root: Repo whose ``pyproject.toml`` declares the pin.
        tool: Distribution name as it appears in the requirement.

    Only a ``>=`` lower bound is recognised, which is how every pin in
    this repo is written; a ``~=`` or bare ``==`` pin reads as "not
    pinned that way", leaving the drift check quiet rather than guessing.

    Returns:
        The ``>=`` version, or ``None`` when the tool is not pinned that
        way anywhere in ``[project.optional-dependencies]``.
    """
    extras = config.read_pyproject_raw(repo_root).get("project", {})
    groups = extras.get("optional-dependencies", {})
    if not isinstance(groups, dict):
        return None
    for reqs in groups.values():
        if not isinstance(reqs, list):
            continue
        for req in reqs:
            # A PEP 508 marker after the version (`; python_version >= "3.11"`)
            # is not part of the bound; keeping it made the version unparseable
            # and left the check silently quiet.
            match = re.fullmatch(
                rf"{re.escape(tool)}\s*>=\s*([0-9][^,\s]*).*",
                str(req).split(";", 1)[0],
            )
            if match:
                return match.group(1)
    return None


def _step_tool_drift(repo_root: Path, step: str, tool: str) -> CheckResult | None:
    """Report an installed step tool older than the version this repo pins.

    A gate that must pass before every commit should not be free to be
    any version: when the machine's copy predates the declared floor, it
    disagrees with the one CI installs about what counts as an error, and
    a change passes here and fails there on code it never touched.

    The lookup assumes the binary name is also the distribution name,
    which holds for the tools in :data:`_STEP_TOOLS` but is not general —
    an unknown distribution simply yields nothing rather than a wrong
    answer.

    Args:
        repo_root: Repo whose pin is read.
        step: Pre-commit step the tool belongs to.
        tool: Binary, and assumed distribution, name.

    Returns:
        An advisory result when the installed version is below the pin,
        otherwise ``None``.
    """
    floor = _declared_lower_bound(repo_root, tool)
    if floor is None:
        return None
    try:
        installed = importlib.metadata.version(tool)
    except importlib.metadata.PackageNotFoundError:
        return None
    installed_t, floor_t = parse_semver(installed), pad_semver(floor)
    if installed_t is None or floor_t is None or installed_t >= floor_t:
        return None
    return CheckResult(
        name=f"step-tool-version:{step}",
        passed=False,
        info=True,
        detail=(
            f"{tool} {installed} is below the pinned floor {floor} — CI "
            f"installs the pin, so the two disagree about what fails. "
            f"Refresh the environment (`./dev/setup.sh`, or your "
            f"equivalent)."
        ),
    )


def _check_step_tools(repo_root: Path) -> list[CheckResult]:
    """Verify the external tool for each enabled pre-commit step is on PATH.

    An opt-in step listed in ``[tool.forge.precommit] enable`` shells out
    to an external tool (``typecheck`` → pyrefly, ``doctest`` → pytest).
    When the step is enabled but its tool is absent, ``forge-precommit``
    hard-fails at commit time; surfacing it here catches the gap before the
    commit instead of after.

    Args:
        repo_root: Directory the doctor was invoked from (its
            ``pyproject.toml`` is read for the enabled-step list).

    Returns:
        One ``CheckResult`` per enabled step that maps to a known tool —
        failing when the tool is missing — plus one advisory
        ``CheckResult`` from :func:`_step_tool_drift` for each present
        tool older than the repo's pinned floor. Empty when no such step
        is enabled.
    """
    precommit = config.read_tool_forge_section(repo_root, "precommit")
    enabled = precommit.get("enable")
    if not isinstance(enabled, list):
        return []
    results: list[CheckResult] = []
    for step in enabled:
        tool = _STEP_TOOLS.get(str(step))
        if tool is None:
            continue
        present = shutil.which(tool) is not None
        results.append(
            CheckResult(
                name=f"step-tool:{step}",
                passed=present,
                detail=(
                    f"{tool} on PATH"
                    if present
                    else f"step '{step}' is enabled but '{tool}' is not on "
                    f"PATH — install it (`pip install {tool}`)."
                ),
            ),
        )
        if present:
            drift = _step_tool_drift(repo_root, str(step), tool)
            if drift is not None:
                results.append(drift)
    return results


def _artifact_path(repo_root: Path, relpath: str) -> Path:
    """Resolve an :data:`_UNDERUSED_ARTIFACTS` path against *repo_root*.

    A ``code_health/`` path names a step log, so it follows the step-log
    directory wherever :func:`forge.git_utils.code_health_dir` puts it.

    Args:
        repo_root: Directory the doctor was invoked from.
        relpath: Repo-relative artifact path from the table.

    Returns:
        The artifact's location.
    """
    head, _, rest = relpath.partition("/")
    if head == "code_health" and rest:
        return code_health_dir(repo_root) / rest
    return repo_root / relpath


def _check_under_used_capabilities(repo_root: Path) -> list[CheckResult]:
    """Surface installed-but-never-run forge capabilities.

    For every entry in :data:`_UNDERUSED_ARTIFACTS`: if the CLI is on
    PATH but the expected artifact is missing, emit an advisory result
    so the consumer knows to run ``install-forge-bootstrap`` (or the
    individual CLI). Never failing — these are INFO-only.

    Args:
        repo_root: Directory the doctor was invoked from. Artifact paths
            are resolved against this.

    Returns:
        One :class:`CheckResult` per under-used capability detected.
        Empty when nothing is under-used.
    """
    results: list[CheckResult] = []
    for cli, artifact_relpath, recommend in _UNDERUSED_ARTIFACTS:
        if shutil.which(cli) is None:
            continue  # not installed — not "under-used", just absent
        artifact = _artifact_path(repo_root, artifact_relpath)
        if artifact.exists():
            continue
        results.append(
            CheckResult(
                name=f"underused:{cli}",
                passed=True,
                detail=(f"{cli} installed but {artifact} missing — run `{recommend}`."),
                info=True,
            )
        )
    return results


def _print_human(results: list[CheckResult]) -> None:
    """Print a human-readable report, separating blocking and INFO results.

    INFO-flagged results render with an ``[i]`` marker and are excluded
    from the pass/fail summary line; they never affect the exit code.

    Args:
        results: List of check results to display.
    """
    blocking = [r for r in results if not r.info]
    pass_count = sum(1 for r in blocking if r.passed)
    fail_count = len(blocking) - pass_count
    info_count = sum(1 for r in results if r.info)

    emit("forge-doctor — install diagnostics")
    emit("=" * 70)
    for r in results:
        if r.info:
            mark = "i"
        elif r.passed:
            mark = "✓"
        else:
            mark = "✗"
        emit(f"  [{mark}] {r.name:<28} {r.detail}")
    emit("=" * 70)
    info_suffix = f", {info_count} info" if info_count else ""
    summary = f"  {pass_count} passed, {fail_count} failed{info_suffix}"
    emit(f"{summary}, {len(blocking)} total")


def main() -> int:
    """Run all forge-doctor checks and print the results.

    Returns:
        ``0`` if every check passed; ``1`` otherwise.
    """
    parser = argparse.ArgumentParser(
        prog="forge-doctor",
        description="Validate a forge install in the current environment.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of human-readable output.",
    )
    parser.add_argument(
        "--plugin-name",
        default="forge",
        type=_validate_plugin_name,
        help=(
            "Claude Code plugin name to check (default: forge): its install "
            "recorded for this checkout, or for a plugin other than forge a "
            "user-scope install. Consumers who don't use Claude Code pass "
            "--skip-plugin-checks."
        ),
    )
    parser.add_argument(
        "--skip-plugin-checks",
        action="store_true",
        help=(
            "Skip all Claude Code plugin checks entirely. Useful for "
            "consumers who only adopt the pip CLIs."
        ),
    )
    args = parser.parse_args()

    results: list[CheckResult] = []
    results.extend(_check_clis())
    results.extend(_check_gh())
    results.extend(_check_step_tools(Path.cwd()))

    if not args.skip_plugin_checks:
        install = resolve_install(Path.cwd(), args.plugin_name)
        install_dir = install.install_dir if install is not None else None
        results.append(_check_plugin_install(install, args.plugin_name))
        results.extend(_check_plugin_manifests(install_dir, args.plugin_name))
        results.extend(_check_plugin_contents(install_dir))

    results.extend(_check_version_skew(Path.cwd()))
    if not args.skip_plugin_checks:
        results.extend(_check_checkout_plugin(Path.cwd()))
        results.extend(_check_global_install(Path.cwd()))
    results.extend(_surface_pin_revision(Path.cwd()))
    results.extend(_check_hooks_path(Path.cwd()))
    results.extend(_check_under_used_capabilities(Path.cwd()))

    if args.json:
        emit(json.dumps([asdict(r) for r in results], indent=2))
    else:
        _print_human(results)

    # Advisory ``info`` results never affect the exit code — they're for
    # discovery, not enforcement.
    return 0 if all(r.passed for r in results if not r.info) else 1


if __name__ == "__main__":
    sys.exit(main())
