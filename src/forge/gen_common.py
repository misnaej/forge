"""Shared helpers for forge's generated-doc CLIs.

The ``forge-gen-*`` generators (``forge-gen-api-digest``,
``forge-gen-cli-reference``) each render a markdown doc and support a
``--check`` mode that verifies the committed file is in sync with what
the generator would produce. The drift-check logic is identical across
generators, so it lives here once, as does the marker-region splice the
generators that manage a block inside a hand-written doc share.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from pathlib import Path


logger = logging.getLogger(__name__)

# The phrase forge-gen-cli-reference logs for each CLI whose --help could
# not be captured. Shared here so forge-precommit can recognise that
# failure in the generator's output without importing the generator (its
# import configures CLI logging).
HELP_UNAVAILABLE_MARKER = "--help unavailable"

# The doc forge-gen-config-doc keeps a key table in, and the markers
# bounding it. Here for the same reason as the marker above: the
# config_doc_check step reads them without importing the generator.
CONFIG_DOC = "forge-docs/configuration.md"
CONFIG_DOC_START = "<!-- forge:config:start -->"
CONFIG_DOC_END = "<!-- forge:config:end -->"


def replace_region(text: str, start: str, end: str, body: str) -> str | None:
    """Replace what lies between the *start* and *end* markers in *text*.

    The markers are kept; *body* is placed verbatim between them, so it
    carries its own leading and trailing newlines. Only the first
    occurrence of each marker counts.

    Args:
        text: Document text holding the managed region.
        start: Start marker string.
        end: End marker string.
        body: New region content.

    Returns:
        The updated text, or ``None`` when either marker is missing or
        *end* comes before *start*.
    """
    head = text.find(start)
    tail = text.find(end)
    if head == -1 or tail == -1 or tail < head + len(start):
        return None
    return text[: head + len(start)] + body + text[tail:]


def check_doc_drift(
    root: Path,
    doc_relpath: str,
    generated: str,
    regen_cmd: str,
) -> int:
    """Compare freshly generated content against a committed doc.

    Args:
        root: Repository root directory.
        doc_relpath: Path of the generated doc, relative to *root*.
        generated: Freshly rendered markdown content.
        regen_cmd: The command that regenerates the doc, named in error
            messages so the caller knows how to fix drift.

    Returns:
        Exit code: ``0`` when the committed doc matches *generated*,
        ``1`` when it is missing or has drifted.
    """
    doc_path = root / doc_relpath
    if not doc_path.exists():
        logger.error(
            "%s does not exist — run `%s` to create it.",
            doc_relpath,
            regen_cmd,
        )
        return 1
    if doc_path.read_text() != generated:
        logger.error(
            "%s is out of sync — run `%s` to regenerate.",
            doc_relpath,
            regen_cmd,
        )
        return 1
    logger.info("%s is in sync.", doc_relpath)
    return 0
