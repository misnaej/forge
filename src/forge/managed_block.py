"""Shared engine for marker-delimited managed blocks in shell hooks.

Forge keeps several canonical Python tuples mirrored into shell hooks
(`forge-gen-commit-types`, `forge-gen-attribution-patterns`). Each hook
carries a ``# <MARKER>_BEGIN`` / ``# <MARKER>_END`` pair with exactly one
``VAR='...'`` assignment between them (optionally preceded by comment
continuation lines). This module owns the block grammar and the
check-or-write flow once, so generator CLIs stay thin and the grammar
cannot drift between them (FOUNDATION §12) — the block content between
the markers is comment lines only, standardized across every generator
that uses this engine.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from pathlib import Path


logger = logging.getLogger(__name__)


@dataclass
class BlockSpec:
    """Specification for a managed block in a shell hook.

    Attributes:
        marker: Marker stem, e.g. "FORGE_COMMIT_TYPES".
        var_name: Shell variable assigned inside the block.
        value: Canonical assignment body (single-quoted).
        label: Repo-relative path used in log messages.
    """

    marker: str
    var_name: str
    value: str
    label: str


def _block_re(marker: str, var_name: str) -> re.Pattern[str]:
    """Compile the managed-block pattern for *marker* / *var_name*.

    Args:
        marker: Marker stem, e.g. ``"FORGE_COMMIT_TYPES"`` — matched as
            ``# <marker>_BEGIN`` / ``# <marker>_END``.
        var_name: Shell variable assigned inside the block.

    Returns:
        Pattern with two groups: everything from the BEGIN marker
        through any comment continuation lines, and everything from
        after the assignment line through the END marker.
    """
    return re.compile(
        rf"(# {re.escape(marker)}_BEGIN[^\n]*\n(?:#[^\n]*\n)*)"
        rf"^{re.escape(var_name)}='[^']*'\s*\n"
        rf"(.*?# {re.escape(marker)}_END)",
        re.DOTALL | re.MULTILINE,
    )


def rewrite_block(content: str, *, marker: str, var_name: str, value: str) -> str:
    """Return *content* with the managed assignment set to *value*.

    Args:
        content: Current hook file text.
        marker: Marker stem (see :func:`_block_re`).
        var_name: Shell variable assigned inside the block.
        value: New single-quoted assignment body.

    Returns:
        The text with the block's assignment line replaced; everything
        outside the block (and the marker/comment lines) byte-preserved.

    Raises:
        ValueError: When *value* contains a single quote (it would break
            the single-quoted shell assignment), or when the markers are
            missing or malformed in *content*.
    """
    if "'" in value:
        msg = (
            f"managed-block value for {var_name} contains a single quote — "
            "it would break the single-quoted shell assignment."
        )
        raise ValueError(msg)
    expected = f"{var_name}='{value}'\n"
    new_content, n = _block_re(marker, var_name).subn(
        lambda m: f"{m.group(1)}{expected}{m.group(2)}",
        content,
        count=1,
    )
    if n == 0:
        msg = f"{marker}_BEGIN / END markers not found — cannot regenerate."
        raise ValueError(msg)
    return new_content


def check_or_write(
    path: Path,
    *,
    spec: BlockSpec,
    check: bool,
) -> int:
    """Run the shared check-or-write flow for one managed block.

    Args:
        path: Absolute path to the hook file.
        spec: Block specification (marker, var_name, value, label).
        check: Verify only (exit 1 on drift) instead of writing.

    Returns:
        ``0`` when the hook is written or already in sync; ``1`` when
        the hook is missing, the markers are absent, or ``check``
        detects drift.
    """
    if not path.is_file():
        logger.error("missing %s — nothing to regenerate.", path)
        return 1
    current = path.read_text()
    try:
        expected_content = rewrite_block(
            current,
            marker=spec.marker,
            var_name=spec.var_name,
            value=spec.value,
        )
    except ValueError:
        logger.exception("cannot regenerate %s", spec.label)
        return 1
    if check:
        if current == expected_content:
            logger.info("OK: %s alternation is in sync.", spec.label)
            return 0
        logger.error(
            "DRIFT: %s alternation does not match the canonical tuple. "
            "Run the generator to regenerate.",
            spec.label,
        )
        return 1
    if current == expected_content:
        logger.info("OK: %s already in sync — no write.", spec.label)
        return 0
    path.write_text(expected_content)
    logger.info("✓ regenerated %s", spec.label)
    return 0
