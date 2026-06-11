"""Tests for install_labels CLI helpers."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from forge import install_labels
from tests.conftest import make_fake_run


if TYPE_CHECKING:
    import pytest


def test_canonical_labels_schema_has_required_fields() -> None:
    """Every canonical label declares name, color, description."""
    for label in install_labels.CANONICAL_LABELS:
        assert {"name", "color", "description"} <= set(label.keys())
        assert label["name"]
        assert label["color"]
        assert label["description"]


def test_canonical_labels_include_all_four_tiers() -> None:
    """The 4 tier labels are present (tier-1-critical … tier-4-low)."""
    names = {label["name"] for label in install_labels.CANONICAL_LABELS}
    expected = {
        f"tier-{n}-{label}"
        for n, label in zip(
            ["1", "2", "3", "4"],
            ["critical", "high", "standard", "low"],
            strict=True,
        )
    }
    assert expected <= names


def test_canonical_labels_colors_are_six_hex() -> None:
    """All label colors are 6-character hex strings (no `#`)."""
    for label in install_labels.CANONICAL_LABELS:
        color = label["color"]
        assert len(color) == 6
        int(color, 16)  # raises if not hex


def test_existing_labels_parses_gh_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """_existing_labels returns the set of names from gh label list JSON output."""
    stdout = json.dumps([{"name": "bug"}, {"name": "tier-1-critical"}])
    monkeypatch.setattr(
        install_labels.subprocess,
        "run",
        make_fake_run(stdout=stdout, returncode=0),
    )
    names = install_labels._existing_labels(None)
    assert names == {"bug", "tier-1-critical"}


def test_existing_labels_returns_empty_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When gh returns non-zero, _existing_labels returns an empty set."""
    monkeypatch.setattr(
        install_labels.subprocess,
        "run",
        make_fake_run(returncode=1),
    )
    names = install_labels._existing_labels(None)
    assert names == set()


def test_create_label_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """_create_label returns True when gh succeeds."""
    monkeypatch.setattr(
        install_labels.subprocess,
        "run",
        make_fake_run(returncode=0),
    )
    ok = install_labels._create_label(
        {"name": "x", "color": "ffffff", "description": "d"},
        repo=None,
    )
    assert ok


def test_create_label_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """_create_label returns False when gh fails."""
    monkeypatch.setattr(
        install_labels.subprocess,
        "run",
        make_fake_run(returncode=1),
    )
    ok = install_labels._create_label(
        {"name": "x", "color": "ffffff", "description": "d"},
        repo=None,
    )
    assert not ok
