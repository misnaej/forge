"""Tests for ``forge.verify_cve_usage`` — the usage-scoped CVE filter.

# MOCKING STRATEGY: pip-audit is never actually run — ``active_cve_ids`` is
# monkeypatched to return a controlled live-CVE set so the intersect + grep
# logic is exercised deterministically. The pattern map and source tree are
# real files under tmp_path.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from forge import verify_cve_usage as cve


if TYPE_CHECKING:
    import pytest


_PATTERN_TOML = """\
['CVE-2024-0001']
package = "lxml"
description = "XML entity expansion"
patterns = ['lxml\\.etree', 'from lxml import etree']
risk = "only exploitable parsing untrusted XML"
mitigation = "ensure XML sources are trusted"
"""


def _write_map(root: Path, body: str = _PATTERN_TOML) -> None:
    """Write a cve_usage_patterns.toml map at the repo root.

    Args:
        root: Repo root.
        body: TOML body for the pattern map.
    """
    (root / cve.PATTERN_FILE).write_text(body, encoding="utf-8")


def _write_src(root: Path, body: str, name: str = "app.py") -> None:
    """Write a Python module under ``src/`` to scan.

    Args:
        root: Repo root.
        body: Module source.
        name: Module filename.
    """
    src = root / "src"
    src.mkdir(exist_ok=True)
    (src / name).write_text(body, encoding="utf-8")


def test_active_cve_with_usage_is_a_finding(tmp_path: Path) -> None:
    """A live CVE whose pattern matches real code yields a finding."""
    _write_map(tmp_path)
    _write_src(tmp_path, "import lxml.etree\nlxml.etree.parse(src)\n")
    findings = cve.scan(tmp_path, cve.load_patterns(tmp_path), {"CVE-2024-0001"})
    assert [(f.path, f.line) for f in findings] == [
        ("src/app.py", 1),
        ("src/app.py", 2),
    ]
    assert findings[0].risk == "only exploitable parsing untrusted XML"
    assert findings[0].mitigation == "ensure XML sources are trusted"


def test_active_cve_without_usage_is_no_finding(tmp_path: Path) -> None:
    """A live CVE whose pattern matches nothing in the code is silent."""
    _write_map(tmp_path)
    _write_src(tmp_path, "import json\njson.loads('{}')\n")
    assert cve.scan(tmp_path, cve.load_patterns(tmp_path), {"CVE-2024-0001"}) == []


def test_inactive_cve_is_skipped(tmp_path: Path) -> None:
    """A CVE not in pip-audit's live set is never checked, even if used."""
    _write_map(tmp_path)
    _write_src(tmp_path, "import lxml.etree\n")
    # CVE-2024-0001 is mapped + used, but not in the active set → skipped.
    assert cve.scan(tmp_path, cve.load_patterns(tmp_path), set()) == []


def test_comment_lines_are_excluded(tmp_path: Path) -> None:
    """A pattern occurrence inside a comment is not counted as usage."""
    _write_map(tmp_path)
    _write_src(tmp_path, "# lxml.etree is mentioned here only in a comment\n")
    assert cve.scan(tmp_path, cve.load_patterns(tmp_path), {"CVE-2024-0001"}) == []


def test_pattern_file_is_not_scanned(tmp_path: Path) -> None:
    """The ``.toml`` map (holding the patterns verbatim) is never scanned.

    Only ``.py`` files are walked, so the pattern file can't self-match. With
    a clean ``.py`` present (no usage), the patterns inside the ``.toml`` must
    not surface as findings.
    """
    _write_map(tmp_path)  # contains 'lxml\.etree' verbatim
    _write_src(tmp_path, "import json\n")  # no lxml usage
    assert cve.scan(tmp_path, cve.load_patterns(tmp_path), {"CVE-2024-0001"}) == []


def test_load_patterns_absent_is_none(tmp_path: Path) -> None:
    """No pattern file → None (the signal to skip the check)."""
    assert cve.load_patterns(tmp_path) is None


