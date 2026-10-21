"""Tests for ``forge.verify_cve_usage`` — the usage-scoped CVE filter.

# MOCKING STRATEGY: pip-audit is never actually run — ``main()`` tests patch
# ``_load_audit_data`` to return a controlled report (``--list-inactive``
# tests patch ``active_cve_ids``), so the coverage + intersect + grep logic
# is exercised deterministically. The pattern map and source tree are real
# files under tmp_path.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from forge import verify_cve_usage as cve
from forge.git_utils import code_health_dir


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


def test_active_cve_ids_reads_sidecar_carrying_scanned_at(tmp_path: Path) -> None:
    """The ``scanned_at`` field the pip_audit step adds does not disturb this reader."""
    sidecar = tmp_path / "audit.json"
    sidecar.write_text(
        json.dumps(
            {
                "scanned_at": 1_700_000_000.0,
                "dependencies": [
                    {
                        "name": "lxml",
                        "vulns": [{"id": "PYSEC-1", "aliases": ["CVE-2024-0001"]}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    assert cve.active_cve_ids(tmp_path, sidecar) == {"PYSEC-1", "CVE-2024-0001"}


def test_main_skips_without_pattern_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """main() exits 0 + logs a skip when there is no pattern map."""
    monkeypatch.setattr(cve, "repo_root", lambda: tmp_path)
    monkeypatch.setattr("sys.argv", ["verify-forge-cve-usage"])
    assert cve.main() == 0
    log = (code_health_dir(tmp_path) / "cve_usage.log").read_text()
    assert cve.pip_audit_json.CVE_USAGE_SKIP_MARKER in log.splitlines()


def _report(*vulns: tuple[str, str, list[str]]) -> dict:
    """Build a pip-audit report with one dependency per ``(package, id, aliases)``.

    Args:
        *vulns: One ``(package, primary id, aliases)`` triple per advisory.

    Returns:
        The parsed-JSON report.
    """
    return {
        "dependencies": [
            {"name": pkg, "version": "1.0", "vulns": [{"id": vid, "aliases": aliases}]}
            for pkg, vid, aliases in vulns
        ]
    }


def _run_main_on(
    monkeypatch: pytest.MonkeyPatch, root: Path, report: dict | None
) -> tuple[int, str]:
    """Run ``main()`` against *root* with *report* as pip-audit's findings.

    Args:
        monkeypatch: Pytest's monkeypatch fixture.
        root: Repo root holding the map and the source tree.
        report: The audit report the loader returns (``None`` = unavailable).

    Returns:
        The exit code and the ``cve_usage.log`` text.
    """
    monkeypatch.setattr(cve, "repo_root", lambda: root)
    monkeypatch.setattr(cve, "_load_audit_data", lambda _root, _audit_json: report)
    monkeypatch.setattr("sys.argv", ["verify-forge-cve-usage"])
    rc = cve.main()
    return rc, (code_health_dir(root) / "cve_usage.log").read_text()


_LXML_ADVISORY = ("lxml", "PYSEC-2024-1", ["CVE-2024-0001"])
_UNMAPPED_ADVISORY = ("requests", "PYSEC-2024-2", ["CVE-2024-0002"])


def test_main_returns_one_on_finding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """main() returns 1 (WARN signal) when vulnerable usage is found.

    SCENARIO: pattern map contains CVE-2024-0001 with lxml patterns; source
        file imports lxml.etree, which matches.
    MOCK SETUP: repo_root → tmp_path; _load_audit_data → a report carrying
        that CVE as an alias, so pip-audit is never invoked.
    EXPECTED BEHAVIOR: exit 1; cve_usage.log names the CVE, the matched
        file location, and that 1 of 1 reported CVEs was checked.
    """
    _write_map(tmp_path)
    _write_src(tmp_path, "import lxml.etree\n")
    rc, log = _run_main_on(monkeypatch, tmp_path, _report(_LXML_ADVISORY))
    assert rc == 1
    assert "CVE-2024-0001" in log
    assert "src/app.py:1" in log
    assert "Checked 1 of 1 reported CVEs" in log


@pytest.mark.parametrize(
    ("advisories", "expected", "unchecked_ids"),
    [
        pytest.param(
            [_LXML_ADVISORY],
            (
                0,
                [
                    (
                        "Checked 1 of 1 reported CVEs against cve_usage_patterns.toml "
                        "— no vulnerable usage found."
                    )
                ],
            ),
            [],
            id="all-checked",
        ),
        pytest.param(
            [_LXML_ADVISORY, _UNMAPPED_ADVISORY],
            (
                1,
                [
                    (
                        "Checked 1 of 2 reported CVEs against cve_usage_patterns.toml "
                        "— no match among the ones checked."
                    )
                ],
            ),
            ["PYSEC-2024-2 (requests)"],
            id="partly-checked",
        ),
        pytest.param(
            [_UNMAPPED_ADVISORY, ("urllib3", "PYSEC-2024-3", [])],
            (
                1,
                [
                    (
                        "Checked 0 of 2 reported CVEs against cve_usage_patterns.toml "
                        "— no match among the ones checked."
                    )
                ],
            ),
            ["PYSEC-2024-2 (requests)", "PYSEC-2024-3 (urllib3)"],
            id="none-checked",
        ),
        pytest.param(
            [],
            (0, ["pip-audit reports no CVEs — nothing to check."]),
            [],
            id="no-advisories",
        ),
    ],
)
def test_main_reports_how_many_reported_cves_it_checked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    advisories: list[tuple[str, str, list[str]]],
    expected: tuple[int, list[str]],
    unchecked_ids: list[str],
) -> None:
    """The log counts what it checked; the all-clear appears only when all were.

    SCENARIO: live CVEs with and without a map entry; no source usage.
    MOCK SETUP: _load_audit_data → the report built from *advisories*.
    EXPECTED BEHAVIOR: exit 1 whenever a reported CVE is NOT checked; the
        unchecked ones are named; a real run never reads as skipped.

    Args:
        tmp_path: Temporary repo root.
        monkeypatch: Pytest's monkeypatch fixture.
        advisories: The ``(package, id, aliases)`` triples pip-audit reports.
        expected: The ``(exit code, lines the log must contain verbatim)`` pair.
        unchecked_ids: Advisory labels the NOT-checked line must name.
    """
    expected_rc, expected_lines = expected
    _write_map(tmp_path)
    _write_src(tmp_path, "import json\n")
    rc, log = _run_main_on(monkeypatch, tmp_path, _report(*advisories))
    assert rc == expected_rc
    for line in expected_lines:
        assert line in log.splitlines()
    if unchecked_ids:
        not_checked = next(ln for ln in log.splitlines() if "NOT checked" in ln)
        for label in unchecked_ids:
            assert label in not_checked
    else:
        assert "NOT checked" not in log
    if expected_rc == 1:
        assert "no vulnerable usage found" not in log
    assert "skipped" not in log
    assert cve.pip_audit_json.CVE_USAGE_SKIP_MARKER not in log


def test_main_marks_a_run_without_audit_data_as_not_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pip-audit unavailable → exit 0 with the skip marker as its own line."""
    _write_map(tmp_path)
    rc, log = _run_main_on(monkeypatch, tmp_path, None)
    assert rc == 0
    assert cve.pip_audit_json.CVE_USAGE_SKIP_MARKER in log.splitlines()


