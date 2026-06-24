"""Tests for forge.pip_audit_json — shared pip-audit JSON invocation module.

# MOCKING STRATEGY: subprocess.run is never actually called. FakeProc (from
# tests/conftest.py) is the Null Object with stdout/stderr/returncode attributes
# that subprocess.run is monkeypatched to return. shutil.which is monkeypatched
# per-test to simulate pip-audit presence or absence. Pure functions
# (ids_from_data, has_vulns, render_report) exercise real logic with no mocks.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from forge import pip_audit_json, precommit
from tests.conftest import FakeProc


if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _data_with_n_vulns(n: int) -> dict:
    """Build a pip-audit data dict with n vulnerabilities, each with 3 aliases.

    Each dependency carries exactly one vulnerability: a primary PYSEC ID plus
    two CVE aliases and one GHSA alias. Both CVE and GHSA formats are chosen
    deliberately — CVE is invisible to ``_ADVISORY_ID_RE`` (only PYSEC/GHSA
    matched) while GHSA is matched. Including a GHSA alias ensures that the
    ``test_render_report_count_matches_advisory_regex`` test genuinely catches
    alias-format inflation: if render_report ever leaked GHSA aliases into
    output, the advisory-count would exceed n.

    Args:
        n: Number of vulnerable dependencies (one vulnerability per dependency).

    Returns:
        A dict matching pip-audit JSON format with n vulnerable dependencies.
    """
    return {
        "dependencies": [
            {
                "name": f"pkg{i}",
                "version": "1.0",
                "vulns": [
                    {
                        "id": f"PYSEC-2024-{i}",
                        "aliases": [
                            f"CVE-2024-{i}A",
                            f"CVE-2024-{i}B",
                            f"GHSA-{i:04d}-{i:04d}-{i:04d}",
                        ],
                        "fix_versions": ["1.1"],
                        "description": "desc",
                    }
                ],
            }
            for i in range(n)
        ]
    }


# ---------------------------------------------------------------------------
# ids_from_data
# ---------------------------------------------------------------------------


def test_ids_from_data_id_and_all_aliases_are_collected() -> None:
    """One vuln with a primary id and three aliases yields four distinct ID strings."""
    data = _data_with_n_vulns(1)
    result = pip_audit_json.ids_from_data(data)
    assert result == {
        "PYSEC-2024-0",
        "CVE-2024-0A",
        "CVE-2024-0B",
        "GHSA-0000-0000-0000",
    }


def test_ids_from_data_primary_id_missing_or_non_string_aliases_still_collected() -> (
    None
):
    """Missing or non-string primary id is skipped; aliases are still collected."""
    missing_id: dict = {
        "dependencies": [
            {"name": "pkg", "version": "1.0", "vulns": [{"aliases": ["CVE-2024-0"]}]}
        ]
    }
    assert pip_audit_json.ids_from_data(missing_id) == {"CVE-2024-0"}

    non_string_id: dict = {
        "dependencies": [
            {
                "name": "pkg",
                "version": "1.0",
                "vulns": [{"id": 42, "aliases": ["CVE-2024-1"]}],
            }
        ]
    }
    assert pip_audit_json.ids_from_data(non_string_id) == {"CVE-2024-1"}


def test_ids_from_data_aliases_non_list_skipped_without_raising() -> None:
    """Non-list aliases value is skipped; primary id is still collected."""
    data: dict = {
        "dependencies": [
            {
                "name": "pkg",
                "version": "1.0",
                "vulns": [{"id": "PYSEC-0", "aliases": "not-a-list"}],
            }
        ]
    }
    assert pip_audit_json.ids_from_data(data) == {"PYSEC-0"}


def test_ids_from_data_non_string_alias_elements_filtered() -> None:
    """Non-string elements in an aliases list are filtered without raising."""
    data: dict = {
        "dependencies": [
            {
                "name": "pkg",
                "version": "1.0",
                "vulns": [{"id": "PYSEC-0", "aliases": [42, "CVE-2024-0", None]}],
            }
        ]
    }
    assert pip_audit_json.ids_from_data(data) == {"PYSEC-0", "CVE-2024-0"}


def test_ids_from_data_dep_not_dict_filtered() -> None:
    """Non-dict entries in the dependencies list are silently filtered."""
    data: dict = {
        "dependencies": [
            "not-a-dict",
            {
                "name": "pkg",
                "version": "1.0",
                "vulns": [{"id": "PYSEC-0", "aliases": []}],
            },
        ]
    }
    assert pip_audit_json.ids_from_data(data) == {"PYSEC-0"}


def test_ids_from_data_vuln_not_dict_filtered() -> None:
    """Non-dict entries in a dependency's vulns list are silently filtered."""
    data: dict = {
        "dependencies": [
            {
                "name": "pkg",
                "version": "1.0",
                "vulns": ["not-a-dict", {"id": "PYSEC-0", "aliases": []}],
            }
        ]
    }
    assert pip_audit_json.ids_from_data(data) == {"PYSEC-0"}


