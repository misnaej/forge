"""forge-doctor — diagnose a forge install in the current environment.

Runs a set of checks and prints a pass/fail report. Exits non-zero if
any check fails.

Checks:
  1. ``forge-scripts`` CLI entry points on PATH (pip package installed).
  2. ``gh`` CLI installed and authenticated (needed by
     ``install-forge-labels`` and any GitHub-aware workflow).
  3. Claude Code plugin checks (skipped unless the harness is detected or
     forced with ``--harness claude``/``all``) — all read this checkout's
     own install, as Claude Code recorded it:
     a. An install of the plugin is recorded for this checkout (for a
        non-forge ``--plugin-name``, a user-scope install also counts).
     b. ``plugin.json`` and ``marketplace.json`` present and well-formed.
     c. ``agents/``, ``skills/``, ``claude-hooks/`` directories populated.
  4. Version skew across install surfaces — the pip package and the
     git-hook sidecar should share a version, and this checkout's plugin
     generated integration (checked separately,
     ``claude:integration``) should be the
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
  8. Vibe integration (skipped unless Vibe is detected or forced with
     ``--harness vibe``/``all``; blocking when ``.vibe/`` exists, advisory
     otherwise) — ``.vibe/hooks.toml`` present
     and parseable with ``strict = true`` on every blocking (tool) entry,
     ``.vibe/forge-roster.json`` valid, ``.vibe/agents/*.toml`` carrying
     ``agent_type = "subagent"`` with matching ``.vibe/prompts/*.md``,
     ``.agents/skills/`` populated, ``AGENTS.md`` present,
     ``forge-hook-dispatch`` on PATH, ``.vibe/config.toml`` model presets
     present and every agent pin resolves, and the checkout listed in Vibe's
     trusted-folders database (without trust, Vibe silently skips every
     project-local guard).

Usage:
    forge-doctor                              # human-readable, auto-detected harnesses
    forge-doctor --json                       # machine-readable
    forge-doctor --harness vibe               # only Vibe registration checks
    forge-doctor --harness claude             # only Claude Code plugin checks
    forge-doctor --harness all                # both, even if not detected
    forge-doctor --skip-plugin-checks         # pip CLIs + Vibe only
"""

import argparse
import importlib.metadata
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path

