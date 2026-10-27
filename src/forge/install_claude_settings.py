"""install-forge-claude-settings — keep the shared forge plugin off in this repo.

Each checkout installs forge for itself, from a marketplace named after
the release it pins (``forge-plugin-sync``). The repo's committed
``.claude/settings.json`` therefore must not enable the shared
``forge@forge`` plugin: a second enabled plugin named ``forge`` makes
Claude Code drop hooks, and the shared ``forge`` marketplace is the
machine-wide registration whose single source made repos overwrite each
other's release. This command removes forge's entry from
``extraKnownMarketplaces`` and sets ``enabledPlugins["forge@forge"]`` to
``false``. ``install-forge-bootstrap`` runs it before ``forge-plugin-sync``,
so ``forge-upgrade`` carries existing repos across.

Idempotent and **merge-preserving**: every other key, marketplace, and
``enabledPlugins`` entry is left untouched. A ``.claude/settings.json``
that exists but cannot be parsed is **never overwritten**.

Usage:

- ``install-forge-claude-settings`` — write / update the block
- ``install-forge-claude-settings --check`` — verify without writing (CI / drift)
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import TYPE_CHECKING

from forge.claude_settings_schema import MARKETPLACE_KEY, PLUGIN_KEY, scaffold
from forge.git_utils import configure_cli_logging
from forge.git_utils import repo_root as get_repo_root


if TYPE_CHECKING:
    from pathlib import Path


configure_cli_logging()
logger = logging.getLogger(__name__)


def _load_settings(path: Path) -> dict[str, object] | None:
    """Return the parsed ``.claude/settings.json``.

    Args:
        path: Path to ``.claude/settings.json``.

    Returns:
        The parsed object when present (the empty-file
        :func:`~forge.claude_settings_schema.scaffold` when the file is
        absent, so a standalone fresh-repo run still seeds the hook arrays
        ``install-forge-claude-md`` would otherwise write), or ``None`` when
        the file exists but is unparseable / not an object — the signal to
        refuse to overwrite it.
    """
    if not path.is_file():
        return scaffold()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def _is_current(settings: dict[str, object]) -> bool:
    """Return True when the shared forge marketplace and plugin are off.

    Args:
        settings: Parsed settings object.

    Returns:
        ``True`` when no ``extraKnownMarketplaces.forge`` entry exists and
        ``enabledPlugins["forge@forge"]`` is ``False``.
    """
    markets = settings.get("extraKnownMarketplaces")
    plugins = settings.get("enabledPlugins")
    if isinstance(markets, dict) and MARKETPLACE_KEY in markets:
        return False
    return isinstance(plugins, dict) and plugins.get(PLUGIN_KEY) is False


def _merge(settings: dict[str, object]) -> dict[str, object]:
    """Return *settings* with the shared forge marketplace and plugin turned off.

    Preserves every other top-level key, every other marketplace, and every
    other ``enabledPlugins`` entry.

    Args:
        settings: Existing parsed settings object.

    Returns:
        A new settings object without forge's marketplace entry and with
        ``forge@forge`` disabled.
    """
    merged = dict(settings)
    markets = settings.get("extraKnownMarketplaces")
    if isinstance(markets, dict):
        remaining = {k: v for k, v in markets.items() if k != MARKETPLACE_KEY}
        if remaining:
            merged["extraKnownMarketplaces"] = remaining
        else:
            merged.pop("extraKnownMarketplaces", None)
    plugins = settings.get("enabledPlugins")
    plugins = dict(plugins) if isinstance(plugins, dict) else {}
    plugins[PLUGIN_KEY] = False
    merged["enabledPlugins"] = plugins
    return merged


def main() -> int:
    """CLI entry point.

    Returns:
        ``0`` on success (written, already-current, or ``--check`` passing);
        ``1`` when ``--check`` finds drift, or when the existing
        ``.claude/settings.json`` cannot be parsed (never overwritten).
    """
    parser = argparse.ArgumentParser(
        prog="install-forge-claude-settings",
        description=(
            "Turn off the shared forge@forge plugin and marketplace in this "
            "repo's .claude/settings.json; each checkout installs its own "
            "release with forge-plugin-sync. Idempotent and merge-preserving."
        ),
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify the block is present without writing (exit 1 on drift).",
    )
    args = parser.parse_args()

    root = get_repo_root()
    settings_path = root / ".claude" / "settings.json"
    settings = _load_settings(settings_path)

    if settings is None:
        logger.error(
            "%s exists but is not parseable JSON — fix or remove it; refusing "
            "to overwrite.",
            settings_path,
        )
        return 1

    if _is_current(settings):
        logger.info(".claude/settings.json keeps the shared %s off.", PLUGIN_KEY)
        return 0
    if args.check:
        logger.error(
            ".claude/settings.json still enables the shared %s or declares its "
            "marketplace — run install-forge-claude-settings.",
            PLUGIN_KEY,
        )
        return 1

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(
        json.dumps(_merge(settings), indent=2) + "\n", encoding="utf-8"
    )
    logger.info("Turned off the shared %s in %s.", PLUGIN_KEY, settings_path)
    logger.info("Run `forge-plugin-sync` to install this checkout's own forge.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