def test_active_cve_ids_none_when_pip_audit_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pip-audit absent → None (skip cleanly), never a hard fail."""
    monkeypatch.setattr(cve.pip_audit_json, "run_json", lambda _root: None)
    assert cve.active_cve_ids(tmp_path) is None


def test_main_skips_without_pattern_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """main() exits 0 + logs a skip when there is no pattern map."""
    monkeypatch.setattr(cve, "repo_root", lambda: tmp_path)
    monkeypatch.setattr("sys.argv", ["verify-forge-cve-usage"])
    assert cve.main() == 0
    log = (tmp_path / "code_health" / "cve_usage.log").read_text()
    assert "skipped" in log


def test_main_returns_one_on_finding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """main() returns 1 (WARN signal) when vulnerable usage is found.

    SCENARIO: pattern map contains CVE-2024-0001 with lxml patterns; source
        file imports lxml.etree, which matches.
    MOCK SETUP: repo_root → tmp_path; active_cve_ids → {"CVE-2024-0001"} so
        pip-audit is never invoked; sys.argv drives the bare main() call.
    EXPECTED BEHAVIOR: exit 1; cve_usage.log names the CVE and the matched
        file location.
    """
    _write_map(tmp_path)
    _write_src(tmp_path, "import lxml.etree\n")
    monkeypatch.setattr(cve, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(
        cve, "active_cve_ids", lambda _root, _audit_json: {"CVE-2024-0001"}
    )
    monkeypatch.setattr("sys.argv", ["verify-forge-cve-usage"])
    assert cve.main() == 1
    log = (tmp_path / "code_health" / "cve_usage.log").read_text()
    assert "CVE-2024-0001" in log
    assert "src/app.py:1" in log


def test_main_returns_zero_when_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """main() returns 0 when the map exists but no usage matches a live CVE."""
    _write_map(tmp_path)
    _write_src(tmp_path, "import json\n")
    monkeypatch.setattr(cve, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(
        cve, "active_cve_ids", lambda _root, _audit_json: {"CVE-2024-0001"}
    )
    monkeypatch.setattr("sys.argv", ["verify-forge-cve-usage"])
    assert cve.main() == 0


# ---------------------------------------------------------------------------
# active_cve_ids — sidecar / audit_json path
# ---------------------------------------------------------------------------


def test_active_cve_ids_reads_sidecar_without_running_pip_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """active_cve_ids reads the sidecar JSON and never invokes pip-audit.

    SCENARIO: code_health/pip_audit.json present; audit_json kwarg supplied.
    MOCK SETUP: pip_audit_json.run_json raises AssertionError if called, so
        any fallback to the live scanner would cause immediate failure.
    EXPECTED BEHAVIOR: IDs come from the sidecar only; no exception raised.
    """
    data: dict = {
        "dependencies": [
            {
                "name": "pkg0",
                "version": "1.0",
                "vulns": [
                    {
                        "id": "PYSEC-2024-0",
                        "aliases": ["CVE-2024-0"],
                        "fix_versions": ["1.1"],
                        "description": "desc",
                    }
                ],
            }
        ]
    }
    sidecar = tmp_path / "code_health" / "pip_audit.json"
    sidecar.parent.mkdir(parents=True)
    sidecar.write_text(json.dumps(data), encoding="utf-8")

    def _no_call(_root: Path) -> None:
        msg = "run_json must not be called when sidecar is provided"
        raise AssertionError(msg)

    monkeypatch.setattr(cve.pip_audit_json, "run_json", _no_call)
    result = cve.active_cve_ids(tmp_path, audit_json=Path("code_health/pip_audit.json"))
    assert result == cve.pip_audit_json.ids_from_data(data)


def test_active_cve_ids_missing_sidecar_returns_none(
    tmp_path: Path,
) -> None:
    """active_cve_ids returns None when audit_json points to a non-existent file."""
    result = cve.active_cve_ids(
        tmp_path, audit_json=Path("code_health/nonexistent.json")
    )
    assert result is None


def test_active_cve_ids_unparseable_sidecar_returns_none(
    tmp_path: Path,
) -> None:
    """active_cve_ids returns None when the sidecar file contains invalid JSON."""
    sidecar = tmp_path / "code_health" / "pip_audit.json"
    sidecar.parent.mkdir(parents=True)
    sidecar.write_text("not json", encoding="utf-8")
    result = cve.active_cve_ids(tmp_path, audit_json=Path("code_health/pip_audit.json"))
    assert result is None


def test_active_cve_ids_out_of_repo_audit_json_returns_none(
    tmp_path: Path,
) -> None:
    """active_cve_ids skips an audit_json path that escapes the repo root.

    SCENARIO: a ``..`` relative path resolves outside *root*. The read is
        confined to the repo so the public --audit-json flag cannot become an
        arbitrary-file-read oracle; an out-of-repo path skips cleanly (None).
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "secret.json"
    outside.write_text(json.dumps({"dependencies": []}), encoding="utf-8")
    result = cve.active_cve_ids(repo, audit_json=Path("../secret.json"))
    assert result is None


