"""Tests for the shared generated-doc drift-check helper."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pytest

from forge.gen_common import check_doc_drift, replace_region


if TYPE_CHECKING:
    from pathlib import Path


DOC_RELPATH = "docs/sample.md"
REGEN_CMD = "forge-gen-sample"
CONTENT = "# Sample\n\ngenerated body\n"


def test_check_doc_drift_returns_zero_when_in_sync(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An in-sync committed doc yields exit 0 and an info log."""
    doc_path = tmp_path / DOC_RELPATH
    doc_path.parent.mkdir(parents=True)
    doc_path.write_text(CONTENT)

    with caplog.at_level(logging.INFO):
        result = check_doc_drift(tmp_path, DOC_RELPATH, CONTENT, REGEN_CMD)

    assert result == 0
    assert any("in sync" in record.getMessage() for record in caplog.records)


def test_check_doc_drift_returns_one_on_drift(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A drifted committed doc yields exit 1 and an error log."""
    doc_path = tmp_path / DOC_RELPATH
    doc_path.parent.mkdir(parents=True)
    doc_path.write_text("# Sample\n\nstale body\n")

    with caplog.at_level(logging.ERROR):
        result = check_doc_drift(tmp_path, DOC_RELPATH, CONTENT, REGEN_CMD)

    assert result == 1
    messages = [record.getMessage() for record in caplog.records]
    assert any("out of sync" in message for message in messages)
    assert any(REGEN_CMD in message for message in messages)


def test_check_doc_drift_returns_one_when_doc_missing(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A missing committed doc yields exit 1 and an error log."""
    with caplog.at_level(logging.ERROR):
        result = check_doc_drift(tmp_path, DOC_RELPATH, CONTENT, REGEN_CMD)

    assert result == 1
    messages = [record.getMessage() for record in caplog.records]
    assert any("does not exist" in message for message in messages)
    assert any(REGEN_CMD in message for message in messages)


def test_replace_region_swaps_body() -> None:
    """Markers and the text outside them survive; only the body changes."""
    text = "head\n<!-- a -->\nold\n<!-- b -->\ntail\n"
    result = replace_region(text, "<!-- a -->", "<!-- b -->", "\nnew\n")
    assert result == "head\n<!-- a -->\nnew\n<!-- b -->\ntail\n"


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("x\n<!-- b -->\n", id="missing-start"),
        pytest.param("x\n<!-- a -->\n", id="missing-end"),
        pytest.param("<!-- b -->\n<!-- a -->\n", id="end-before-start"),
    ],
)
def test_replace_region_none_on_missing_or_misordered(text: str) -> None:
    """An absent or misordered marker pair yields None, never a splice.

    Args:
        text: Document text with a missing, partial or reversed marker pair.
    """
    assert replace_region(text, "<!-- a -->", "<!-- b -->", "\nnew\n") is None
