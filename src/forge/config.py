"""Read forge-internal config from a repo's ``pyproject.toml``.

Loader for the ``[tool.forge]`` table. **Forge sets this in its own
repo to support its internal release workflow; consumer repos do not
need a ``[tool.forge]`` block.** Defaults collapse every CLI to
standard single-branch behaviour pointing at ``main``, so consumers
who never touch this stay on the conventional flow.

```toml
[tool.forge]
base_branch = "main"   # default
dev_branch  = "main"   # default — set to "dev" for forge's own repo
```

The shell hook ``claude-hooks/block_protected_branches.sh`` carries an
intentionally parallel inline-Python implementation that reads the
same two keys (so the hook has no ``forge-scripts`` dependency at
git-invocation time). If you add a new ``[tool.forge]`` key here,
mirror it there.
"""

from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from pathlib import Path


logger = logging.getLogger(__name__)


# Default to single-branch flow: every CLI / hook treats both
# "channels" as the same branch unless the consumer's pyproject opts
# into dual-track by setting ``dev_branch`` to something other than
# ``base_branch``. Backwards-compatible with every existing consumer
# repo that has no ``[tool.forge]`` block.
DEFAULT_BASE_BRANCH = "main"
DEFAULT_DEV_BRANCH = "main"

# Repo-wide project layout. ``[tool.forge].source_dirs`` / ``test_dirs`` are
# the single ground truth for "what are this repo's source / test roots",
# shared by every layout-consuming tool (ruff, api-digest, docstring-coverage,
# doctest, typecheck) via :func:`resolve_tool_roots` so the answer lives in
# one place. Split into source vs test (semantic) rather than a flat union, so
# a tool that wants only source roots (e.g. api-digest) takes ``source_dirs``
# without test dirs leaking in.
#
# These constants are the ``ForgeConfig`` field defaults (bare construction);
# real reads against a repo with neither key set fall back to *smart detection*
# (:func:`detect_source_dirs` / :func:`detect_test_dirs`) rather than a fixed
# name list — forge used to guess from a broad 8-name tuple that scanned
# phantom dirs and ignored the configured roots.
DEFAULT_SOURCE_DIRS = ("src",)
DEFAULT_TEST_DIRS = ("tests",)


def detect_source_dirs(repo_root: Path) -> list[str]:
    """Smart-detect the repo's source roots when ``source_dirs`` is unset.

    Mirrors how a packaging tool locates code instead of guessing from a
    fixed name list: ``src/`` when it exists (the src-layout), otherwise
    every top-level directory that is an importable package (contains an
    ``__init__.py``).

    Args:
        repo_root: Git repo root.

    Returns:
        Repo-relative source-root names. ``["src"]`` for a src-layout repo;
        the sorted top-level package names for a flat layout; ``[]`` when
        neither is found.
    """
    if (repo_root / "src").is_dir():
        return ["src"]
    return sorted(
        p.name
        for p in repo_root.iterdir()
        if p.is_dir() and (p / "__init__.py").is_file()
    )


def detect_test_dirs(repo_root: Path) -> list[str]:
    """Smart-detect the repo's test roots when ``test_dirs`` is unset.

    Args:
        repo_root: Git repo root.

    Returns:
        The existing subset of the conventional test roots ``("tests",
        "test")``, in that preference order; ``[]`` when neither exists.
    """
    return [d for d in ("tests", "test") if (repo_root / d).is_dir()]


