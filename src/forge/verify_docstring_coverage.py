"""verify-forge-docstring-coverage — measure docstring coverage % across the repo.

Third of three layered docstring enforcers (see FOUNDATION §8).
**Ruff D100-D107** are the actual blocking gate for missing
docstrings on modules, classes, and public functions / methods —
they fire on the modified files in every commit and refuse the
commit when a top-level public symbol lacks a docstring.
``verify-forge-docstrings`` is the correctness layer for docstrings
that exist (Args match signature, Returns present, etc.). This CLI
is the **non-blocking reporter** that surfaces the residue ruff
misses — primarily nested functions and closures — together with
aggregate % and an optional README badge.

The step is non-blocking by design (``step_docstring_coverage``
returns ``StepResult(non_blocking=True)``). Blocking here would
duplicate ruff's gate and mostly fire on nested-function edge cases
ruff already considers acceptable. The MISSING list in the log is
the dispatch contract: ``forge:precommit-fixer`` reads it and adds
docstrings per entry.

Reads ``[tool.interrogate]`` (the tool's own native section — forge
does not wrap it) for threshold + excludes (``fail-under`` default 90).
Scan roots default to the repo-wide layout
``[tool.forge].source_dirs + test_dirs`` (default ``src`` + ``tests``);
a per-tool ``[tool.forge.docstring_coverage].paths`` overrides them
(interrogate has no scan-root concept). ``[tool.forge.docstring_coverage]
.badge = true`` writes ``.badges/DocstringCoverage.svg`` for README
embedding. Writes ``code_health/docstring_coverage.log``.

Exit codes:

- ``0`` — coverage meets or exceeds the threshold, OR the CLI
  self-skipped because no ``pyproject.toml`` / no ``src/`` was found.
- ``1`` — coverage is below the threshold. The wrapping precommit
  step is non-blocking so this still does not refuse the commit; the
  exit code only matters when the CLI is invoked standalone.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from interrogate.badge_gen import create as create_badge
from interrogate.config import InterrogateConfig
from interrogate.coverage import InterrogateCoverage

from forge.config import (
    DEFAULT_SOURCE_DIRS,
    DEFAULT_TEST_DIRS,
    read_pyproject_raw,
)
from forge.git_utils import capturing_to_step_log, configure_cli_logging


configure_cli_logging()
logger = logging.getLogger(__name__)


_DEFAULT_FAIL_UNDER = 90.0
_BADGE_DIR = ".badges"
_BADGE_FILENAME = "DocstringCoverage.svg"


def _interrogate_config(data: dict) -> tuple[InterrogateConfig, float, list[str]]:
    """Build the interrogate config + threshold + excludes from TOML data.

    Args:
        data: Parsed ``pyproject.toml`` data.

    Returns:
        Tuple of ``(InterrogateConfig, fail_under, excludes)``.
        Missing keys fall back to interrogate / forge defaults so a
        consumer with no ``[tool.interrogate]`` section still runs.
    """
    interrogate_section = data.get("tool", {}).get("interrogate", {})
    fail_under = float(interrogate_section.get("fail-under", _DEFAULT_FAIL_UNDER))
    excludes = list(interrogate_section.get("exclude", []))
    config = InterrogateConfig(
        ignore_init_method=interrogate_section.get("ignore-init-method", False),
        ignore_init_module=interrogate_section.get("ignore-init-module", False),
        ignore_magic=interrogate_section.get("ignore-magic", False),
        ignore_module=interrogate_section.get("ignore-module", False),
        ignore_nested_classes=interrogate_section.get("ignore-nested-classes", False),
        ignore_nested_functions=interrogate_section.get(
            "ignore-nested-functions", False
        ),
        ignore_private=interrogate_section.get("ignore-private", False),
        ignore_property_decorators=interrogate_section.get(
            "ignore-property-decorators", False
        ),
        ignore_property_setters=interrogate_section.get(
            "ignore-property-setters", False
        ),
        ignore_semiprivate=interrogate_section.get("ignore-semiprivate", False),
        ignore_overloaded_functions=interrogate_section.get(
            "ignore-overloaded-functions", False
        ),
        fail_under=fail_under,
    )
    return config, fail_under, excludes


def _badge_enabled(data: dict) -> bool:
    """Return True when the consumer opted into badge generation.

    Args:
        data: Parsed ``pyproject.toml`` data.

    Returns:
        Value of ``[tool.forge.docstring_coverage].badge`` or ``False``
        when the key is missing.
    """
    section = data.get("tool", {}).get("forge", {}).get("docstring_coverage", {})
    return bool(section.get("badge", False))


def _write_badge(repo_root: Path, results: object) -> Path:
    """Write a coverage SVG badge under ``.badges/`` and return its path.

    Args:
        repo_root: Repository root. Badge written to
            ``<repo_root>/.badges/DocstringCoverage.svg``.
        results: :class:`interrogate.coverage.InterrogateResults` from
            the coverage run.

    Returns:
        Absolute path to the generated badge SVG.
    """
    badge_dir = repo_root / _BADGE_DIR
    badge_dir.mkdir(exist_ok=True)
    badge_path = badge_dir / _BADGE_FILENAME
    create_badge(str(badge_path), results)
    return badge_path


def _emit_missing_list(results: object) -> None:
    """Print a parseable ``MISSING:`` section listing every undocumented symbol.

    The format is one ``MISSING: <path>:<line>:<name>`` line per
    undocumented node, prefixed by a ``## Missing docstrings (N)``
    header. ``forge:precommit-fixer`` greps the section to dispatch
    docstring additions per symbol.

    Args:
        results: :class:`interrogate.coverage.InterrogateResults` with
            ``file_results`` carrying ``CovNode`` records.
    """
    missing_nodes: list[tuple[str, int, str]] = []
    for file_result in getattr(results, "file_results", []):
        filename = getattr(file_result, "filename", "")
        missing_nodes.extend(
            (filename, getattr(node, "lineno", 0), getattr(node, "name", "?"))
            for node in getattr(file_result, "nodes", [])
            if not getattr(node, "covered", True)
        )
    if not missing_nodes:
        return
    logger.info("")
    logger.info("## Missing docstrings (%d)", len(missing_nodes))
    for path, lineno, name in missing_nodes:
        logger.info("MISSING: %s:%s:%s", path, lineno, name)


def _scan_paths(data: dict, repo_root: Path) -> list[str]:
    """Resolve the docstring-coverage scan roots from config, safely.

    A per-tool ``[tool.forge.docstring_coverage].paths`` override wins
    when set (interrogate has no scan-root concept of its own). Otherwise
    the default is the **repo-wide layout** —
    ``[tool.forge].source_dirs + test_dirs`` (default ``src`` + ``tests``)
    — so the project's roots live in one place. Each root resolves
    against *repo_root*; any that escapes the repo (absolute path or
    ``..`` traversal) is rejected so the reporter never reads files
    outside the repository (mirrors ``gen_api_digest.detect_roots``).
    Non-existent roots are dropped.

    Args:
        data: Parsed ``pyproject.toml`` data.
        repo_root: Repository root the configured paths resolve against.

    Returns:
        Existing in-repo directory paths to scan, as strings. Empty when
        none of the configured roots exist.
    """
    forge = data.get("tool", {}).get("forge", {})
    configured = forge.get("docstring_coverage", {}).get("paths")
    if isinstance(configured, list):
        # Per-tool override.
        raw_paths = list(configured)
    else:
        # Default to the repo-wide layout ([tool.forge].source_dirs +
        # test_dirs). Read from the already-parsed dict here (mirrors
        # forge.config.load_config's read of the same keys — keep in sync)
        # to avoid re-reading the file.
        raw_paths = list(forge.get("source_dirs", DEFAULT_SOURCE_DIRS)) + list(
            forge.get("test_dirs", DEFAULT_TEST_DIRS)
        )
    root_resolved = repo_root.resolve()
    scan: list[str] = []
    for raw in raw_paths:
        resolved = (repo_root / raw).resolve()
        if not resolved.is_relative_to(root_resolved):
            logger.error(
                "Ignoring docstring_coverage path %r — outside repo root.",
                raw,
            )
            continue
        if resolved.is_dir():
            scan.append(str(resolved))
    return scan


def main() -> int:
    """CLI entry point for ``verify-forge-docstring-coverage``.

    Returns:
        Process exit code. ``0`` when coverage meets the configured
        threshold or no config is found; ``1`` when below threshold.
    """
    argparse.ArgumentParser(
        prog="verify-forge-docstring-coverage",
        description=(
            "Measure docstring coverage with interrogate. Reads "
            "[tool.interrogate] for the gate and "
            "[tool.forge.docstring_coverage].badge for SVG output. "
            "Writes code_health/docstring_coverage.log."
        ),
    ).parse_args()

    repo_root = Path.cwd()
    with capturing_to_step_log(repo_root, "docstring_coverage"):
        data = read_pyproject_raw(repo_root)
        if not data:
            logger.info("(no pyproject.toml — skipped)")
            return 0

        config, fail_under, excludes = _interrogate_config(data)
        paths = _scan_paths(data, repo_root)
        if not paths:
            logger.info(
                "(none of the configured docstring_coverage paths exist — skipped)"
            )
            return 0

        cov = InterrogateCoverage(
            paths=paths,
            conf=config,
            excluded=tuple(excludes) if excludes else None,
        )
        results = cov.get_coverage()
        cov.print_results(results, output=None, verbosity=1)

        _emit_missing_list(results)

        if _badge_enabled(data):
            badge_path = _write_badge(repo_root, results)
            logger.info("badge written: %s", badge_path)

        if results.perc_covered < fail_under:
            logger.error(
                "docstring coverage %.1f%% < fail-under %.1f%%",
                results.perc_covered,
                fail_under,
            )
            return 1
        logger.info(
            "docstring coverage %.1f%% >= fail-under %.1f%%",
            results.perc_covered,
            fail_under,
        )
        return 0


if __name__ == "__main__":
    sys.exit(main())