from forge import config
from forge.config import installed_console_scripts
from forge.gen_vibe_agents import flatten_vibe_instructions
from forge.git_utils import (
    FORGE_DIST_NAME,
    code_health_dir,
    emit,
    git_path,
    pad_semver,
    parse_semver,
    run_git,
)
from forge.install_claude import CLAUDE_DIR, HOOKS_SUBDIR, install_claude
from forge.upgrade import pin_revision_mismatch, pip_command
from forge.version_surfaces import (
    SKEW_REMEDIATION,
    hook_sidecar_version,
    pip_version,
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
                detail=f"aligned at v{cur} across {aligned}",
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


_STEP_TOOLS: dict[str, str] = {
    "typecheck": "pyrefly",
    "doctest": "pytest",
}


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


def _vibe_home() -> Path:
    """Return Vibe's home directory (``VIBE_HOME`` or ``~/.vibe``).

    Returns:
        Path to the Vibe home.
    """
    return Path(os.environ.get("VIBE_HOME", Path.home() / ".vibe"))


def _vibe_hooks_toml_result(vibe_dir: Path) -> CheckResult:
    """Check ``.vibe/hooks.toml`` exists, parses, and every entry is strict.

    Args:
        vibe_dir: The checkout's ``.vibe`` directory.

    Returns:
        The check result for the hooks file.
    """
    hooks_toml = vibe_dir / "hooks.toml"
    if not hooks_toml.is_file():
        return CheckResult(
            name="vibe:hooks.toml",
            passed=False,
            detail=f"missing {hooks_toml} — run `install-forge-vibe`",
        )
    try:
        hooks_data = tomllib.loads(hooks_toml.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError) as exc:
        return CheckResult(
            name="vibe:hooks.toml", passed=False, detail=f"unparseable: {exc}"
        )
    entries = hooks_data.get("hooks", [])
    # strict is required on tool hooks (pre_tool/post_tool) — without it a
    # guard failure is only a warning and the guard is disarmed. Vibe
    # forbids strict on post_agent, so those entries are exempt.
    problems = [
        h.get("name", f"#{i}")
        for i, h in enumerate(entries)
        if h.get("type") in ("pre_tool", "post_tool")
        and (not h.get("strict") or not h.get("name") or not h.get("command"))
    ]
    if problems:
        return CheckResult(
            name="vibe:hooks.toml",
            passed=False,
            detail=(
                f"{len(problems)}/{len(entries)} tool-hook entries missing "
                f"strict=true or name/command: {', '.join(problems[:5])}"
            ),
        )
    return CheckResult(
        name="vibe:hooks.toml",
        passed=True,
        detail=f"{len(entries)} hooks, tool hooks all strict=true",
    )


def _vibe_roster_result(vibe_dir: Path) -> CheckResult:
    """Check the ``.vibe/forge-roster.json`` sidecar exists, parses, and maps events.

    Args:
        vibe_dir: The checkout's ``.vibe`` directory.

    Returns:
        The check result for the roster sidecar.
    """
    roster_json = vibe_dir / "forge-roster.json"
    if not roster_json.is_file():
        return CheckResult(
            name="vibe:forge-roster.json",
            passed=False,
            detail=f"missing {roster_json} — run `install-forge-vibe`",
        )
    try:
        roster = json.loads(roster_json.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return CheckResult(
            name="vibe:forge-roster.json", passed=False, detail=f"unparseable: {exc}"
        )
    n_events = len(roster.get("events", {}))
    return CheckResult(
        name="vibe:forge-roster.json",
        passed=n_events > 0,
        detail=f"{n_events} events mapped",
    )


def _vibe_agents_result(vibe_dir: Path) -> CheckResult:
    """Check subagent TOMLs are valid and each has its prompt pair.

    Args:
        vibe_dir: The checkout's ``.vibe`` directory.

    Returns:
        The check result for the subagent TOML + prompt pairs.
    """
    agents_dir = vibe_dir / "agents"
    prompts_dir = vibe_dir / "prompts"
    tomls = sorted(agents_dir.glob("*.toml")) if agents_dir.is_dir() else []
    if not tomls:
        return CheckResult(
            name="vibe:agents",
            passed=False,
            detail=f"no subagent TOMLs in {agents_dir} — run `install-forge-vibe`",
        )
    bad: list[str] = []
    for toml_path in tomls:
        try:
            data = tomllib.loads(toml_path.read_text(encoding="utf-8"))
        except (tomllib.TOMLDecodeError, OSError):
            bad.append(toml_path.stem)
            continue
        if data.get("agent_type") != "subagent" or not data.get("system_prompt_id"):
            bad.append(toml_path.stem)
        elif not (prompts_dir / f"{data['system_prompt_id']}.md").is_file():
            bad.append(f"{toml_path.stem} (no prompt)")
    return CheckResult(
        name="vibe:agents",
        passed=not bad,
        detail=(
            f"{len(tomls)} subagents, all valid with prompts"
            if not bad
            else f"invalid: {', '.join(bad[:5])}"
        ),
    )


def _vibe_skills_result(repo_root: Path) -> CheckResult:
    """Check skills are published under ``.agents/skills/``.

    Args:
        repo_root: Checkout to inspect.

    Returns:
        The check result for the published skills.
    """
    skills_dir = repo_root / ".agents" / "skills"
    n_skills = len(list(skills_dir.iterdir())) if skills_dir.is_dir() else 0
    return CheckResult(
        name="vibe:skills",
        passed=n_skills > 0,
        detail=(
            f"{n_skills} skills in .agents/skills/"
            if n_skills
            else f"missing or empty {skills_dir} — run `install-forge-vibe`"
        ),
    )


def _vibe_agents_md_result(repo_root: Path) -> CheckResult:
    """Check ``AGENTS.md`` is present and in sync with its sources.

    Content drift (FOUNDATION.md / CLAUDE.md changed since the last
    install) fails the check: Vibe loads AGENTS.md verbatim, so a stale
    file means the session runs stale rules. When the source files are
    absent (not a forge checkout), presence-only applies.

    Args:
        repo_root: Checkout to inspect.

    Returns:
        The check result for AGENTS.md.
    """
    agents_md = repo_root / "AGENTS.md"
    if not agents_md.is_file():
        return CheckResult(
            name="vibe:AGENTS.md",
            passed=False,
            detail=f"missing {agents_md} — run `install-forge-vibe`",
        )
    expected = flatten_vibe_instructions(repo_root)
    if expected is not None and agents_md.read_text(encoding="utf-8") != expected:
        return CheckResult(
            name="vibe:AGENTS.md",
            passed=False,
            detail=(
                "stale vs FOUNDATION.md + CLAUDE.md (or their @-includes) — "
                "run `install-forge-vibe`, then restart the Vibe session"
            ),
        )
    return CheckResult(
        name="vibe:AGENTS.md",
        passed=True,
        detail=f"{agents_md.stat().st_size} bytes, in sync",
    )


def _vibe_dispatcher_result() -> CheckResult:
    """Check the ``forge-hook-dispatch`` CLI is on PATH.

    Returns:
        The check result for the dispatcher CLI.
    """
    dispatcher = shutil.which("forge-hook-dispatch")
    return CheckResult(
        name="vibe:dispatcher",
        passed=dispatcher is not None,
        detail=dispatcher
        or "forge-hook-dispatch not on PATH — reinstall forge-scripts",
    )


def _vibe_trusted_folder_result(repo_root: Path) -> CheckResult:
    """Check the checkout is in Vibe's trusted-folders database.

    Args:
        repo_root: Checkout to inspect.

    Returns:
        The check result for folder trust.
    """
    trusted_file = _vibe_home() / "trusted_folders.toml"
    trusted = False
    if trusted_file.is_file():
        try:
            trusted_data = tomllib.loads(trusted_file.read_text(encoding="utf-8"))
            trusted = str(repo_root.resolve()) in [
                str(Path(p).resolve()) for p in trusted_data.get("trusted", [])
            ]
        except (tomllib.TOMLDecodeError, OSError):
            trusted = False
    return CheckResult(
        name="vibe:trusted-folder",
        passed=trusted,
        detail=(
            "checkout is in Vibe's trusted_folders.toml"
            if trusted
            else (
                f"{repo_root} NOT in {_vibe_home()}/trusted_folders.toml — "
                "Vibe loads no .vibe/ guards or AGENTS.md here. Trust it in "
                "the Vibe CLI (or `vibe --trust`) to activate the integration."
            )
        ),
    )


def _check_claude_integration(repo_root: Path) -> list[CheckResult]:
    """Check forge's Claude Code integration artifacts in this checkout.

    The integration is generated, not installed, so the only question is
    whether what is on disk still matches what forge ships — the same
    judgment ``install-forge-claude --check`` makes, asked here so one
    command reports it beside every other surface. Drift is advisory
    rather than blocking: regenerating is one command and never
    destructive, and a stale agent definition degrades a session rather
    than disarming a guard.

    A missing ``.claude/`` reads as not generated yet, pointing at the
    installer, for the same reason the Vibe equivalent does.

    Args:
        repo_root: Checkout to inspect.

    Returns:
        One result: generated and current, drifted, or not generated.
    """
    if not (repo_root / ".claude").is_dir():
        return [
            CheckResult(
                name="claude:not-generated",
                passed=True,
                info=True,
                detail=(
                    "no .claude/ — Claude Code integration not generated; "
                    "run `install-forge-claude` to enable it"
                ),
            )
        ]
    try:
        drift, error = install_claude(repo_root, check=True)
    except OSError as exc:
        return [
            CheckResult(
                name="claude:integration",
                passed=False,
                info=True,
                detail=f"integration could not be compared ({exc})",
            )
        ]
    if error:
        # forge's own sentence, naming a path from this checkout — not a
        # value read out of another tool's file, so `safe_text` would
        # only reduce it to "<unprintable>" and lose the diagnostic.
        return [CheckResult(name="claude:integration", passed=False, detail=error)]
    if drift:
        named_limit = 3
        shown = ", ".join(drift[:named_limit]) + (
            " …" if len(drift) > named_limit else ""
        )
        # A *missing* guard script is not ordinary drift. Its registration
        # in settings.json is tracked while the script itself is ignored,
        # so a fresh clone has the registration and not the file — and a
        # hook whose command is absent exits 127, which Claude Code does
        # not treat as a block. The guard then silently does not run,
        # where the retired plugin was all-or-nothing. That one is
        # blocking; everything else stays advisory, since regenerating is
        # one command and a stale agent definition degrades a session
        # rather than disarming a guard.
        missing = [
            path
            for path in drift
            if path.startswith(f"{CLAUDE_DIR}/{HOOKS_SUBDIR}/")
            and not (repo_root / path).exists()
        ]
        if missing:
            named = ", ".join(missing[:named_limit]) + (
                " …" if len(missing) > named_limit else ""
            )
            return [
                CheckResult(
                    name="claude:integration",
                    passed=False,
                    detail=(
                        f"{len(missing)} registered guard script(s) are missing "
                        f"from this checkout ({named}) — the registration runs a "
                        "command that is not there, which does not block, so the "
                        "guard does not run. Fix with `install-forge-claude`"
                    ),
                )
            ]
        return [
            CheckResult(
                name="claude:integration",
                passed=False,
                info=True,
                detail=(
                    f"{len(drift)} generated file(s) differ from what forge "
                    f"ships ({shown}) — run `install-forge-claude`"
                ),
            )
        ]
    return [
        CheckResult(
            name="claude:integration",
            passed=True,
            detail="generated agents, skills, hooks and settings are current",
        )
    ]


def _check_vibe_integration(repo_root: Path) -> list[CheckResult]:
    """Check forge's Mistral Vibe integration artifacts in this checkout.

    Vibe integration is opt-in (``install-forge-vibe``): when ``.vibe/`` is
    absent the result is a single advisory pointing at the installer, never
    a failure. When ``.vibe/`` exists the artifacts are the safety layer —
    hooks.toml without ``strict = true`` silently disarms every guard — so
    each present-but-broken piece is a blocking failure. Also verifies the
    checkout is in Vibe's trusted-folders database, because Vibe skips all
    project-local config in untrusted folders without any on-screen signal.

    Args:
        repo_root: Checkout to inspect.

    Returns:
        List of check results (advisory when not installed, blocking when
        partially installed).
    """
    vibe_dir = repo_root / ".vibe"
    if not vibe_dir.is_dir():
        return [
            CheckResult(
                name="vibe:not-installed",
                passed=True,
                detail=(
                    "no .vibe/ — Vibe integration not installed; "
                    "run `install-forge-vibe` to enable it"
                ),
                info=True,
            )
        ]

    return [
        _vibe_hooks_toml_result(vibe_dir),
        _vibe_roster_result(vibe_dir),
        _vibe_agents_result(vibe_dir),
        _vibe_skills_result(repo_root),
        _vibe_agents_md_result(repo_root),
        _vibe_dispatcher_result(),
        _vibe_models_result(repo_root),
        _vibe_trusted_folder_result(repo_root),
    ]


def _vibe_models_result(repo_root: Path) -> CheckResult:
    """Check .vibe/config.toml carries the forge model presets that agent pins rely on.

    Generated .vibe/agents/*.toml carry active_model = "forge-<tier>"; an
    unresolvable alias rejects the whole agent file at Vibe load time, so
    the presets are a hard dependency of the install.

    Args:
        repo_root: Checkout to inspect.

    Returns:
        The check result for the model presets.
    """
    config_path = repo_root / ".vibe" / "config.toml"
    if not config_path.is_file():
        return CheckResult(
            name="vibe:models",
            passed=False,
            detail=f"missing {config_path} — run `install-forge-vibe`",
        )
    try:
        entries = _read_model_entries(config_path)
    except (tomllib.TOMLDecodeError, OSError) as exc:
        return CheckResult(
            name="vibe:models", passed=False, detail=f"unparseable: {exc}"
        )
    problem = _preset_problem(entries)
    if problem:
        return CheckResult(name="vibe:models", passed=False, detail=problem)
    unresolved = _unresolved_agent_pins(repo_root / ".vibe" / "agents", set(entries))
    if unresolved:
        return CheckResult(
            name="vibe:models",
            passed=False,
            detail=(
                f"agent pins without a configured alias: {', '.join(unresolved[:5])}"
            ),
        )
    return CheckResult(
        name="vibe:models",
        passed=True,
        detail=f"{len(entries)} model aliases, presets present",
    )


_FORGE_PRESET_ALIASES = frozenset({"forge-haiku", "forge-sonnet", "forge-opus"})


def _read_model_entries(config_path: Path) -> dict[str, dict[str, object]]:
    """Read a config.toml and key its model entries by alias.

    Args:
        config_path: Path to the TOML file to parse.

    Returns:
        Mapping of alias (falling back to name) to raw model entry, handling
        both the [[models]] list form and the [models.<alias>] map form.
    """
    with config_path.open("rb") as f:
        models = tomllib.load(f).get("models", [])
    if isinstance(models, dict):
        return {
            str(alias): entry
            for alias, entry in models.items()
            if isinstance(entry, dict)
        }
    entries: dict[str, dict[str, object]] = {}
    for entry in models:
        if isinstance(entry, dict):
            alias = entry.get("alias", entry.get("name"))
            if isinstance(alias, str):
                entries[alias] = entry
    return entries


def _preset_problem(entries: dict[str, dict[str, object]]) -> str | None:
    """Describe why the forge preset aliases are unusable, if they are.

    Vibe drops a [[models]] entry that lacks name or provider as an orphan
    override, so an alias without both rejects every agent pinning it.

    Args:
        entries: Model entries keyed by alias, from _read_model_entries.

    Returns:
        The failure detail, or None when every preset is a whole model.
    """
    missing = sorted(_FORGE_PRESET_ALIASES - entries.keys())
    if missing:
        return f"missing preset aliases {missing} — run `install-forge-vibe`"
    incomplete = sorted(
        alias
        for alias in _FORGE_PRESET_ALIASES
        if not {"name", "provider"} <= entries[alias].keys()
    )
    if incomplete:
        return (
            f"preset entries missing name/provider: {incomplete} "
            "— run `install-forge-vibe`"
        )
    return None


def _unresolved_agent_pins(agents_dir: Path, aliases: set[str]) -> list[str]:
    """List generated agent files whose active_model pin has no model entry.

    Args:
        agents_dir: Directory of generated .vibe/agents/*.toml files.
        aliases: Configured model aliases.

    Returns:
        ``<file-stem>:<pin>`` strings for every unresolved pin.
    """
    unresolved: list[str] = []
    if not agents_dir.is_dir():
        return unresolved
    for toml_path in sorted(agents_dir.glob("*.toml")):
        try:
            pin = tomllib.loads(toml_path.read_text()).get("active_model")
        except (tomllib.TOMLDecodeError, OSError):
            continue
        if pin and pin not in aliases:
            unresolved.append(f"{toml_path.stem}:{pin}")
    return unresolved


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
        "--skip-plugin-checks",
        action="store_true",
        help=(
            "Skip the Claude Code integration check entirely. Useful for "
            "consumers who only adopt the pip CLIs. Implies --harness vibe "
            "when no --harness is given."
        ),
    )
    parser.add_argument(
        "--harness",
        choices=["auto", "all", "claude", "vibe"],
        default=None,
        help=(
            "Which agent harness registration layer to check: 'claude' checks "
            "the Claude Code plugin install, 'vibe' checks .vibe/ + .agents/ "
            "+ trust, 'all' checks both, 'auto' (default) checks only the "
            "harnesses detected on this system — a harness that is not "
            "installed is skipped, never failed. Common checks (CLIs, gh, "
            "git hooks) always run."
        ),
    )
    args = parser.parse_args()

    if args.harness:
        harness = args.harness
    elif args.skip_plugin_checks:
        harness = "vibe"
    else:
        harness = "auto"

    if harness == "auto":
        detected: set[str] = set()
        if (
            shutil.which("vibe")
            or _vibe_home().is_dir()
            or (Path.cwd() / ".vibe").is_dir()
        ):
            detected.add("vibe")
        if (Path.cwd() / ".claude").is_dir() or (Path.home() / ".claude").is_dir():
            detected.add("claude")
        check_claude = "claude" in detected
        check_vibe = "vibe" in detected
    else:
        check_claude = harness in ("all", "claude")
        check_vibe = harness in ("all", "vibe")

    results: list[CheckResult] = []
    results.extend(_check_clis())
    results.extend(_check_gh())
    results.extend(_check_step_tools(Path.cwd()))

    if check_claude:
        results.extend(_check_claude_integration(Path.cwd()))

    results.extend(_check_version_skew(Path.cwd()))
    results.extend(_surface_pin_revision(Path.cwd()))
    results.extend(_check_hooks_path(Path.cwd()))
    if check_vibe:
        results.extend(_check_vibe_integration(Path.cwd()))
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
