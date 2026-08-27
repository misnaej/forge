"""forge-doctor — diagnose a forge install in the current environment.

Runs a set of checks and prints a pass/fail report. Exits non-zero if
any check fails.

Checks:
  1. ``forge-scripts`` CLI entry points on PATH (pip package installed).
  2. ``gh`` CLI installed and authenticated (needed by
     ``install-forge-labels`` and any GitHub-aware workflow).
  3. (Plugin checks, optional) — only run when a Claude Code plugin
     cache for the configured plugin name is present:
     a. Plugin directory found under ``~/.claude/plugins/cache/``.
     b. ``plugin.json`` and ``marketplace.json`` present and well-formed.
     c. ``agents/``, ``skills/``, ``claude-hooks/`` directories populated.
  4. Version skew across install surfaces (#184) — the pip package, the
     git-hook sidecar, and the cached plugin should share a version; a
     lagging surface is reported as an advisory with the exact command to
     converge it (never fails the exit code — the report line is the signal).

Usage:
    forge-doctor                              # human-readable
    forge-doctor --json                       # machine-readable
    forge-doctor --plugin-name myrepo         # check a non-forge plugin
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from importlib import metadata
from pathlib import Path

from forge import config
from forge.config import installed_console_scripts
from forge.git_utils import emit, parse_semver
from forge.install_githooks import SIDECAR_NAME as _HOOK_VERSION_SIDECAR
from forge.upgrade import pin_revision_mismatch, pip_command


# Drift is only meaningful across at least two surfaces; a single present
# surface (e.g. a pip-only consumer with no hooks/plugin) has nothing to
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

DIST_NAME = "forge-scripts"


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


# A plugin name is joined under ``~/.claude/plugins/cache/`` as a single
# path component, so it must be a bare directory name. The charset excludes
# both path separators (``/`` / ``\``) — so the value can never split into
# multiple components — and, by requiring a leading alphanumeric, a bare
# ``..``. Those two together fully bar a parent-escaping ``..`` component
# from ever forming; a ``..`` *substring* like ``a..b`` is a harmless
# literal directory name and is allowed. ``\Z`` (not ``$``) also rejects a
# trailing newline, which ``$`` would admit.
_SAFE_PLUGIN_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\Z")


def _validate_plugin_name(name: str) -> str:
    """Argparse ``type`` for ``--plugin-name`` — reject a cache-escaping value.

    ``--plugin-name`` is joined straight onto ``~/.claude/plugins/cache/`` as
    one path component, so a value carrying a path separator (which would
    split it into multiple components, one possibly ``..``) or a bare ``..``
    could point the plugin reads at an arbitrary directory (#200). Fail
    loudly at parse time rather than silently reading the wrong tree.

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


def _find_plugin_dir(plugin_name: str) -> Path | None:
    """Locate a Claude Code plugin cache directory by name.

    Only checks the canonical ``~/.claude/plugins/cache/<plugin>`` path
    that Claude Code populates on ``/plugin install``. The marketplace
    source dir (``~/.claude/plugins/marketplaces/...``) is intentionally
    not searched here: marketplace dir names are ``<org>-<plugin>``,
    and embedding an org prefix in this lookup would tie
    ``--plugin-name`` to a single ``<org>/<plugin>`` source.

    Args:
        plugin_name: Plugin identifier (e.g. ``"forge"``).

    Returns:
        Absolute path to the plugin cache if found, otherwise ``None``.
    """
    cache = Path.home() / ".claude" / "plugins" / "cache" / plugin_name
    return cache if cache.is_dir() else None


def _check_plugin_install(plugin_name: str) -> CheckResult:
    """Verify Claude Code has installed the named plugin locally.

    Args:
        plugin_name: Plugin identifier (e.g. ``"forge"``).

    Returns:
        A ``CheckResult`` for the plugin install status.
    """
    found = _find_plugin_dir(plugin_name)
    if found is None:
        return CheckResult(
            name="plugin:installed",
            passed=False,
            detail=(f"{plugin_name} not in ~/.claude/plugins/cache/ or marketplaces/."),
        )
    return CheckResult(name="plugin:installed", passed=True, detail=str(found))


def _read_json(path: Path) -> tuple[dict, str | None]:
    """Read a JSON file. Returns (data, error_message_or_None).

    Args:
        path: Path to the JSON file to read.

    Returns:
        Tuple of (parsed JSON data dict, error message or None).
    """
    if not path.is_file():
        return {}, f"missing: {path}"
    try:
        with path.open() as fh:
            return json.load(fh), None
    except json.JSONDecodeError as exc:
        return {}, f"invalid JSON in {path}: {exc}"


def _find_install_dir(plugin_root: Path) -> Path | None:
    """Walk the Claude Code cache layout to find the active plugin install.

    Claude Code stores installed plugins under
    ``~/.claude/plugins/cache/<plugin>/<plugin>/<version>/`` — two levels
    nested below the cache slot, with one directory per cached version.
    Older versions and forks may flatten to one level or none. Walk
    up to two levels looking for the first directory that carries a
    ``.claude-plugin/plugin.json``; when multiple versions are
    present, pick the one with the highest semver-shaped name.

    Args:
        plugin_root: Cache slot for the plugin
            (``~/.claude/plugins/cache/<plugin>``).

    Returns:
        Path of the directory carrying ``.claude-plugin/plugin.json`` (the
        install root for diagnostics), or ``None`` when no valid layout is
        found at any depth.
    """
    candidates: list[Path] = []
    for depth_glob in (".claude-plugin", "*/.claude-plugin", "*/*/.claude-plugin"):
        candidates.extend(plugin_root.glob(depth_glob))
    valid = [c.parent for c in candidates if (c / "plugin.json").is_file()]
    if not valid:
        return None
    return max(valid, key=lambda p: _version_key(p.name))


def _version_key(name: str) -> tuple[int, ...]:
    """Return a sortable key for a version-shaped directory name.

    Args:
        name: Directory name (typically a bare semver like ``"1.13.0"``;
            falls back to a tuple of zeros when the name isn't
            version-shaped so the comparison degrades gracefully).

    Returns:
        Tuple of integers — ``(1, 13, 0)`` for ``"1.13.0"``,
        ``(0,)`` for any non-numeric name. Comparing tuples
        component-wise gives correct semver ordering (``1.13`` > ``1.9``,
        which lexicographic string compare gets wrong).
    """
    parts = name.split(".")
    try:
        return tuple(int(p) for p in parts)
    except ValueError:
        return (0,)


# One forge install exposes its version through three independently-written
# surfaces; when they drift, generated artifacts differ between contributors
# and the hooks may run older logic than the CLIs (#184). Remediation per
# surface — the single command that re-converges that one onto the current line.
_SKEW_REMEDIATION = {
    "pip package": "forge-upgrade --apply",
    "git hooks": "install-forge-githooks",
    "plugin cache": "/plugin update forge@forge (then /reload-plugins)",
}


def _surface_pip_version() -> str | None:
    """Version of the installed ``forge-scripts`` package, or None if absent."""
    try:
        return metadata.version(DIST_NAME)
    except metadata.PackageNotFoundError:
        return None


def _surface_hook_version(repo_root: Path) -> str | None:
    """Forge version recorded in the git-hook sidecar, or None when absent.

    Reads the gitignored ``.githooks/.forge-hook-version`` sidecar written by
    ``install-forge-githooks`` (the tracked hook *marker* deliberately omits
    the version to stay byte-stable across bumps — see that CLI). A repo whose
    hooks aren't forge-managed simply has no sidecar and is skipped.

    Args:
        repo_root: Directory whose ``.githooks/`` is inspected.

    Returns:
        The recorded version string (may carry a ``.devN+g<sha>`` suffix), or
        None when the sidecar is missing or empty.
    """
    sidecar = repo_root / ".githooks" / _HOOK_VERSION_SIDECAR
    if not sidecar.is_file():
        return None
    return sidecar.read_text(encoding="utf-8").strip() or None


def _surface_plugin_version(plugin_root: Path | None) -> str | None:
    """Version of the cached Claude Code plugin install, or None when absent.

    Prefers the ``version`` field of the installed ``plugin.json`` (robust to
    a flattened cache layout) and falls back to the cache directory name.

    Args:
        plugin_root: Cache slot for the plugin, or None when uncached.

    Returns:
        The cached plugin's version string, or None when no install is found.
    """
    if plugin_root is None:
        return None
    install_dir = _find_install_dir(plugin_root)
    if install_dir is None:
        return None
    data, err = _read_json(install_dir / ".claude-plugin" / "plugin.json")
    if err is None and data.get("version"):
        return str(data["version"])
    return install_dir.name


def _check_version_skew(
    repo_root: Path,
    plugin_root: Path | None,
) -> list[CheckResult]:
    """Compare forge's version across its install surfaces and flag drift (#184).

    Reads the three surfaces a single forge install presents — the pip
    ``forge-scripts`` package, the git-hook sidecar, and the cached Claude Code
    plugin — normalizes each with :func:`parse_semver` (so a ``.devN+g<sha>``
    editable version and a bare-semver plugin compare cleanly), and reports the
    ones lagging the highest (current) line with the exact command to converge
    them. Absent surfaces (no hooks, no plugin — e.g. a pip-only consumer) are
    skipped, not failed.

    Posture: a lagging surface is reported as an **advisory** (``info``), never
    a failure — it carries the remediation but never sways ``forge-doctor``'s
    exit code, matching the ``under_used_capabilities`` advisory pattern.
    Advisory-not-failing keeps the exit contract stable across environments (no
    fail-locally/pass-in-CI split, and no hard-fail on a CI runner that
    legitimately predates an install — FOUNDATION §15); the remediation line in
    the report is the actionable signal, not the exit code.

    Args:
        repo_root: Repo whose ``.githooks/`` sidecar is read.
        plugin_root: Cache slot for the plugin, or None when uncached / skipped.

    Returns:
        A single ``version_skew`` result when surfaces align (or too few to
        compare), else one advisory result per lagging surface naming its
        remediation.
    """
    raw = {
        "pip package": _surface_pip_version(),
        "git hooks": _surface_hook_version(repo_root),
        "plugin cache": _surface_plugin_version(plugin_root),
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
                detail=f"aligned at v{cur} ({aligned})",
            )
        ]

    return [
        CheckResult(
            name=f"version_skew:{name.replace(' ', '_')}",
            passed=False,
            info=True,
            detail=(
                f"{name} at v{'.'.join(str(n) for n in triple)}, behind current "
                f"v{cur} — run `{_SKEW_REMEDIATION[name]}`"
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
    plugin_root: Path | None,
    plugin_name: str,
) -> list[CheckResult]:
    """Validate plugin.json + marketplace.json under the installed plugin root.

    Args:
        plugin_root: Root directory of the installed plugin, or None if not found.
        plugin_name: Expected plugin name to match against ``plugin.json`` /
            ``marketplace.json``.

    Returns:
        List of check results for plugin.json and marketplace.json validation.
    """
    if plugin_root is None:
        return [
            CheckResult(
                name="plugin.json", passed=False, detail="plugin not installed"
            ),
            CheckResult(
                name="marketplace.json", passed=False, detail="plugin not installed"
            ),
        ]

    install_dir = _find_install_dir(plugin_root)
    manifest_dir = (install_dir / ".claude-plugin") if install_dir else None
    if manifest_dir is None:
        return [
            CheckResult(
                name="plugin.json", passed=False, detail="no .claude-plugin/ dir found"
            ),
            CheckResult(
                name="marketplace.json",
                passed=False,
                detail="no .claude-plugin/ dir found",
            ),
        ]

    plugin_data, plugin_err = _read_json(manifest_dir / "plugin.json")
    market_data, market_err = _read_json(manifest_dir / "marketplace.json")

    plugin_ok = plugin_err is None and plugin_data.get("name") == plugin_name
    market_ok = market_err is None and market_data.get("name") == plugin_name

    return [
        CheckResult(
            name="plugin.json",
            passed=plugin_ok,
            detail=plugin_err
            or f"name={plugin_data.get('name')}, version={plugin_data.get('version')}",
        ),
        CheckResult(
            name="marketplace.json",
            passed=market_ok,
            detail=market_err or f"name={market_data.get('name')}",
        ),
    ]


def _check_plugin_contents(plugin_root: Path | None) -> list[CheckResult]:
    """Verify the expected plugin sub-directories contain files.

    Args:
        plugin_root: Root directory of the installed plugin, or None if not found.

    Returns:
        List of check results for each expected plugin directory.
    """
    if plugin_root is None:
        return [
            CheckResult(name=f"plugin/{d}", passed=False, detail="plugin not installed")
            for d in EXPECTED_PLUGIN_DIRS
        ]

    plugin_dir = _find_install_dir(plugin_root)
    if plugin_dir is None:
        return [
            CheckResult(
                name=f"plugin/{d}",
                passed=False,
                detail="plugin dir layout unrecognised",
            )
            for d in EXPECTED_PLUGIN_DIRS
        ]

    results = []
    for d in EXPECTED_PLUGIN_DIRS:
        sub = plugin_dir / d
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
# them against the repo root.
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
        failing when the tool is missing. Empty when no such step is
        enabled.
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
    return results


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
        artifact = repo_root / artifact_relpath
        if artifact.exists():
            continue
        results.append(
            CheckResult(
                name=f"underused:{cli}",
                passed=True,
                detail=(
                    f"{cli} installed but {artifact_relpath} missing — "
                    f"run `{recommend}`."
                ),
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
            "Claude Code plugin name to check (default: forge). The plugin "
            "checks self-skip if no install is found, so consumers who don't "
            "use Claude Code can ignore this flag."
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

    plugin_root: Path | None = None
    if not args.skip_plugin_checks:
        plugin_check = _check_plugin_install(args.plugin_name)
        results.append(plugin_check)
        plugin_root = (
            _find_plugin_dir(args.plugin_name) if plugin_check.passed else None
        )
        results.extend(_check_plugin_manifests(plugin_root, args.plugin_name))
        results.extend(_check_plugin_contents(plugin_root))

    results.extend(_check_version_skew(Path.cwd(), plugin_root))
    results.extend(_surface_pin_revision(Path.cwd()))
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
