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

Usage:
    forge-doctor                              # human-readable
    forge-doctor --json                       # machine-readable
    forge-doctor --plugin-name myrepo         # check a non-forge plugin
"""

import argparse
import json
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from importlib import metadata
from pathlib import Path

from forge.git_utils import emit


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
    try:
        dist = metadata.distribution(DIST_NAME)
    except metadata.PackageNotFoundError:
        return []
    return sorted(ep.name for ep in dist.entry_points if ep.group == "console_scripts")


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
        CheckResult(name="gh:installed", passed=True, detail=shutil.which("gh")),
        CheckResult(
            name="gh:authenticated",
            passed=auth.returncode == 0,
            detail="ok" if auth.returncode == 0 else "run `gh auth login`",
        ),
    ]


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

    if not args.skip_plugin_checks:
        plugin_check = _check_plugin_install(args.plugin_name)
        results.append(plugin_check)
        plugin_root = (
            _find_plugin_dir(args.plugin_name) if plugin_check.passed else None
        )
        results.extend(_check_plugin_manifests(plugin_root, args.plugin_name))
        results.extend(_check_plugin_contents(plugin_root))

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
