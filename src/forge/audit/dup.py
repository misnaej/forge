"""forge-audit-dup: duplicate function-body detection.

Detects functions whose bodies were copied across files rather than
deduplicated into a shared helper. Operates per-function across the
whole scanned tree:

    Step 1: parse each .py file with ``ast``.
    Step 2: for every ``FunctionDef`` / ``AsyncFunctionDef``, normalize the
            body (strip docstring + comments via ``ast.unparse``) and
            compute a SHA-256 hash + a set of k-gram token shingles.
    Step 3: group by hash → exact duplicates.
    Step 4: pairwise Jaccard on shingles within compatible token-length
            buckets → near-duplicates above threshold.
    Step 5: group by bare function name across files → name collisions
            (different bodies, same name in multiple files).

Findings are written to ``code_health/audit_dup.log`` in the standard
format. Severity heuristics:

    * CRITICAL — exact body match in 3+ files
    * HIGH     — exact body match in 2 files
    * MEDIUM   — near-duplicate (Jaccard ≥ threshold) across files
    * LOW      — name collision only (different bodies)

Maps to Robert C. Martin's CRP / CCP (Common Reuse + Common Closure):
helpers that share a body share a reason to change and a reason to be
reused — they belong in one place.
"""

from __future__ import annotations

import ast
import hashlib
import io
import keyword
import logging
import sys
import tokenize
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from forge.audit.common import (
    Finding,
    Scope,
    Severity,
    exit_code_for,
    iter_files,
    make_audit_parser,
    relpath,
    resolve_roots,
    write_log,
)
from forge.git_utils import configure_cli_logging


if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path


configure_cli_logging()
logger = logging.getLogger(__name__)


DEFAULT_MIN_TOKENS = 30
DEFAULT_JACCARD_THRESHOLD = 0.85
DEFAULT_SHINGLE_SIZE = 5
LENGTH_BUCKET_TOLERANCE = 0.25
MIN_GROUP_SIZE = 2
EXACT_HIGH_PATH_COUNT = 2
EXACT_CRITICAL_PATH_COUNT = 3


@dataclass(frozen=True)
class CodeUnit:
    """One function definition extracted from the source tree.

    Attributes:
        path: Repo-relative POSIX path to the file.
        line: 1-based line number of the ``def`` statement.
        qualified_name: ``"name"`` for module-level, ``"Class.name"`` for
            methods, deeper for nested defs.
        bare_name: Just the function name (used for name-collision grouping).
        body_hash: Hex SHA-256 of normalized body source.
        token_count: Tokens after normalization.
        shingles: Frozen set of k-gram token tuples.
    """

    path: str
    line: int
    qualified_name: str
    bare_name: str
    body_hash: str
    token_count: int
    shingles: frozenset[tuple[str, ...]] = field(default_factory=frozenset)


def _strip_docstring(body: list[ast.stmt]) -> list[ast.stmt]:
    """Return ``body`` with a leading docstring (if any) removed.

    Args:
        body: List of statements from a function or class body.

    Returns:
        New list omitting the first element if it is a string-expression.
    """
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        return body[1:]
    return body


