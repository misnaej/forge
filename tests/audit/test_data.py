"""Tests for ``forge.audit.data`` structured-data integrity checks."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from forge.audit import common, data
from forge.audit.common import Scope, Severity
from forge.audit.data import (
    DataConfig,
    _check_csv,
    _check_json,
    _check_one,
    _check_toml,
    _check_yaml,
    _gather_files,
    run,
)


if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def fake_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create a docs/data layout and point common.repo_root at it.

    Returns:
        The repo root path.
    """
    (tmp_path / "docs").mkdir()
    monkeypatch.setattr(common, "repo_root", lambda: tmp_path)
    return tmp_path


def _write(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` after ensuring parents exist.

    Args:
        path: Destination file path.
        text: Content to write.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_check_csv_clean_returns_no_findings(fake_repo: Path) -> None:
    """A CSV where every row matches the header column count is clean."""
    f = fake_repo / "docs" / "ok.csv"
    _write(f, "name,direction,weight\na,1,0.5\nb,-1,0.8\n")
    assert _check_csv(f) == []


def test_check_csv_reports_misaligned_row(fake_repo: Path) -> None:
    """Unquoted comma inside a description splits the row."""
    f = fake_repo / "docs" / "bad.csv"
    _write(
        f,
        "name,direction,description\n"
        "a,1,fine description\n"
        "b,-1,{none: 0, low: 1, high: 2}\n",
    )
    findings = _check_csv(f)
    assert len(findings) == 1
    assert findings[0].severity is Severity.HIGH
    assert "column mismatch" in findings[0].message
    assert findings[0].line == 3


def test_check_json_clean_parses_successfully(fake_repo: Path) -> None:
    """Valid JSON with no schema sibling yields no findings."""
    f = fake_repo / "docs" / "ok.json"
    _write(f, json.dumps({"a": 1, "b": [2, 3]}))
    assert _check_json(f) == []


def test_check_json_reports_decode_error(fake_repo: Path) -> None:
    """Invalid JSON yields a HIGH parse-error finding with line info."""
    f = fake_repo / "docs" / "bad.json"
    _write(f, "{ this isn't json }")
    findings = _check_json(f)
    assert len(findings) == 1
    assert findings[0].severity is Severity.HIGH
    assert "JSON parse error" in findings[0].message


def test_check_json_validates_with_jsonschema_when_available(
    fake_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``*.schema.json`` sits next to a JSON file, validate against it."""
    pytest.importorskip("jsonschema")
    f = fake_repo / "docs" / "config.json"
    schema = fake_repo / "docs" / "config.json.schema.json"
    _write(f, json.dumps({"version": "not-a-number"}))
    _write(
        schema,
        json.dumps(
            {
                "type": "object",
                "properties": {"version": {"type": "number"}},
            },
        ),
    )
    findings = _check_json(f)
    assert any(fnd.severity is Severity.MEDIUM for fnd in findings)
    assert any("schema violation" in fnd.message for fnd in findings)


def test_check_json_low_when_schema_present_but_jsonschema_missing(
    fake_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A schema sibling without ``jsonschema`` installed yields a LOW notice."""
    monkeypatch.setattr(data, "_jsonschema_mod", None)
    f = fake_repo / "docs" / "config.json"
    schema = fake_repo / "docs" / "config.json.schema.json"
    _write(f, json.dumps({"x": 1}))
    _write(schema, json.dumps({"type": "object"}))
    findings = _check_json(f)
    assert any(fnd.severity is Severity.LOW for fnd in findings)
    assert any("jsonschema` is not installed" in fnd.message for fnd in findings)


def test_check_toml_clean(fake_repo: Path) -> None:
    """Valid TOML yields no findings (Python 3.11+)."""
    f = fake_repo / "docs" / "ok.toml"
    _write(f, '[section]\nkey = "value"\n')
    assert _check_toml(f) == []


def test_check_toml_reports_parse_error(fake_repo: Path) -> None:
    """Malformed TOML yields a HIGH parse-error finding."""
    f = fake_repo / "docs" / "bad.toml"
    _write(f, "[section\nkey = no quotes\n")
    findings = _check_toml(f)
    assert len(findings) == 1
    assert findings[0].severity is Severity.HIGH


def test_check_yaml_low_when_pyyaml_missing(
    fake_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without PyYAML the YAML checker emits a LOW skip notice."""
    monkeypatch.setattr(data, "_yaml_mod", None)
    f = fake_repo / "docs" / "any.yaml"
    _write(f, "")
    findings = _check_yaml(f)
    assert len(findings) == 1
    assert findings[0].severity is Severity.LOW


def test_check_one_dispatches_on_suffix(fake_repo: Path) -> None:
    """Suffix → checker dispatch picks the right routine."""
    f_csv = fake_repo / "docs" / "x.csv"
    _write(f_csv, "a,b\n1,2\n")
    assert _check_one(f_csv) == []
    f_unknown = fake_repo / "docs" / "x.txt"
    _write(f_unknown, "nothing to check")
    assert _check_one(f_unknown) == []


def test_gather_files_skips_lock_files(fake_repo: Path) -> None:
    """``package-lock.json`` and friends are excluded from the file list."""
    _write(fake_repo / "docs" / "ok.json", "{}")
    _write(fake_repo / "docs" / "package-lock.json", "{}")
    paths = _gather_files(Scope.FULL, [fake_repo / "docs"], (".json",))
    names = {p.name for p in paths}
    assert "ok.json" in names
    assert "package-lock.json" not in names


def test_run_reports_csv_misalignment_as_high(fake_repo: Path) -> None:
    """End-to-end: a misaligned CSV produces a HIGH finding and exit 1."""
    _write(
        fake_repo / "docs" / "metrics.csv",
        "name,direction,description\na,1,fine\nb,-1,{none: 0, low: 1, high: 2}\n",
    )
    code = run(Scope.FULL, [fake_repo / "docs"], DataConfig(suffixes=(".csv",)))
    log_path = fake_repo / "code_health" / "audit_data.log"
    log_text = log_path.read_text(encoding="utf-8")
    assert "[HIGH]" in log_text
    assert "column mismatch" in log_text
    assert code == 1


def test_run_clean_repo_returns_zero(fake_repo: Path) -> None:
    """A clean docs tree yields exit 0 and a zero-finding log."""
    _write(fake_repo / "docs" / "ok.csv", "a,b\n1,2\n")
    code = run(Scope.FULL, [fake_repo / "docs"], DataConfig(suffixes=(".csv",)))
    log_path = fake_repo / "code_health" / "audit_data.log"
    log_text = log_path.read_text(encoding="utf-8")
    assert "# findings: 0" in log_text
    assert code == 0
