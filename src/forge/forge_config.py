"""forge-config — show what forge config this repo sets, and what it should.

Answers "which ``[tool.*]`` sections does forge actually read, and what
do I still need to set?" in one command, so a consumer never has to hunt
through docs or source.

Two responsibilities:

1. **List** every ``[tool.forge.*]`` key forge reads, its current value
   (or ``<default>`` when unset), and a one-line description.
2. **Advise** — for any recommended-but-unset key, print what to add.

Transparency over wrapping: forge reads some third-party tools from
their **own native sections** (notably ``[tool.interrogate]`` for
docstring coverage). Rather than hide that behind a forge namespace,
this CLI names it explicitly — forge reads ``[tool.interrogate]``; it is
the tool's own config, not a forge wrapper. Forge-specific keys the tool
has no concept of (``badge``, ``paths``) live under
``[tool.forge.docstring_coverage]``.

Read-only: prints a report and exits ``0``. Surfaced as a post-install
nudge by ``install-forge-bootstrap`` and on demand via ``forge-config``.
"""

from __future__ import annotations

import argparse
import logging
import tomllib
from dataclasses import dataclass
from pathlib import Path

from forge.git_utils import configure_cli_logging


configure_cli_logging()
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ConfigKey:
    """One ``[tool.forge.*]`` key forge reads in a consumer repo.

    Attributes:
        path: Full key path under the TOML root, e.g.
            ``("tool", "forge", "base_branch")``.
        default: Value forge falls back to when the key is unset.
        description: One-line purpose, shown beside the value.
        recommended: When ``True``, the advisor nudges the consumer to
            set the key if it is currently absent.
    """

    path: tuple[str, ...]
    default: object
    description: str
    recommended: bool = False


# The forge config surface, declared once. ``forge-config`` is the single
# place that enumerates what forge reads — there is no metadata registry
# elsewhere to keep in sync, so new ``[tool.forge.*]`` keys are added here.
CONFIG_KEYS: tuple[ConfigKey, ...] = (
    ConfigKey(
        ("tool", "forge", "base_branch"),
        "main",
        "Slow-channel / release branch (protected; promotion target).",
    ),
    ConfigKey(
        ("tool", "forge", "dev_branch"),
        "dev",
        "Fast-channel integration branch (protected). Set == base_branch "
        "for single-branch repos.",
    ),
    ConfigKey(
        ("tool", "forge", "cli_wiring", "enabled"),
        default=False,
        description="Opt into the cli_wiring pre-commit step (every [project.scripts] "
        "reachable from a wiring source).",
    ),
    ConfigKey(
        ("tool", "forge", "docstring_coverage", "badge"),
        default=False,
        description="Write .badges/DocstringCoverage.svg for README embedding.",
    ),
    ConfigKey(
        ("tool", "forge", "docstring_coverage", "paths"),
        ["src", "tests"],
        "Scan roots for the coverage report (forge-specific; interrogate "
        "has no scan-root concept).",
        recommended=True,
    ),
)

# Third-party tools forge reads from their OWN native section rather than
# wrapping under [tool.forge.*]. Named here so consumers see what forge
# reads without it being hidden behind a forge namespace.
NATIVE_SECTIONS: tuple[tuple[str, str], ...] = (
    (
        "tool.interrogate",
        "Docstring-coverage gate (fail-under, exclude, ignore-*) read by "
        "verify-forge-docstring-coverage. interrogate's own section — "
        "forge reads it directly, not a forge wrapper.",
    ),
)


_UNSET = object()


def _lookup(data: dict, path: tuple[str, ...]) -> object:
    """Return the value at *path* in nested *data*, or ``_UNSET`` if absent.

    Args:
        data: Parsed ``pyproject.toml`` data.
        path: Key path under the TOML root.

    Returns:
        The configured value, or the ``_UNSET`` sentinel when any segment
        of the path is missing.
    """
    node: object = data
    for segment in path:
        if not isinstance(node, dict) or segment not in node:
            return _UNSET
        node = node[segment]
    return node


def _section_of(key: ConfigKey) -> str:
    """Return the section header (path without the leaf key) for *key*.

    Args:
        key: The config key whose parent section is wanted.

    Returns:
        Dotted section path, e.g. ``"tool.forge.docstring_coverage"``.
    """
    return ".".join(key.path[:-1])


def build_report(data: dict) -> list[str]:
    """Build the ``forge-config`` report lines from parsed pyproject data.

    Args:
        data: Parsed ``pyproject.toml`` data (``{}`` when absent).

    Returns:
        Human-readable report lines: per-section key listing with current
        values or defaults, the native-section pointers, and a suggested-
        setup block for recommended keys that are unset.
    """
    lines = ["forge config in this repo (pyproject.toml)", "=" * 42]
    missing: list[ConfigKey] = []
    current_section = ""
    for key in CONFIG_KEYS:
        section = _section_of(key)
        if section != current_section:
            lines.append(f"[{section}]")
            current_section = section
        value = _lookup(data, key.path)
        leaf = key.path[-1]
        if value is _UNSET:
            lines.append(f"  {leaf:<14} = <default: {key.default!r}>   (not set)")
            if key.recommended:
                missing.append(key)
        else:
            lines.append(f"  {leaf:<14} = {value!r}")

    lines.append("")
    for section, desc in NATIVE_SECTIONS:
        present = _lookup(data, tuple(section.split("."))) is not _UNSET
        flag = "set" if present else "not set"
        lines.append(f"[{section}]  ({flag} — native tool section, read by forge)")
        lines.append(f"  {desc}")

    if missing:
        lines.append("")
        lines.append("Suggested setup (forge reads these but you haven't set them):")
        for key in missing:
            lines.append(f"  • [{_section_of(key)}].{key.path[-1]} — {key.description}")
            lines.append(f"        {key.path[-1]} = {key.default!r}")
    return lines


def _read_pyproject(repo_root: Path) -> dict:
    """Load ``pyproject.toml`` from *repo_root*, or ``{}`` when absent.

    Args:
        repo_root: Repository root containing ``pyproject.toml``.

    Returns:
        Parsed TOML data, or an empty dict when the file is missing or
        unparseable (treated as "no forge config set").
    """
    pyproject = repo_root / "pyproject.toml"
    if not pyproject.is_file():
        return {}
    try:
        return tomllib.loads(pyproject.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def main() -> int:
    """Entry point for ``forge-config``.

    Returns:
        Always ``0`` — a read-only advisory report, never a gate.
    """
    parser = argparse.ArgumentParser(
        prog="forge-config",
        description=(
            "List the [tool.forge.*] config forge reads in this repo, with "
            "current values / defaults, the native tool sections forge "
            "reads, and advice on recommended-but-unset keys."
        ),
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List forge config + advice (the default action).",
    )
    parser.parse_args()

    for line in build_report(_read_pyproject(Path.cwd())):
        logger.info("%s", line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
