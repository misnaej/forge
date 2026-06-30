"""forge-precommit — pre-commit dispatcher CLI.

Generic Python pre-commit check dispatcher. Auto-detects source
directories and runs an ordered sequence on every commit. The default
sequence: env-sync (a deadly-fast install-freshness gate that runs first —
blocks when a declared ``[project.scripts]`` CLI is not installed, i.e. a
stale editable install), ruff (always ``ruff format`` + ``ruff check --fix
--unsafe-fixes`` in-place, with modified tracked files re-staged via
``git add``), docstring verification (over the diff vs main), test-name
verification (over the diff vs main), repo-structure verification
(``REPO_STRUCTURE.md`` vs the actual tree), plugin manifest JSON
validation, Claude Code plugin-version drift guard (when applicable), a
CHANGELOG-history guard (fires only on a branch that merged the base
branch in — a promotion — so main's curated entries can't be dropped),
and ``pip-audit`` dependency vulnerability scan (non-blocking — warns
but does not refuse a commit). Shipped to consumers via the
``forge-scripts`` pip package and invoked by ``.githooks/pre-commit``
after ``install-forge-githooks``.

Four further steps are **opt-in** (off by default): ``doctest``
(``pytest --doctest-modules``), ``typecheck`` (``pyrefly``),
``doc_consistency`` (doc claims vs repo state), and ``c4`` (C4 diagram
drift; self-skips when no ``[tool.forge.c4]``). Opt in by listing them in
``[tool.forge.precommit] enable``. The same table's
``disable`` list force-skips any default step; ``--only`` / ``--skip``
do the same for a single run. This override layer sits on top of each
step's own self-skip — it never weakens one, and ``disable`` beats
``enable`` when a name appears in both.

Pytest is intentionally NOT in the default sequence — it is too slow for
pre-commit and belongs in CI. The opt-in ``doctest`` step runs pytest
only over docstring examples in configured paths, and is non-blocking by
default for the same speed reason. Consumers that want the full suite on
commit call ``pytest`` directly in ``.githooks/pre-commit``.

Step outputs are written to ``code_health/<step>.log`` per FOUNDATION §13
so downstream tooling can read the latest results without re-running.

Usage:

- ``forge-precommit`` — run the default sequence
- ``forge-precommit --json`` — machine-readable summary on stdout
- ``forge-precommit --only ruff,doctest`` — run just these steps
- ``forge-precommit --skip pip_audit`` — default sequence minus a step

Consumers add repo-specific bash steps by editing ``.githooks/pre-commit``
directly — lines before the ``forge-precommit`` call run first; lines
after run only if the forge sequence passed.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

from forge import config, pip_audit_json
from forge.config import resolve_model_section
from forge.git_utils import (
    SCOPE_ALL,
    VALID_SCOPES,
    emit,
    latest_v_tag,
    parse_semver,
    read_local_plugin_version,
    require_cli,
    stage_modified_paths,
    write_step_log,
)
from forge.git_utils import repo_root as get_repo_root
from forge.run_context import is_non_interactive


if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    StepFn = Callable[[Path], "StepResult"]


# ANSI colors (suppressed if stdout isn't a TTY — keep machine-readable).
def _color(code: str) -> str:
    r"""Return *code* if stdout is a TTY, else an empty string.

    Args:
        code: ANSI escape sequence (e.g. ``"\033[0;32m"``).

    Returns:
        Either *code* unchanged (interactive terminal) or ``""`` (piped),
        so colorized output never pollutes machine-readable consumers.
    """
    return code if sys.stdout.isatty() else ""


GREEN = _color("\033[0;32m")
RED = _color("\033[0;31m")
YELLOW = _color("\033[0;33m")
NC = _color("\033[0m")


@dataclass
class StepResult:
    """Outcome of a single pre-commit step.

    Attributes:
        name: Step display name (used in stdout and the log filename slug).
        passed: True if the step exited 0.
        output: Captured combined stdout+stderr of the underlying command.
        skipped: True if the step was skipped (e.g. conditional check did
            not apply). A skipped step counts as passed for exit-code
            purposes but is reported distinctly in the summary.
        non_blocking: True if a failure of this step should NOT fail the
            overall pre-commit run. Used for advisory checks (e.g.
            ``pip-audit``) where a non-zero result is informational, not
            grounds to refuse a commit. Reported as ``WARN`` instead of
            ``FAIL`` in the step summary.
    """

    name: str
    passed: bool
    output: str
    skipped: bool = False
    non_blocking: bool = False


@dataclass(frozen=True)
class StepDef:
    """A registry entry: a step's name, its function, and whether it runs by default.

    Co-locating the three properties keeps the step registry a single
    source of truth — a name can't drift between a separate ordered list
    and a separate default-on set. ``default_on=False`` marks an opt-in
    step that runs only when listed in ``[tool.forge.precommit] enable``
    (or ``--only``).

    Attributes:
        name: Step identifier used in config, CLI flags, and the log slug.
        fn: The ``step_*`` callable producing this step's ``StepResult``.
        default_on: Whether the step is part of the default sequence.
    """

    name: str
    fn: StepFn
    default_on: bool = True


def _forge_step_config(repo_root: Path, step: str) -> dict[str, object]:
    """Return the ``[tool.forge.<step>]`` table, or ``{}`` when absent.

    Single navigation point for the repeated ``tool → forge → <step>``
    lookup every config-reading step performs. Uses the forgiving
    :func:`config.read_pyproject_raw` loader, so a missing or malformed
    ``pyproject.toml`` yields ``{}`` rather than raising.

    Args:
        repo_root: Git repo root.
        step: The ``[tool.forge.<step>]`` subsection name (e.g. ``"doctest"``).

    Returns:
        The subsection dict, or ``{}`` when any level is missing.
    """
    data = config.read_pyproject_raw(repo_root)
    return ((data.get("tool") or {}).get("forge") or {}).get(step) or {}


def _resolve_scope(repo_root: Path, step: str) -> str:
    """Resolve a step's file-selection scope: per-step override → global → ``"all"``.

    Reads ``[tool.forge.precommit]``: a ``scope_overrides.<step>`` entry wins,
    else the global ``scope`` key, else the default ``"all"`` (the whole
    tracked source tree — the strict floor per FOUNDATION §4). An unknown
    value falls back to ``"all"`` rather than raising.

    Args:
        repo_root: Git repo root.
        step: Registry step name (e.g. ``"ruff"``, ``"test_naming_check"``).

    Returns:
        ``"all"`` or ``"diff"``.
    """
    cfg = _forge_step_config(repo_root, "precommit")
    overrides = cfg.get("scope_overrides")
    if isinstance(overrides, dict):
        per_step = overrides.get(step)
        if isinstance(per_step, str) and per_step in VALID_SCOPES:
            return per_step
    global_scope = cfg.get("scope")
    if isinstance(global_scope, str) and global_scope in VALID_SCOPES:
        return global_scope
    return SCOPE_ALL


def _run(
    cmd: list[str],
    cwd: Path,
) -> tuple[bool, str]:
    """Run *cmd* and capture combined output.

    Args:
        cmd: Argv list (no shell).
        cwd: Working directory.

    Returns:
        Tuple of (passed, combined-output). ``passed`` is True if the
        command exited 0.
    """
    proc = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    output = proc.stdout + (("\n" + proc.stderr) if proc.stderr else "")
    return proc.returncode == 0, output


# ---------------------------------------------------------------------------
# Individual steps
# ---------------------------------------------------------------------------


def _declared_scripts(repo_root: Path) -> tuple[str, set[str]] | None:
    """Return ``(package_name, declared [project.scripts] names)`` or ``None``.

    Args:
        repo_root: Git repo root.

    Returns:
        The repo package name and the set of its declared console-script
        names, or ``None`` when there is no usable ``[project.name]`` +
        ``[project.scripts]`` table to verify against.
    """
    project = config.read_pyproject_raw(repo_root).get("project") or {}
    name = project.get("name")
    scripts = project.get("scripts")
    if not isinstance(name, str) or not isinstance(scripts, dict) or not scripts:
        return None
    return name, set(scripts)


def _installed_console_scripts(name: str) -> set[str] | None:
    """Return *name*'s installed ``console_scripts`` entry-point names.

    Args:
        name: Distribution name (``[project.name]``).

    Returns:
        The set of installed console-script names, or ``None`` when the
        distribution is not installed at all (nothing to compare against).
    """
    try:
        dist = importlib.metadata.distribution(name)
    except importlib.metadata.PackageNotFoundError:
        return None
    return {ep.name for ep in dist.entry_points if ep.group == "console_scripts"}


def missing_console_scripts(repo_root: Path) -> list[str]:
    """Declared ``[project.scripts]`` names not registered as console scripts.

    The shared staleness signal: ``step_env_sync`` blocks on it,
    ``step_auto_rebuild`` heals on it. Empty when there is nothing to compare
    — no ``[project.scripts]`` table, or the distribution is not installed at
    all (a fresh checkout that legitimately predates install, which neither
    step treats as stale).

    Args:
        repo_root: Git repo root.

    Returns:
        Sorted declared console-script names absent from the install.
    """
    declared = _declared_scripts(repo_root)
    if declared is None:
        return []
    name, scripts = declared
    installed = _installed_console_scripts(name)
    if installed is None:
        return []
    return sorted(scripts - installed)


# Captures only a clean X.Y.Z, so a match always feeds parse_semver a valid
# triple (the `pin_v is None` guard below is defensive, not reachable here).
_FORGE_SCRIPTS_PIN_RE = re.compile(r"^forge-scripts\s*==\s*(\d+\.\d+\.\d+)\b")


def _forge_scripts_pin_drift(repo_root: Path) -> tuple[str, str] | None:
    """Return ``(pinned, installed)`` when forge-scripts is pinned ahead of install.

    Reads ``[project.dependencies]`` for an exact ``forge-scripts==X.Y.Z`` pin
    and compares it to the installed ``forge-scripts`` version. Bounded to the
    unambiguous ``==`` form: channel pins (``@main``/``@dev``), range
    specifiers (``>=``/``~=``), and extras (``forge-scripts[dev]==``) are not
    matched and produce no drift.

    Args:
        repo_root: Git repo root.

    Returns:
        ``(pinned_version, installed_version)`` only when the install is
        strictly older than an ``==`` pin; ``None`` otherwise — no pin, a
        non-``==`` form, forge-scripts not installed, an editable/setuptools-scm
        dev build (no meaningful comparison), or already current.
    """
    project = config.read_pyproject_raw(repo_root).get("project") or {}
    deps = project.get("dependencies")
    if not isinstance(deps, list):
        return None
    pinned = next(
        (
            m.group(1)
            for dep in deps
            if isinstance(dep, str) and (m := _FORGE_SCRIPTS_PIN_RE.match(dep.strip()))
        ),
        None,
    )
    if pinned is None:
        return None
    try:
        have = importlib.metadata.version("forge-scripts")
    except importlib.metadata.PackageNotFoundError:
        return None
    if "dev" in have or "+" in have:  # editable / setuptools-scm build — can't compare
        return None
    pin_v, have_v = parse_semver(pinned), parse_semver(have)
    if pin_v is None or have_v is None or have_v >= pin_v:
        return None
    return pinned, have


def step_auto_rebuild(repo_root: Path) -> StepResult:
    """Reinstall a stale editable install before ``env_sync`` blocks the commit.

    When a pulled change adds a ``[project.scripts]`` CLI, the editable
    install goes stale and ``env_sync`` blocks the very next commit (the
    papercut that forced a manual ``./dev/setup.sh`` mid-merge). This heals it
    first: when a declared console script is missing, it runs the configured
    ``[tool.forge.env_sync].rebuild_command`` so the later ``env_sync`` — and
    every ``require_cli`` in the same run — see a fresh install.

    Bounded so it never surprises (FOUNDATION §2): it acts only when something
    is actually missing, only with an **explicitly configured** rebuild
    command (never a defaulted ``pip install`` — a repo that sets no command is
    untouched), only interactively (self-skips CI / non-interactive per §15),
    and never when ``FORGE_NO_AUTO_REBUILD`` is set (the opt-out). Non-blocking:
    a failed rebuild warns, and ``env_sync`` still renders the actionable
    block.

    Args:
        repo_root: Git repo root.

    Returns:
        ``StepResult`` (``non_blocking=True`` when it acts); skipped when
        disabled, in CI, opted out, nothing is missing, or no rebuild command
        is configured.
    """
    if is_non_interactive() or os.environ.get("FORGE_NO_AUTO_REBUILD"):
        return StepResult(
            name="auto_rebuild",
            passed=True,
            output="(CI / non-interactive or FORGE_NO_AUTO_REBUILD — skipped)",
            skipped=True,
        )
    command = _forge_step_config(repo_root, "env_sync").get("rebuild_command")
    if not isinstance(command, str) or not command.strip():
        return StepResult(
            name="auto_rebuild",
            passed=True,
            output="(no [tool.forge.env_sync].rebuild_command configured — skipped)",
            skipped=True,
        )
    missing = missing_console_scripts(repo_root)
    if not missing:
        return StepResult(
            name="auto_rebuild",
            passed=True,
            output="(install fresh — nothing to rebuild)",
            skipped=True,
        )
    emit(
        f"env_sync: {len(missing)} stale console script(s) — running "
        f"`{command}` (set FORGE_NO_AUTO_REBUILD=1 to disable)…"
    )
    passed, output = _run(shlex.split(command), cwd=repo_root)
    note = "rebuilt" if passed else "rebuild FAILED — env_sync will block below"
    return StepResult(
        name="auto_rebuild",
        passed=passed,
        output=f"$ {command}\n{output.strip() or '(no output)'}\n\n{note}",
        non_blocking=True,
    )


def step_env_sync(repo_root: Path) -> StepResult:
    """Fail fast when the local install is stale vs the repo's declared CLIs.

    Editable installs do **not** auto-register new ``[project.scripts]``
    entry points — they are baked into the distribution's metadata at
    install time. So when a PR adds a new CLI, every contributor who has
    not reinstalled is silently missing it: the gate runs old code and a
    later ``require_cli`` hard-fails deep in the sequence (the failure that
    blocked a merge commit when ``forge-gen-c4`` landed, #82). This step
    surfaces that **first**, with one in-process ``importlib.metadata``
    lookup (no subprocess, no network — sub-millisecond):

    - **Entry-point freshness (blocking):** every declared
      ``[project.scripts]`` name must be an installed console script. A
      missing one means the install is stale; the message names the exact
      reinstall command. Block by default; ``[tool.forge.env_sync].blocking
      = false`` downgrades it to a WARN (consumer escape hatch, same shape
      as ``pip_audit``). It never auto-installs (FOUNDATION §2).
    - **forge-scripts version-pin drift (non-blocking WARN):** when the repo
      pins ``forge-scripts==X.Y.Z`` in ``[project.dependencies]`` and the
      installed version is older, warn with the reinstall command. Always a
      WARN (version comparison is fuzzier than the entry-point check), and
      self-skips channel pins / range specs / editable installs / no pin
      (see :func:`_forge_scripts_pin_drift`). The blocking entry-point
      failure takes priority over this advisory.

    Self-skips when there is nothing to verify (no ``[project.scripts]`` and
    no pin, package not installed at all) or in CI / non-interactive contexts
    (FOUNDATION §15 — a fresh runner checkout legitimately predates install).

    Args:
        repo_root: Git repo root.

    Returns:
        ``StepResult`` with ``passed=True`` for all self-skip paths. A
        missing entry point fails (``non_blocking`` = inverse of
        ``[tool.forge.env_sync].blocking``, default blocking); otherwise an
        install behind the forge-scripts ``==`` pin fails non-blocking (WARN).
    """
    if is_non_interactive():
        return StepResult(
            name="env_sync",
            passed=True,
            output="(CI / non-interactive — skipped)",
            skipped=True,
        )
    pin_drift = _forge_scripts_pin_drift(repo_root)
    declared = _declared_scripts(repo_root)

    missing: list[str] = []
    installed_known = False
    scripts_count = 0
    if declared is not None:
        name, scripts = declared
        installed = _installed_console_scripts(name)
        if installed is not None:
            installed_known = True
            scripts_count = len(scripts)
            missing = sorted(scripts - installed)

        # Blocking entry-point freshness takes priority over the pin advisory.
        if missing:
            blocking = bool(
                _forge_step_config(repo_root, "env_sync").get("blocking", True)
            )
            return StepResult(
                name="env_sync",
                passed=False,
                output=(
                    f"⛔ Stale install of '{name}': {len(missing)} declared "
                    f"console script(s) not registered — {', '.join(missing)}.\n\n"
                    "A [project.scripts] entry was added since you last installed, so "
                    "the gate may run old code. Re-run `./dev/setup.sh` (or "
                    '`pip install -e ".[dev]"`, or your repo\'s equivalent) to '
                    "register the new entry point(s)."
                ),
                non_blocking=not blocking,
            )

    # Secondary: forge-scripts pinned ahead of the install (always a WARN).
    if pin_drift is not None:
        pinned, have = pin_drift
        return StepResult(
            name="env_sync",
            passed=False,
            output=(
                f"⚠️  Installed forge-scripts {have} is behind the pin "
                f"forge-scripts=={pinned}. Re-run `./dev/setup.sh` (or your "
                "repo's equivalent) to install the pinned version."
            ),
            non_blocking=True,
        )

    if declared is None:
        return StepResult(
            name="env_sync",
            passed=True,
            output="(no [project.scripts] to verify — skipped)",
            skipped=True,
        )
    if not installed_known:
        return StepResult(
            name="env_sync",
            passed=True,
            output=f"({declared[0]} not installed — skipped)",
            skipped=True,
        )
    return StepResult(
        name="env_sync",
        passed=True,
        output=f"all {scripts_count} declared console script(s) installed.",
    )


def step_ruff(repo_root: Path) -> StepResult:
    """Run ``fix-forge-ruff`` — owns the ruff phase end-to-end.

    Thin shell-out so the orchestration logic (auto-detect dirs, ruff
    format, ruff check --fix --unsafe-fixes, git-add re-stage, write
    log) lives in one place — ``forge.fix_ruff``. Same pattern as the
    other step CLIs (verify-forge-docstrings, verify-forge-test-naming,
    etc.).

    Args:
        repo_root: Git repo root.

    Returns:
        ``StepResult`` mirroring fix-forge-ruff's exit code. Skipped
        when no source dirs exist.

    Raises:
        SystemExit: If ``fix-forge-ruff`` is not on PATH.
    """
    scope = _resolve_scope(repo_root, "ruff")
    if scope == SCOPE_ALL and not config.resolve_tool_roots(
        repo_root, "ruff", include_tests=True
    ):
        return StepResult(
            name="ruff",
            passed=True,
            output="(no source dirs detected — skipped)",
            skipped=True,
        )
    require_cli("fix-forge-ruff", caller="forge-precommit")
    passed, output = _run(["fix-forge-ruff", "--scope", scope], cwd=repo_root)
    return StepResult(name="ruff", passed=passed, output=output)


def step_docstrings(repo_root: Path) -> StepResult:
    """Run ``verify-forge-docstrings`` over the resolved scope.

    Scope is ``all`` by default (the whole tracked source tree) and is
    switchable to ``diff`` (modified files vs main) per
    ``[tool.forge.precommit]`` — see :func:`_resolve_scope`. The resolved
    mode is forwarded as ``--scope``; the CLI owns file selection.

    Args:
        repo_root: Git repo root.

    Returns:
        ``StepResult`` for this step. The CLI itself reports "no files
        to check" cleanly when nothing matches — never silently skipped
        from this dispatcher.

    Raises:
        SystemExit: If ``verify-forge-docstrings`` is not on PATH.
    """
    require_cli("verify-forge-docstrings", caller="forge-precommit")
    scope = _resolve_scope(repo_root, "docstring_verification")
    passed, output = _run(["verify-forge-docstrings", "--scope", scope], cwd=repo_root)
    return StepResult(name="docstring_verification", passed=passed, output=output)


def step_docstring_coverage(repo_root: Path) -> StepResult:
    """Run ``verify-forge-docstring-coverage`` — full-codebase % reporter.

    Non-blocking by design. Ruff D100-D107 (enabled via
    ``select = ["ALL"]``) already block missing docstrings on
    modules, classes, public functions, and methods - the genuine
    enforcement layer for any repo using the foundation ruff config.
    This step adds aggregate % reporting, per-file table, badge
    generation, and catches the cases ruff misses (notably nested
    functions and closures). ``forge:precommit-fixer`` reads the log
    and addresses listed missing-docstring entries.

    When ``[tool.forge.docstring_coverage].badge = true`` the CLI
    also writes ``.badges/docstring-coverage.svg``.

    The CLI self-skips with exit 0 when ``pyproject.toml`` or
    ``src/`` is missing, so consumer repos that haven't opted in
    don't fail the hook.

    Args:
        repo_root: Git repo root.

    Returns:
        ``StepResult`` mirroring the CLI exit code.

    Raises:
        SystemExit: If ``verify-forge-docstring-coverage`` is not on PATH.
    """
    require_cli("verify-forge-docstring-coverage", caller="forge-precommit")
    passed, output = _run(["verify-forge-docstring-coverage"], cwd=repo_root)
    skipped = "skipped" in output
    return StepResult(
        name="docstring_coverage",
        passed=passed,
        output=output,
        skipped=skipped,
        non_blocking=True,
    )


def step_test_naming(repo_root: Path) -> StepResult:
    """Run ``verify-forge-test-naming`` over the resolved scope.

    Scope is ``all`` by default (every tracked test file) and switchable to
    ``diff`` per ``[tool.forge.precommit]`` — see :func:`_resolve_scope`. The
    CLI is warning-only by design — it surfaces naming issues in the
    ``test_naming_check.log`` but always exits 0, so this step never
    refuses a commit.

    Args:
        repo_root: Git repo root.

    Returns:
        ``StepResult`` for this step.

    Raises:
        SystemExit: If ``verify-forge-test-naming`` is not on PATH.
    """
    require_cli("verify-forge-test-naming", caller="forge-precommit")
    scope = _resolve_scope(repo_root, "test_naming_check")
    passed, output = _run(["verify-forge-test-naming", "--scope", scope], cwd=repo_root)
    return StepResult(name="test_naming_check", passed=passed, output=output)


def step_repo_structure(repo_root: Path) -> StepResult:
    """Run ``verify-forge-repo-structure``; hard-fail if missing (FOUNDATION §2).

    The underlying CLI parses ``REPO_STRUCTURE.md`` and compares the paths
    it documents against the actual repository tree, reporting drift in
    both directions. It exits 1 on drift, so this step refuses a commit
    when ``REPO_STRUCTURE.md`` is out of sync.

    Args:
        repo_root: Git repo root.

    Returns:
        ``StepResult`` for this step. Skipped when no ``REPO_STRUCTURE.md``
        exists at the repo root.

    Raises:
        SystemExit: If ``verify-forge-repo-structure`` is not on PATH.
    """
    if not (repo_root / "REPO_STRUCTURE.md").is_file():
        return StepResult(
            name="repo_structure_check",
            passed=True,
            output="(no REPO_STRUCTURE.md — skipped)",
            skipped=True,
        )
    require_cli("verify-forge-repo-structure", caller="forge-precommit")
    passed, output = _run(["verify-forge-repo-structure"], cwd=repo_root)
    return StepResult(name="repo_structure_check", passed=passed, output=output)


def step_manifest_json(repo_root: Path) -> StepResult:
    """Run ``verify-forge-manifest`` — owns the manifest-JSON validation phase.

    Thin shell-out matching the pattern of every other phase step. The
    CLI itself decides whether to skip (no ``.claude-plugin/`` dir) and
    writes the corresponding log entry.

    Args:
        repo_root: Git repo root.

    Returns:
        ``StepResult`` mirroring the CLI exit code.

    Raises:
        SystemExit: If ``verify-forge-manifest`` is not on PATH.
    """
    require_cli("verify-forge-manifest", caller="forge-precommit")
    passed, output = _run(["verify-forge-manifest"], cwd=repo_root)
    skipped = "skipped" in output
    return StepResult(
        name="manifest_json", passed=passed, output=output, skipped=skipped
    )


def step_commit_types_parity(repo_root: Path) -> StepResult:
    """Run ``forge-gen-commit-types --check`` — managed-block parity guard.

    Verifies the conventional-commit alternation in
    ``claude-hooks/check_commit_format.sh`` matches the canonical
    ``CONVENTIONAL_COMMIT_TYPES`` tuple in ``forge.pr_squash_comment``.
    Self-skips when the hook file is absent (consumer repos that do
    not ship the forge Claude-plugin layout).

    Args:
        repo_root: Git repo root.

    Returns:
        ``StepResult`` mirroring the CLI exit code, or a skipped result
        when ``claude-hooks/check_commit_format.sh`` is absent.

    Raises:
        SystemExit: If ``forge-gen-commit-types`` is not on PATH.
    """
    if not (repo_root / "claude-hooks" / "check_commit_format.sh").is_file():
        return StepResult(
            name="commit_types_parity",
            passed=True,
            output="(no claude-hooks/check_commit_format.sh — skipped)",
            skipped=True,
        )
    require_cli("forge-gen-commit-types", caller="forge-precommit")
    passed, output = _run(["forge-gen-commit-types", "--check"], cwd=repo_root)
    return StepResult(name="commit_types_parity", passed=passed, output=output)


def step_c4(repo_root: Path) -> StepResult:
    """Run ``forge-gen-c4 --check`` — C4 model + README-block drift guard.

    Keeps ``docs/architecture.dsl`` and the managed README C4 block in sync
    with the actual import graph, so a structural change that is not
    regenerated fails the commit (and thus the PR). Self-skips when the repo
    has no ``[tool.forge.c4]`` model configured.

    Args:
        repo_root: Git repo root.

    Returns:
        ``StepResult`` mirroring the CLI exit code, or a skipped result when
        no C4 model is configured.

    Raises:
        SystemExit: If ``forge-gen-c4`` is not on PATH.
    """
    if resolve_model_section(repo_root) is None:
        return StepResult(
            name="c4",
            passed=True,
            output="(no [tool.forge.c4] model — skipped)",
            skipped=True,
        )
    require_cli("forge-gen-c4", caller="forge-precommit")
    passed, output = _run(["forge-gen-c4", "--check"], cwd=repo_root)
    return StepResult(name="c4", passed=passed, output=output)


# Maximum residual ``pip-audit`` advisories allowed before the WARN
# escalates to a loud banner. The check is non-blocking either way;
# above this threshold the output is prefixed with a visible nudge
# asking the contributor to file a tracking issue if there isn't one —
# quietly accumulating residuals are the failure mode this step exists
# to surface.
_PIP_AUDIT_LOUDNESS_THRESHOLD = 10

# Sidecar holding pip-audit's parsed JSON, written by ``step_pip_audit`` and
# reused by ``step_cve_usage`` so the two steps share one pip-audit scan per
# commit instead of each hitting the OSV network independently (#78).
PIP_AUDIT_SIDECAR = "code_health/pip_audit.json"

# Matches a full ``pip-audit`` advisory ID:
# - ``PYSEC-YYYY-N`` (year + sequence number, both digit-only)
# - ``GHSA-xxxx-yyyy-zzzz`` (three 4-char alphanumeric segments)
# The text output prints one such ID per finding line, so counting
# matches equals counting reported findings.
_ADVISORY_ID_RE = re.compile(
    r"\b(?:PYSEC-\d+-\d+|GHSA-[a-z0-9]{4}-[a-z0-9]{4}-[a-z0-9]{4})\b",
)


def _count_pip_audit_advisories(output: str) -> int:
    """Count advisory ID occurrences in a ``pip-audit`` text-mode output.

    Uses :func:`re.findall` over the canonical advisory-ID pattern
    (see :data:`_ADVISORY_ID_RE`) — every non-overlapping occurrence
    is counted. ``pip-audit`` prints exactly one advisory ID per
    finding line in its default text output, so the resulting count
    equals the number of reported findings.

    Args:
        output: Raw stdout/stderr captured from the ``pip-audit`` run.

    Returns:
        The number of advisory ID occurrences found.
    """
    return len(_ADVISORY_ID_RE.findall(output))


def step_pip_audit(repo_root: Path) -> StepResult:
    """Run ``pip-audit --skip-editable`` and report findings as non-blocking.

    ``pip-audit`` scans the current Python environment against the OSV
    vulnerability database. Findings are advisory — a CVE in a dev
    dependency should not refuse a commit, but the contributor should
    see it. Runs **once** in JSON mode (:func:`forge.pip_audit_json.run_json`),
    writes the parsed findings to the ``code_health/pip_audit.json`` sidecar
    that ``step_cve_usage`` reuses (#78), and renders the human log from the
    same JSON.

    Loudness escalation: when the residual advisory count exceeds
    :data:`_PIP_AUDIT_LOUDNESS_THRESHOLD`, the output is prefixed with
    a visible banner listing the count and nudging the contributor to
    file a tracking issue. The step remains non-blocking; only the
    rendering changes. Below the threshold the original short WARN
    line is preserved.

    Non-blocking by default: a failing audit (CVEs found) sets
    ``passed=False`` AND ``non_blocking=True`` so ``run_all`` reports
    ``WARN`` instead of ``FAIL`` and the overall exit code is unaffected.
    Set ``[tool.forge.pip_audit].blocking = true`` to make CVE findings a
    hard ``FAIL`` (same opt-in pattern as ``typecheck``/``doctest``). A
    missing ``pip-audit`` binary always renders as a non-blocking WARN
    regardless — that is a broken-install signal, not a CVE finding.

    ``pip-audit`` ships as a core forge dependency (it backs this default
    step — #71), so a missing binary signals a broken install rather than
    an unconfigured optional tool. That case renders as a **loud
    non-blocking WARN** — never a silent skip — because a security gate
    that quietly does nothing gives false assurance.

    Args:
        repo_root: Git repo root (used as working directory).

    Returns:
        ``StepResult`` for this step; ``non_blocking`` is the inverse of
        ``[tool.forge.pip_audit].blocking`` for CVE findings, always
        ``True`` when the binary is missing.
    """
    blocking = bool(_forge_step_config(repo_root, "pip_audit").get("blocking", False))
    run = pip_audit_json.run_json(repo_root)
    if run is None:
        return StepResult(
            name="pip_audit",
            passed=False,
            output=(
                "⚠️  pip-audit not on PATH — the CVE scan did NOT run. "
                "pip-audit ships as a core forge dependency; reinstall "
                "forge-scripts to restore it (`pip install -e '.[dev]'`, "
                "or your repo's equivalent)."
            ),
            non_blocking=True,
        )
    if run.data is None:
        return StepResult(
            name="pip_audit",
            passed=False,
            output=(
                "⚠️  pip-audit produced no parseable JSON — the CVE scan did "
                "not complete:\n\n" + (run.stderr.strip() or "(no stderr)")
            ),
            non_blocking=True,
        )
    _write_audit_sidecar(repo_root, run.data)
    output = pip_audit_json.render_report(run.data)
    passed = not pip_audit_json.has_vulns(run.data)
    if not passed:
        count = _count_pip_audit_advisories(output)
        if count > _PIP_AUDIT_LOUDNESS_THRESHOLD:
            banner = (
                f"⚠️  pip_audit has {count} advisories "
                f"(> threshold {_PIP_AUDIT_LOUDNESS_THRESHOLD}). "
                "Consider filing a tracking issue if there isn't one — "
                "these accumulate silently.\n\n"
            )
            output = banner + output
    return StepResult(
        name="pip_audit",
        passed=passed,
        output=output,
        non_blocking=not blocking,
    )


def _write_audit_sidecar(repo_root: Path, data: dict) -> None:
    """Persist pip-audit's parsed JSON to the shared sidecar.

    Written by :func:`step_pip_audit` so :func:`step_cve_usage` can reuse the
    same scan (#78) instead of invoking pip-audit a second time. A write
    failure is swallowed with a warning rather than propagated: the sidecar is
    an optimization, and ``step_cve_usage`` already falls back to a standalone
    pip-audit run when it is absent — a non-blocking step must not crash the
    whole pre-commit run over an unwritable ``code_health/``.

    Args:
        repo_root: Git repo root.
        data: Parsed pip-audit JSON (``AuditRun.data``).
    """
    path = repo_root / PIP_AUDIT_SIDECAR
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
    except OSError as exc:
        emit(f"pip_audit: sidecar write failed ({exc}); cve_usage runs standalone")


def step_cve_usage(repo_root: Path) -> StepResult:
    """Run ``verify-forge-cve-usage`` — the usage-scoped second stage on pip_audit.

    Where ``pip_audit`` flags vulnerable *packages*, this flags vulnerable
    *usage*: it greps the source for the patterns of CVEs that pip-audit is
    *currently* reporting, surfacing only real matches with risk + mitigation
    (see :mod:`forge.verify_cve_usage`). **Opt-in by presence** of a
    ``cve_usage_patterns.toml`` map at the repo root — the CLI self-skips
    cleanly when it (or pip-audit) is absent, so consumers who haven't opted
    in never see it.

    **Non-blocking** (advisory), mirroring ``pip_audit``: a finding sets
    ``passed=False`` + ``non_blocking=True`` so ``run_all`` renders ``WARN``,
    not ``FAIL``. ``forge:precommit-fixer`` escalates findings at PR
    finalization (strict), same as ``pip_audit``.

    Reuses ``step_pip_audit``'s scan: when the ``code_health/pip_audit.json``
    sidecar exists (the normal case — ``pip_audit`` runs first), it is passed
    via ``--audit-json`` so pip-audit is invoked **once** per commit (#78). If
    the sidecar is absent (``pip_audit`` disabled or skipped), the CLI falls
    back to running pip-audit itself, so the check still works standalone.
    The sidecar is trusted as current; ``pip_audit`` rewrites it every run and
    sits immediately before this step, so the only stale case is an explicit
    ``--skip pip_audit`` leaving a prior run's file — then this step reuses
    that older scan rather than re-running.

    Args:
        repo_root: Git repo root.

    Returns:
        ``StepResult`` for this step; ``non_blocking=True`` whenever the
        check actually ran, ``skipped`` when there is no pattern map.
    """
    if not (repo_root / "cve_usage_patterns.toml").is_file():
        return StepResult(
            name="cve_usage",
            passed=True,
            output="(no cve_usage_patterns.toml — skipped)",
            skipped=True,
        )
    require_cli("verify-forge-cve-usage", caller="forge-precommit")
    cmd = ["verify-forge-cve-usage"]
    if (repo_root / PIP_AUDIT_SIDECAR).is_file():
        cmd += ["--audit-json", PIP_AUDIT_SIDECAR]
    passed, output = _run(cmd, cwd=repo_root)
    skipped = "skipped" in output
    return StepResult(
        name="cve_usage",
        passed=passed,
        output=output,
        skipped=skipped,
        non_blocking=True,
    )


def step_cli_wiring(repo_root: Path) -> StepResult:
    """Run ``verify-forge-cli-wiring`` — assert every script has a real caller.

    The verifier greps the wiring source paths
    (``src/forge/install_bootstrap.py``, ``src/forge/precommit.py``,
    ``src/forge/audit/``, ``.githooks/``, ``claude-hooks/``, ``dev/``,
    ``agents/``, ``skills/``) for every ``[project.scripts]`` entry
    and fails on unreachable CLIs. Forge-specific wiring layout, so
    the step requires an explicit opt-in: add ``[tool.forge.cli_wiring]
    enabled = true`` to the consumer's ``pyproject.toml``. Self-skips
    otherwise so consumer repos with different layouts stay green.

    Args:
        repo_root: Git repo root.

    Returns:
        ``StepResult`` mirroring the CLI exit code, or a skipped result
        when the consumer has not opted in.

    Raises:
        SystemExit: If ``verify-forge-cli-wiring`` is not on PATH.
    """
    if not _cli_wiring_enabled(repo_root):
        return StepResult(
            name="cli_wiring",
            passed=True,
            output=(
                "(no [tool.forge.cli_wiring] enabled = true in "
                "pyproject.toml — skipped)"
            ),
            skipped=True,
        )
    require_cli("verify-forge-cli-wiring", caller="forge-precommit")
    passed, output = _run(["verify-forge-cli-wiring"], cwd=repo_root)
    return StepResult(name="cli_wiring", passed=passed, output=output)


def _cli_wiring_enabled(repo_root: Path) -> bool:
    """Return True when the repo has opted into the cli_wiring check.

    Opt-in marker: ``[tool.forge.cli_wiring] enabled = true`` in
    ``pyproject.toml``. Forge itself sets this; consumer repos must
    enable explicitly because the default :data:`WIRING_SOURCES`
    glob list is forge-layout-specific.

    Args:
        repo_root: Git repo root.

    Returns:
        ``True`` when the marker is present and truthy; ``False``
        otherwise (including when ``pyproject.toml`` is missing or
        malformed).
    """
    return bool(_forge_step_config(repo_root, "cli_wiring").get("enabled"))


def step_plugin_version(repo_root: Path) -> StepResult:
    """Run ``verify-forge-plugin-version`` — owns the rolling-next guard.

    Thin shell-out matching the pattern of every other phase step. The
    CLI itself decides whether to skip (no plugin.json, no tags, release
    commit) and writes the corresponding log entry.

    Args:
        repo_root: Git repo root.

    Returns:
        ``StepResult`` mirroring the CLI exit code.

    Raises:
        SystemExit: If ``verify-forge-plugin-version`` is not on PATH.
    """
    require_cli("verify-forge-plugin-version", caller="forge-precommit")
    passed, output = _run(["verify-forge-plugin-version"], cwd=repo_root)
    skipped = "skipped" in output
    return StepResult(
        name="plugin_version", passed=passed, output=output, skipped=skipped
    )


def _one_step_successors(tag: tuple[int, int, int]) -> set[tuple[int, int, int]]:
    """Return the three valid rolling-next successors of a tagged release.

    Args:
        tag: The latest tag's ``(major, minor, patch)``.

    Returns:
        The set of versions exactly one rolling-next step ahead — a patch
        bump, a minor bump, or a major bump.
    """
    major, minor, patch = tag
    return {(major, minor, patch + 1), (major, minor + 1, 0), (major + 1, 0, 0)}


def step_release_tag_guard(repo_root: Path) -> StepResult:
    """Block when an intermediate rolling-next release was never tagged (#66).

    Forge tags **every** merge to its dev branch (``forge-next-prep --tag``),
    so ``plugin.json`` — which names the *next* release — must always sit
    **exactly one** rolling-next step (patch+1 / minor+1 / major+1) ahead of
    the latest ``v*`` tag. A larger gap means a prior release's tag was
    skipped and is about to be buried by a further bump (the failure mode of
    #66, where v1.25.0 shipped untagged). This guard refuses that commit and
    points at ``forge-next-prep --tag``.

    Self-skips (passes) for any repo this cadence does not apply to: a
    single-track repo, one without ``.claude-plugin/plugin.json``, one with
    no tags yet, or when ``plugin.json`` is not strictly ahead of the latest
    tag (the ``plugin_version`` step owns the "must be ahead" rule, and an
    equal value is a reproduced-release tree).

    Args:
        repo_root: Git repo root.

    Returns:
        ``StepResult`` — blocking failure only on a genuine tag gap;
        skipped/pass otherwise.
    """
    if not config.load_config(repo_root).dual_track:
        return StepResult(
            name="release_tag_guard",
            passed=True,
            output="(single-track repo — skipped)",
            skipped=True,
        )
    plugin_ver = read_local_plugin_version(repo_root)
    latest = latest_v_tag(repo_root)
    if plugin_ver is None or latest is None:
        return StepResult(
            name="release_tag_guard",
            passed=True,
            output="(no plugin.json or no v* tags — skipped)",
            skipped=True,
        )
    pv = parse_semver(plugin_ver)
    lv = parse_semver(latest)
    if pv is None or lv is None or pv <= lv:
        return StepResult(
            name="release_tag_guard",
            passed=True,
            output="(plugin.json not strictly ahead of latest tag — skipped)",
            skipped=True,
        )
    if pv in _one_step_successors(lv):
        return StepResult(
            name="release_tag_guard",
            passed=True,
            output=f"plugin.json {plugin_ver} is one step ahead of v{latest}.",
        )
    return StepResult(
        name="release_tag_guard",
        passed=False,
        output=(
            f"plugin.json {plugin_ver} is more than one release ahead of the "
            f"latest tag v{latest} — an intermediate rolling-next release was "
            f"never tagged and will be lost. Run `forge-next-prep --tag` to "
            f"tag it before bumping further (FOUNDATION / docs/release-process)."
        ),
    )


def step_changelog_history(repo_root: Path) -> StepResult:
    """Run ``verify-forge-changelog-history`` — the dropped-``@base``-entry guard.

    Thin shell-out (matching ``step_plugin_version``). The CLI self-skips
    unless ``origin/<base>`` is an ancestor of ``HEAD`` — a ``dev → main``
    promotion or other main-merge — so it fires only when main's curated
    CHANGELOG history could be regressed by a conflict resolved blindly
    toward dev. See ``docs/release-process.md`` §5 and #120.

    Args:
        repo_root: Git repo root.

    Returns:
        ``StepResult`` mirroring the CLI exit code.

    Raises:
        SystemExit: If ``verify-forge-changelog-history`` is not on PATH.
    """
    require_cli("verify-forge-changelog-history", caller="forge-precommit")
    passed, output = _run(["verify-forge-changelog-history"], cwd=repo_root)
    skipped = "skipped" in output
    return StepResult(
        name="changelog_history", passed=passed, output=output, skipped=skipped
    )


def _cfg_str_list(cfg: dict[str, object], key: str, default: list[str]) -> list[str]:
    """Return a ``[tool.forge.*]`` list-valued key narrowed to ``list[str]``.

    TOML values arrive typed as ``object``; this coerces a list-valued key
    to ``list[str]`` (stringifying items) and falls back to *default* when
    the key is absent or not a list — so a scalar like ``paths = "src"`` is
    ignored rather than iterated character-by-character.

    Args:
        cfg: A ``[tool.forge.<step>]`` subsection.
        key: The list-valued key to read.
        default: Fallback when the key is absent or not a list.

    Returns:
        The key's items as strings, or a copy of *default*.
    """
    value = cfg.get(key)
    if isinstance(value, list):
        return [str(item) for item in value]
    return list(default)


def step_doctest(repo_root: Path) -> StepResult:
    """Run ``pytest --doctest-modules`` over docstring examples (opt-in).

    Executes the ``>>>`` examples embedded in docstrings so they cannot
    rot silently — forge already enforces docstring presence (ruff D),
    Args/Returns accuracy (``verify_docstrings``), and coverage
    (``docstring_coverage``), but not example *execution*. Scans the roots
    from :func:`forge.config.resolve_tool_roots` (granular
    ``[tool.forge.doctest].paths`` → ``[tool.forge].source_dirs`` → smart
    auto-detect), skipping when none resolve. Non-blocking unless
    ``blocking = true``: pytest is slow and a false failure that refuses a
    commit trains ``--no-verify``.

    Opt-in only — runs when listed in ``[tool.forge.precommit] enable``
    (or ``--only``). When pytest output contains ``"no tests ran"`` (no
    docstring examples collected), the step is treated as a skip rather
    than a failure.

    Args:
        repo_root: Git repo root.

    Returns:
        ``StepResult`` for the doctest run; ``non_blocking`` is the
        inverse of ``[tool.forge.doctest] blocking``.

    Raises:
        SystemExit: If ``pytest`` is not on PATH.
    """
    blocking = bool(_forge_step_config(repo_root, "doctest").get("blocking", False))
    paths = config.resolve_tool_roots(repo_root, "doctest")
    if not paths:
        return StepResult(
            name="doctest",
            passed=True,
            output="(no source dirs detected — skipped)",
            skipped=True,
        )
    require_cli("pytest", caller="forge-precommit")
    passed, output = _run(
        ["pytest", "--doctest-modules", "--doctest-continue-on-failure", *paths],
        cwd=repo_root,
    )
    # pytest>=8 prints "no tests ran" when zero examples were collected
    # (exit 5) — a skip for an advisory doctest sweep, not a failure.
    if "no tests ran" in output:
        return StepResult(name="doctest", passed=True, output=output, skipped=True)
    return StepResult(
        name="doctest", passed=passed, output=output, non_blocking=not blocking
    )


def step_typecheck(repo_root: Path) -> StepResult:
    """Run pyrefly over the source tree (opt-in).

    pyrefly is forge's type checker — same Astral model as ruff (single
    Rust binary, pyproject-native, reads/migrates ``[tool.mypy]`` config),
    so it slots into forge's existing toolchain with no Node runtime.
    Scans the roots from :func:`forge.config.resolve_tool_roots` (granular
    ``[tool.forge.typecheck].paths`` → ``[tool.forge].source_dirs`` → smart
    auto-detect), skipping when none resolve. Non-blocking unless
    ``blocking = true``: a type-checker false positive that refuses a commit
    trains ``--no-verify``, so the gate is advisory by default.

    Opt-in only — runs when listed in ``[tool.forge.precommit] enable``.
    When opted in but ``pyrefly`` is absent, fails loudly (the consumer
    opted in and must install it) rather than silently passing.

    Args:
        repo_root: Git repo root.

    Returns:
        ``StepResult`` mirroring pyrefly's exit code; ``non_blocking`` is
        the inverse of ``blocking``. Skipped (passing) when no source dirs
        resolve.

    Raises:
        SystemExit: If ``pyrefly`` is not on PATH.
    """
    blocking = bool(_forge_step_config(repo_root, "typecheck").get("blocking", False))
    paths = config.resolve_tool_roots(repo_root, "typecheck")
    if not paths:
        return StepResult(
            name="typecheck",
            passed=True,
            output="(no source dirs detected — skipped)",
            skipped=True,
        )
    require_cli("pyrefly", caller="forge-precommit")
    passed, output = _run(["pyrefly", "check", *paths], cwd=repo_root)
    return StepResult(
        name="typecheck", passed=passed, output=output, non_blocking=not blocking
    )


def step_doc_consistency(repo_root: Path) -> StepResult:
    """Run ``verify-forge-doc-consistency`` — doc claims vs repo state (opt-in).

    Checks the machine-checkable subset of documentation claims: every
    ``[project.scripts]`` CLI name appears in ``docs/cli-reference.md``.
    Opt-in via ``[tool.forge.precommit] enable``; non-blocking (doc drift
    is a warning, not grounds to refuse a commit).

    Args:
        repo_root: Git repo root.

    Returns:
        ``StepResult`` mirroring the CLI exit code; always ``non_blocking``.

    Raises:
        SystemExit: If ``verify-forge-doc-consistency`` is not on PATH.
    """
    require_cli("verify-forge-doc-consistency", caller="forge-precommit")
    passed, output = _run(["verify-forge-doc-consistency"], cwd=repo_root)
    return StepResult(
        name="doc_consistency", passed=passed, output=output, non_blocking=True
    )


# Generators that write a tracked doc but have no drift gate of their own —
# step_regen_docs keeps each fresh (regenerate + re-stage) when it exists.
_REGEN_DOCS: tuple[tuple[str, str], ...] = (
    ("forge-gen-api-digest", "docs/api-digest.md"),
    ("forge-gen-cli-reference", "docs/cli-reference.md"),
)


def step_regen_docs(repo_root: Path) -> StepResult:
    """Regenerate the otherwise-unwired generated docs and re-stage them.

    ``docs/api-digest.md`` and ``docs/cli-reference.md`` come from
    deterministic generators but — unlike the C4 model and the commit-types
    hook — have no drift gate, so they silently rot. This refreshes them the
    way the ruff step refreshes formatting: regenerate in place, then
    ``git add`` the result into the commit. Only docs that **already exist**
    are touched (sync, never bootstrap a surprise tracked file in a consumer
    repo); the step self-skips when neither exists. Non-blocking — a generator
    crash warns rather than refusing the commit.

    Args:
        repo_root: Git repo root.

    Returns:
        ``StepResult`` (``non_blocking=True``); skipped when neither doc
        exists. ``passed`` is False only when a present generator errored.

    Raises:
        SystemExit: If a needed ``forge-gen-*`` CLI is not on PATH.
    """
    targets = [(cli, rel) for cli, rel in _REGEN_DOCS if (repo_root / rel).exists()]
    if not targets:
        return StepResult(
            name="regen_docs",
            passed=True,
            output="(no generated docs present — skipped)",
            skipped=True,
        )
    passed = True
    sections: list[str] = []
    for cli, _rel in targets:
        require_cli(cli, caller="forge-precommit")
        ok, output = _run([cli], cwd=repo_root)
        passed = passed and ok
        sections.append(f"$ {cli}\n{output.strip() or '(no output)'}")
    restaged = stage_modified_paths(repo_root, [rel for _, rel in targets])
    if restaged:
        sections.append("Re-staged: " + ", ".join(restaged))
    return StepResult(
        name="regen_docs",
        passed=passed,
        output="\n\n".join(sections),
        non_blocking=True,
    )


_VENDORED_DATA_DIR = "src/forge/data"
_VENDORED_MD = "src/forge/data/VENDORED.md"
_VENDORED_SHA_RE = re.compile(r"\*\*SHA-256:\*\*\s*`([0-9a-f]{64})`")
_VENDORED_HEADER_RE = re.compile(r"^##\s+(\S+)\s*$")


def _vendored_documented_hashes(repo_root: Path) -> dict[str, str]:
    """Parse ``VENDORED.md`` into a ``{filename: sha256}`` map.

    Each ``## <filename>`` section documents one vendored asset; the
    ``**SHA-256:** `<hash>``` line beneath it records its expected digest.

    Args:
        repo_root: Git repo root.

    Returns:
        Mapping of vendored filename to documented lowercase SHA-256; empty
        when ``VENDORED.md`` is absent.
    """
    md = repo_root / _VENDORED_MD
    if not md.exists():
        return {}
    hashes: dict[str, str] = {}
    current: str | None = None
    for line in md.read_text(encoding="utf-8").splitlines():
        header = _VENDORED_HEADER_RE.match(line)
        if header:
            current = header.group(1)
            continue
        sha = _VENDORED_SHA_RE.search(line)
        if sha and current:
            hashes[current] = sha.group(1)
    return hashes


def _sha256_file(path: Path) -> str:
    """Return *path*'s SHA-256 hex digest, read in 64 KiB chunks.

    Chunked so a multi-megabyte vendored bundle never loads whole into
    memory.

    Args:
        path: File to hash.

    Returns:
        Lowercase 64-character hex digest.
    """
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def step_vendored_integrity(repo_root: Path) -> StepResult:
    """Verify each vendored ``data/*.js`` blob matches its ``VENDORED.md`` hash.

    The large third-party bundles under ``src/forge/data/`` (Mermaid, the ELK
    layout) carry a documented provenance SHA-256 in ``VENDORED.md``, but
    nothing enforces it — a silently swapped or corrupted blob, or a stale
    ``VENDORED.md`` after a manual re-bundle, would pass unnoticed. This turns
    the documented hash into an enforced invariant: a mismatch, or a vendored
    ``*.js`` with no documented hash, **fails the commit**. An orphaned entry
    (documented but the file is gone) is a non-fatal note. Self-skips when
    there is no ``VENDORED.md`` or no vendored ``*.js``.

    Args:
        repo_root: Git repo root.

    Returns:
        ``StepResult``; skipped when nothing to verify, otherwise blocking on
        any hash mismatch or undocumented blob.
    """
    data_dir = repo_root / _VENDORED_DATA_DIR
    blobs = sorted(data_dir.glob("*.js")) if data_dir.is_dir() else []
    documented = _vendored_documented_hashes(repo_root)
    if not blobs or not documented:
        return StepResult(
            name="vendored_integrity",
            passed=True,
            output="(no vendored *.js + VENDORED.md to verify — skipped)",
            skipped=True,
        )
    problems: list[str] = []
    for blob in blobs:
        expected = documented.get(blob.name)
        if expected is None:
            problems.append(f"{blob.name}: no SHA-256 entry in VENDORED.md")
            continue
        actual = _sha256_file(blob)
        if actual != expected:
            problems.append(
                f"{blob.name}: SHA-256 mismatch\n"
                f"    expected {expected}\n    actual   {actual}"
            )
    present = {b.name for b in blobs}
    orphans = [name for name in sorted(documented) if name not in present]
    lines = [f"Verified {len(blobs)} vendored asset(s) against VENDORED.md."]
    if orphans:
        lines.append("Note (documented but absent): " + ", ".join(orphans))
    if problems:
        lines.append("FAIL:\n  " + "\n  ".join(problems))
    return StepResult(
        name="vendored_integrity",
        passed=not problems,
        output="\n".join(lines),
    )


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def _write_log(repo_root: Path, result: StepResult) -> None:
    """Persist *result*'s output to ``code_health/<name>.log``.

    Thin wrapper over ``git_utils.write_step_log`` so every step writes
    via the shared helper.

    Args:
        repo_root: Git repo root.
        result: Step outcome to log.
    """
    write_step_log(repo_root, result.name, result.output)


def _print_step_line(result: StepResult) -> None:
    """Print a one-line status for *result* (SKIP/PASS/WARN/FAIL).

    Non-blocking failures render as ``WARN`` in yellow — visibly distinct
    from blocking ``FAIL`` so the contributor can tell at a glance that
    the commit is not blocked.

    Args:
        result: Step outcome.
    """
    if result.skipped:
        marker, color = "SKIP", ""
    elif result.passed:
        marker, color = "PASS", GREEN
    elif result.non_blocking:
        marker, color = "WARN", YELLOW
    else:
        marker, color = "FAIL", RED
    emit(f"{result.name:<24} {color}{marker}{NC}")


# Ordered registry — the single source of truth for which steps exist,
# their run order, and which are on by default. Opt-in steps
# (default_on=False) run only when named in [tool.forge.precommit].enable
# or --only. auto_rebuild runs first (heals a stale install before env_sync
# would block on it); env_sync is the fast freshness gate; regen_docs and ruff
# follow because they mutate + re-stage files before any validator sees the
# diff (regen_docs refreshes generated docs, ruff reformats source).
_STEP_REGISTRY: tuple[StepDef, ...] = (
    StepDef("auto_rebuild", step_auto_rebuild),
    StepDef("env_sync", step_env_sync),
    StepDef("regen_docs", step_regen_docs),
    StepDef("ruff", step_ruff),
    StepDef("docstring_verification", step_docstrings),
    StepDef("docstring_coverage", step_docstring_coverage),
    StepDef("test_naming_check", step_test_naming),
    StepDef("repo_structure_check", step_repo_structure),
    StepDef("manifest_json", step_manifest_json),
    StepDef("cli_wiring", step_cli_wiring),
    StepDef("commit_types_parity", step_commit_types_parity),
    StepDef("plugin_version", step_plugin_version),
    StepDef("release_tag_guard", step_release_tag_guard),
    StepDef("changelog_history", step_changelog_history),
    StepDef("vendored_integrity", step_vendored_integrity),
    StepDef("pip_audit", step_pip_audit),
    StepDef("cve_usage", step_cve_usage),
    StepDef("doctest", step_doctest, default_on=False),
    StepDef("typecheck", step_typecheck, default_on=False),
    StepDef("doc_consistency", step_doc_consistency, default_on=False),
    StepDef("c4", step_c4, default_on=False),
)

_DEFAULT_ON: frozenset[str] = frozenset(d.name for d in _STEP_REGISTRY if d.default_on)


def _validate_step_names(names: Sequence[str]) -> None:
    """Raise ``ValueError`` listing any *names* that are not registered steps.

    Args:
        names: Step names referenced by config or CLI flags.

    Raises:
        ValueError: When one or more names are unknown; the message names
            the offenders and lists every valid step.
    """
    unknown = sorted(set(names) - {d.name for d in _STEP_REGISTRY})
    if unknown:
        valid = ", ".join(d.name for d in _STEP_REGISTRY)
        msg = f"unknown step name(s): {unknown}. Valid names: {valid}"
        raise ValueError(msg)


def _resolve_steps(
    repo_root: Path,
    *,
    skip: Sequence[str] = (),
    only: Sequence[str] = (),
) -> list[StepDef]:
    """Resolve which steps to run, in registry order.

    ``--only`` selects the base set (those steps instead of the defaults);
    otherwise the base set is the default-on steps plus
    ``[tool.forge.precommit] enable``, minus ``[tool.forge.precommit]
    disable``. ``skip`` then subtracts from **either** base — so
    ``--only X --skip X`` runs nothing, and ``--skip`` is never silently
    ignored. An explicit exclusion wins: ``disable`` / ``skip`` beat
    ``enable`` when a name appears in both. Every referenced name is
    validated against the registry; an unknown name raises ``ValueError``
    so the caller prints one clean message instead of leaking a traceback
    on a config typo.

    Args:
        repo_root: Git repo root.
        skip: Step names to force-skip for this run (CLI ``--skip``).
        only: When non-empty, the exact set of steps to run (CLI ``--only``).

    Returns:
        The selected ``StepDef`` entries in registry order.

    Raises:
        ValueError: When any referenced name is not a registered step.
    """
    precommit_cfg = _forge_step_config(repo_root, "precommit")
    enable = _cfg_str_list(precommit_cfg, "enable", [])
    disable = _cfg_str_list(precommit_cfg, "disable", [])
    _validate_step_names([*enable, *disable, *skip, *only])
    base = set(only) if only else (_DEFAULT_ON | set(enable)) - set(disable)
    chosen = base - set(skip)
    return [d for d in _STEP_REGISTRY if d.name in chosen]


def run_all(
    repo_root: Path | None = None,
    *,
    print_progress: bool = True,
    skip: Sequence[str] = (),
    only: Sequence[str] = (),
) -> list[StepResult]:
    """Run the resolved step sequence in order and return their results.

    The sequence is resolved from the registry via :func:`_resolve_steps`
    (``[tool.forge.precommit] enable/disable`` plus ``skip`` / ``only``).
    ``step_env_sync`` runs first (a fast in-process install-freshness gate);
    ``step_ruff`` follows and shells out to ``fix-forge-ruff`` (applies ruff
    fixes and re-stages modified tracked files); the rest verify only.

    Args:
        repo_root: Override the auto-detected git repo root. Useful in tests.
        print_progress: Print one-line PASS/FAIL/SKIP per step. Disable for
            JSON output to keep stdout machine-readable.
        skip: Step names to force-skip for this run.
        only: When non-empty, run exactly these steps.

    Returns:
        List of ``StepResult``, one per executed step, in execution order.

    Raises:
        ValueError: When ``skip`` / ``only`` / config names an unknown step.
    """
    root = repo_root if repo_root is not None else get_repo_root()
    results: list[StepResult] = []
    this_module = sys.modules[__name__]
    for step_def in _resolve_steps(root, skip=skip, only=only):
        # Resolve each step by name through the module namespace rather than
        # calling ``step_def.fn`` directly. The registry captured the
        # original function objects at import time, so a test that does
        # ``monkeypatch.setattr(precommit, "step_ruff", stub)`` would be
        # invisible to a direct ``step_def.fn`` call. Re-resolving by name is
        # the load-bearing seam that keeps per-step monkeypatching working.
        fn = getattr(this_module, step_def.fn.__name__)
        result = fn(root)
        if print_progress:
            _print_step_line(result)
        _write_log(root, result)
        results.append(result)
    return results


def _split_csv(values: Sequence[str]) -> list[str]:
    """Flatten repeatable / comma-separated CLI values into a clean name list.

    ``--skip a,b --skip c`` and ``--skip a --skip b --skip c`` both yield
    ``["a", "b", "c"]``. Empty fragments (from stray commas) are dropped.

    Args:
        values: Raw ``append``-collected argument values.

    Returns:
        Flattened, stripped, non-empty tokens in order.
    """
    out: list[str] = []
    for value in values:
        out.extend(token.strip() for token in value.split(",") if token.strip())
    return out


def main() -> int:
    """CLI entry point.

    Returns:
        ``0`` if every non-skipped step passed; ``1`` otherwise.
    """
    parser = argparse.ArgumentParser(
        prog="forge-precommit",
        description=(
            "Run the forge pre-commit check sequence: ruff (format + check, "
            "self-healing with --unsafe-fixes on failure) + docstring "
            "verification (diff vs main) + test-name verification (diff vs "
            "main) + repo-structure verification (REPO_STRUCTURE.md vs the "
            "tree) + plugin manifest JSON + plugin version drift guard + "
            "pip-audit (non-blocking) — when applicable. Ruff fixes apply "
            "automatically on every run; fixed files are re-staged. Pytest "
            "is not in the default sequence — run it in CI or wire it into "
            ".githooks/pre-commit explicitly. Used by any repo that adopts "
            "forge via install-forge-githooks."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit a JSON summary on stdout instead of human output.",
    )
    parser.add_argument(
        "--skip",
        action="append",
        default=[],
        metavar="STEP[,STEP...]",
        help="Force-skip these steps for this run (repeatable or comma-separated).",
    )
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        metavar="STEP[,STEP...]",
        help="Run exactly these steps (repeatable or comma-separated).",
    )
    args = parser.parse_args()

    skip = _split_csv(args.skip)
    only = _split_csv(args.only)
    try:
        results = run_all(print_progress=not args.json, skip=skip, only=only)
    except ValueError as exc:
        emit(f"{RED}forge-precommit: {exc}{NC}")
        return 1

    blocking_failures = [r for r in results if not r.passed and not r.non_blocking]
    non_blocking_warnings = [r for r in results if not r.passed and r.non_blocking]
    if args.json:
        emit(json.dumps([asdict(r) for r in results], indent=2))
    else:
        emit("")
        if blocking_failures:
            emit(f"{RED}Pre-commit checks failed:{NC}")
            for r in blocking_failures:
                emit(f"  - {r.name}: see code_health/{r.name}.log")
            if non_blocking_warnings:
                emit(
                    f"{YELLOW}Plus {len(non_blocking_warnings)} non-blocking "
                    f"warning(s) — see code_health/ logs.{NC}",
                )
        elif non_blocking_warnings:
            emit(
                f"{YELLOW}All blocking checks passed. "
                f"{len(non_blocking_warnings)} non-blocking warning(s):{NC}",
            )
            for r in non_blocking_warnings:
                emit(f"  - {r.name}: see code_health/{r.name}.log")
        else:
            emit(f"{GREEN}All checks passed.{NC}")

    return 1 if blocking_failures else 0


if __name__ == "__main__":
    sys.exit(main())