def _normalize_body(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """Render the function body to canonical source (no docstring).

    Args:
        node: AST node for a function or async function.

    Returns:
        Newline-joined ``ast.unparse`` of each non-docstring body statement.
        Comments are already absent — ``ast`` does not retain them.
    """
    stripped = _strip_docstring(node.body)
    if not stripped:
        return ""
    return "\n".join(ast.unparse(stmt) for stmt in stripped)


def _tokenize_body(source: str) -> list[str]:
    """Tokenize ``source`` into a stable string sequence for shingling.

    Folding rules (chosen so renamed locals do not defeat similarity):

        * Layout tokens (NEWLINE, INDENT, DEDENT, NL, COMMENT, ENCODING,
          ENDMARKER) are dropped.
        * String literals collapse to ``"STR"``.
        * Numeric literals collapse to ``"NUM"``.
        * Keywords keep their text (``if``, ``return``, ``for``, …) so
          control flow stays distinguishable.
        * All other identifiers collapse to ``"ID"``. This makes the
          shingle similarity robust to renaming locals/parameters while
          still differentiating structurally distinct code.

    Args:
        source: Body source returned by ``_normalize_body``.

    Returns:
        Ordered list of token strings.
    """
    if not source.strip():
        return []
    skip = {
        tokenize.NEWLINE,
        tokenize.INDENT,
        tokenize.DEDENT,
        tokenize.NL,
        tokenize.COMMENT,
        tokenize.ENCODING,
        tokenize.ENDMARKER,
    }
    tokens: list[str] = []
    try:
        readline = io.BytesIO(source.encode("utf-8")).readline
        for tok in tokenize.tokenize(readline):
            if tok.type in skip:
                continue
            if tok.type == tokenize.STRING:
                tokens.append("STR")
            elif tok.type == tokenize.NUMBER:
                tokens.append("NUM")
            elif tok.type == tokenize.NAME:
                tokens.append(tok.string if keyword.iskeyword(tok.string) else "ID")
            else:
                tokens.append(tok.string)
    except tokenize.TokenError:
        logger.debug("tokenize failed on a snippet — skipping")
        return []
    return tokens


def _shingles(tokens: list[str], k: int) -> frozenset[tuple[str, ...]]:
    """Return the set of ``k``-grams over the token sequence.

    Args:
        tokens: Token list from ``_tokenize_body``.
        k: Window size.

    Returns:
        Frozen set of ``k``-length tuples. Empty if ``len(tokens) < k``.
    """
    if len(tokens) < k:
        return frozenset()
    return frozenset(tuple(tokens[i : i + k]) for i in range(len(tokens) - k + 1))


_FuncDef = ast.FunctionDef | ast.AsyncFunctionDef


def _walk_functions(tree: ast.Module) -> Iterable[tuple[_FuncDef, str]]:
    """Yield every function definition with its qualified-name prefix.

    Args:
        tree: Parsed module.

    Yields:
        ``(node, qualified_name)`` pairs. Qualified names use dot notation
        for nesting (``"Outer.inner"``, ``"Foo.method"``).
    """

    def walk(node: ast.AST, prefix: str) -> Iterable[tuple[_FuncDef, str]]:
        """Recurse over ``node``'s children, prefixing names with ``prefix``.

        Args:
            node: Parent AST node.
            prefix: Dotted qualified-name prefix accumulated so far.

        Yields:
            ``(function_node, qualified_name)`` for every descendant ``def``.
        """
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qname = f"{prefix}.{child.name}" if prefix else child.name
                yield child, qname
                yield from walk(child, qname)
            elif isinstance(child, ast.ClassDef):
                qname = f"{prefix}.{child.name}" if prefix else child.name
                yield from walk(child, qname)

    yield from walk(tree, "")


def extract_units(path: Path, *, min_tokens: int, shingle_size: int) -> list[CodeUnit]:
    """Extract every function-sized unit from a single file.

    Args:
        path: Absolute path to a ``.py`` file.
        min_tokens: Skip units whose normalized body has fewer tokens.
        shingle_size: K-gram window for similarity shingles.

    Returns:
        List of ``CodeUnit`` records. Empty on parse failure.
    """
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except (SyntaxError, UnicodeDecodeError) as exc:
        logger.debug("skipping %s: %s", path, exc)
        return []

    rel = relpath(path)
    units: list[CodeUnit] = []
    for node, qname in _walk_functions(tree):
        body_src = _normalize_body(node)
        tokens = _tokenize_body(body_src)
        if len(tokens) < min_tokens:
            continue
        body_hash = hashlib.sha256(body_src.encode("utf-8")).hexdigest()
        units.append(
            CodeUnit(
                path=rel,
                line=node.lineno,
                qualified_name=qname,
                bare_name=node.name,
                body_hash=body_hash,
                token_count=len(tokens),
                shingles=_shingles(tokens, shingle_size),
            ),
        )
    return units


def _group_by_hash(units: list[CodeUnit]) -> list[list[CodeUnit]]:
    """Group units sharing an identical body hash.

    Args:
        units: All extracted units.

    Returns:
        Groups of size ≥ 2 ordered by descending group size.
    """
    buckets: dict[str, list[CodeUnit]] = defaultdict(list)
    for u in units:
        buckets[u.body_hash].append(u)
    return sorted(
        (g for g in buckets.values() if len(g) >= MIN_GROUP_SIZE),
        key=lambda g: (-len(g), g[0].path, g[0].line),
    )


def _jaccard(a: frozenset[tuple[str, ...]], b: frozenset[tuple[str, ...]]) -> float:
    """Jaccard similarity between two shingle sets.

    Args:
        a: First shingle set.
        b: Second shingle set.

    Returns:
        Size of the intersection divided by size of the union. Returns
        ``0.0`` for two empty sets.
    """
    if not a and not b:
        return 0.0
    return len(a & b) / len(a | b)


def _find_near_dups(
    units: list[CodeUnit],
    exact_dup_ids: set[int],
    *,
    threshold: float,
) -> list[tuple[CodeUnit, CodeUnit, float]]:
    """Pairwise scan for near-duplicate pairs above the Jaccard threshold.

    Length-bucketed to keep the O(N²) factor manageable on large repos:
    only units within a token-count tolerance of each other are compared.
    Units that are part of an exact-dup group are excluded (already
    reported with higher severity).

    Args:
        units: All extracted units.
        exact_dup_ids: ``id()`` of units already reported as exact duplicates.
        threshold: Minimum Jaccard similarity to report.

    Returns:
        Triples ``(a, b, similarity)`` for pairs above the threshold, sorted
        by descending similarity.
    """
    candidates = [u for u in units if id(u) not in exact_dup_ids and u.shingles]
    candidates.sort(key=lambda u: u.token_count)

    pairs: list[tuple[CodeUnit, CodeUnit, float]] = []
    for i, a in enumerate(candidates):
        lo = a.token_count * (1.0 - LENGTH_BUCKET_TOLERANCE)
        hi = a.token_count * (1.0 + LENGTH_BUCKET_TOLERANCE)
        for b in candidates[i + 1 :]:
            if b.token_count > hi:
                break
            if b.token_count < lo:
                continue
            sim = _jaccard(a.shingles, b.shingles)
            if sim >= threshold:
                pairs.append((a, b, sim))
    pairs.sort(key=lambda t: (-t[2], t[0].path, t[1].path))
    return pairs


def _find_name_collisions(
    units: list[CodeUnit],
    exact_dup_ids: set[int],
) -> list[list[CodeUnit]]:
    """Group units sharing a bare name across files but with different bodies.

    Args:
        units: All extracted units.
        exact_dup_ids: ``id()`` of units already reported as exact duplicates.

    Returns:
        Groups (size ≥ 2) of same-name units in multiple files with at least
        two distinct body hashes.
    """
    by_name: dict[str, list[CodeUnit]] = defaultdict(list)
    for u in units:
        if id(u) in exact_dup_ids:
            continue
        by_name[u.bare_name].append(u)

    groups: list[list[CodeUnit]] = []
    for name, items in by_name.items():
        if name.startswith("__") and name.endswith("__"):
            continue
        paths = {u.path for u in items}
        hashes = {u.body_hash for u in items}
        if len(paths) >= MIN_GROUP_SIZE and len(hashes) >= MIN_GROUP_SIZE:
            groups.append(sorted(items, key=lambda u: (u.path, u.line)))
    groups.sort(key=lambda g: (-len(g), g[0].bare_name))
    return groups


def _exact_severity(paths: set[str]) -> Severity:
    """Pick severity for an exact-duplicate group.

    Args:
        paths: Set of distinct file paths the group spans.

    Returns:
        ``CRITICAL`` for 3+ files, ``HIGH`` for 2 files, ``MEDIUM`` for
        same-file duplicates.
    """
    if len(paths) >= EXACT_CRITICAL_PATH_COUNT:
        return Severity.CRITICAL
    if len(paths) >= EXACT_HIGH_PATH_COUNT:
        return Severity.HIGH
    return Severity.MEDIUM


def _build_exact_findings(
    groups: list[list[CodeUnit]],
) -> tuple[list[Finding], set[int]]:
    """Render exact-duplicate groups as ``Finding`` records.

    Args:
        groups: Exact-duplicate clusters from ``_group_by_hash``.

    Returns:
        ``(findings, ids)`` — list of findings (one per group, pinned at the
        first occurrence) and the set of ``id()`` for every unit covered.
    """
    findings: list[Finding] = []
    covered: set[int] = set()
    for group in groups:
        paths = {u.path for u in group}
        severity = _exact_severity(paths)
        head = group[0]
        others = group[1:]
        evidence = tuple(
            f"also at {u.path}:{u.line} ({u.qualified_name})" for u in others
        )
        findings.append(
            Finding(
                audit="dup",
                severity=severity,
                path=head.path,
                line=head.line,
                message=(
                    f"exact body duplicate of {head.qualified_name} "
                    f"across {len(group)} sites ({len(paths)} files)"
                ),
                evidence=evidence,
            ),
        )
        covered.update(id(u) for u in group)
    return findings, covered


def _build_near_findings(
    pairs: list[tuple[CodeUnit, CodeUnit, float]],
) -> list[Finding]:
    """Render near-duplicate pairs as ``Finding`` records.

    Args:
        pairs: Triples from ``_find_near_dups``.

    Returns:
        One finding per pair. Cross-file pairs are MEDIUM; same-file pairs LOW.
    """
    findings: list[Finding] = []
    for a, b, sim in pairs:
        cross_file = a.path != b.path
        severity = Severity.MEDIUM if cross_file else Severity.LOW
        findings.append(
            Finding(
                audit="dup",
                severity=severity,
                path=a.path,
                line=a.line,
                message=(
                    f"near-duplicate ({sim:.2f}) of {a.qualified_name} "
                    f"vs {b.qualified_name}"
                ),
                evidence=(f"compare {b.path}:{b.line} ({b.qualified_name})",),
            ),
        )
    return findings


def _build_name_findings(groups: list[list[CodeUnit]]) -> list[Finding]:
    """Render name-collision groups as informational findings.

    Args:
        groups: Same-name clusters with diverging bodies.

    Returns:
        One LOW-severity finding per group.
    """
    findings: list[Finding] = []
    for group in groups:
        head = group[0]
        evidence = tuple(
            f"also at {u.path}:{u.line} ({u.qualified_name})" for u in group[1:]
        )
        findings.append(
            Finding(
                audit="dup",
                severity=Severity.LOW,
                path=head.path,
                line=head.line,
                message=(
                    f"name collision on '{head.bare_name}' across "
                    f"{len(group)} files (bodies differ — verify intent)"
                ),
                evidence=evidence,
            ),
        )
    return findings


def _summary(
    n_units: int,
    n_exact: int,
    n_near: int,
    n_name: int,
) -> str:
    """Render the one-paragraph audit summary.

    Args:
        n_units: Total scanned function units.
        n_exact: Exact-duplicate group count.
        n_near: Near-duplicate pair count.
        n_name: Name-collision group count.

    Returns:
        One-line summary suitable for the log header.
    """
    return (
        f"Scanned {n_units} function units. "
        f"Found {n_exact} exact-duplicate group(s), "
        f"{n_near} near-duplicate pair(s), "
        f"{n_name} name-collision group(s)."
    )


@dataclass(frozen=True)
class DupConfig:
    """Tunable knobs for the duplicate-detection pipeline.

    Attributes:
        min_tokens: Skip function units with fewer normalized tokens.
        threshold: Minimum Jaccard similarity to report a near-duplicate.
        shingle_size: K-gram window size for shingles.
        output: Optional log-path override.
    """

    min_tokens: int = DEFAULT_MIN_TOKENS
    threshold: float = DEFAULT_JACCARD_THRESHOLD
    shingle_size: int = DEFAULT_SHINGLE_SIZE
    output: Path | None = None


def run(scope: Scope, roots: list[Path], config: DupConfig) -> int:
    """Execute the full duplicate-detection pipeline.

    Args:
        scope: ``FULL`` or ``CHANGED``.
        roots: Directories to walk for ``FULL`` scope.
        config: Tunable knobs (token threshold, Jaccard, shingle size, output).

    Returns:
        Process exit code (0 = clean or only LOW findings, 1 otherwise).
    """
    units: list[CodeUnit] = []
    for path in iter_files(scope, roots):
        units.extend(
            extract_units(
                path,
                min_tokens=config.min_tokens,
                shingle_size=config.shingle_size,
            ),
        )

    exact_groups = _group_by_hash(units)
    exact_findings, covered = _build_exact_findings(exact_groups)

    near_pairs = _find_near_dups(units, covered, threshold=config.threshold)
    near_findings = _build_near_findings(near_pairs)

    name_groups = _find_name_collisions(units, covered)
    name_findings = _build_name_findings(name_groups)

    findings = exact_findings + near_findings + name_findings
    summary = _summary(len(units), len(exact_groups), len(near_pairs), len(name_groups))
    write_log("dup", findings, summary, output=config.output)
    return exit_code_for(findings)


def main() -> int:
    """CLI entry point for ``forge-audit-dup``.

    Returns:
        Process exit code.
    """
    parser = make_audit_parser(
        prog="forge-audit-dup",
        description="Detect duplicate / near-duplicate / name-colliding functions.",
    )
    parser.add_argument(
        "--min-tokens",
        type=int,
        default=DEFAULT_MIN_TOKENS,
        help=(
            "Skip functions with fewer normalized tokens "
            f"(default: {DEFAULT_MIN_TOKENS})."
        ),
    )
    parser.add_argument(
        "--jaccard-threshold",
        type=float,
        default=DEFAULT_JACCARD_THRESHOLD,
        help=(
            "Minimum Jaccard similarity for near-duplicate report "
            f"(default: {DEFAULT_JACCARD_THRESHOLD})."
        ),
    )
    parser.add_argument(
        "--shingle-size",
        type=int,
        default=DEFAULT_SHINGLE_SIZE,
        help=f"K-gram shingle window (default: {DEFAULT_SHINGLE_SIZE}).",
    )
    args = parser.parse_args()
    scope = Scope(args.scope)
    roots = resolve_roots(args.roots)
    config = DupConfig(
        min_tokens=args.min_tokens,
        threshold=args.jaccard_threshold,
        shingle_size=args.shingle_size,
        output=args.output,
    )
    return run(scope, roots, config)


if __name__ == "__main__":
    sys.exit(main())
