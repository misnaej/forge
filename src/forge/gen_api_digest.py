"""Generate a compact markdown API digest of a codebase's symbols.

The digest is a fast index of every top-level symbol — functions, async
functions, and classes, plus the public methods of those classes —
across a repo's source roots. Both public API and internal helpers
(names starting with ``_``) are indexed; internal symbols are tagged
``(internal)`` so a reader can still tell them apart. Its purpose is DRY
enforcement: before writing a new helper, an agent (or developer) can
scan ``docs/api-digest.md`` to check whether a function for the task
already exists, instead of writing a third copy. Reuse candidates are
very often private helpers, so the digest deliberately includes them.

Class methods are an exception: only public methods are listed. Private
and dunder methods are class-internal with near-zero cross-module reuse
value and would only bloat the index.

Unlike full API documentation, the digest is deliberately terse — one
line per symbol: a signature reconstructed from the AST plus the first
line of the symbol's docstring. It is an index, not a reference manual.

The generator is repo-agnostic. Source roots come from the shared
``forge.config.resolve_tool_roots`` resolution — granular
``[tool.forge.api_digest].paths`` → repo-wide ``[tool.forge].source_dirs``
→ smart auto-detect (``src/`` when present, else top-level packages). Pass
``--roots`` to override all of it.

Usage:

    # Regenerate docs/api-digest.md
    forge-gen-api-digest

    # Generate from explicit source roots
    forge-gen-api-digest --roots src lib

    # Verify docs/api-digest.md is in sync
    forge-gen-api-digest --check

Exit Codes:
    0: The doc was written (default mode), or it is already in sync
       (``--check`` mode).
    1: ``--check`` detected drift between the generated content and the
       committed ``docs/api-digest.md`` (the file is left untouched).
"""

from __future__ import annotations

import argparse
import ast
import logging
import sys
from typing import TYPE_CHECKING, NamedTuple

from forge.config import resolve_tool_roots
from forge.gen_common import check_doc_drift
from forge.git_utils import configure_cli_logging, repo_root


if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


configure_cli_logging()
logger = logging.getLogger(__name__)


# Path of the generated digest doc, relative to the repo root.
DOC_RELPATH = "docs/api-digest.md"
# Directory names skipped when walking source roots for modules.
SKIP_DIR_NAMES = frozenset({"__pycache__", "tests", "test"})


class Symbol(NamedTuple):
    """One top-level symbol extracted from a module.

    Attributes:
        kind: ``"function"`` or ``"class"``.
        signature: The reconstructed signature line (e.g.
            ``foo(x: int) -> str`` or ``class Bar``).
        summary: First line of the symbol's docstring, or an empty
            string when it has none.
        methods: Public methods of a class as ``(signature, summary)``
            pairs. Always empty for functions.
        internal: True when the symbol is an internal helper (its name
            starts with an underscore).
    """

    kind: str
    signature: str
    summary: str
    methods: tuple[tuple[str, str], ...]
    internal: bool


class ModuleDigest(NamedTuple):
    """The top-level symbols of a single module.

    Attributes:
        dotted: Dotted module path relative to the repo root with the
            source-root prefix kept (e.g. ``src/forge/doctor.py`` →
            ``forge.doctor``).
        summary: First line of the module-level docstring — the module's
            stated purpose — or an empty string when it has none.
        symbols: Top-level symbols in source order (public API and
            internal helpers).
    """

    dotted: str
    summary: str
    symbols: tuple[Symbol, ...]


def detect_roots(root: Path, explicit: list[str] | None) -> list[Path]:
    """Resolve the source roots to scan for Python modules.

    When *explicit* is given (``--roots``), each entry is resolved against
    the repo root; any root that escapes the repo (an absolute path or one
    using ``..``) is rejected with an error and excluded, so the scan never
    reads files outside the repository. Otherwise the shared
    :func:`forge.config.resolve_tool_roots` resolution applies (granular
    ``[tool.forge.api_digest].paths`` → repo-wide ``[tool.forge].source_dirs``
    → smart auto-detect), so api-digest indexes the same roots every other
    layout-aware forge tool does. Source-only — test dirs are not indexed.

    Args:
        root: Repository root directory.
        explicit: Roots passed via ``--roots``, or ``None`` to
            auto-detect.

    Returns:
        Existing source-root directories inside the repo, sorted by name.
    """
    if explicit:
        resolved = [(r, (root / r).resolve()) for r in explicit]
        inside: list[Path] = []
        for raw, path in resolved:
            if not path.is_relative_to(root.resolve()):
                logger.error(
                    "Ignoring --roots entry %r — resolves to %s, outside the repo.",
                    raw,
                    path,
                )
                continue
            inside.append(path)
        return sorted((p for p in inside if p.is_dir()), key=lambda p: p.name)

    roots = resolve_tool_roots(root, "api_digest")
    return sorted((root / d for d in roots), key=lambda p: p.name)


