"""Generate the forge CLI reference doc from each console script's ``--help``.

Forge's console-script CLIs (registered under ``[project.scripts]`` in
``pyproject.toml``) are the package's real public surface. This module
discovers those CLIs via :mod:`importlib.metadata`, captures each one's
argparse ``--help`` output, and renders a single markdown reference page
at ``docs/cli-reference.md``.

Help is captured by invoking ``python -m <module>`` with the current
interpreter (``sys.executable``) rather than the bare script name. The
module is derived from the entry-point value (the part before ``:``).
This works even when a CLI's console script is not yet on ``PATH`` —
fresh installs and CI runners included.

Usage:

    # Regenerate docs/cli-reference.md
    forge-gen-cli-reference

    # Verify docs/cli-reference.md is in sync
    forge-gen-cli-reference --check

Exit Codes:
    0: The doc was written (default mode), or it is already in sync
       (``--check`` mode).
    1: ``--check`` detected drift between the generated content and the
       committed ``docs/cli-reference.md`` (the file is left untouched).
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from importlib import metadata
from typing import NamedTuple

from forge.gen_common import check_doc_drift
from forge.git_utils import configure_cli_logging, repo_root


configure_cli_logging()
logger = logging.getLogger(__name__)


# Distribution that owns forge's console scripts.
DISTRIBUTION = "forge-scripts"
# Path of the generated reference doc, relative to the repo root.
DOC_RELPATH = "docs/cli-reference.md"
# Pinned terminal width for ``--help`` capture. argparse's HelpFormatter
# honors ``$COLUMNS`` to decide line wrapping, so the captured output is
# environment-dependent unless we pin it. The drift gate
# (``forge-gen-cli-reference --check``) is otherwise unwinnable on any
# runner whose width differs from where the committed reference was
# generated. ``80`` matches argparse's traditional default and produces
# the narrowest reasonable layout for readable markdown.
CLI_REFERENCE_COLUMNS = "80"


class CliEntry(NamedTuple):
    """A single forge console-script CLI.

    Attributes:
        name: The console-script name (e.g. ``forge-precommit``).
        module: The importable module providing the entry point (e.g.
            ``forge.precommit``), derived from the part of the
            entry-point value before ``:``.
    """

    name: str
    module: str


def discover_clis(distribution: str = DISTRIBUTION) -> list[CliEntry]:
    """Discover the console-script CLIs shipped by a distribution.

    Reads ``console_scripts`` entry points from the installed
    distribution metadata and resolves each one's importable module.

    Args:
        distribution: Name of the installed distribution to inspect.
            Defaults to :data:`DISTRIBUTION`.

    Returns:
        CLI entries sorted by console-script name.

    Raises:
        importlib.metadata.PackageNotFoundError: If the distribution is
            not installed.
    """
    dist = metadata.distribution(distribution)
    entries = [
        CliEntry(name=ep.name, module=ep.value.split(":", 1)[0])
        for ep in dist.entry_points
        if ep.group == "console_scripts"
    ]
    return sorted(entries, key=lambda entry: entry.name)


def capture_help(entry: CliEntry) -> str:
    """Capture the ``--help`` output of a single CLI.

    Invokes ``python -m <module> --help`` with the current interpreter
    so help is available even when the console script is not on
    ``PATH``. The subprocess environment pins ``COLUMNS`` to
    :data:`CLI_REFERENCE_COLUMNS` so argparse's HelpFormatter produces
    byte-identical output regardless of the caller's terminal width.

    Args:
        entry: The CLI to capture help for.

    Returns:
        The stripped ``--help`` output, or a short placeholder line when
        the CLI exits non-zero or produces no output.
    """
    proc = subprocess.run(
        [sys.executable, "-m", entry.module, "--help"],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "COLUMNS": CLI_REFERENCE_COLUMNS},
    )
    output = (proc.stdout or proc.stderr).strip()
    if proc.returncode != 0 or not output:
        logger.warning(
            "Could not capture --help for %s (exit %d)",
            entry.name,
            proc.returncode,
        )
        return f"(--help unavailable for {entry.name})"
    return output


def render_reference(entries: list[CliEntry]) -> str:
    """Render the full CLI reference markdown document.

    Args:
        entries: The CLIs to document, in the order they should appear.

    Returns:
        The complete markdown content for ``docs/cli-reference.md``,
        ending with a single trailing newline.
    """
    lines = [
        "# CLI Reference",
        "",
        "Forge's console-script CLIs are its real public surface. This page "
        "documents each CLI's command-line interface, captured from its "
        "`--help` output.",
        "",
        "> **Generated file — do not edit by hand.** Regenerate with "
        "`forge-gen-cli-reference`; check for drift with "
        "`forge-gen-cli-reference --check`.",
        "",
    ]
    for entry in entries:
        help_text = capture_help(entry)
        lines.append(f"## {entry.name}")
        lines.append("")
        lines.append("```text")
        lines.append(help_text)
        lines.append("```")
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def main() -> int:
    """Generate or verify the forge CLI reference doc.

    Returns:
        Exit code: ``0`` when the doc was written or is in sync, ``1``
        when ``--check`` detected drift or a missing doc.
    """
    parser = argparse.ArgumentParser(
        prog="forge-gen-cli-reference",
        description="Generate docs/cli-reference.md from forge CLI --help output.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify docs/cli-reference.md is in sync; do not write.",
    )
    args = parser.parse_args()

    root = repo_root()
    entries = discover_clis()
    logger.info("Discovered %d forge CLIs.", len(entries))
    generated = render_reference(entries)

    if args.check:
        return check_doc_drift(
            root,
            DOC_RELPATH,
            generated,
            "forge-gen-cli-reference",
        )

    doc_path = root / DOC_RELPATH
    doc_path.parent.mkdir(parents=True, exist_ok=True)
    doc_path.write_text(generated)
    logger.info("Wrote %s (%d CLIs).", DOC_RELPATH, len(entries))
    return 0


if __name__ == "__main__":
    sys.exit(main())