def _advisory(vid: str, *aliases: str) -> cve.pip_audit_json.Advisory:
    """Build an Advisory for the coverage matrix.

    Args:
        vid: Primary ID.
        *aliases: Alias IDs.

    Returns:
        The advisory, package ``pkg``.
    """
    return cve.pip_audit_json.Advisory(package="pkg", id=vid, aliases=aliases)


@pytest.mark.parametrize(
    ("entry_key", "patterns_value", "advisory", "checked"),
    [
        pytest.param("PYSEC-1", ["foo"], _advisory("PYSEC-1"), True, id="id-keyed"),
        pytest.param(
            "CVE-1", ["foo"], _advisory("PYSEC-1", "CVE-1"), True, id="alias-keyed"
        ),
        pytest.param("CVE-9", ["foo"], _advisory("PYSEC-1"), False, id="no-entry"),
        pytest.param(
            "PYSEC-1", "foo", _advisory("PYSEC-1"), False, id="patterns-not-a-list"
        ),
        pytest.param("PYSEC-1", [], _advisory("PYSEC-1"), False, id="empty-patterns"),
        pytest.param(
            "PYSEC-1", [42], _advisory("PYSEC-1"), False, id="non-string-pattern"
        ),
        pytest.param(
            "PYSEC-1", ["(unclosed"], _advisory("PYSEC-1"), False, id="bad-regex"
        ),
        pytest.param(
            "PYSEC-1",
            ["(unclosed", "foo"],
            _advisory("PYSEC-1"),
            True,
            id="one-good-regex-is-enough",
        ),
    ],
)
def test_coverage_checks_an_advisory_only_with_a_usable_pattern(
    *,
    entry_key: str,
    patterns_value: object,
    advisory: cve.pip_audit_json.Advisory,
    checked: bool,
) -> None:
    """An advisory is checked iff one of its IDs keys an entry with a usable pattern.

    Args:
        entry_key: The pattern-map key.
        patterns_value: The entry's ``patterns`` value.
        advisory: The reported advisory.
        checked: Whether coverage must count it as checked.
    """
    patterns: dict = {entry_key: {"package": "pkg", "patterns": patterns_value}}
    cov = cve.coverage(patterns, [advisory])
    assert (cov.checked == [advisory]) is checked
    assert (cov.unchecked == [advisory]) is not checked


