"""verify-forge-cli-wiring — assert every project script has a real caller.

Forge ships CLIs along several invocation paths: ``install-forge-bootstrap``
STEPS, ``forge.precommit`` step functions, ``forge-audit-all``, git hooks,
Claude Code hooks, ``dev/setup.sh``, foundation agents, ``/forge:*`` skills.
A new ``[project.scripts]`` entry that lands without being added to any of
those paths is a dangling CLI — installed but never invoked.

This verifier proves wiring by **reachability**: for every console-script
in ``pyproject.toml`` it greps the wiring source paths (listed in
:data:`WIRING_SOURCES` below) and passes when the name appears at least
once outside the script's own implementation file. The grep is the
contract — no parallel metadata registry to keep in sync.

If a CLI is intentionally unwired (release tooling, debugging utility,
externally invoked), list it in ``cli_wiring_exempt.toml`` at the repo
root with a one-line ``reason``:

    [exempt."forge-some-tool"]
    reason = "Invoked only from CI; no in-repo caller by design."

Exit codes:
    0  every script has at least one wiring hit (or a documented exempt entry)
    1  one or more scripts are unreachable, OR ``pyproject.toml`` could not
       be read, OR ``cli_wiring_exempt.toml`` is malformed.

Integration:
    Called by ``forge-precommit`` as the ``cli_wiring`` step; output is
    written to ``code_health/cli_wiring.log``.
"""

from __future__ import annotations

import argparse
import logging
import sys
import tomllib
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from pathlib import Path

from forge.git_utils import capturing_to_step_log, configure_cli_logging, repo_root


configure_cli_logging()
logger = logging.getLogger(__name__)


# Paths (relative to repo root) the verifier greps for each CLI name.
# A glob ending in ``/`` is treated as recursive (all files under the
# directory). A glob with ``*`` is expanded by :func:`Path.glob`.
WIRING_SOURCES: tuple[str, ...] = (
    "src/forge/install_bootstrap.py",
    "src/forge/precommit.py",
    "src/forge/audit/",
    ".githooks/",
    "claude-hooks/",
    "dev/",
    "agents/",
    "skills/",
)


def _entry_module_path(entry_point: str) -> str:
    """Translate a ``[project.scripts]`` entry point to its source path.

    ``"forge.audit.all:main"`` → ``"src/forge/audit/all.py"``.

    Args:
        entry_point: The ``module:callable`` string from pyproject.

    Returns:
        Relative path to the implementation file. Returned even when
        the file does not exist (caller may skip the resulting path).
    """
    module = entry_point.split(":", 1)[0]
    return "src/" + module.replace(".", "/") + ".py"


def _expand_source(root: Path, source: str) -> list[Path]:
    """Expand a :data:`WIRING_SOURCES` entry into concrete files.

    Args:
        root: Repo root.
        source: A path string from :data:`WIRING_SOURCES`.

    Returns:
        List of files to grep. Directories expand recursively to every
        regular file; single-file entries return themselves when they
        exist.
    """
    target = root / source
    if not target.exists():
        return []
    if target.is_dir():
        return [p for p in target.rglob("*") if p.is_file()]
    return [target]


def _build_wiring_index(root: Path) -> list[tuple[Path, str]]:
    """Read every wiring source once. Returns ``(path, text)`` pairs.

    Args:
        root: Repo root.

    Returns:
        List of ``(absolute_path, file_text)`` for every file under
        :data:`WIRING_SOURCES`. Files that fail to decode as UTF-8 are
        skipped silently — binary blobs do not carry CLI names.
    """
    index: list[tuple[Path, str]] = []
    for source in WIRING_SOURCES:
        for path in _expand_source(root, source):
            text = path.read_text(encoding="utf-8", errors="ignore")
            index.append((path, text))
    return index


def _reachable(name: str, self_path: Path, index: list[tuple[Path, str]]) -> list[Path]:
    """Return wiring files where *name* appears, excluding *self_path*.

    Args:
        name: CLI name to grep for.
        self_path: Implementation file to exclude from hits (a CLI
            naming itself in its own ``argparse`` ``prog=`` does not
            count as wiring).
        index: Output of :func:`_build_wiring_index`.

    Returns:
        Sorted list of paths where *name* appears as a substring.
    """
    hits: list[Path] = []
    for path, text in index:
        if path == self_path:
            continue
        if name in text:
            hits.append(path)
    return sorted(hits)


