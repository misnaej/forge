"""Validate ``.claude-plugin/*.json`` files: JSON parse + version schema.

Standalone phase CLI for the ``manifest_json`` step in the forge
pre-commit sequence. Owns the manifest-validation phase end to end —
checks every ``.json`` file under ``.claude-plugin/`` parses as JSON,
rejects a hook matcher repeated within one event (a roster keys hooks by
matcher, so a repeat drops one group's hooks silently),
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


def _repeated_matchers(groups: list[object]) -> list[str]:
    """Return each matcher that repeats within one event's group list, in order.

    Args:
        groups: One event's hook groups; non-dict groups and non-str
            matchers are skipped.

    Returns:
        The repeated matcher strings, in order of their second appearance.
    """
    seen: set[str] = set()
    repeated: list[str] = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        matcher = group.get("matcher", "")
        if not isinstance(matcher, str):
            continue
        if matcher in seen:
            repeated.append(matcher)
        seen.add(matcher)
    return repeated


def _duplicate_matcher_error(manifest: Path) -> str | None:
    """Return an error if a hook event repeats a matcher, else None.

    A harness roster keys hooks by matcher rather than appending them
    (``forge.install_vibe`` writes ``events[event].matchers[matcher]``),
    so a second group naming a matcher the event already used replaces
    the first group's hooks instead of adding to them. Whichever group
    loses is dropped in silence, which is how a registered guard stops
    running with nothing on screen. Groups sharing a matcher belong
    merged into one ``hooks`` list in the manifest.

    Args:
        manifest: Manifest file to inspect.

    Returns:
        A message naming every repeated ``event/matcher`` pair, or
        ``None`` when the file is not ``plugin.json``, carries no hook
        table, or repeats nothing.
    """
    if manifest.name != "plugin.json":
        return None
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return None
    repeated: list[str] = []
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            continue
        repeated.extend(
            f"{event}/{matcher or '(no matcher)'}"
            for matcher in _repeated_matchers(groups)
        )
    if not repeated:
        return None
    return (
        f"{manifest.name}: matcher repeated within one event: "
        f"{', '.join(repeated)} — a roster keys hooks by matcher, so the "
        "later group silently replaces the earlier one; merge their hooks "
        "lists into a single group"
    )


def main() -> int:
    """Validate every ``.claude-plugin/*.json`` file and write the log.

    Returns:
        ``0`` on success or when no ``.claude-plugin/`` dir exists (skip).
        ``1`` when at least one manifest fails to parse, ``plugin.json``
        carries a non-strict-semver version value, or ``plugin.json``
        repeats a hook matcher within one event.
    """
    argparse.ArgumentParser(
        prog="verify-forge-manifest",
        description=(
            "Validate that every .claude-plugin/*.json file parses as JSON, "
            "that no hook event in plugin.json repeats a matcher, and that "
            "plugin.json's version field, when present, is bare X.Y.Z "
            "semver. Writes code_health/manifest_json.log."
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
            if (
                err := _parse_json_error(manifest)
                or _version_error(manifest)
                or _duplicate_matcher_error(manifest)
            )
            is not None
        ]
        if errors:
            logger.info("%s", "\n".join(errors))
            return 1
        logger.info("OK")
        return 0


if __name__ == "__main__":
    sys.exit(main())
