"""verify-forge-repo-structure — verify REPO_STRUCTURE.md matches the actual tree.

Parses ``REPO_STRUCTURE.md`` at the repo root, extracts the filesystem
paths it documents, and compares them against the real repository to
detect drift in both directions:

- **Documented but missing** — a path named in ``REPO_STRUCTURE.md`` does
  not exist on disk (stale documentation).
- **Important but undocumented** — a top-level file or directory that
  should be documented is absent from ``REPO_STRUCTURE.md``.

Path extraction is generic: backtick-quoted paths, section headers of the
form ``## Name (`path/`)``, numbered items, and indented file/subdir
references under a directory section are all recognised.

Usage:

    # Check REPO_STRUCTURE.md against the repo
    verify-forge-repo-structure

    # Show every extracted path before reporting drift
    verify-forge-repo-structure --verbose

Exit Codes:
    0: REPO_STRUCTURE.md is in sync with the repository.
    1: Drift detected, or REPO_STRUCTURE.md is missing.

Integration:
    Called by ``forge-precommit`` as the ``repo_structure_check`` step;
    its output is written to ``code_health/repo_structure_check.log``.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from typing import TYPE_CHECKING

from forge.git_utils import capturing_to_step_log, configure_cli_logging, repo_root


if TYPE_CHECKING:
    from pathlib import Path


configure_cli_logging()
logger = logging.getLogger(__name__)


# Patterns to always ignore when scanning the top-level tree.
IGNORE_PATTERNS = (
    r"^\.git$",
    r"^\.plan$",
    r"^\.cache$",
    r"^\.ruff_cache$",
    r"^\.pytest_cache$",
    r"^\.mypy_cache$",
    r"^__pycache__$",
    r"^.*\.egg-info$",
    r"^build$",
    r"^dist$",
    r"^tmp$",
    r"^code_health$",
    r"^.*\.pyc$",
    r"^.*\.pyo$",
    r"^.*\.swp$",
    r"^.*~$",
)

# Top-level items that MUST be documented in REPO_STRUCTURE.md.
MUST_DOCUMENT = frozenset(
    {
        # Directories
        "src",
        "tests",
        "agents",
        "skills",
        "claude-hooks",
        "docs",
        "dev",
        ".claude-plugin",
        ".githooks",
        ".github",
        # Files
        "CLAUDE.md",
        "FOUNDATION.md",
        "README.md",
        "REPO_STRUCTURE.md",
        "CONTRIBUTING.md",
        "LICENSE",
        "pyproject.toml",
        "ruff.toml",
    },
)

# Section headers that introduce a path-bearing markdown section.
_SECTION_WITH_PATH = re.compile(r"^##[^(]*\(`([^)]+)`\)")
_SECTION_WITHOUT_PATH = re.compile(r"^## ")
# Numbered list items: ``1. **Name (`path/`)**`` or ``12. **Core Modules**``.
_NUMBERED_WITH_PATH = re.compile(r"^\d+\.\s+\*\*[^(]*\(`([^)]+)`\)")
_NUMBERED_WITHOUT_PATH = re.compile(r"^\d+\.\s+\*\*[^(]+\*\*\s*$")
# Indented file/subdir references under a directory section.
_FILE_REFERENCE = re.compile(r"^\s+-\s+([a-zA-Z0-9_]+\.py)(?::|$|\s)")
_SUBDIR_REFERENCE = re.compile(r"^\s+-\s+([a-zA-Z0-9_]+)/:")
# Inline backtick paths and top-level file/dot-directory references.
_BACKTICK_PATH = re.compile(r"`([a-zA-Z0-9_./\-]+/?)`")
_TOP_LEVEL_FILE = re.compile(
    r"^\s*-\s+([A-Za-z][A-Za-z0-9_.\-]*"
    r"\.(?:md|toml|yml|yaml|ini|txt|rc|cfg|sh))(?::|$|\s)",
)
_TOP_LEVEL_BARE_FILE = re.compile(r"^\s*-\s+(LICENSE)(?::|$|\s)")
_DOT_DIR_REFERENCE = re.compile(r"^\s*-\s+(\.[a-zA-Z0-9_\-]+)/?:")
_VERSION_LIKE = re.compile(r"^\d+\.\d+")


def should_ignore(name: str) -> bool:
    """Check whether a top-level path name should be ignored.

    Args:
        name: The file or directory name to check.

    Returns:
        True if the name matches any ignore pattern.
    """
    return any(re.match(pattern, name) for pattern in IGNORE_PATTERNS)


def _filter_paths(paths: set[str]) -> set[str]:
    """Filter out non-filesystem strings from extracted paths.

    Args:
        paths: Set of candidate path strings to filter.

    Returns:
        Filtered set containing only plausible filesystem paths.
    """
    filtered: set[str] = set()
    for path in paths:
        if path.startswith(("http", "--", "#")):
            continue
        if _VERSION_LIKE.match(path):
            continue
        filtered.add(path)
    return filtered


def _add_inline_paths(line: str, paths: set[str]) -> None:
    """Extract backtick paths and top-level references from a single line.

    Args:
        line: The markdown line to scan.
        paths: Set to add any extracted paths to (mutated in place).
    """
    for match in _BACKTICK_PATH.finditer(line):
        path = match.group(1).rstrip("/")
        if path and not path.startswith("-") and "/" in path:
            paths.add(path)

    top_file_match = _TOP_LEVEL_FILE.match(line)
    if top_file_match:
        paths.add(top_file_match.group(1))

    bare_file_match = _TOP_LEVEL_BARE_FILE.match(line)
    if bare_file_match:
        paths.add(bare_file_match.group(1))

    dot_dir_match = _DOT_DIR_REFERENCE.match(line)
    if dot_dir_match:
        paths.add(dot_dir_match.group(1))


def extract_paths_from_markdown(content: str) -> set[str]:
    """Extract filesystem paths mentioned in REPO_STRUCTURE.md.

    Parses the markdown with context awareness: file references indented
    under a directory section are resolved relative to that section's path.

    Args:
        content: The markdown content to parse.

    Returns:
        Set of filesystem paths documented in the markdown.
    """
    paths: set[str] = set()
    package_context: str | None = None
    subsection_context: str | None = None

    for line in content.split("\n"):
        if line.startswith("# "):
            package_context = None
            subsection_context = None
            continue

        section_match = _SECTION_WITH_PATH.match(line)
        if section_match:
            path = section_match.group(1).rstrip("/")
            paths.add(path)
            package_context = path
            subsection_context = None
            continue

        if _SECTION_WITHOUT_PATH.match(line) and "(`" not in line:
            package_context = None
            subsection_context = None
            continue

        numbered_with_path = _NUMBERED_WITH_PATH.match(line)
        if numbered_with_path:
            path = numbered_with_path.group(1).rstrip("/")
            paths.add(path)
            subsection_context = path
            continue

        if _NUMBERED_WITHOUT_PATH.match(line):
            subsection_context = package_context
            continue

        current_context = subsection_context or package_context

        file_match = _FILE_REFERENCE.match(line)
        if file_match and current_context:
            paths.add(f"{current_context}/{file_match.group(1)}")
            continue

        subdir_match = _SUBDIR_REFERENCE.match(line)
        if subdir_match and current_context:
            subdir_path = f"{current_context}/{subdir_match.group(1)}"
            paths.add(subdir_path)
            subsection_context = subdir_path
            continue

        _add_inline_paths(line, paths)

    return _filter_paths(paths)


def path_is_covered(path: str, documented_paths: set[str]) -> bool:
    """Check whether a path is covered by the documented paths.

    A path is covered if it appears directly in *documented_paths* or if
    any documented path is a child of it (e.g. ``src`` is covered when
    ``src/forge`` is documented).

    Args:
        path: The path to check.
        documented_paths: Set of paths documented in REPO_STRUCTURE.md.

    Returns:
        True if the path is covered by documentation.
    """
    if path in documented_paths:
        return True
    return any(doc.startswith(path + "/") for doc in documented_paths)


def get_actual_top_level(root: Path) -> set[str]:
    """Get the top-level items that should be documented.

    Args:
        root: Repository root directory.

    Returns:
        Set of top-level file/directory names present on disk and listed
        in ``MUST_DOCUMENT``.
    """
    return {item.name for item in root.iterdir() if item.name in MUST_DOCUMENT}


def verify_documented_paths_exist(documented_paths: set[str], root: Path) -> set[str]:
    """Find documented paths that do not exist on disk.

    Paths rooted under a gitignored directory (e.g. ``code_health/``,
    ``.plan/``) are skipped — they are runtime artifacts absent from a
    clean checkout, so their absence is not documentation drift.

    Args:
        documented_paths: Set of paths extracted from REPO_STRUCTURE.md.
        root: Repository root directory.

    Returns:
        Set of documented paths that are absent from the filesystem.
    """
    not_found: set[str] = set()
    for path in documented_paths:
        if should_ignore(path.split("/", 1)[0]):
            continue
        if ("/" in path or path in MUST_DOCUMENT) and not (root / path).exists():
            not_found.add(path)
    return not_found


def verify_structure(
    root: Path,
    *,
    verbose: bool = False,
) -> tuple[set[str], set[str], int]:
    """Verify REPO_STRUCTURE.md against the actual repository tree.

    Args:
        root: Repository root directory.
        verbose: Whether to log every extracted path.

    Returns:
        Tuple of ``(documented_not_found, important_not_documented,
        paths_checked)``.

    Raises:
        FileNotFoundError: If ``REPO_STRUCTURE.md`` does not exist.
    """
    repo_structure_path = root / "REPO_STRUCTURE.md"
    if not repo_structure_path.exists():
        msg = "REPO_STRUCTURE.md not found"
        raise FileNotFoundError(msg)

    documented_paths = extract_paths_from_markdown(repo_structure_path.read_text())

    if verbose:
        logger.info("Extracted paths from REPO_STRUCTURE.md:")
        for path in sorted(documented_paths):
            logger.info("  %s", path)
        logger.info("")

    documented_not_found = verify_documented_paths_exist(documented_paths, root)

    important_not_documented = {
        item
        for item in get_actual_top_level(root)
        if not path_is_covered(item, documented_paths)
    }

    return documented_not_found, important_not_documented, len(documented_paths)


def _log_issues(not_found: set[str], not_documented: set[str]) -> None:
    """Log details about the drift found.

    Args:
        not_found: Paths documented but not present on disk.
        not_documented: Top-level items present but not documented.
    """
    if not_found:
        logger.warning("DOCUMENTED BUT NOT FOUND (remove from REPO_STRUCTURE.md):")
        logger.warning("-" * 50)
        for path in sorted(not_found):
            logger.warning("  - %s", path)
        logger.warning("")

    if not_documented:
        logger.warning("IMPORTANT BUT NOT DOCUMENTED (add to REPO_STRUCTURE.md):")
        logger.warning("-" * 50)
        for path in sorted(not_documented):
            logger.warning("  + %s", path)
        logger.warning("")


def _log_fix_instructions(not_found: set[str], not_documented: set[str]) -> None:
    """Log instructions for resolving the detected drift.

    Args:
        not_found: Paths documented but not present on disk.
        not_documented: Top-level items present but not documented.
    """
    logger.info("HOW TO FIX:")
    logger.info("-" * 50)
    if not_found:
        logger.info("For paths documented but not found:")
        logger.info("  Remove or update these entries in REPO_STRUCTURE.md")
        logger.info("")
    if not_documented:
        logger.info("For paths that exist but aren't documented:")
        logger.info("  Add these to the appropriate section in REPO_STRUCTURE.md")
        logger.info("")


def main() -> int:
    """Verify REPO_STRUCTURE.md is in sync with the repository tree.

    Returns:
        Exit code: ``0`` when in sync, ``1`` when drift is detected or
        ``REPO_STRUCTURE.md`` is missing.
    """
    parser = argparse.ArgumentParser(
        prog="verify-forge-repo-structure",
        description="Verify REPO_STRUCTURE.md is in sync with actual structure.",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Show all extracted paths.",
    )
    args = parser.parse_args()

    root = repo_root()

    with capturing_to_step_log(root, "repo_structure_check"):
        logger.info("=" * 70)
        logger.info("REPO_STRUCTURE.md VERIFICATION")
        logger.info("=" * 70)
        logger.info("")

        try:
            not_found, not_documented, total = verify_structure(
                root,
                verbose=args.verbose,
            )
        except FileNotFoundError:
            logger.exception("REPO_STRUCTURE.md not found")
            return 1

        has_issues = bool(not_found or not_documented)
        _log_issues(not_found, not_documented)

        logger.info("=" * 70)
        if has_issues:
            logger.warning("RESULT: DRIFT DETECTED")
            logger.warning("  - Documented but missing: %d", len(not_found))
            logger.warning("  - Important but undocumented: %d", len(not_documented))
            logger.info("=" * 70)
            logger.info("")
            _log_fix_instructions(not_found, not_documented)
            return 1

        logger.info("RESULT: REPO_STRUCTURE.md is in sync")
        logger.info("  - Paths checked: %d", total)
        logger.info("=" * 70)
        return 0


if __name__ == "__main__":
    sys.exit(main())