def test_ids_from_data_dependencies_missing_gives_empty() -> None:
    """Parsed data with no 'dependencies' key yields an empty set."""
    assert pip_audit_json.ids_from_data({}) == set()


def test_ids_from_data_dependencies_not_list_gives_empty() -> None:
    """A non-list 'dependencies' value yields an empty set without raising."""
    assert pip_audit_json.ids_from_data({"dependencies": "not-a-list"}) == set()


def test_ids_from_data_empty_deps_gives_empty() -> None:
    """An empty dependencies list yields an empty set."""
    assert pip_audit_json.ids_from_data({"dependencies": []}) == set()


# ---------------------------------------------------------------------------
# has_vulns
# ---------------------------------------------------------------------------


def test_has_vulns_one_vuln_returns_true() -> None:
    """A dependency with at least one vuln entry reports True."""
    assert pip_audit_json.has_vulns(_data_with_n_vulns(1)) is True


def test_has_vulns_empty_vulns_list_returns_false() -> None:
    """A dependency with an empty vulns list reports False."""
    data: dict = {"dependencies": [{"name": "pkg", "version": "1.0", "vulns": []}]}
    assert pip_audit_json.has_vulns(data) is False


def test_has_vulns_no_deps_returns_false() -> None:
    """No dependencies at all reports False."""
    assert pip_audit_json.has_vulns({"dependencies": []}) is False


def test_has_vulns_dep_without_vulns_key_returns_false() -> None:
    """A dependency with no 'vulns' key defaults to an empty list, so False."""
    data: dict = {"dependencies": [{"name": "pkg", "version": "1.0"}]}
    assert pip_audit_json.has_vulns(data) is False


# ---------------------------------------------------------------------------
# render_report
# ---------------------------------------------------------------------------


def test_render_report_two_vulns_has_header_with_count() -> None:
    """Two vulnerabilities produce a header announcing exactly two advisories."""
    report = pip_audit_json.render_report(_data_with_n_vulns(2))
    assert report.startswith("2 dependency vulnerability advisory(ies):")


def test_render_report_primary_id_in_output_aliases_not() -> None:
    """render_report includes the primary PYSEC id but omits all aliases from each line.

    CVE and GHSA aliases are both excluded so _count_pip_audit_advisories counts
    one advisory per finding line, not one per id — preserving the
    one-advisory-per-line invariant that the loudness threshold relies on.
    """
    data = _data_with_n_vulns(1)
    report = pip_audit_json.render_report(data)
    assert "PYSEC-2024-0" in report
    assert "CVE-2024-0A" not in report
    assert "CVE-2024-0B" not in report
    assert "GHSA-0000-0000-0000" not in report


def test_render_report_fix_versions_comma_joined() -> None:
    """Multiple fix versions are comma-joined on the output line."""
    data: dict = {
        "dependencies": [
            {
                "name": "pkg",
                "version": "1.0",
                "vulns": [
                    {"id": "PYSEC-0", "fix_versions": ["1.1", "2.0"], "aliases": []}
                ],
            }
        ]
    }
    report = pip_audit_json.render_report(data)
    assert "1.1, 2.0" in report


def test_render_report_absent_or_empty_fix_shows_none_placeholder() -> None:
    """An empty or absent fix_versions list renders as '(fix: none)'."""
    data: dict = {
        "dependencies": [
            {
                "name": "pkg",
                "version": "1.0",
                "vulns": [{"id": "PYSEC-0", "fix_versions": [], "aliases": []}],
            }
        ]
    }
    report = pip_audit_json.render_report(data)
    assert "(fix: none)" in report


def test_render_report_multiline_description_only_first_line_shown() -> None:
    """A multi-line description is truncated to its first line in the output."""
    data: dict = {
        "dependencies": [
            {
                "name": "pkg",
                "version": "1.0",
                "vulns": [
                    {
                        "id": "PYSEC-0",
                        "fix_versions": [],
                        "aliases": [],
                        "description": "first line\nsecond line",
                    }
                ],
            }
        ]
    }
    report = pip_audit_json.render_report(data)
    assert "first line" in report
    assert "second line" not in report


def test_render_report_absent_description_renders_line_without_desc_segment() -> None:
    """A vuln with no description key renders its line without the '— ...' suffix."""
    data: dict = {
        "dependencies": [
            {
                "name": "pkg",
                "version": "1.0",
                "vulns": [{"id": "PYSEC-0", "fix_versions": [], "aliases": []}],
            }
        ]
    }
    report = pip_audit_json.render_report(data)
    assert "PYSEC-0" in report
    assert " — " not in report


def test_render_report_no_vulns_clean_one_liner() -> None:
    """No vulnerabilities yields the canonical clean single-line message."""
    report = pip_audit_json.render_report({"dependencies": []})
    assert report == "No known vulnerabilities found in non-editable dependencies."


