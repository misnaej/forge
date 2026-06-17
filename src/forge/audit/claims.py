"""forge-audit-claims: extract domain claims from docstrings/comments.

Surfaces every sentence that asserts a comparison or causal claim
involving a configured DOMAIN TERM, so the agent (or human reviewer)
can cross-check the assertion against the project's methodology
document.

This is **extraction only**. No verification logic lives here. Findings
are emitted at ``REVIEW`` severity, which keeps exit code at 0 — the
log is consumed by ``design-checker``, which delegates batched
verification to ``knowledge-search`` against configured sources.

Lexicon:

    * A small built-in default catches generic CS/math terms (``gradient``,
      ``loss``, ``accuracy``, ``latency``, ``throughput``, ``iteration``).
    * Repos extend via ``forge-audit-claims.toml`` at the repo root:

        ::

            lexicon = ["kl", "rmsd", "sie", "conserved", "stable", "folded"]

    * ``--no-default-lexicon`` drops the built-in entirely.
"""

from __future__ import annotations

import ast
import io
import logging
import re
import sys
import tokenize
import tomllib
from dataclasses import dataclass
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
from forge.git_utils import configure_cli_logging, repo_root


if TYPE_CHECKING:
    from pathlib import Path


configure_cli_logging()
logger = logging.getLogger(__name__)


DEFAULT_LEXICON: frozenset[str] = frozenset(
    {
        "gradient",
        "loss",
        "accuracy",
        "latency",
        "throughput",
        "iteration",
        "complexity",
        "stability",
    },
)
CLAIMS_CONFIG_FILENAME = "forge-audit-claims.toml"
SUPPRESSION_PREFIXES: tuple[str, ...] = (
    "noqa",
    "type:",
    "pragma:",
    "fmt:",
    "isort:",
    "ruff:",
    "mypy:",
)
COMMENT_PREVIEW = 200


COMPARISON_RE = re.compile(
    r"\b(lower|higher|more|less|larger|smaller|greater|fewer|bigger)\b"
    r".{0,80}?"
    r"\b(?:=|means|implies?|equates?\s+to|leads?\s+to|causes?|yields?|gives?)\b",
    re.IGNORECASE | re.DOTALL,
)
CAUSATION_RE = re.compile(
    r"\b(causes?|leads?\s+to|results?\s+in|implies?|drives?|forces?)\b",
    re.IGNORECASE,
)
EQUATION_RE = re.compile(
    r"\b([A-Za-z_][\w.-]{1,})\s*=\s*([A-Za-z_][\w.-]{1,})\b",
)


@dataclass(frozen=True)
class ClaimsConfig:
    """Tunable knobs for the claims audit.

    Attributes:
        lexicon: Domain terms — claims must contain at least one to be
            reported. Case-insensitive match against lower-cased text.
        output: Optional log-path override.
    """

    lexicon: frozenset[str]
    output: Path | None = None


def _is_suppression_comment(line_text: str) -> bool:
    """Return ``True`` if a comment is a known lint/type-checker directive.

    Args:
        line_text: Raw line text containing a ``#`` comment.

    Returns:
        Whether the comment is one of the well-known suppression directives
        and should therefore be skipped by claim extraction.
    """
    idx = line_text.find("#")
    if idx < 0:
        return False
    comment_body = line_text[idx + 1 :].strip().lower()
    return any(comment_body.startswith(prefix) for prefix in SUPPRESSION_PREFIXES)


def _looks_like_claim(text: str) -> bool:
    """Return ``True`` if ``text`` matches any of the claim patterns.

    Args:
        text: One line of text.

    Returns:
        Whether at least one comparison / causation / equation pattern fires.
    """
    if COMPARISON_RE.search(text):
        return True
    if CAUSATION_RE.search(text):
        return True
    return bool(EQUATION_RE.search(text))


def _matched_terms(text: str, lexicon: frozenset[str]) -> list[str]:
    """Return the lexicon terms that appear in ``text`` (case-insensitive).

    Args:
        text: One line of text.
        lexicon: Domain-term set.

    Returns:
        Sorted matching terms; empty list when nothing matches.
    """
    low = text.lower()
    return sorted({term for term in lexicon if term in low})


