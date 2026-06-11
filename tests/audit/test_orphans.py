"""Tests for ``forge.audit.orphans`` — mocked since vulture is optional."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest

from forge.audit import common, orphans
from forge.audit.common import Scope, Severity
from forge.audit.orphans import (
    OrphansConfig,
    _build_findings,
    _severity,
    run,
)


if TYPE_CHECKING:
    from pathlib import Path


@dataclass
class FakeVultureItem:
    """Stand-in for a ``vulture.Item`` record.

    Attributes:
        filename: Path of the file containing the unused symbol.
        first_lineno: 1-based line number.
        typ: Vulture symbol type (``"function"``, ``"variable"``, …).
        name: Symbol name.
        confidence: Vulture confidence percentage.
    """

    filename: str
    first_lineno: int
    typ: str
    name: str
    confidence: int


class FakeVulture:
    """Mocked vulture module surface used in tests.

    Attributes:
        items: Items to return from ``get_unused_code``.
        scavenged: Paths handed to ``scavenge`` (for assertion).
        verbose: Whether the caller passed verbose=False.
    """

    def __init__(self, items: list[FakeVultureItem]) -> None:
        """Store the items the fake will hand back.

        Args:
            items: Pre-baked unused-symbol records.
        """
        self.items = items
        self.scavenged: list[str] = []
        self.verbose: bool | None = None

    def Vulture(self, *, verbose: bool = True) -> FakeVulture:  # noqa: N802
        """Return ``self`` so the run() code uses our fake instance.

        Args:
            verbose: Captured for assertion.

        Returns:
            This instance.
        """
        self.verbose = verbose
        return self

    def scavenge(self, paths: list[str]) -> None:
        """Record the scavenge call for assertion.

        Args:
            paths: Paths handed by ``run()``.
        """
        self.scavenged = list(paths)

    def get_unused_code(self, *, min_confidence: int = 0) -> list[FakeVultureItem]:
        """Return the pre-baked items above ``min_confidence``.

        Args:
            min_confidence: Vulture confidence floor.

        Returns:
            Subset of the items meeting the confidence cutoff.
        """
        return [i for i in self.items if i.confidence >= min_confidence]


@pytest.fixture
def fake_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create a src-layout repo and point common.repo_root at it.

    Returns:
        The repo root path.
    """
    (tmp_path / "src").mkdir()
    monkeypatch.setattr(common, "repo_root", lambda: tmp_path)
    return tmp_path


def test_severity_medium_at_or_above_floor() -> None:
    """Confidence ≥ 95 maps to MEDIUM."""
    assert _severity(95) is Severity.MEDIUM
    assert _severity(99) is Severity.MEDIUM


def test_severity_low_below_floor() -> None:
    """Confidence below 95 maps to LOW."""
    assert _severity(80) is Severity.LOW
    assert _severity(94) is Severity.LOW


def test_build_findings_renders_each_item(fake_repo: Path) -> None:
    """Each vulture item produces a structured ``Finding``."""
    items: list[object] = [
        FakeVultureItem(
            filename=str(fake_repo / "src" / "mod.py"),
            first_lineno=10,
            typ="function",
            name="_unused_helper",
            confidence=96,
        ),
        FakeVultureItem(
            filename=str(fake_repo / "src" / "other.py"),
            first_lineno=3,
            typ="variable",
            name="_leftover",
            confidence=82,
        ),
    ]
    (fake_repo / "src" / "mod.py").write_text("", encoding="utf-8")
    (fake_repo / "src" / "other.py").write_text("", encoding="utf-8")
    findings = _build_findings(items)
    assert len(findings) == 2
    assert findings[0].severity is Severity.MEDIUM
    assert "_unused_helper" in findings[0].message
    assert findings[1].severity is Severity.LOW
    assert "_leftover" in findings[1].message


def test_run_returns_zero_when_no_findings(
    fake_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A clean codebase yields exit 0 and writes a zero-finding log."""
    fake = FakeVulture(items=[])
    monkeypatch.setattr(orphans, "_load_vulture", lambda: fake)
    code = run(Scope.FULL, [fake_repo / "src"], OrphansConfig())
    log_path = fake_repo / "code_health" / "audit_orphans.log"
    log_text = log_path.read_text(encoding="utf-8")
    assert "# findings: 0" in log_text
    assert code == 0


def test_run_returns_one_when_medium_findings(
    fake_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A MEDIUM finding flips exit to 1."""
    (fake_repo / "src" / "mod.py").write_text("", encoding="utf-8")
    fake = FakeVulture(
        items=[
            FakeVultureItem(
                filename=str(fake_repo / "src" / "mod.py"),
                first_lineno=1,
                typ="function",
                name="dead",
                confidence=97,
            ),
        ],
    )
    monkeypatch.setattr(orphans, "_load_vulture", lambda: fake)
    code = run(Scope.FULL, [fake_repo / "src"], OrphansConfig())
    log_path = fake_repo / "code_health" / "audit_orphans.log"
    log_text = log_path.read_text(encoding="utf-8")
    assert "[MEDIUM]" in log_text
    assert code == 1


def test_run_low_only_returns_zero(
    fake_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LOW-only findings should not block: exit 0."""
    (fake_repo / "src" / "mod.py").write_text("", encoding="utf-8")
    fake = FakeVulture(
        items=[
            FakeVultureItem(
                filename=str(fake_repo / "src" / "mod.py"),
                first_lineno=1,
                typ="variable",
                name="x",
                confidence=82,
            ),
        ],
    )
    monkeypatch.setattr(orphans, "_load_vulture", lambda: fake)
    code = run(Scope.FULL, [fake_repo / "src"], OrphansConfig())
    log_path = fake_repo / "code_health" / "audit_orphans.log"
    log_text = log_path.read_text(encoding="utf-8")
    assert "[LOW]" in log_text
    assert code == 0


def test_run_passes_min_confidence_to_vulture(
    fake_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--min-confidence`` is forwarded to ``get_unused_code``."""
    fake = FakeVulture(
        items=[
            FakeVultureItem(
                filename=str(fake_repo / "src" / "x.py"),
                first_lineno=1,
                typ="function",
                name="low",
                confidence=85,
            ),
        ],
    )
    monkeypatch.setattr(orphans, "_load_vulture", lambda: fake)
    code = run(
        Scope.FULL,
        [fake_repo / "src"],
        OrphansConfig(min_confidence=90),
    )
    log_path = fake_repo / "code_health" / "audit_orphans.log"
    log_text = log_path.read_text(encoding="utf-8")
    assert "# findings: 0" in log_text
    assert code == 0
