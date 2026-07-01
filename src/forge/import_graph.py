"""Shared AST import-graph primitives.

The two pure, audit-agnostic building blocks for static import analysis:
turning a ``.py`` path into a dotted module name, and extracting the set
of import targets from a parsed module. Both are derived purely from the
syntax tree — no runtime instrumentation, no import execution.

They live here rather than inside their consumers because both
``forge.audit.deps`` (module-coupling graph for architecture metrics) and
``forge.smart_test`` (reverse test→source reachability graph) build on the
same primitive — "what does this module import?". Sharing it keeps that
primitive a single source of truth (FOUNDATION §12) instead of copied.
"""

from __future__ import annotations

import ast
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from pathlib import Path


def _rel_to_dotted(rel: Path) -> str | None:
    """Convert a root-relative ``.py`` path to a dotted module name.

    ``pkg/mod.py`` → ``pkg.mod``; ``pkg/__init__.py`` → ``pkg`` (the package
    is named by its dir, not its ``__init__``); a bare ``__init__.py`` at the
    root → ``None``.

    Args:
        rel: Path relative to a ``sys.path`` root, suffix included.

    Returns:
        Dotted module name, or ``None`` when nothing remains after dropping
        an ``__init__`` leaf.
    """
    parts = list(rel.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) if parts else None


def resolve_module_name(path: Path, package_roots: list[Path]) -> str | None:
    """Translate a ``.py`` path to a dotted module name.

    Args:
        path: Absolute path to a Python source file.
        package_roots: Candidate ancestor directories (``src``, ``lib``, …).

    Returns:
        Dotted module name (``"forge.audit.dup"``) or ``None`` if the path
        is not under any known root.
    """
    for root in package_roots:
        try:
            rel = path.resolve().relative_to(root.resolve())
        except ValueError:
            continue
        return _rel_to_dotted(rel)
    return None


def resolve_package_module_name(path: Path, repo_root: Path) -> str | None:
    """Name a source file by its real import root, derived from package layout.

    Unlike :func:`resolve_module_name` — which strips a *configured* scan-dir
    prefix — this climbs the ``__init__.py`` chain to find the actual
    ``sys.path`` root, so the emitted dotted name matches what importers use
    regardless of how the scan dir was configured. The root is the first
    ancestor that is **not** a package (has no ``__init__.py``); the walk is
    floored at *repo_root* so it never escapes the repo.

    This resolves the source-dir/import-root split that misnames modules for
    two layouts a plain scan-dir strip gets wrong:

    - a ``source_dirs`` entry that is itself a package (``libs/`` with an
      ``__init__.py`` → ``libs.thing.core``, not ``thing.core``), and
    - a ``source_dirs`` entry holding a nested ``*/src`` root
      (``projects/APP/src/pkg/runner.py`` → ``pkg.runner``, not
      ``APP.src.pkg.runner``).

    A plain ``src/pkg/mod.py`` (``src`` has no ``__init__.py``) still resolves
    to ``pkg.mod`` — identical to the scan-dir strip, so no behavior change.

    Args:
        path: Absolute path to a Python source file.
        repo_root: Git repo root; the highest the walk may climb.

    Returns:
        Dotted module name, or ``None`` if *path* is not under *repo_root*.
    """
    root = path.parent
    while root != repo_root and (root / "__init__.py").exists():
        root = root.parent
    try:
        rel = path.resolve().relative_to(root.resolve())
    except ValueError:
        return None
    return _rel_to_dotted(rel)


def extract_import_targets(tree: ast.Module, current_module: str) -> set[str]:
    """Return the set of fully-qualified import-candidate targets.

    Relative imports are resolved against ``current_module``. For
    ``from X import Y`` we emit BOTH ``X`` and ``X.Y`` as candidates —
    at parse time we cannot know whether ``Y`` is a submodule (an edge to
    ``X.Y``) or an attribute of ``X`` (an edge to ``X``). A consumer that
    cares picks the deepest target present in its own graph, so attribute
    imports collapse to ``X`` and submodule imports resolve to ``X.Y``.

    Args:
        tree: Parsed module.
        current_module: Dotted name of the importing module (for relative
            import resolution).

    Returns:
        Set of dotted target candidates.
    """
    targets: set[str] = set()
    parts = current_module.split(".")
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                targets.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            level = node.level or 0
            base = ".".join(parts[: len(parts) - level]) if level else ""
            base_module = (
                (f"{base}.{node.module}" if base else node.module)
                if node.module
                else base
            )
            if not base_module:
                continue
            targets.add(base_module)
            for alias in node.names:
                if alias.name != "*":
                    targets.add(f"{base_module}.{alias.name}")
    return targets