def _is_test_module(path: Path) -> bool:
    """Return whether a module path is a test module to skip.

    Args:
        path: Absolute path to a ``.py`` file.

    Returns:
        True when the file is a test module (``test_*.py``,
        ``*_test.py``, or ``conftest.py``).
    """
    name = path.name
    return (
        name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py"
    )


def iter_modules(roots: list[Path]) -> Iterator[Path]:
    """Yield Python module files under the given source roots.

    Skips ``__pycache__`` and test directories, test modules, and
    ``__main__.py`` entry-point shims.

    Args:
        roots: Source-root directories to walk.

    Yields:
        Absolute paths to module files, sorted within each root.
    """
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            # Two-layer test exclusion: skip whole `tests/`/`test/` dirs by
            # path part, then skip stray test files (test_*.py etc.) that
            # live elsewhere via the per-file name check.
            if SKIP_DIR_NAMES & set(path.parts):
                continue
            if path.name == "__main__.py" or _is_test_module(path):
                continue
            yield path


def _annotation(node: ast.expr | None) -> str:
    """Render an AST annotation node as source text.

    Args:
        node: An annotation expression node, or ``None`` when absent.

    Returns:
        The unparsed annotation, or an empty string when *node* is
        ``None``.
    """
    return "" if node is None else ast.unparse(node)


def _format_arg(arg: ast.arg, default: ast.expr | None) -> str:
    """Render a single argument with its annotation and default.

    Args:
        arg: The argument node.
        default: The default-value node, or ``None`` when the argument
            has no default.

    Returns:
        The formatted argument fragment (e.g. ``x: int = 3``).
    """
    text = arg.arg
    annotation = _annotation(arg.annotation)
    if annotation:
        text += f": {annotation}"
    if default is not None:
        sep = " = " if annotation else "="
        text += f"{sep}{ast.unparse(default)}"
    return text


def _positional_args(args: ast.arguments) -> list[str]:
    """Render the positional (and positional-only) arguments.

    Args:
        args: The arguments node of a function definition.

    Returns:
        Formatted positional argument fragments, with a ``/`` marker
        appended when positional-only arguments are present.
    """
    positional = list(args.posonlyargs) + list(args.args)
    defaults: list[ast.expr | None] = [None] * (len(positional) - len(args.defaults))
    defaults.extend(args.defaults)

    parts = [
        _format_arg(arg, default)
        for arg, default in zip(positional, defaults, strict=True)
    ]
    if args.posonlyargs:
        parts.insert(len(args.posonlyargs), "/")
    return parts


def _keyword_only_args(args: ast.arguments) -> list[str]:
    """Render the keyword-only arguments, including the ``*`` marker.

    Args:
        args: The arguments node of a function definition.

    Returns:
        Formatted keyword-only argument fragments. Empty when the
        function has no keyword-only arguments and no bare ``*args``.
    """
    if not args.kwonlyargs:
        return []
    parts: list[str] = []
    if args.vararg is None:
        parts.append("*")
    parts.extend(
        _format_arg(arg, default)
        for arg, default in zip(args.kwonlyargs, args.kw_defaults, strict=True)
    )
    return parts


