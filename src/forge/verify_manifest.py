"""Validate ``.claude-plugin/*.json`` files: JSON parse + version schema.

Standalone phase CLI for the ``manifest_json`` step in the forge
pre-commit sequence. Owns the manifest-validation phase end to end —
checks every ``.json`` file under ``.claude-plugin/`` parses as JSON,
and additionally schema-checks ``plugin.json``'s ``version`` field
(strict bare semver) when present. The schema check exists because a
version value carrying JSON escapes can be *valid JSON* while smuggling
injected keys past a textual rewrite (CWE-116 — found via
``forge-rebump``'s review); one mechanical writer plus one schema gate
is the fail-closed pair. Writes the combined result to
``code_health/manifest_json.log``.

``forge-precommit`` shells out to this CLI; agents may invoke it
standalone to refresh just ``manifest_json.log``.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from pathlib import Path

from forge.git_utils import capturing_to_step_log, configure_cli_logging


configure_cli_logging()
logger = logging.getLogger(__name__)


# Strict bare semver — no ``v`` prefix, no suffixes, no whitespace.
# Applied via fullmatch (``$`` alone would tolerate one trailing
# newline). The manifest carries the exact release triple; anything else
# is either a mistake or a payload (module-local by convention, like
# next_prep's).
_STRICT_SEMVER_RE = re.compile(r"\d+\.\d+\.\d+")


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


def _version_error(manifest: Path) -> str | None:
    """Return an error when ``plugin.json``'s version is not strict semver.

    Scope is deliberately narrow: only ``plugin.json``, and only when a
    ``version`` key exists — the field is optional in the plugin schema
    (the ``plugin_version`` step owns the rolling-next requirement
    separately), and other manifests (``marketplace.json``) keep
    plain-parse behavior. A file that does not parse returns ``None`` —
    the parse error belongs to :func:`_parse_json_error`, not here.

    Args:
        manifest: Path to a ``.json`` file under ``.claude-plugin/``.

    Returns:
        ``"plugin.json: ..."`` when the version value is non-string or
        not a bare ``X.Y.Z`` triple, else ``None``.
    """
    if manifest.name != "plugin.json":
        return None
    try:
        data = json.loads(manifest.read_text())
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or "version" not in data:
        return None
    version = data["version"]
    if not isinstance(version, str):
        return (
            f"{manifest.name}: version must be a string, got {type(version).__name__}"
        )
    if not _STRICT_SEMVER_RE.fullmatch(version):
        return (
            f"{manifest.name}: version {version!r} is not bare semver "
            "(expected X.Y.Z — no v prefix, no suffix)"
        )
    return None


def main() -> int:
    """Validate every ``.claude-plugin/*.json`` file and write the log.

    Returns:
        ``0`` on success or when no ``.claude-plugin/`` dir exists (skip).
        ``1`` when at least one manifest fails to parse, or
        ``plugin.json`` carries a non-strict-semver version value.
    """
    argparse.ArgumentParser(
        prog="verify-forge-manifest",
        description=(
            "Validate that every .claude-plugin/*.json file parses as JSON "
            "and that plugin.json's version field, when present, is bare "
            "X.Y.Z semver. Writes code_health/manifest_json.log."
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
            if (err := _parse_json_error(manifest) or _version_error(manifest))
            is not None
        ]
        if errors:
            logger.info("%s", "\n".join(errors))
            return 1
        logger.info("OK")
        return 0


if __name__ == "__main__":
    sys.exit(main())
