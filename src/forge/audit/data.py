"""forge-audit-data: structured-data integrity checks.

Verifies column alignment in CSV files (unquoted commas in description
fields misalign every subsequent column silently) and applies
syntax-level guardrails on YAML / JSON / TOML configs that get
hand-edited.

Scope:

    * ``.csv`` — verify every data row has the same column count as the
      header (catches unquoted commas in description fields).
    * ``.json`` — verify the file parses.
    * ``.toml`` — verify the file parses (Python 3.11+; older interpreters
      skip TOML files).
    * ``.yaml`` / ``.yml`` — verify the file parses (requires PyYAML;
      skipped silently when PyYAML is not installed).
    * Optional schema check: when ``<file>.schema.json`` sits next to a
      ``.json`` and ``jsonschema`` is importable, validate.

Severity:

    * HIGH   — CSV column-count mismatch, parse failure
    * MEDIUM — jsonschema validation error
    * LOW    — file skipped (parser unavailable)
"""

from __future__ import annotations

import csv
import json
import logging
import sys
import tomllib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from forge.audit.common import (
    Finding,
    Scope,
    Severity,
    count_by_severity,
    exit_code_for,
    iter_files,
    make_audit_parser,
    relpath,
    resolve_roots,
    write_log,
)
from forge.git_utils import configure_cli_logging


if TYPE_CHECKING:
    from pathlib import Path
    from typing import Any

try:
    import yaml as _yaml_mod
except ImportError:
    _yaml_mod = None

try:
    import jsonschema as _jsonschema_mod
except ImportError:
    _jsonschema_mod = None


configure_cli_logging()
logger = logging.getLogger(__name__)


DEFAULT_DATA_SUFFIXES: tuple[str, ...] = (".csv", ".json", ".toml", ".yaml", ".yml")
SKIPPED_FILENAMES: frozenset[str] = frozenset(
    {
        "package-lock.json",
        "yarn.lock",
        "uv.lock",
        "poetry.lock",
        "Cargo.lock",
    },
)


@dataclass(frozen=True)
class DataConfig:
    """Tunable knobs for the data audit.

    Attributes:
        suffixes: File suffixes to scan (default CSV/JSON/TOML/YAML).
        output: Optional log-path override.
    """

    suffixes: tuple[str, ...] = DEFAULT_DATA_SUFFIXES
    output: Path | None = field(default=None)


def _gather_files(
    scope: Scope,
    roots: list[Path],
    suffixes: tuple[str, ...],
) -> list[Path]:
    """Collect candidate data files across the configured suffixes.

    Args:
        scope: Audit scope.
        roots: Scan roots.
        suffixes: File extensions to include (with leading dot).

    Returns:
        Deduplicated absolute paths, lock-files excluded.
    """
    seen: set[Path] = set()
    out: list[Path] = []
    for suffix in suffixes:
        for path in iter_files(scope, roots, suffix=suffix):
            if path in seen or path.name in SKIPPED_FILENAMES:
                continue
            seen.add(path)
            out.append(path)
    return out


def _check_csv(path: Path) -> list[Finding]:
    """Verify CSV column count is consistent across every row.

    Args:
        path: Absolute path to a ``.csv`` file.

    Returns:
        One HIGH finding per misaligned row; one HIGH finding on parse failure.
    """
    rel = relpath(path)
    try:
        with path.open(encoding="utf-8", newline="") as fh:
            reader = csv.reader(fh)
            try:
                header = next(reader)
            except StopIteration:
                return []
            expected = len(header)
            findings: list[Finding] = []
            for line_no, row in enumerate(reader, start=2):
                if len(row) != expected:
                    findings.append(
                        Finding(
                            audit="data",
                            severity=Severity.HIGH,
                            path=rel,
                            line=line_no,
                            message=(
                                f"CSV column mismatch: row has {len(row)} "
                                f"columns, header has {expected}"
                            ),
                            evidence=(",".join(row)[:200],),
                        ),
                    )
            return findings
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        return [
            Finding(
                audit="data",
                severity=Severity.HIGH,
                path=rel,
                line=0,
                message=f"CSV parse error: {exc}",
            ),
        ]


