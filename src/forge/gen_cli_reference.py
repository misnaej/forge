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

A CLI whose ``--help`` cannot be captured (it exits non-zero or prints
nothing — usually a stale install) is never recorded as documentation:
in either mode the run names each such CLI, writes nothing and exits 1,
so a committed page cannot silently lose a section while every check
reports it in sync.

Exit Codes:
    0: The doc was written (default mode), or it is already in sync
       (``--check`` mode).
    1: ``--check`` detected drift between the generated content and the
       committed ``docs/cli-reference.md``, or (either mode) at least one
       CLI's ``--help`` could not be captured. The file is left untouched.
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from typing import NamedTuple

from forge.gen_common import HELP_UNAVAILABLE_MARKER, check_doc_drift
from forge.git_utils import (
    FORGE_DIST_NAME,
    configure_cli_logging,
    console_script_modules,
    repo_root,
)


configure_cli_logging()
logger = logging.getLogger(__name__)


# Distribution that owns forge's console scripts.
DISTRIBUTION = FORGE_DIST_NAME
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


class RenderedReference(NamedTuple):
    """The rendered CLI reference, and the CLIs whose help was unavailable.

    A NamedTuple rather than an exception: captures run inside a parallel
    ``pool.map``, where raising on the first failure would abort the rest
    and hide which other CLIs also failed.

    Attributes:
        text: The complete markdown document, or ``""`` when *unavailable*
            is non-empty — a page missing a section must never be written.
        unavailable: Names of the CLIs whose ``--help`` could not be
            captured, in entry order.
    """

    text: str
    unavailable: list[str]


def discover_clis(distribution: str = DISTRIBUTION) -> list[CliEntry]:
    """Discover the console-script CLIs shipped by a distribution.

    Reads ``console_scripts`` entry points from the installed
    distribution metadata and resolves each one's importable module.

    Args:
        distribution: Name of the installed distribution to inspect.
            Defaults to :data:`DISTRIBUTION`.

    Returns:
        CLI entries sorted by console-script name; empty when the
        distribution is not installed.
    """
    entries = [
        CliEntry(name=name, module=module)
        for name, module in (console_script_modules(distribution) or {}).items()
    ]
    return sorted(entries, key=lambda entry: entry.name)


def capture_help(entry: CliEntry) -> str | None:
    """Capture the ``--help`` output of a single CLI.

    Invokes ``python -m <module> --help`` with the current interpreter
    so help is available even when the console script is not on
    ``PATH``. The subprocess environment pins ``COLUMNS`` to
    :data:`CLI_REFERENCE_COLUMNS` so argparse's HelpFormatter produces
    byte-identical output regardless of the caller's terminal width.
    A failed capture is logged here, once, with the exit code; callers
    only count it.

    Args:
        entry: The CLI to capture help for.

    Returns:
        The stripped ``--help`` output, or ``None`` when the CLI exits
        non-zero or produces no output.
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
        logger.error(
            "%s for %s (exit %d)",
            HELP_UNAVAILABLE_MARKER,
            entry.name,
            proc.returncode,
        )
        return None
    return output


def render_reference(entries: list[CliEntry]) -> RenderedReference:
    """Render the full CLI reference markdown document.

    Args:
        entries: The CLIs to document, in the order they should appear.

    Returns:
        The markdown content for ``docs/cli-reference.md`` (ending with a
        single trailing newline) and the CLIs whose help was unavailable;
        the text is empty whenever any help was unavailable.
    """
    lines = [
        "# CLI Reference",
        "",
        (
            "Forge's console-script CLIs are its real public surface. This page "
            "documents each CLI's command-line interface, captured from its "
            "`--help` output."
        ),
        "",
        (
            "> **Generated file — do not edit by hand.** Regenerate with "
            "`forge-gen-cli-reference`; check for drift with "
            "`forge-gen-cli-reference --check`."
        ),
        "",
    ]
    # Each capture is an independent `python -m <module> --help` subprocess,
    # so they run concurrently; ``map`` keeps the results in entry order,
    # so the document is byte-identical to a sequential run.
    with ThreadPoolExecutor(max_workers=min(16, len(entries) or 1)) as pool:
        helps = list(pool.map(capture_help, entries))
    captured = [help_text for help_text in helps if help_text is not None]
    if len(captured) != len(entries):
        unavailable = [
            entry.name
            for entry, help_text in zip(entries, helps, strict=True)
            if help_text is None
        ]
        return RenderedReference(text="", unavailable=unavailable)
    for entry, help_text in zip(entries, captured, strict=True):
        lines.append(f"## {entry.name}")
        lines.append("")
        lines.append("```text")
        lines.append(help_text)
        lines.append("```")
        lines.append("")
    return RenderedReference(text="\n".join(lines).rstrip("\n") + "\n", unavailable=[])


def main() -> int:
    """Generate or verify the forge CLI reference doc.

    Returns:
        Exit code: ``0`` when the doc was written or is in sync, ``1``
        when ``--check`` detected drift or a missing doc, or when any
        CLI's ``--help`` could not be captured (nothing is written).
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
    rendered = render_reference(entries)
    if rendered.unavailable:
        # Each CLI was already named by capture_help; this line says what
        # it means for the doc and what to do about it.
        logger.error(
            "%s left untouched: --help could not be captured for %d CLI(s) "
            "above, and a page without their sections would read as complete. "
            "A stale or broken install is the usual cause — refresh the "
            "install and run again.",
            DOC_RELPATH,
            len(rendered.unavailable),
        )
        return 1

    if args.check:
        return check_doc_drift(
            root,
            DOC_RELPATH,
            rendered.text,
            "forge-gen-cli-reference",
        )

    doc_path = root / DOC_RELPATH
    doc_path.parent.mkdir(parents=True, exist_ok=True)
    doc_path.write_text(rendered.text)
    logger.info("Wrote %s (%d CLIs).", DOC_RELPATH, len(entries))
    return 0


if __name__ == "__main__":
    sys.exit(main())
