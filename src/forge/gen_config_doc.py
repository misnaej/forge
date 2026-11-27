"""Generate the key tables in ``forge-docs/configuration.md``.

Every ``[tool.forge.*]`` key and its default is declared once, as
:data:`forge.forge_config.CONFIG_KEYS`. The configuration reference
renders that list into several managed regions, so the defaults it shows
cannot drift from what ``forge-config`` reports:

.. code-block:: markdown

    <!-- forge:config:start -->
    | Key | Default |
    ...
    <!-- forge:config:end -->

That index region covers every key. A key may additionally name a doc
group (:data:`forge.forge_config.DOC_GROUPS`), which gives it a row in
that group's own richer region — prose, and the underlying setting it
maps to — under the marker pair
``<!-- forge:config:<group>:start/end -->``. Each region is rewritten
whole; text outside every region is byte-preserved.

Usage:

    # Regenerate every region
    forge-gen-config-doc

    # Verify every region is in sync (no write)
    forge-gen-config-doc --check

Exit Codes:
    0: The regions were written (default), or are already in sync
       (``--check``).
    1: The doc or any region's markers are missing, or ``--check``
       detected drift between the generated regions and the committed
       doc.
"""

from __future__ import annotations

import argparse
import logging
import sys

from forge.forge_config import CONFIG_KEYS, DOC_GROUPS, keys_by_section
from forge.gen_common import (
    CONFIG_DOC,
    CONFIG_DOC_END,
    CONFIG_DOC_START,
    check_doc_drift,
    config_region_markers,
    replace_region,
)
from forge.git_utils import configure_cli_logging, repo_root


configure_cli_logging()
logger = logging.getLogger(__name__)

REGEN_CMD = "forge-gen-config-doc"


def _toml_literal(value: object) -> str:
    """Return *value* spelled as it would be written in TOML.

    The page exists to be copied into ``pyproject.toml``, where Python's
    spelling is wrong: ``True`` is not a TOML boolean, and a repr's single
    quotes are not its string syntax. Python's container reprs collide with
    TOML too — a tuple is written as an array, a table uses ``key = value``.

    Args:
        value: A default to spell. Booleans are checked before integers,
            since ``bool`` is a subclass of ``int``.

    Returns:
        The TOML spelling, without surrounding backticks.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return f'"{value}"'
    if isinstance(value, tuple | list):
        return "[" + ", ".join(_toml_literal(item) for item in value) + "]"
    if isinstance(value, dict):
        inner = ", ".join(f"{k} = {_toml_literal(v)}" for k, v in value.items())
        return "{" + inner + "}"
    return str(value)


def _render_default(default: object) -> str:
    """Return *default* as a table cell.

    A string with whitespace describes the default rather than being one
    (``"unset (step skipped)"``), so it renders as plain text; quoting it
    would read as a value to type. An empty string is the unset default, and
    so is ``None`` — the sentinel a pass-through key uses for "omit this and
    leave the underlying tool's own default alone". An empty array or table
    renders literally, because there the empty collection *is* the default
    value. Everything else is spelled the way TOML spells it, since that is
    what a reader copies out of this page.

    Args:
        default: A :class:`~forge.forge_config.ConfigKey` default.

    Returns:
        The cell text, ``|``-escaped.
    """
    if default is None or default == "":
        cell = "unset"
    elif isinstance(default, str) and any(c.isspace() for c in default):
        cell = default
    else:
        cell = f"`{_toml_literal(default)}`"
    return cell.replace("|", r"\|")


def _escape_cell(text: str) -> str:
    """Return *text* safe to place in a markdown table cell.

    An unescaped ``|`` ends the cell and shifts every column after it, so
    prose carrying one would corrupt the table rather than fail loudly.

    Args:
        text: Registry-held prose for one cell.

    Returns:
        The text with any ``|`` escaped.
    """
    return text.replace("|", r"\|")


def render_table() -> str:
    """Render every config key and its default as a markdown table.

    Literal defaults use ``repr`` so they read the way ``forge-config``
    prints them.

    Returns:
        The region body, newline-delimited from both markers.
    """
    lines = ["", "| Key | Default |", "|---|---|"]
    for section, keys in keys_by_section():
        for key in keys:
            default = _render_default(key.default)
            lines.append(f"| `[{section}].{key.path[-1]}` | {default} |")
    return "\n".join(lines) + "\n"


def render_group_table(group: str) -> str:
    """Render one key group's reference table for its generated region.

    Richer than the index table: a group documents each key's prose and, for
    a group of pass-through settings, the underlying setting it maps to. The
    Mermaid column appears only where the group's keys declare one, so a
    group of forge's own settings is not given an empty column.

    Args:
        group: Region name (a :data:`~forge.forge_config.DOC_GROUPS` member).

    Returns:
        The region body, newline-delimited from both markers.

    Raises:
        ValueError: When no registered key names *group* — the region would
            render as a bare header and the markers would silently rot.
    """
    rows = [(key, key.doc) for key in CONFIG_KEYS if key.doc and key.doc.group == group]
    if not rows:
        msg = f"no registered key names doc group {group!r}"
        raise ValueError(msg)
    with_mermaid = any(doc.mermaid for _key, doc in rows)
    if with_mermaid:
        header = "| Key | Default | → Mermaid (scope) | What it does |"
        rule = "|---|---|---|---|"
    else:
        header = "| Key | Default | What it does |"
        rule = "|---|---|---|"
    lines = ["", header, rule]
    for key, doc in rows:
        cells = [f"`{key.path[-1]}`", _render_default(key.default)]
        if with_mermaid:
            cells.append(_escape_cell(doc.mermaid))
        cells.append(_escape_cell(doc.detail))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def main() -> int:
    """Entry point for ``forge-gen-config-doc``.

    Returns:
        ``0`` when the table is written or already in sync; ``1`` when
        the doc or its markers are missing, or ``--check`` detects drift.
    """
    parser = argparse.ArgumentParser(
        prog=REGEN_CMD,
        description=(
            f"Regenerate the key tables in {CONFIG_DOC} from "
            "forge.forge_config.CONFIG_KEYS."
        ),
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify every region is in sync without writing. Exit 1 on drift.",
    )
    args = parser.parse_args()

    root = repo_root()
    path = root / CONFIG_DOC
    if not path.is_file():
        logger.error("missing %s — nothing to regenerate.", CONFIG_DOC)
        return 1
    current = path.read_text()
    regions = [(CONFIG_DOC_START, CONFIG_DOC_END, render_table())]
    regions += [
        (*config_region_markers(group), render_group_table(group))
        for group in DOC_GROUPS
    ]
    expected = current
    for start, end, body in regions:
        replaced = replace_region(expected, start, end, body)
        if replaced is None:
            logger.error(
                "%s lacks the %s / %s markers — add them where the table belongs.",
                CONFIG_DOC,
                start,
                end,
            )
            return 1
        expected = replaced
    if args.check:
        return check_doc_drift(root, CONFIG_DOC, expected, REGEN_CMD)
    if current == expected:
        logger.info("OK: %s already in sync — no write.", CONFIG_DOC)
        return 0
    path.write_text(expected)
    logger.info("✓ regenerated %s", CONFIG_DOC)
    return 0


if __name__ == "__main__":
    sys.exit(main())