def format_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """Reconstruct a function's signature from its AST node.

    Args:
        node: The function or async-function definition node.

    Returns:
        A signature string of the form ``name(params) -> return``,
        prefixed with ``async `` for async functions.
    """
    args = node.args
    parts = _positional_args(args)

    if args.vararg is not None:
        vararg = f"*{args.vararg.arg}"
        annotation = _annotation(args.vararg.annotation)
        parts.append(f"{vararg}: {annotation}" if annotation else vararg)

    parts.extend(_keyword_only_args(args))

    if args.kwarg is not None:
        kwarg = f"**{args.kwarg.arg}"
        annotation = _annotation(args.kwarg.annotation)
        parts.append(f"{kwarg}: {annotation}" if annotation else kwarg)

    prefix = "async " if isinstance(node, ast.AsyncFunctionDef) else ""
    returns = _annotation(node.returns)
    suffix = f" -> {returns}" if returns else ""
    return f"{prefix}{node.name}({', '.join(parts)}){suffix}"


def _summary_line(
    node: ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef,
) -> str:
    """Return the first line of an AST node's docstring.

    Args:
        node: A module, class, or function node.

    Returns:
        The stripped first line of the docstring, or an empty string
        when the node has no docstring.
    """
    docstring = ast.get_docstring(node)
    if not docstring:
        return ""
    return docstring.strip().split("\n", 1)[0].strip()


def _is_public(name: str) -> bool:
    """Return whether a symbol name is public.

    Args:
        name: The symbol name to test.

    Returns:
        True when *name* does not start with an underscore.
    """
    return not name.startswith("_")


def _is_dunder(name: str) -> bool:
    """Return whether a symbol name is a dunder name.

    Args:
        name: The symbol name to test.

    Returns:
        True when *name* both starts and ends with a double underscore
        (e.g. ``__init__``).
    """
    return name.startswith("__") and name.endswith("__")


def _class_methods(node: ast.ClassDef) -> tuple[tuple[str, str], ...]:
    """Extract the public methods of a class.

    Args:
        node: The class definition node.

    Returns:
        ``(signature, summary)`` pairs for each public method, in
        source order.
    """
    return tuple(
        (format_signature(child), _summary_line(child))
        for child in node.body
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
        and _is_public(child.name)
    )


def extract_symbols(tree: ast.Module) -> tuple[Symbol, ...]:
    """Extract top-level symbols from a parsed module.

    Both public API and internal helpers (names starting with an
    underscore) are extracted; internal symbols are flagged via
    ``Symbol.internal``. Top-level dunder names (``__getattr__`` and the
    like) are skipped — they are framework hooks, not reuse candidates.

    Args:
        tree: The parsed module AST.

    Returns:
        Functions and classes defined at module level, in source order.
    """
    symbols: list[Symbol] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if _is_dunder(node.name):
                continue
            symbols.append(
                Symbol(
                    kind="function",
                    signature=format_signature(node),
                    summary=_summary_line(node),
                    methods=(),
                    internal=not _is_public(node.name),
                ),
            )
        elif isinstance(node, ast.ClassDef):
            if _is_dunder(node.name):
                continue
            symbols.append(
                Symbol(
                    kind="class",
                    signature=f"class {node.name}",
                    summary=_summary_line(node),
                    methods=_class_methods(node),
                    internal=not _is_public(node.name),
                ),
            )
    return tuple(symbols)


def _dotted_name(path: Path, root: Path) -> str:
    """Render a module path as a dotted name relative to the repo root.

    A leading ``src`` segment is dropped so ``src/forge/doctor.py``
    becomes ``forge.doctor``.

    Args:
        path: Absolute path to the module file.
        root: Repository root directory.

    Returns:
        The dotted module name.
    """
    rel = path.resolve().relative_to(root.resolve())
    parts = list(rel.with_suffix("").parts)
    # Only a leading `src` segment is stripped: it is a packaging layout
    # prefix, not part of the importable dotted name. Other top-level
    # directories (e.g. a package name) are kept as-is.
    if parts and parts[0] == "src":
        parts = parts[1:]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def build_digest(root: Path, roots: list[Path]) -> list[ModuleDigest]:
    """Build the per-module digest for every module under the roots.

    Modules that fail to parse, cannot be read, or resolve outside the
    repo root are skipped with a warning. A module is included when it has
    top-level symbols **or** a module-level docstring — a docstring-only
    module is the purest statement of a module's purpose, so it earns an
    entry (header + summary) even with nothing to index. Only a module with
    neither symbols nor a docstring is dropped.

    Args:
        root: Repository root directory.
        roots: Source-root directories to scan.

    Returns:
        Module digests sorted by dotted name.
    """
    digests: list[ModuleDigest] = []
    for path in iter_modules(roots):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, OSError, UnicodeDecodeError, ValueError) as exc:
            logger.warning("Skipping %s — could not parse (%s)", path, exc)
            continue
        symbols = extract_symbols(tree)
        summary = _summary_line(tree)
        if symbols or summary:
            digests.append(
                ModuleDigest(
                    dotted=_dotted_name(path, root),
                    summary=summary,
                    symbols=symbols,
                ),
            )
    return sorted(digests, key=lambda d: d.dotted)