def test_render_report_count_matches_advisory_regex() -> None:
    """_count_pip_audit_advisories on render_report output counts primary IDs only.

    Each finding contributes exactly one PYSEC primary id to the rendered text;
    CVE and GHSA aliases are both intentionally omitted from each line. Because
    the _ADVISORY_ID_RE regex matches PYSEC *and* GHSA formats, any GHSA alias
    that leaked into render output would inflate the count — this test catches
    that regression. The count must equal the number of findings, not the number
    of advisory IDs per finding.
    """
    # 3 vulns, each with PYSEC id + 2 CVE aliases + 1 GHSA alias.
    # If GHSA aliases appeared in the report, count would be 3+3=6 instead of 3.
    data = _data_with_n_vulns(3)
    report = pip_audit_json.render_report(data)
    assert precommit._count_pip_audit_advisories(report) == 3


# ---------------------------------------------------------------------------
# run_json
# ---------------------------------------------------------------------------


def test_run_json_binary_missing_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """run_json returns None when pip-audit is absent from PATH.

    SCENARIO: shutil.which reports pip-audit missing.
    MOCK SETUP: pip_audit_json.shutil.which → None; subprocess.run not called.
    EXPECTED BEHAVIOR: None returned — the missing-binary sentinel.
    """
    monkeypatch.setattr(pip_audit_json.shutil, "which", lambda _name: None)
    assert pip_audit_json.run_json(tmp_path) is None


def test_run_json_valid_json_stdout_returns_audit_run_with_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """run_json parses valid JSON stdout into AuditRun.data.

    SCENARIO: pip-audit present; stdout is parseable pip-audit JSON with 1 finding.
    MOCK SETUP: shutil.which → present; subprocess.run → FakeProc with
        json-encoded data as stdout and returncode 1 (findings present).
    EXPECTED BEHAVIOR: AuditRun.data equals the parsed dict.
    """
    data = _data_with_n_vulns(1)
    fake_proc = FakeProc(stdout=json.dumps(data), returncode=1)
    monkeypatch.setattr(pip_audit_json.shutil, "which", lambda _name: "/fake/pip-audit")
    monkeypatch.setattr(pip_audit_json.subprocess, "run", lambda *_a, **_kw: fake_proc)
    result = pip_audit_json.run_json(tmp_path)
    assert result is not None
    assert result.data == data


def test_run_json_non_json_stdout_returns_data_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-JSON stdout produces an AuditRun with data=None and the real returncode.

    SCENARIO: pip-audit present but stdout is not parseable JSON (operational error).
    MOCK SETUP: shutil.which → present; subprocess.run → FakeProc with
        non-JSON stdout and returncode 1.
    EXPECTED BEHAVIOR: AuditRun.data is None; AuditRun.returncode == 1.
    """
    fake_proc = FakeProc(stdout="not valid json", stderr="error msg", returncode=1)
    monkeypatch.setattr(pip_audit_json.shutil, "which", lambda _name: "/fake/pip-audit")
    monkeypatch.setattr(pip_audit_json.subprocess, "run", lambda *_a, **_kw: fake_proc)
    result = pip_audit_json.run_json(tmp_path)
    assert result is not None
    assert result.data is None
    assert result.returncode == 1


def test_run_json_parseable_empty_dict_not_treated_as_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty-dict stdout ('{}') is parsed as data={}, not treated as a parse error.

    SCENARIO: pip-audit returns '{}' — parseable but semantically empty.
    MOCK SETUP: shutil.which → present; subprocess.run → FakeProc with '{}' stdout.
    EXPECTED BEHAVIOR: AuditRun.data == {} (not None).
    """
    fake_proc = FakeProc(stdout="{}", returncode=0)
    monkeypatch.setattr(pip_audit_json.shutil, "which", lambda _name: "/fake/pip-audit")
    monkeypatch.setattr(pip_audit_json.subprocess, "run", lambda *_a, **_kw: fake_proc)
    result = pip_audit_json.run_json(tmp_path)
    assert result is not None
    assert result.data == {}


def test_run_json_clean_run_returns_passing_audit_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """run_json returns AuditRun with the parsed data and returncode 0 on a clean scan.

    SCENARIO: pip-audit present; scans one dependency with no vulnerabilities.
    MOCK SETUP: shutil.which → present; subprocess.run → FakeProc with a
        dependency carrying an empty vulns list and returncode 0 (clean).
    EXPECTED BEHAVIOR: AuditRun.data equals the parsed dict; AuditRun.returncode == 0.
    """
    clean_data: dict = {
        "dependencies": [{"name": "safe-pkg", "version": "2.0", "vulns": []}]
    }
    fake_proc = FakeProc(stdout=json.dumps(clean_data), returncode=0)
    monkeypatch.setattr(pip_audit_json.shutil, "which", lambda _name: "/fake/pip-audit")
    monkeypatch.setattr(pip_audit_json.subprocess, "run", lambda *_a, **_kw: fake_proc)
    result = pip_audit_json.run_json(tmp_path)
    assert result is not None
    assert result.data == clean_data
    assert result.returncode == 0