@dataclass(frozen=True)
class ForgeConfig:
    """Repo configuration sourced from ``[tool.forge]``.

    Release-channel semantics live in FOUNDATION §6; the project-layout
    rationale in §8 / `docs/configuration.md`. This class carries the
    `[tool.forge]` values forge reads repo-wide.

    Attributes:
        base_branch: Name of the slow channel (typically ``"main"``).
        dev_branch: Name of the fast channel (typically ``"dev"``).
            Equal to ``base_branch`` when the consumer hasn't opted
            into dual-track.
        source_dirs: Repo source roots. ``load_config`` smart-detects these
            when ``source_dirs`` is absent from ``pyproject.toml`` (``src/``
            when present, otherwise top-level packages); the field default
            ``["src"]`` applies only to bare dataclass construction.
        test_dirs: Repo test roots. ``load_config`` smart-detects these when
            ``test_dirs`` is absent from ``pyproject.toml`` (``tests/`` then
            ``test/``); the field default ``["tests"]`` applies only to bare
            dataclass construction.
    """

    base_branch: str = DEFAULT_BASE_BRANCH
    dev_branch: str = DEFAULT_DEV_BRANCH
    source_dirs: list[str] = field(default_factory=lambda: list(DEFAULT_SOURCE_DIRS))
    test_dirs: list[str] = field(default_factory=lambda: list(DEFAULT_TEST_DIRS))

    @property
    def dual_track(self) -> bool:
        """Return ``True`` when base and dev are distinct branches.

        Single source of truth for "should the dual-track UX kick in?"

        Returns:
            ``True`` when the repo has opted into the dual-track model
            by setting ``dev_branch`` to a name other than
            ``base_branch``; ``False`` otherwise (single-branch flow).
        """
        return self.base_branch != self.dev_branch


def read_pyproject_raw(repo_root: Path) -> dict:
    """Return the full parsed ``pyproject.toml`` dict, or ``{}`` on failure.

    The canonical "load the whole TOML, degrade to empty on missing /
    unreadable / unparseable" reader shared by every forge config
    consumer (``load_config`` here, plus the docstring-coverage step and
    the ``forge-config`` advisor). Deliberately forgiving — config reads
    happen in hot paths and any failure should degrade to defaults, not
    block the workflow.

    Args:
        repo_root: Git repo root containing ``pyproject.toml``.

    Returns:
        Parsed TOML data, or an empty dict when the file is missing,
        unreadable, or not valid TOML.
    """
    pyproject = repo_root / "pyproject.toml"
    if not pyproject.is_file():
        return {}
    try:
        text = pyproject.read_text()
    except OSError as exc:
        logger.debug("forge.config: could not read %s (%s)", pyproject, exc)
        return {}
    try:
        return tomllib.loads(text)
    except ValueError as exc:
        logger.debug("forge.config: could not parse %s (%s)", pyproject, exc)
        return {}


DEFAULT_C4_MODEL_FILE = "c4.toml"


def _read_toml_file(path: Path) -> dict | None:
    """Parse a standalone TOML file, degrading to ``None`` on any failure.

    Args:
        path: Path to the TOML model file.

    Returns:
        Parsed table, or ``None`` when the file is missing, unreadable, or
        not valid TOML.
    """
    if not path.is_file():
        return None
    try:
        return tomllib.loads(path.read_text())
    except (OSError, ValueError):
        logger.exception("Could not read C4 model file %s", path)
        return None


def resolve_model_section(repo_root: Path) -> dict | None:
    """Locate the C4 model table — external file or inline pyproject.

    Resolution, highest precedence first:

    1. ``[tool.forge.c4].config`` — an explicit path to a standalone TOML
       model file (the model's tables live at that file's top level).
    2. A conventional ``c4.toml`` at the repo root (used when present and
       ``[tool.forge.c4]`` carries no inline ``system``).
    3. The inline ``[tool.forge.c4]`` table itself.

    Keeping the verbose model out of ``pyproject.toml`` is the point of
    (1)/(2): a Structurizr model is its own artifact, like ``ruff.toml``.

    Args:
        repo_root: Repository root directory.

    Returns:
        The model table dict, or ``None`` when C4 generation is not opted
        into (no section, no file, and no inline ``system``).
    """
    section = (
        read_pyproject_raw(repo_root).get("tool", {}).get("forge", {}).get("c4", {})
    )
    configured = section.get("config")
    if configured:
        candidate = (repo_root / configured).resolve()
        if not candidate.is_relative_to(repo_root.resolve()):
            logger.error(
                "C4 model path %r escapes the repository root — refusing to read.",
                configured,
            )
            return None
        return _read_toml_file(candidate)
    if not section.get("system"):
        return _read_toml_file(repo_root / DEFAULT_C4_MODEL_FILE)
    return section