def _docstring_findings(
    source_lines: list[str],
    docstring: str,
    docstring_lineno: int,
    rel: str,
    lexicon: frozenset[str],
) -> list[Finding]:
    """Build claim findings from one docstring.

    Args:
        source_lines: The raw file lines (used to locate the actual claim
            line so log output points at the right place).
        docstring: ``ast.get_docstring`` output.
        docstring_lineno: ``lineno`` of the AST node holding the docstring.
        rel: Repo-relative path.
        lexicon: Active domain-term lexicon.

    Returns:
        One REVIEW finding per line that matches both a claim pattern and
        the lexicon filter.
    """
    findings: list[Finding] = []
    for inner_offset, line in enumerate(docstring.splitlines()):
        stripped = line.strip()
        if not stripped or not _looks_like_claim(stripped):
            continue
        terms = _matched_terms(stripped, lexicon)
        if not terms:
            continue
        absolute_line = _locate_claim_line(
            source_lines,
            docstring_lineno,
            stripped,
            fallback_offset=inner_offset,
        )
        findings.append(
            Finding(
                audit="claims",
                severity=Severity.REVIEW,
                path=rel,
                line=absolute_line,
                message=f"claim mentions {', '.join(terms)} — verify",
                evidence=(stripped[:COMMENT_PREVIEW],),
            ),
        )
    return findings


def _locate_claim_line(
    source_lines: list[str],
    start_line: int,
    claim_text: str,
    *,
    fallback_offset: int,
) -> int:
    """Find the absolute line number containing ``claim_text``.

    Searches forward from ``start_line``; falls back to
    ``start_line + fallback_offset`` if no match is found (rare — happens
    when ``ast.get_docstring`` dedents text the file does not).

    Args:
        source_lines: All file lines (1-based via ``[i-1]`` indexing).
        start_line: Line where the docstring node starts.
        claim_text: The matched claim substring.
        fallback_offset: Offset to add to ``start_line`` if not found.

    Returns:
        1-based absolute line number.
    """
    needle = claim_text.lower()
    last = min(len(source_lines), start_line + 200)
    for line_no in range(start_line, last + 1):
        if line_no < 1 or line_no > len(source_lines):
            continue
        if needle in source_lines[line_no - 1].lower():
            return line_no
    return start_line + fallback_offset


def _docstring_node_findings(
    tree: ast.Module,
    source_lines: list[str],
    rel: str,
    lexicon: frozenset[str],
) -> list[Finding]:
    """Scan every module / class / function docstring in a tree.

    Args:
        tree: Parsed module.
        source_lines: File contents, line-split.
        rel: Repo-relative path.
        lexicon: Active lexicon.

    Returns:
        All claim findings from docstrings in this module.
    """
    findings: list[Finding] = []
    targets: list[tuple[ast.AST, int]] = [(tree, 1)]
    targets.extend(
        (node, node.lineno)
        for node in ast.walk(tree)
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    )
    for target, lineno in targets:
        doc = ast.get_docstring(target)
        if doc is None:
            continue
        findings.extend(_docstring_findings(source_lines, doc, lineno, rel, lexicon))
    return findings