def test_active_cve_ids_relative_path_resolves_against_root_not_cwd(
    tmp_path: Path,
) -> None:
    """active_cve_ids resolves a relative audit_json path against root, not CWD.

    SCENARIO: sidecar under tmp_path; audit_json is a relative Path; CWD may
        differ from root. The function must locate the file via root / audit_json,
        not Path.cwd() / audit_json — real file I/O confirms the resolution.
    """
    data: dict = {"dependencies": []}
    sidecar = tmp_path / "code_health" / "pip_audit.json"
    sidecar.parent.mkdir(parents=True)
    sidecar.write_text(json.dumps(data), encoding="utf-8")
    # Pass a relative path; only root-relative resolution finds the file.
    result = cve.active_cve_ids(tmp_path, audit_json=Path("code_health/pip_audit.json"))
    # An empty deps list → empty set, not None — proving the file was found.
    assert result == set()


def test_active_cve_ids_parse_fail_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """active_cve_ids returns None when pip-audit runs but produces non-JSON output.

    SCENARIO: pip-audit binary present but stdout is not parseable (operational
        error, network failure, or truncated output).
    MOCK SETUP: pip_audit_json.run_json → AuditRun(data=None, ...) — the
        non-parseable sentinel; no sidecar path provided.
    EXPECTED BEHAVIOR: None returned (clean skip), not an exception.
    """
    bad_run = cve.pip_audit_json.AuditRun(data=None, stderr="x", returncode=1)
    monkeypatch.setattr(cve.pip_audit_json, "run_json", lambda _root: bad_run)
    assert cve.active_cve_ids(tmp_path) is None


# ---------------------------------------------------------------------------
# load_patterns error paths
# ---------------------------------------------------------------------------


def test_load_patterns_parse_error_returns_none(tmp_path: Path) -> None:
    """load_patterns returns None when the pattern file contains invalid TOML."""
    (tmp_path / cve.PATTERN_FILE).write_text("NOT TOML [\n", encoding="utf-8")
    assert cve.load_patterns(tmp_path) is None


# ---------------------------------------------------------------------------
# inactive_cves
# ---------------------------------------------------------------------------


def test_inactive_cves_returns_only_dormant() -> None:
    """inactive_cves returns only CVEs absent from the active set."""
    patterns: dict = {
        "CVE-A": {"package": "pkgA"},
        "CVE-B": {"package": "pkgB"},
    }
    result = cve.inactive_cves(patterns, {"CVE-A"})
    assert result == [("CVE-B", "pkgB")]


def test_inactive_cves_empty_when_all_live() -> None:
    """inactive_cves returns an empty list when every mapped CVE is live."""
    patterns: dict = {"CVE-A": {"package": "pkgA"}}
    assert cve.inactive_cves(patterns, {"CVE-A"}) == []


def test_inactive_cves_sorted_output() -> None:
    """inactive_cves returns pairs sorted by CVE ID regardless of insertion order."""
    patterns: dict = {
        "CVE-Z": {"package": "pkgZ"},
        "CVE-A": {"package": "pkgA"},
    }
    result = cve.inactive_cves(patterns, set())
    assert result == [("CVE-A", "pkgA"), ("CVE-Z", "pkgZ")]


