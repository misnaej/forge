"""Tests for ``forge.verify_cve_usage`` — the usage-scoped CVE filter.

# MOCKING STRATEGY: pip-audit is never actually run — ``active_cve_ids`` is
# monkeypatched to return a controlled live-CVE set so the intersect + grep
# logic is exercised deterministically. The pattern map and source tree are
# real files under tmp_path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from forge import verify_cve_usage as cve


if TYPE_CHECKING:
    from pathlib import Path

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
    monkeypatch.setattr(cve.shutil, "which", lambda _name: None)
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
    """main() returns 1 (WARN signal) when vulnerable usage is found."""
    _write_map(tmp_path)
    _write_src(tmp_path, "import lxml.etree\n")
    monkeypatch.setattr(cve, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(cve, "active_cve_ids", lambda _root: {"CVE-2024-0001"})
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
    monkeypatch.setattr(cve, "active_cve_ids", lambda _root: {"CVE-2024-0001"})
    monkeypatch.setattr("sys.argv", ["verify-forge-cve-usage"])
    assert cve.main() == 0