def test_scan_skips_a_bad_regex_instead_of_crashing(tmp_path: Path) -> None:
    """A regex that does not compile is unusable, not a traceback; good ones run."""
    _write_map(
        tmp_path,
        "['CVE-1']\npackage = \"lxml\"\npatterns = ['(unclosed', 'lxml\\.etree']\n",
    )
    _write_src(tmp_path, "import lxml.etree\n")
    findings = cve.scan(tmp_path, cve.load_patterns(tmp_path), {"CVE-1"})
    assert [(f.cve, f.line) for f in findings] == [("CVE-1", 1)]


def test_main_with_only_a_bad_regex_reports_not_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An entry whose only pattern is malformed leaves its CVE NOT checked, exit 1."""
    _write_map(tmp_path, "['CVE-2024-0001']\npackage = \"lxml\"\npatterns = ['(']\n")
    _write_src(tmp_path, "import lxml.etree\n")
    rc, log = _run_main_on(monkeypatch, tmp_path, _report(_LXML_ADVISORY))
    assert rc == 1
    assert "Checked 0 of 1 reported CVEs" in log
    assert "PYSEC-2024-1 (lxml)" in log


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
    sidecar.parent.mkdir(parents=True, exist_ok=True)
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
    sidecar.parent.mkdir(parents=True, exist_ok=True)
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
    sidecar.parent.mkdir(parents=True, exist_ok=True)
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
    assert not (code_health_dir(tmp_path) / "cve_usage.log").exists()


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
    assert not (code_health_dir(tmp_path) / "cve_usage.log").exists()


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
    assert not (code_health_dir(tmp_path) / "cve_usage.log").exists()


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
    assert not (code_health_dir(tmp_path) / "cve_usage.log").exists()


_ONE_VULN_REPORT: dict = {
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


@pytest.fixture
def _no_live_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail loudly if a sidecar read falls back to running pip-audit."""

    def _no_call(_root: Path) -> None:
        msg = "run_json must not be called when a sidecar path is given"
        raise AssertionError(msg)

    monkeypatch.setattr(cve.pip_audit_json, "run_json", _no_call)


@pytest.mark.usefixtures("_no_live_scan")
def test_active_cve_ids_reads_sidecar_in_out_of_repo_override_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sidecar inside a relocated (out-of-repo) log directory is read."""
    repo = tmp_path / "repo"
    repo.mkdir()
    logs = tmp_path / "relocated-logs"
    logs.mkdir()
    monkeypatch.setenv("FORGE_CODE_HEALTH_DIR", str(logs))
    sidecar = logs / "pip_audit.json"
    sidecar.write_text(json.dumps(_ONE_VULN_REPORT), encoding="utf-8")

    result = cve.active_cve_ids(repo, audit_json=sidecar)

    assert result == cve.pip_audit_json.ids_from_data(_ONE_VULN_REPORT)
    assert result


@pytest.mark.usefixtures("_no_live_scan")
def test_active_cve_ids_skips_sidecar_elsewhere_outside_repo_and_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The override widens the allowed read to one directory, not to everywhere."""
    repo = tmp_path / "repo"
    repo.mkdir()
    logs = tmp_path / "relocated-logs"
    logs.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.setenv("FORGE_CODE_HEALTH_DIR", str(logs))
    stray = elsewhere / "pip_audit.json"
    stray.write_text(json.dumps(_ONE_VULN_REPORT), encoding="utf-8")

    assert cve.active_cve_ids(repo, audit_json=stray) is None
