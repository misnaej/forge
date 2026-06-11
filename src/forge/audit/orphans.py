"""forge-audit-orphans: dead-code detection via vulture.

Reports symbols that vulture flags as unused with high confidence
(default ≥ 80%). Aimed at finding helpers that were never wired up,
imports left after a refactor, and dataclasses that survived a deletion.

Vulture is dynamically-aware-blind (it cannot see runtime introspection,
plugin entry points, attribute access via ``getattr``, …), so false
positives are common. The confidence floor mitigates this; the agent
reading this log should still verify before deleting anything.

Severity:

    * MEDIUM — confidence ≥ 95% (very likely dead)
    * LOW    — confidence in [80%, 95%) (probably dead, double-check)

Requires the ``[audit]`` extra:

    pip install -e ".[audit]"

When ``vulture`` is not importable, the script fails loudly with that hint.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from pathlib import Path

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


configure_cli_logging()
logger = logging.getLogger(__name__)


DEFAULT_MIN_CONFIDENCE = 80
HIGH_CONFIDENCE_FLOOR = 95
VULTURE_MISSING_HINT = (
    "vulture is not installed. Run:\n"
    '    pip install -e ".[audit]"\n'
    "(or your project's equivalent) and retry."
)


@dataclass(frozen=True)
class OrphansConfig:
    """Tunable knobs for the orphans audit.

    Attributes:
        min_confidence: Minimum vulture confidence (0-100) to report.
        output: Optional log-path override.
    """

    min_confidence: int = DEFAULT_MIN_CONFIDENCE
    output: Path | None = None


def _load_vulture() -> object:
    """Import the vulture module or exit with an install hint.

    Returns:
        The imported ``vulture`` module.
    """
    try:
        import vulture  # noqa: PLC0415 — optional dep, must be guarded
    except ImportError:
        sys.stderr.write(f"forge-audit-orphans: {VULTURE_MISSING_HINT}\n")
        raise SystemExit(1) from None
    return vulture


def _severity(confidence: int) -> Severity:
    """Map a vulture confidence percentage to a finding severity.

    Args:
        confidence: Vulture confidence (0-100).

    Returns:
        ``MEDIUM`` for very-likely dead code, ``LOW`` otherwise.
    """
    if confidence >= HIGH_CONFIDENCE_FLOOR:
        return Severity.MEDIUM
    return Severity.LOW


def _build_findings(items: list[object]) -> list[Finding]:
    """Translate vulture items to ``Finding`` records.

    Args:
        items: Iterable returned by ``Vulture.get_unused_code()``.

    Returns:
        One finding per item.
    """
    findings: list[Finding] = []
    for item in items:
        filename = relpath(Path(item.filename))  # type: ignore[attr-defined]
        line_no = int(item.first_lineno)  # type: ignore[attr-defined]
        typ = str(item.typ)  # type: ignore[attr-defined]
        name = str(item.name)  # type: ignore[attr-defined]
        confidence = int(item.confidence)  # type: ignore[attr-defined]
        findings.append(
            Finding(
                audit="orphans",
                severity=_severity(confidence),
                path=filename,
                line=line_no,
                message=f"unused {typ} '{name}' (confidence {confidence}%)",
            ),
        )
    return findings


def _scavenge_paths(
    scope: Scope,
    roots: list[Path],
) -> list[Path]:
    """Decide what paths to hand to ``Vulture.scavenge``.

    For ``Scope.FULL`` we pass the resolved roots directly (vulture walks
    recursively, so this is the cheapest option). For ``Scope.CHANGED`` we
    enumerate via ``iter_files`` and pass the explicit file list.

    Args:
        scope: Audit scope.
        roots: Resolved scan roots.

    Returns:
        Paths to feed to vulture.
    """
    if scope is Scope.FULL:
        return list(roots)
    return list(iter_files(scope, roots))


def run(scope: Scope, roots: list[Path], config: OrphansConfig) -> int:
    """Execute the orphans audit.

    Args:
        scope: ``FULL`` or ``CHANGED``.
        roots: Resolved scan roots.
        config: Tunable knobs.

    Returns:
        Process exit code (0 = clean / LOW-only, 1 otherwise).
    """
    vulture = _load_vulture()
    v = vulture.Vulture(verbose=False)  # type: ignore[attr-defined]
    paths = _scavenge_paths(scope, roots)
    if not paths:
        write_log("orphans", [], "No paths to scavenge.", output=config.output)
        return 0
    v.scavenge([str(p) for p in paths])
    items = list(v.get_unused_code(min_confidence=config.min_confidence))
    findings = _build_findings(items)
    summary = (
        f"Scanned {len(paths)} path(s). "
        f"Found {len(findings)} unused symbol(s) "
        f"(min_confidence={config.min_confidence})."
    )
    write_log("orphans", findings, summary, output=config.output)
    return exit_code_for(findings)


def main() -> int:
    """CLI entry point for ``forge-audit-orphans``.

    Returns:
        Process exit code.
    """
    parser = make_audit_parser(
        prog="forge-audit-orphans",
        description="Detect unused code via vulture (>= min-confidence).",
    )
    parser.add_argument(
        "--min-confidence",
        type=int,
        default=DEFAULT_MIN_CONFIDENCE,
        help=(
            "Minimum vulture confidence (0-100) to report "
            f"(default: {DEFAULT_MIN_CONFIDENCE})."
        ),
    )
    args = parser.parse_args()
    scope = Scope(args.scope)
    roots = resolve_roots(args.roots)
    config = OrphansConfig(
        min_confidence=args.min_confidence,
        output=args.output,
    )
    return run(scope, roots, config)


if __name__ == "__main__":
    sys.exit(main())