def _comment_findings(
    text: str,
    source_lines: list[str],
    rel: str,
    lexicon: frozenset[str],
) -> list[Finding]:
    """Scan every inline ``#`` comment for claims.

    Args:
        text: Full file text.
        source_lines: File contents, line-split.
        rel: Repo-relative path.
        lexicon: Active lexicon.

    Returns:
        All claim findings from comments in this module.
    """
    findings: list[Finding] = []
    seen_lines: set[int] = set()
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type != tokenize.COMMENT:
                continue
            line_no = tok.start[0]
            if line_no in seen_lines:
                continue
            seen_lines.add(line_no)
            if not (1 <= line_no <= len(source_lines)):
                continue
            full_line = source_lines[line_no - 1]
            if _is_suppression_comment(full_line):
                continue
            comment_body = tok.string.lstrip("#").strip()
            if not _looks_like_claim(comment_body):
                continue
            terms = _matched_terms(comment_body, lexicon)
            if not terms:
                continue
            findings.append(
                Finding(
                    audit="claims",
                    severity=Severity.REVIEW,
                    path=rel,
                    line=line_no,
                    message=f"claim mentions {', '.join(terms)} — verify",
                    evidence=(comment_body[:COMMENT_PREVIEW],),
                ),
            )
    except tokenize.TokenError as exc:
        logger.debug("tokenize failed in %s: %s", rel, exc)
    return findings


def _scan_file(path: Path, lexicon: frozenset[str]) -> list[Finding]:
    """Scan a single ``.py`` file for claim candidates.

    Args:
        path: Absolute path to a Python source file.
        lexicon: Active lexicon.

    Returns:
        All claim findings in the file.
    """
    try:
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text, filename=str(path))
    except (SyntaxError, UnicodeDecodeError, OSError) as exc:
        logger.debug("skipping %s: %s", path, exc)
        return []
    rel = relpath(path)
    source_lines = text.splitlines()
    findings: list[Finding] = []
    findings.extend(_docstring_node_findings(tree, source_lines, rel, lexicon))
    findings.extend(_comment_findings(text, source_lines, rel, lexicon))
    return findings


def load_repo_lexicon(*, use_default: bool = True) -> frozenset[str]:
    """Read ``forge-audit-claims.toml`` (if present) and merge with default.

    Args:
        use_default: Whether to seed with the foundation default lexicon.

    Returns:
        Active lexicon (lower-cased terms).
    """
    base: set[str] = set(DEFAULT_LEXICON) if use_default else set()
    config_path = repo_root() / CLAIMS_CONFIG_FILENAME
    if not config_path.exists():
        return frozenset(base)
    try:
        with config_path.open("rb") as fh:
            data = tomllib.load(fh)
    except (tomllib.TOMLDecodeError, OSError) as exc:
        logger.warning("failed to read %s: %s", config_path, exc)
        return frozenset(base)
    extra = data.get("lexicon", [])
    if isinstance(extra, list):
        base.update(str(t).strip().lower() for t in extra if str(t).strip())
    return frozenset(base)


def run(scope: Scope, roots: list[Path], config: ClaimsConfig) -> int:
    """Execute the claims-extraction pipeline.

    Args:
        scope: ``FULL`` or ``CHANGED``.
        roots: Scan roots.
        config: Active config (lexicon + output).

    Returns:
        Process exit code. Always ``0`` because findings are REVIEW-only —
        downstream consumers decide whether to escalate based on
        ``knowledge-search`` verification.
    """
    findings: list[Finding] = []
    n_files = 0
    for path in iter_files(scope, roots):
        n_files += 1
        findings.extend(_scan_file(path, config.lexicon))
    summary = (
        f"Scanned {n_files} file(s). "
        f"Extracted {len(findings)} candidate claim(s) for verification. "
        f"Lexicon size: {len(config.lexicon)}."
    )
    write_log("claims", findings, summary, output=config.output)
    return exit_code_for(findings)


def main() -> int:
    """CLI entry point for ``forge-audit-claims``.

    Returns:
        Process exit code.
    """
    parser = make_audit_parser(
        prog="forge-audit-claims",
        description="Extract domain claims from docstrings/comments for verification.",
    )
    parser.add_argument(
        "--no-default-lexicon",
        action="store_true",
        help="Disable the built-in lexicon (use only forge-audit-claims.toml).",
    )
    args = parser.parse_args()
    scope = Scope(args.scope)
    roots = resolve_roots(args.roots)
    lexicon = load_repo_lexicon(use_default=not args.no_default_lexicon)
    config = ClaimsConfig(lexicon=lexicon, output=args.output)
    return run(scope, roots, config)


if __name__ == "__main__":
    sys.exit(main())
