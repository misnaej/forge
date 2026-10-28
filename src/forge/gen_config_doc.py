"""Generate the key/default table in ``forge-docs/configuration.md``.

Every ``[tool.forge.*]`` key and its default is declared once, as
:data:`forge.forge_config.CONFIG_KEYS`. The configuration reference
renders that list as a table between two markers, so the defaults it
shows cannot drift from what ``forge-config`` reports:

.. code-block:: markdown

    <!-- forge:config:start -->
    | Key | Default |
    ...
    <!-- forge:config:end -->

Text outside the markers is byte-preserved.

Usage:

    # Regenerate the table
    forge-gen-config-doc

    # Verify the table is in sync (no write)
    forge-gen-config-doc --check

Exit Codes:
    0: The table was written (default), or is already in sync
       (``--check``).
    1: The doc or its markers are missing, or ``--check`` detected
       drift between the generated table and the committed doc.
"""

from __future__ import annotations

import argparse
import logging
import sys

from forge.forge_config import keys_by_section
from forge.gen_common import (
    CONFIG_DOC,
    CONFIG_DOC_END,
    CONFIG_DOC_START,
    check_doc_drift,
    replace_region,
)
from forge.git_utils import configure_cli_logging, repo_root


configure_cli_logging()
logger = logging.getLogger(__name__)

REGEN_CMD = "forge-gen-config-doc"


def render_table() -> str:
    """Render every config key and its default as a markdown table.

    Defaults use ``repr`` so the table reads the way ``forge-config``
    prints them.

    Returns:
        The region body, newline-delimited from both markers.
    """
    lines = ["", "| Key | Default |", "|---|---|"]
    for section, keys in keys_by_section():
        for key in keys:
            default = repr(key.default).replace("|", r"\|")
            lines.append(f"| `[{section}].{key.path[-1]}` | `{default}` |")
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
            f"Regenerate the key/default table in {CONFIG_DOC} from "
            "forge.forge_config.CONFIG_KEYS."
        ),
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify the table is in sync without writing. Exit 1 on drift.",
    )
    args = parser.parse_args()

    root = repo_root()
    path = root / CONFIG_DOC
    if not path.is_file():
        logger.error("missing %s — nothing to regenerate.", CONFIG_DOC)
        return 1
    current = path.read_text()
    expected = replace_region(current, CONFIG_DOC_START, CONFIG_DOC_END, render_table())
    if expected is None:
        logger.error(
            "%s lacks the %s / %s markers — add them where the table belongs.",
            CONFIG_DOC,
            CONFIG_DOC_START,
            CONFIG_DOC_END,
        )
        return 1
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
