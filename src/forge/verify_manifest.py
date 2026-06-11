"""Validate that ``.claude-plugin/*.json`` files parse as JSON.

Standalone phase CLI for the ``manifest_json`` step in the forge
pre-commit sequence. Owns the manifest-validation phase end to end —
checks every ``.json`` file under ``.claude-plugin/`` and writes the
combined result to ``code_health/manifest_json.log``.

``forge-precommit`` shells out to this CLI; agents may invoke it
standalone to refresh just ``manifest_json.log``.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from forge.git_utils import capturing_to_step_log, configure_cli_logging


configure_cli_logging()
logger = logging.getLogger(__name__)


def _parse_json_error(manifest: Path) -> str | None:
    """Return a formatted error if *manifest* is invalid JSON, else None.

    Args:
        manifest: Path to a ``.json`` file to validate.

    Returns:
        ``"<filename>: <error>"`` on parse failure, or ``None`` if the file
        parses cleanly.
    """
    try:
        json.loads(manifest.read_text())
    except json.JSONDecodeError as exc:
        return f"{manifest.name}: {exc}"
    return None


def main() -> int:
    """Validate every ``.claude-plugin/*.json`` file and write the log.

    Returns:
        ``0`` on success or when no ``.claude-plugin/`` dir exists (skip).
        ``1`` when at least one manifest fails to parse.
    """
    argparse.ArgumentParser(
        prog="verify-forge-manifest",
        description=(
            "Validate that every .claude-plugin/*.json file parses as JSON. "
            "Writes code_health/manifest_json.log."
        ),
    ).parse_args()

    repo_root = Path.cwd()
    with capturing_to_step_log(repo_root, "manifest_json"):
        plugin_dir = repo_root / ".claude-plugin"
        if not plugin_dir.is_dir():
            logger.info("(no .claude-plugin/ dir — skipped)")
            return 0

        errors = [
            err
            for manifest in plugin_dir.glob("*.json")
            if (err := _parse_json_error(manifest)) is not None
        ]
        if errors:
            logger.info("%s", "\n".join(errors))
            return 1
        logger.info("OK")
        return 0


if __name__ == "__main__":
    sys.exit(main())