def test_inactive_cves_missing_package_key_uses_question_mark() -> None:
    """An entry without a 'package' key uses '?' as the package placeholder."""
    patterns: dict = {"CVE-A": {}}
    result = cve.inactive_cves(patterns, set())
    assert result == [("CVE-A", "?")]


# ---------------------------------------------------------------------------
# --list-inactive via main()
# ---------------------------------------------------------------------------


def test_list_inactive_exits_zero_without_pattern_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--list-inactive exits 0 when no pattern map exists; no log file written."""
    monkeypatch.setattr(cve, "repo_root", lambda: tmp_path)
    monkeypatch.setattr("sys.argv", ["verify-forge-cve-usage", "--list-inactive"])
    rc = cve.main()
    assert rc == 0
    assert not (tmp_path / "code_health" / "cve_usage.log").exists()


def test_list_inactive_exits_zero_when_active_ids_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--list-inactive exits 0 when pip-audit is unavailable (active_cve_ids → None).

    SCENARIO: pattern file present; pip-audit unavailable.
    MOCK SETUP: repo_root → tmp_path; active_cve_ids → None; --list-inactive flag.
    EXPECTED BEHAVIOR: exit 0 (read-only, always informational).
    """
    (tmp_path / cve.PATTERN_FILE).write_text(
        '["CVE-A"]\npackage = "pkgA"\n', encoding="utf-8"
    )
    monkeypatch.setattr(cve, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(cve, "active_cve_ids", lambda _root, _audit_json: None)
    monkeypatch.setattr("sys.argv", ["verify-forge-cve-usage", "--list-inactive"])
    assert cve.main() == 0
    assert not (tmp_path / "code_health" / "cve_usage.log").exists()


def test_list_inactive_reports_dormant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """--list-inactive logs dormant CVEs (mapped but not in pip-audit's live set).

    SCENARIO: pattern map has CVE-A (dormant) and CVE-B (live in active set).
        --list-inactive must report CVE-A as a prune candidate.
    MOCK SETUP: repo_root → tmp_path; active_cve_ids → {"CVE-B"};
        --list-inactive flag.
    EXPECTED BEHAVIOR: exit 0, CVE-A in log output, no cve_usage.log written.
    """
    (tmp_path / cve.PATTERN_FILE).write_text(
        '["CVE-A"]\npackage = "pkgA"\n["CVE-B"]\npackage = "pkgB"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(cve, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(cve, "active_cve_ids", lambda _root, _audit_json: {"CVE-B"})
    monkeypatch.setattr("sys.argv", ["verify-forge-cve-usage", "--list-inactive"])
    with caplog.at_level(logging.INFO):
        rc = cve.main()
    assert rc == 0
    assert "CVE-A" in caplog.text
    assert not (tmp_path / "code_health" / "cve_usage.log").exists()


def test_list_inactive_all_live_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """--list-inactive logs a clean message when all mapped CVEs are live.

    SCENARIO: every CVE in the pattern map is in pip-audit's active set.
    MOCK SETUP: repo_root → tmp_path; active_cve_ids → {"CVE-A"};
        --list-inactive flag.
    EXPECTED BEHAVIOR: exit 0, "All mapped CVEs" in log output.
    """
    (tmp_path / cve.PATTERN_FILE).write_text(
        '["CVE-A"]\npackage = "pkgA"\n', encoding="utf-8"
    )
    monkeypatch.setattr(cve, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(cve, "active_cve_ids", lambda _root, _audit_json: {"CVE-A"})
    monkeypatch.setattr("sys.argv", ["verify-forge-cve-usage", "--list-inactive"])
    with caplog.at_level(logging.INFO):
        rc = cve.main()
    assert rc == 0
    assert "All mapped CVEs" in caplog.text
    assert not (tmp_path / "code_health" / "cve_usage.log").exists()
