"""Shared helpers for forge's generated-doc CLIs.

The ``forge-gen-*`` generators (``forge-gen-api-digest``,
``forge-gen-cli-reference``) each render a markdown doc and support a
``--check`` mode that verifies the committed file is in sync with what
the generator would produce. The drift-check logic is identical across
generators, so it lives here once.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from pathlib import Path


logger = logging.getLogger(__name__)


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