def _render_symbol(symbol: Symbol) -> list[str]:
    """Render one symbol (and any methods) as markdown lines.

    Internal helpers (``Symbol.internal``) get an ``(internal)`` tag
    after their signature so a reader can tell them apart from public
    API at a glance.

    Args:
        symbol: The symbol to render.

    Returns:
        Markdown lines for the symbol, without a trailing blank line.
    """
    tag = " _(internal)_" if symbol.internal else ""
    summary = f" — {symbol.summary}" if symbol.summary else ""
    lines = [f"- `{symbol.signature}`{tag}{summary}"]
    for method_sig, method_summary in symbol.methods:
        method_note = f" — {method_summary}" if method_summary else ""
        lines.append(f"  - `{method_sig}`{method_note}")
    return lines


def count_symbols(digests: list[ModuleDigest]) -> int:
    """Return the total number of top-level symbols across all modules.

    Args:
        digests: Per-module digests.

    Returns:
        The sum of top-level symbols (public API and internal helpers)
        over every module digest.
    """
    return sum(len(d.symbols) for d in digests)


def render_digest(digests: list[ModuleDigest]) -> str:
    """Render the full API digest markdown document.

    Args:
        digests: Per-module digests, in the order they should appear.

    Returns:
        The complete markdown content for ``docs/api-digest.md``, ending
        with a single trailing newline.
    """
    symbol_count = count_symbols(digests)
    lines = [
        "# API Digest",
        "",
        "A compact index of this codebase's symbols — every top-level "
        "function and class, with its signature and one-line summary. "
        "Both public API and internal helpers are indexed; internal "
        "helpers are tagged _(internal)_. Use it to check whether a "
        "helper for a task already exists before writing a new one "
        "(DRY) — reuse candidates are often private.",
        "",
        "> **Generated file — do not edit by hand.** Regenerate with "
        "`forge-gen-api-digest`; check for drift with "
        "`forge-gen-api-digest --check`.",
        "",
        f"_{len(digests)} modules, {symbol_count} symbols._",
        "",
    ]
    for digest in digests:
        lines.append(f"## `{digest.dotted}`")
        lines.append("")
        summary = digest.summary or "(no module docstring)"
        lines.append(f"> _{summary}_")
        lines.append("")
        for symbol in digest.symbols:
            lines.extend(_render_symbol(symbol))
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def main() -> int:
    """Generate or verify the API digest doc.

    Returns:
        Exit code: ``0`` when the doc was written or is in sync, ``1``
        when ``--check`` detected drift or a missing doc.
    """
    parser = argparse.ArgumentParser(
        prog="forge-gen-api-digest",
        description=(
            "Generate docs/api-digest.md indexing top-level functions and "
            "classes (public API and internal helpers)."
        ),
    )
    parser.add_argument(
        "--roots",
        nargs="*",
        default=None,
        help="Source dirs to scan. Auto-detected (src/ or packages) if omitted.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify docs/api-digest.md is in sync; do not write.",
    )
    args = parser.parse_args()

    root = repo_root()
    roots = detect_roots(root, args.roots)
    if not roots:
        logger.error("No source roots found — pass --roots to specify them.")
        return 1
    logger.info(
        "Scanning %d source root(s): %s",
        len(roots),
        ", ".join(r.name for r in roots),
    )

    digests = build_digest(root, roots)
    generated = render_digest(digests)

    if args.check:
        return check_doc_drift(
            root,
            DOC_RELPATH,
            generated,
            "forge-gen-api-digest",
        )

    doc_path = root / DOC_RELPATH
    doc_path.parent.mkdir(parents=True, exist_ok=True)
    doc_path.write_text(generated)
    logger.info(
        "Wrote %s (%d modules, %d symbols).",
        DOC_RELPATH,
        len(digests),
        count_symbols(digests),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