def load_config(repo_root: Path) -> ForgeConfig:
    """Read ``[tool.forge]`` from *repo_root*'s ``pyproject.toml``.

    Returns the defaults when the file is missing, can't be read,
    lacks a ``[tool.forge]`` table, or doesn't parse as TOML.
    Deliberately forgiving — config reads happen in hot paths (hooks,
    agents, pre-commit) and any failure should degrade to default
    behaviour, not block the workflow.

    Args:
        repo_root: Git repo root.

    Returns:
        Populated :class:`ForgeConfig`. With no overrides, both
        ``base_branch`` and ``dev_branch`` default to ``"main"`` —
        ``dual_track`` is ``False``, every CLI collapses to
        single-branch flow. Override ``dev_branch`` in
        ``[tool.forge]`` to opt in.
    """
    section = read_pyproject_raw(repo_root).get("tool", {}).get("forge", {})
    source_dirs = (
        list(section["source_dirs"])
        if "source_dirs" in section
        else detect_source_dirs(repo_root)
    )
    test_dirs = (
        list(section["test_dirs"])
        if "test_dirs" in section
        else detect_test_dirs(repo_root)
    )
    return ForgeConfig(
        base_branch=section.get("base_branch", DEFAULT_BASE_BRANCH),
        dev_branch=section.get("dev_branch", DEFAULT_DEV_BRANCH),
        source_dirs=source_dirs,
        test_dirs=test_dirs,
    )


def _existing_dirs(repo_root: Path, dirs: list[str]) -> list[str]:
    """Filter *dirs* to existing in-repo paths, de-duplicated, order-preserving.

    Args:
        repo_root: Git repo root the paths must stay within.
        dirs: Candidate repo-relative paths.

    Returns:
        The subset that resolves inside *repo_root* and exists on disk, with
        duplicates removed and original order kept. Dropped: blank entries,
        option-like entries (leading ``-``, which would be parsed as a flag
        by the consuming tool), and paths escaping the repo (absolute or
        ``..``) — so the scan never reaches outside and no configured value
        can inject a flag into a tool's argv.
    """
    root = repo_root.resolve()
    out: list[str] = []
    for d in dict.fromkeys(dirs):
        if not d.strip() or d.lstrip().startswith("-"):
            logger.debug("dropping scan root %r — blank or option-like", d)
            continue
        resolved = (repo_root / d).resolve()
        if not (resolved.is_relative_to(root) and resolved.exists()):
            logger.debug("dropping scan root %r — outside repo or missing", d)
            continue
        out.append(d)
    return out


def resolve_tool_roots(
    repo_root: Path,
    tool: str,
    *,
    include_tests: bool = False,
) -> list[str]:
    """Resolve the scan roots a layout-consuming *tool* should use.

    The single resolution every path-scanning forge tool shares (ruff,
    api-digest, docstring-coverage, doctest, typecheck), so "where is the
    code" is answered in one place. Precedence, highest first:

    1. ``[tool.forge.<tool>].paths`` — the tool's own granular override
       (a full replacement; tests are the caller's to include in it).
    2. ``[tool.forge].source_dirs`` (plus ``test_dirs`` when *include_tests*)
       — the repo-wide definition every tool shares.
    3. Smart auto-detect (:func:`detect_source_dirs` / :func:`detect_test_dirs`)
       — used only when neither of the above is set.

    Explicit CLI arguments (e.g. ``--roots``, ruff's positional dirs) are a
    higher override still and are handled by each CLI before calling this.

    Args:
        repo_root: Git repo root.
        tool: The ``[tool.forge.<tool>]`` subsection name (e.g. ``"ruff"``,
            ``"api_digest"``, ``"docstring_coverage"``).
        include_tests: When ``True``, append the resolved test roots to the
            source roots (for tools that lint / scan tests too, e.g. ruff).

    Returns:
        Existing in-repo directory paths to scan, de-duplicated. ``[]`` when
        nothing resolves (the caller decides whether that is a skip).
    """
    forge = read_pyproject_raw(repo_root).get("tool", {}).get("forge", {})
    tool_section = forge.get(tool)
    if isinstance(tool_section, dict):
        granular = tool_section.get("paths")
        if isinstance(granular, list):
            return _existing_dirs(repo_root, [str(p) for p in granular])

    if "source_dirs" in forge:
        roots = [str(p) for p in forge["source_dirs"]]
    else:
        roots = detect_source_dirs(repo_root)
    if include_tests:
        if "test_dirs" in forge:
            roots += [str(p) for p in forge["test_dirs"]]
        else:
            roots += detect_test_dirs(repo_root)
    return _existing_dirs(repo_root, roots)
