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

import fnmatch
import logging
import tomllib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from forge.git_utils import (
    get_modified_files,
    get_tracked_files,
    get_untracked_files,
    path_escapes_repo,
)
from forge.run_context import is_non_interactive


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
        exclude: Repo-wide glob patterns (fnmatch, matched against
            repo-relative paths) that whole-tree file-selecting steps
            (``docstring_verification``, ``test_naming_check``) skip. The
            single place to name paths — vendored / generated Python a repo
            does not author — that are already excluded from ruff /
            interrogate and should not be scanned or blocked here either.
            Empty by default. See :func:`filter_excluded`.
    """

    base_branch: str = DEFAULT_BASE_BRANCH
    dev_branch: str = DEFAULT_DEV_BRANCH
    source_dirs: list[str] = field(default_factory=lambda: list(DEFAULT_SOURCE_DIRS))
    test_dirs: list[str] = field(default_factory=lambda: list(DEFAULT_TEST_DIRS))
    exclude: list[str] = field(default_factory=list)

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


def read_tool_forge_section(repo_root: Path, section: str = "") -> dict:
    """Return a ``[tool.forge.<section>]`` table, or ``{}`` when absent.

    The single navigation point for the ``tool → forge → <section>``
    lookup (FOUNDATION §12) — every module that previously hand-rolled
    the two-level ``.get`` chain routes here. An empty *section* returns
    the whole ``[tool.forge]`` table. Forgiving like
    :func:`read_pyproject_raw`: any missing level degrades to ``{}``.

    Args:
        repo_root: Git repo root containing ``pyproject.toml``.
        section: Subsection name (e.g. ``"precommit"``, ``"pr"``), or
            empty for the whole ``[tool.forge]`` table.

    Returns:
        The requested table, or ``{}`` when any level is missing or not
        a table.
    """
    forge = (read_pyproject_raw(repo_root).get("tool") or {}).get("forge") or {}
    if not isinstance(forge, dict):
        return {}
    if not section:
        return forge
    sub = forge.get(section) or {}
    return sub if isinstance(sub, dict) else {}


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
    section = read_tool_forge_section(repo_root, "c4")
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
    section = read_tool_forge_section(repo_root)
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
    raw_exclude = section.get("exclude", [])
    # Guard the common footgun: ``exclude = "vendor"`` (a bare string, brackets
    # forgotten) would otherwise iterate into ``["v", "e", ...]``. Only a list
    # of globs is meaningful; anything else degrades to no excludes.
    exclude = [str(p) for p in raw_exclude] if isinstance(raw_exclude, list) else []
    return ForgeConfig(
        base_branch=section.get("base_branch", DEFAULT_BASE_BRANCH),
        dev_branch=section.get("dev_branch", DEFAULT_DEV_BRANCH),
        source_dirs=source_dirs,
        test_dirs=test_dirs,
        exclude=exclude,
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
    forge = read_tool_forge_section(repo_root)
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


def filter_under_roots(files: list[str], roots: list[str]) -> list[str]:
    """Keep only *files* that live under one of *roots* (source-tree scoping).

    The Option-3 half of the whole-tree exclude story (issue #83): a
    ``--scope all`` step means "whole *source* tree", not "every tracked
    file", so a file outside the repo's declared source / test roots
    (``.devcontainer/`` scripts, root-level tooling, vendored trees) is
    never scanned. Aligns the git-ls-files steps
    (``docstring_verification``, ``test_naming_check``) with how ruff /
    api-digest / coverage already scope via :func:`resolve_tool_roots`.

    Args:
        files: Repo-relative file paths (typically from
            :func:`forge.git_utils.get_tracked_files`).
        roots: Repo-relative directory roots to keep files under. An empty
            list keeps nothing — the caller resolves roots before calling.

    Returns:
        The subset of *files* under some root, original order preserved. A
        file matches a root when it equals the root or sits beneath it
        (``root/…``); a bare root name never partial-matches a sibling
        (``src`` does not admit ``src_extra/x.py``).
    """
    prefixes = tuple(f"{r.rstrip('/')}/" for r in roots)
    bare = {r.rstrip("/") for r in roots}
    return [f for f in files if f in bare or f.startswith(prefixes)]


def filter_excluded(files: list[str], globs: list[str]) -> list[str]:
    """Drop *files* matching any exclude *glob* (the ``[tool.forge].exclude`` half).

    The Option-1 half of issue #83: a repo-wide, uniformly-honored exclude
    so a directory already excluded from ruff / interrogate can be skipped
    by forge's whole-tree steps too, without editing forge source. A
    pattern matches a repo-relative path either as an fnmatch glob
    (``*.gen.py``, ``vendor/**``) or as a directory prefix — a bare
    directory name (``vendor`` or ``vendor/``) excludes its whole subtree.

    Args:
        files: Repo-relative file paths.
        globs: Exclude patterns from ``[tool.forge].exclude``.

    Returns:
        The subset of *files* matching no pattern, original order preserved.
        When *globs* is empty, *files* is returned as-is (same list object —
        no copy is made).
    """
    if not globs:
        return files
    dir_prefixes = tuple(f"{g.rstrip('/')}/" for g in globs)
    dir_names = {g.rstrip("/") for g in globs}
    return [
        f
        for f in files
        if not (
            f in dir_names
            or f.startswith(dir_prefixes)
            or any(fnmatch.fnmatch(f, g) for g in globs)
        )
    ]


def select_diff_files(
    repo_root: Path,
    *,
    roots: list[str] | None = None,
    apply_exclude: bool = False,
    drop_deleted: bool = True,
    suffix: str = ".py",
) -> list[str]:
    """Select the modified files a diff-scoped step should check.

    The single home for every step's ``scope = "diff"`` file selection —
    ruff, docstring_verification, test_naming_check, typecheck all route
    here instead of each hand-rolling a ``get_modified_files`` recipe. The
    knobs stay per-step *by design*, not accident: root-restriction and
    exclude-globbing legitimately differ (test-naming only wants test dirs;
    ``[tool.forge].exclude`` is scoped to the two whole-tree steps, while
    ruff/typecheck own their exclusions via ``ruff.toml`` / pyrefly's
    ``project_excludes``). ``drop_deleted`` is the one behavior every step
    shares: a path deleted in the diff still appears in
    ``git diff --name-only`` but errors when handed to a tool that opens it.

    Args:
        repo_root: Git repo root (threaded through to ``get_modified_files``
            so an in-process caller is not at the mercy of the cwd-cached
            global root).
        roots: When given, restrict the diff to files under these roots
            (each becomes a path prefix). A root of ``"."`` — or ``None`` —
            means no restriction (the whole diff).
        apply_exclude: Apply the ``[tool.forge].exclude`` globs. Only the
            two whole-tree steps set this; ruff/typecheck leave it off.
        drop_deleted: Drop paths that no longer exist on disk (deletions in
            the diff). Default on — a deleted file has nothing to check.
        suffix: File suffix filter, forwarded to ``get_modified_files``.

    Returns:
        Repo-relative modified-file paths matching the selected filters,
        every one guaranteed to resolve inside *repo_root*. Empty when
        nothing in scope changed — the caller decides the skip. The diff
        base is the repo's configured ``[tool.forge].base_branch``.
    """
    prefixes: tuple[str, ...] | None = None
    if roots is not None:
        norm = [r.rstrip("/") for r in roots]
        if "." not in norm:
            prefixes = tuple(f"{r}/" for r in norm)
    modified = get_modified_files(
        prefix=prefixes,
        suffix=suffix,
        repo_root=repo_root,
        base_branch=load_config(repo_root).base_branch,
    )
    files: list[str] = []
    for f in modified:
        # Defense-in-depth: a `git diff` path is always repo-relative, but a
        # diff-scoped step must never hand its tool a path that escapes the
        # repo. Drop (don't raise) — this is a library selector shared by four
        # in-process steps, and an escaping path is an anomaly to skip, not
        # grounds to abort the whole pre-commit. Untrusted argv keeps its
        # fail-loud guard in `fix_ruff._validate_paths` (the `scope=all` path).
        if path_escapes_repo(repo_root, f):
            continue
        if drop_deleted and not (repo_root / f).is_file():
            continue
        files.append(f)
    if apply_exclude:
        files = filter_excluded(files, load_config(repo_root).exclude)
    return files


def tracked_files_under_roots(
    repo_root: Path,
    roots: list[str],
    *,
    suffix: str = ".py",
) -> list[str]:
    """Select the git-tracked files under *roots*, minus repo-wide excludes.

    The one "which source files apply" selector every whole-tree
    file-scanning forge tool shares — the composition of
    :func:`forge.git_utils.get_tracked_files` (the tracked set — never a
    raw filesystem walk), :func:`filter_under_roots` (source-tree scoping),
    and :func:`filter_excluded` (the ``[tool.forge].exclude`` half). Sourcing
    from the tracked set is what makes output reproducible across machines
    (issue #161): an untracked / gitignored file — a locally-cloned vendored
    repo, a machine-local data dir — is by definition not part of the
    committed source surface, so it must never be indexed or checked. The
    docstring-verification, test-naming, and api-digest whole-tree passes all
    route through here so the guarantee is uniform rather than re-derived
    (and drifting) per tool.

    Side effect (dev-loop only): emits one ``logger.warning`` via
    :func:`_warn_untracked_under_roots` naming any *untracked, non-gitignored*
    source under *roots* the tracked-set scan skipped — the "forgot to
    ``git add``" case (issue #164). The returned list is unaffected; the
    warning self-skips in CI. A gitignored file is deliberately silent (it is
    declared out of scope, per #161).

    Args:
        repo_root: Git repo root — the source of ``[tool.forge].exclude``.
        roots: Repo-relative directory roots to keep files under (as produced
            by :func:`resolve_tool_roots`). An empty list keeps nothing.
        suffix: File suffix to select. Defaults to ``".py"``.

    Returns:
        Repo-relative tracked file paths under some root, with
        ``[tool.forge].exclude`` globs removed, sorted (the
        :func:`get_tracked_files` order).
    """
    tracked = get_tracked_files(suffix=suffix, repo_root=repo_root)
    files = filter_under_roots(tracked, roots)
    _warn_untracked_under_roots(repo_root, roots, suffix)
    return filter_excluded(files, load_config(repo_root).exclude)


def _warn_untracked_under_roots(repo_root: Path, roots: list[str], suffix: str) -> None:
    """Warn (dev-loop only) when untracked source under *roots* goes unscanned.

    Side effect: emits one ``logger.warning`` naming the count of untracked,
    non-gitignored ``suffix`` files under *roots* the tracked-set scan skipped
    — the "forgot to ``git add``" case (issue #164). Self-skips in
    non-interactive / CI contexts (FOUNDATION §15): the warning recommends a
    manual ``git add``, meaningless on a CI checkout. Gitignored files are
    deliberately out of scope (issue #161) and never warned about.

    Args:
        repo_root: Git repo root — where the untracked-set query runs.
        roots: Repo-relative directory roots the scan is scoped to.
        suffix: File suffix the scan selects (e.g. ``".py"``).
    """
    if is_non_interactive():
        return
    untracked = filter_under_roots(
        get_untracked_files(suffix=suffix, repo_root=repo_root),
        roots,
    )
    if untracked:
        logger.warning(
            "%d untracked %s file(s) under %s not indexed — 'git add' to "
            "include (only git-tracked files are scanned, for reproducibility).",
            len(untracked),
            suffix,
            ", ".join(roots),
        )