def _check_json(path: Path) -> list[Finding]:
    """Parse a JSON file; report any decode error.

    Args:
        path: Absolute path to a ``.json`` file.

    Returns:
        One HIGH finding on parse failure, empty list on success.
    """
    rel = relpath(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return [
            Finding(
                audit="data",
                severity=Severity.HIGH,
                path=rel,
                line=exc.lineno,
                message=f"JSON parse error: {exc.msg}",
            ),
        ]
    except (OSError, UnicodeDecodeError) as exc:
        return [
            Finding(
                audit="data",
                severity=Severity.HIGH,
                path=rel,
                line=0,
                message=f"JSON read error: {exc}",
            ),
        ]
    return _check_jsonschema(path, data)


def _check_jsonschema(path: Path, data: object) -> list[Finding]:
    """Validate a parsed JSON document against ``<path>.schema.json`` if present.

    Args:
        path: Path to the data file (NOT the schema).
        data: Parsed JSON payload.

    Returns:
        One MEDIUM finding per schema violation; LOW finding when a schema
        sibling is present but jsonschema cannot be imported.
    """
    schema_path = path.with_suffix(path.suffix + ".schema.json")
    if not schema_path.exists():
        return []
    rel = relpath(path)
    if _jsonschema_mod is None:
        return [
            Finding(
                audit="data",
                severity=Severity.LOW,
                path=rel,
                line=0,
                message=(
                    f"schema present at {schema_path.name} but `jsonschema` "
                    'is not installed (`pip install -e ".[audit]"`).'
                ),
            ),
        ]
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        return [
            Finding(
                audit="data",
                severity=Severity.MEDIUM,
                path=rel,
                line=0,
                message=f"schema {schema_path.name} unreadable: {exc}",
            ),
        ]
    findings: list[Finding] = []
    validator_cls = _jsonschema_mod.validators.validator_for(schema)
    validator = validator_cls(schema)
    # jsonschema validates arbitrary parsed JSON; its stub types the
    # instance as a JSON union that `object` is not assignable to. The
    # value really is dynamic JSON, so cast at this single boundary.
    for error in sorted(validator.iter_errors(cast("Any", data)), key=lambda e: e.path):
        loc = ".".join(str(p) for p in error.absolute_path) or "<root>"
        findings.append(
            Finding(
                audit="data",
                severity=Severity.MEDIUM,
                path=rel,
                line=0,
                message=f"schema violation at {loc}: {error.message}",
            ),
        )
    return findings


def _check_toml(path: Path) -> list[Finding]:
    """Parse a TOML file; report any decode error.

    Args:
        path: Absolute path to a ``.toml`` file.

    Returns:
        One HIGH finding on parse failure.
    """
    rel = relpath(path)
    try:
        with path.open("rb") as fh:
            tomllib.load(fh)
    except (tomllib.TOMLDecodeError, OSError) as exc:
        return [
            Finding(
                audit="data",
                severity=Severity.HIGH,
                path=rel,
                line=0,
                message=f"TOML parse error: {exc}",
            ),
        ]
    return []


def _check_yaml(path: Path) -> list[Finding]:
    """Parse a YAML file; report any decode error.

    Args:
        path: Absolute path to a ``.yaml`` / ``.yml`` file.

    Returns:
        One HIGH finding on parse failure. One LOW finding when PyYAML is
        not installed (the file is skipped).
    """
    rel = relpath(path)
    if _yaml_mod is None:
        return [
            Finding(
                audit="data",
                severity=Severity.LOW,
                path=rel,
                line=0,
                message="YAML parser unavailable (PyYAML not installed); skipped.",
            ),
        ]
    try:
        with path.open(encoding="utf-8") as fh:
            _yaml_mod.safe_load(fh)
    except _yaml_mod.YAMLError as exc:
        return [
            Finding(
                audit="data",
                severity=Severity.HIGH,
                path=rel,
                line=0,
                message=f"YAML parse error: {exc}",
            ),
        ]
    except (OSError, UnicodeDecodeError) as exc:
        return [
            Finding(
                audit="data",
                severity=Severity.HIGH,
                path=rel,
                line=0,
                message=f"YAML read error: {exc}",
            ),
        ]
    return []


_DISPATCH = {
    ".csv": _check_csv,
    ".json": _check_json,
    ".toml": _check_toml,
    ".yaml": _check_yaml,
    ".yml": _check_yaml,
}


def _check_one(path: Path) -> list[Finding]:
    """Dispatch a single file to the appropriate parser.

    Args:
        path: Absolute path to a data file.

    Returns:
        Findings produced by the matched checker, empty list if no checker.
    """
    checker = _DISPATCH.get(path.suffix.lower())
    if checker is None:
        return []
    return checker(path)


def run(scope: Scope, roots: list[Path], config: DataConfig) -> int:
    """Execute the data-integrity audit.

    Args:
        scope: ``FULL`` or ``CHANGED``.
        roots: Scan roots.
        config: Tunable knobs.

    Returns:
        Process exit code (0 = clean / LOW-only, 1 otherwise).
    """
    paths = _gather_files(scope, roots, config.suffixes)
    findings: list[Finding] = []
    for path in paths:
        findings.extend(_check_one(path))
    counts = count_by_severity(findings)
    summary = (
        f"Scanned {len(paths)} file(s). "
        f"Found {counts[Severity.HIGH]} HIGH (parse / column mismatch), "
        f"{counts[Severity.MEDIUM]} MEDIUM (schema), "
        f"{counts[Severity.LOW]} LOW (skipped)."
    )
    write_log("data", findings, summary, output=config.output)
    return exit_code_for(findings)


def main() -> int:
    """CLI entry point for ``forge-audit-data``.

    Returns:
        Process exit code.
    """
    parser = make_audit_parser(
        prog="forge-audit-data",
        description="Structured-data integrity (CSV alignment + parse checks).",
    )
    args = parser.parse_args()
    scope = Scope(args.scope)
    roots = resolve_roots(args.roots)
    config = DataConfig(output=args.output)
    return run(scope, roots, config)


if __name__ == "__main__":
    sys.exit(main())
