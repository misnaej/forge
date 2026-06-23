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
from dataclasses import dataclass
from pathlib import Path

from forge.config import read_pyproject_raw
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
        "main",
        "Fast-channel integration branch (protected). Defaults to "
        "base_branch (single-track); set to e.g. 'dev' to opt into "
        "dual-track.",
    ),
    ConfigKey(
        ("tool", "forge", "source_dirs"),
        "smart-detect (src/ or top-level packages)",
        "Repo source roots — the single definition every layout-aware tool "
        "(ruff, api-digest, docstring-coverage, doctest, typecheck) scans. "
        "Unset → smart auto-detect: src/ if present, else top-level packages.",
    ),
    ConfigKey(
        ("tool", "forge", "test_dirs"),
        "smart-detect (tests/ or test/)",
        "Repo test roots (added for tools that scan tests too, e.g. ruff, "
        "coverage). Unset → smart auto-detect of tests/ then test/.",
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
        description="Generate interrogate's coverage badge to "
        ".badges/DocstringCoverage.svg for README embedding.",
    ),
    ConfigKey(
        ("tool", "forge", "docstring_coverage", "paths"),
        "source_dirs + test_dirs",
        "Per-tool override of the coverage scan roots; otherwise inherits "
        "the repo-wide [tool.forge].source_dirs + test_dirs.",
    ),
    ConfigKey(
        ("tool", "forge", "ruff", "paths"),
        "source_dirs + test_dirs",
        "Per-tool override of ruff's scan roots; otherwise inherits the "
        "repo-wide [tool.forge].source_dirs + test_dirs.",
    ),
    ConfigKey(
        ("tool", "forge", "api_digest", "paths"),
        "source_dirs",
        "Per-tool override of api-digest's scan roots; otherwise inherits "
        "the repo-wide [tool.forge].source_dirs (source only, no tests).",
    ),
    ConfigKey(
        ("tool", "forge", "precommit", "disable"),
        default=[],
        description="Force-skip these pre-commit steps by name (over each "
        "step's own self-skip).",
    ),
    ConfigKey(
        ("tool", "forge", "precommit", "enable"),
        default=[],
        description="Opt into normally-off pre-commit steps by name "
        "(doctest, typecheck, doc_consistency).",
    ),
    ConfigKey(
        ("tool", "forge", "precommit", "scope"),
        default="all",
        description="Default file scope for scope-aware steps (ruff, "
        "docstring_verification, test_naming_check): 'all' (whole tracked "
        "tree) or 'diff' (modified files vs main).",
    ),
    ConfigKey(
        ("tool", "forge", "precommit", "scope_overrides"),
        default={},
        description="Per-step scope overrides, e.g. {ruff = 'diff'}. Each "
        "value is 'all' or 'diff'; wins over the global 'scope' key.",
    ),
    ConfigKey(
        ("tool", "forge", "doctest", "paths"),
        "source_dirs (smart-detect)",
        "Per-tool override of doctest's scan roots; otherwise inherits the "
        "repo-wide [tool.forge].source_dirs (source only, no tests).",
    ),
    ConfigKey(
        ("tool", "forge", "doctest", "blocking"),
        default=False,
        description="Make the doctest step fail the commit on a broken "
        "example (default: non-blocking WARN).",
    ),
    ConfigKey(
        ("tool", "forge", "typecheck", "paths"),
        "source_dirs (smart-detect)",
        "Per-tool override of typecheck's scan roots; otherwise inherits the "
        "repo-wide [tool.forge].source_dirs (source only, no tests).",
    ),
    ConfigKey(
        ("tool", "forge", "typecheck", "blocking"),
        default=False,
        description="Make the typecheck step fail the commit on a checker "
        "error (default: non-blocking WARN).",
    ),
    ConfigKey(
        ("tool", "forge", "pip_audit", "blocking"),
        default=False,
        description="Make the pip_audit step fail the commit on a CVE "
        "finding (default: non-blocking WARN; a missing pip-audit binary "
        "stays a WARN regardless).",
    ),
    ConfigKey(
        ("tool", "forge", "cve_usage", "paths"),
        "source_dirs + test_dirs",
        "Per-tool override of the CVE-usage scan roots; otherwise inherits "
        "the repo-wide [tool.forge].source_dirs + test_dirs.",
    ),
    ConfigKey(
        ("tool", "forge", "badges", "enabled"),
        default=False,
        description="Opt into the README status-badge block written by "
        "install-forge-readme-badges (and the bootstrap readme-badges step).",
    ),
    ConfigKey(
        ("tool", "forge", "badges", "readme"),
        "README.md",
        "README file the badge managed-block is written into.",
    ),
    ConfigKey(
        ("tool", "forge", "badges", "workflow"),
        "first workflow alphabetically",
        "GitHub Actions workflow filename for the CI badge (under "
        ".github/workflows); otherwise the first one is used.",
    ),
)

# Third-party tools forge reads from their OWN native section rather than
# wrapping under [tool.forge.*]. Named here so consumers see what forge
# reads without it being hidden behind a forge namespace.
# Path tuples match CONFIG_KEYS.path encoding (not dotted strings).
NATIVE_SECTIONS: tuple[tuple[tuple[str, ...], str], ...] = (
    (
        ("tool", "interrogate"),
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
    for path, desc in NATIVE_SECTIONS:
        present = _lookup(data, path) is not _UNSET
        flag = "set" if present else "not set"
        section = ".".join(path)
        lines.append(f"[{section}]  ({flag} — native tool section, read by forge)")
        lines.append(f"  {desc}")

    if missing:
        lines.append("")
        lines.append("Suggested setup (forge reads these but you haven't set them):")
        for key in missing:
            lines.append(f"  • [{_section_of(key)}].{key.path[-1]} — {key.description}")
            lines.append(f"        {key.path[-1]} = {key.default!r}")
    return lines


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

    for line in build_report(read_pyproject_raw(Path.cwd())):
        logger.info("%s", line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
