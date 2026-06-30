"""Shared AST import-graph primitives.

The two pure, audit-agnostic building blocks for static import analysis:
turning a ``.py`` path into a dotted module name, and extracting the set
of import targets from a parsed module. Both are derived purely from the
syntax tree — no runtime instrumentation, no import execution.

They live here rather than inside their first consumer because a second
one is coming. ``forge.audit.deps`` uses them today to build a
module-coupling graph for architecture metrics; a planned change-driven
test-selection subsystem (#8) will build a reverse test→source
reachability graph from the same primitive — "what does this module
import?". Extracting them now keeps that primitive a single source of
truth (FOUNDATION §12) instead of copied when the second consumer lands.
"""

from __future__ import annotations

import ast
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from pathlib import Path


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
        parts = list(rel.with_suffix("").parts)
        if parts and parts[-1] == "__init__":
            parts = parts[:-1]
        if not parts:
            return None
        return ".".join(parts)
    return None


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