def _read_exempt(root: Path) -> dict[str, str]:
    """Load the optional ``cli_wiring_exempt.toml`` exempt list.

    Args:
        root: Repo root.

    Returns:
        Map of exempt CLI name → reason. Empty when the file does not
        exist (the file is optional).

    Raises:
        ValueError: When the file exists but is malformed or missing
            required fields. The caller surfaces this as a verify
            failure.
    """
    path = root / "cli_wiring_exempt.toml"
    if not path.is_file():
        return {}
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    exempt: dict[str, str] = {}
    for name, entry in (data.get("exempt") or {}).items():
        if not isinstance(entry, dict):
            msg = f"cli_wiring_exempt.toml: entry for {name!r} is not a table"
            raise TypeError(msg)
        reason = entry.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            msg = f"cli_wiring_exempt.toml: entry {name!r} missing 'reason' field"
            raise TypeError(msg)
        exempt[name] = reason
    return exempt


def _classify_scripts(
    root: Path,
    scripts: dict[str, str],
    exempt: dict[str, str],
    index: list[tuple[Path, str]],
) -> tuple[list[str], list[str]]:
    """Classify each script as reachable, exempt, or unreachable.

    Args:
        root: Repo root.
        scripts: Map of script name to entry point.
        exempt: Map of exempt script names to reasons.
        index: Wiring index from :func:`_build_wiring_index`.

    Returns:
        Tuple of ``(unreachable_names, stale_exempt_names)``.
    """
    unreachable: list[str] = []
    for name, entry_point in scripts.items():
        if name in exempt:
            logger.info("EXEMPT %-32s — %s", name, exempt[name])
            continue
        self_path = root / _entry_module_path(entry_point)
        hits = _reachable(name, self_path, index)
        if not hits:
            unreachable.append(name)
        else:
            logger.info("OK     %-32s — %d wiring hit(s)", name, len(hits))
    stale_exempt = sorted(set(exempt) - set(scripts))
    return unreachable, stale_exempt


def _emit_report(unreachable: list[str], stale_exempt: list[str]) -> None:
    """Log findings: unreachable scripts and stale exempt entries.

    Args:
        unreachable: Scripts with no wiring hits.
        stale_exempt: Exempt entries that no longer exist in scripts.
    """
    if unreachable:
        logger.error(
            "[ERROR] %d console-script(s) have no caller in any wiring "
            "source (and are not in cli_wiring_exempt.toml):",
            len(unreachable),
        )
        for name in unreachable:
            logger.error("  - %s", name)
        logger.error(
            "Wire the CLI into one of: %s. If it is intentionally "
            "unwired, add it to cli_wiring_exempt.toml with a reason.",
            ", ".join(WIRING_SOURCES),
        )

    if stale_exempt:
        logger.warning(
            "[WARN] %d cli_wiring_exempt.toml entr(y/ies) reference a "
            "name no longer in [project.scripts]:",
            len(stale_exempt),
        )
        for name in stale_exempt:
            logger.warning("  - %s", name)


def _check_wiring(root: Path) -> int:
    """Run the reachability check and report findings.

    Args:
        root: Repo root.

    Returns:
        ``0`` when every script is reachable or documented as exempt;
        ``1`` otherwise (also ``1`` when pyproject / exempt file are
        unreadable).
    """
    pyproject_path = root / "pyproject.toml"
    try:
        with pyproject_path.open("rb") as fh:
            pyproject = tomllib.load(fh)
    except (FileNotFoundError, tomllib.TOMLDecodeError):
        logger.exception("could not read %s", pyproject_path)
        return 1

    try:
        exempt = _read_exempt(root)
    except (tomllib.TOMLDecodeError, TypeError):
        logger.exception("cli_wiring_exempt.toml is malformed")
        return 1

    scripts: dict[str, str] = (pyproject.get("project") or {}).get("scripts", {})
    if not scripts:
        logger.info("OK: no [project.scripts] entries to check.")
        return 0

    index = _build_wiring_index(root)
    unreachable, stale_exempt = _classify_scripts(root, scripts, exempt, index)
    _emit_report(unreachable, stale_exempt)

    return 1 if unreachable else 0


def main() -> int:
    """Entry point for ``verify-forge-cli-wiring``.

    Returns:
        ``0`` when every script is reachable; ``1`` on any unreachable
        CLI or malformed input.
    """
    parser = argparse.ArgumentParser(
        prog="verify-forge-cli-wiring",
        description=(
            "Verify every [project.scripts] entry in pyproject.toml is "
            "reachable from at least one wiring source path "
            "(install-forge-bootstrap STEPS, forge.precommit steps, "
            "audit/, git hooks, claude-hooks, dev/, agents/, skills/) "
            "or is listed in cli_wiring_exempt.toml with a reason."
        ),
    )
    parser.parse_args()

    root = repo_root()

    with capturing_to_step_log(root, "cli_wiring"):
        return _check_wiring(root)


if __name__ == "__main__":
    sys.exit(main())
