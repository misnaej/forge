"""install-forge-claude-settings — enable the forge plugin per repo.

Writes / verifies the consumer repo's ``.claude/settings.json`` so Claude
Code registers forge's marketplace and enables the ``forge@forge`` plugin
in **this repo only**. Per-repo enablement is the recommended model: a
global ``/plugin install`` activates the plugin in *every* repo, and its
agents then error in repos that lack ``forge-scripts``. A committed
``.claude/settings.json`` keeps the plugin scoped to the repos that opt in
(see ``docs/adopting.md`` Track 3).

Idempotent and **merge-preserving**: every other key, marketplace, and
``enabledPlugins`` entry is left untouched — only forge's marketplace
entry and the ``forge@forge`` flag are set. A ``.claude/settings.json``
that exists but cannot be parsed is **never overwritten**.

The marketplace ``ref`` (branch / tag) is resolved in order:
``--ref`` → the ``forge-scripts`` pip-pin ref in ``pyproject.toml`` (so the
plugin channel tracks the pip channel) → ``main``.

Usage:

- ``install-forge-claude-settings`` — write / update the block
- ``install-forge-claude-settings --ref dev`` — pin the marketplace to ``@dev``
- ``install-forge-claude-settings --check`` — verify without writing (CI / drift)
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import TYPE_CHECKING

from forge.claude_settings_schema import (
    MARKETPLACE_KEY,
    PLUGIN_KEY,
    marketplace_entry,
    scaffold,
)
from forge.git_utils import configure_cli_logging
from forge.git_utils import repo_root as get_repo_root
from forge.upgrade import find_pin


if TYPE_CHECKING:
    from pathlib import Path


configure_cli_logging()
logger = logging.getLogger(__name__)


_DEFAULT_REF = "main"


def _resolve_ref(repo_root: Path, cli_ref: str | None) -> str:
    """Resolve the marketplace ref: ``--ref`` → pip-pin ref → ``"main"``.

    Auto-matching the ``forge-scripts`` pip pin keeps the plugin channel
    aligned with the package channel (a repo pinned ``@dev`` gets ``@dev``
    plugin enablement with zero config).

    Args:
        repo_root: Consumer repo root.
        cli_ref: The ``--ref`` value, or ``None`` when unset.

    Returns:
        The resolved ref string.
    """
    if cli_ref:
        return cli_ref
    pin = find_pin(repo_root)
    if pin is not None:
        return pin.ref
    return _DEFAULT_REF


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


def _is_current(settings: dict[str, object], ref: str) -> bool:
    """Return True when the forge marketplace (at *ref*) and plugin enable are set.

    Args:
        settings: Parsed settings object.
        ref: Expected marketplace ref.

    Returns:
        ``True`` when ``extraKnownMarketplaces.forge`` matches *ref* and
        ``enabledPlugins["forge@forge"]`` is ``True``.
    """
    markets = settings.get("extraKnownMarketplaces")
    plugins = settings.get("enabledPlugins")
    if not isinstance(markets, dict) or not isinstance(plugins, dict):
        return False
    return markets.get(MARKETPLACE_KEY) == marketplace_entry(ref) and (
        plugins.get(PLUGIN_KEY) is True
    )


def _merge(settings: dict[str, object], ref: str) -> dict[str, object]:
    """Return *settings* with forge's marketplace + plugin enable merged in.

    Preserves every other top-level key, every other marketplace, and every
    other ``enabledPlugins`` entry.

    Args:
        settings: Existing parsed settings object.
        ref: Marketplace ref to write.

    Returns:
        A new settings object with the forge entries set.
    """
    markets = settings.get("extraKnownMarketplaces")
    markets = dict(markets) if isinstance(markets, dict) else {}
    plugins = settings.get("enabledPlugins")
    plugins = dict(plugins) if isinstance(plugins, dict) else {}
    markets[MARKETPLACE_KEY] = marketplace_entry(ref)
    plugins[PLUGIN_KEY] = True
    return {**settings, "extraKnownMarketplaces": markets, "enabledPlugins": plugins}


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
            "Enable the forge Claude Code plugin in this repo by writing "
            "the marketplace + enabledPlugins block to .claude/settings.json "
            "(per-repo, never global). Idempotent and merge-preserving."
        ),
    )
    parser.add_argument(
        "--ref",
        help=(
            "Marketplace ref (branch/tag) to pin. Defaults to the "
            "forge-scripts pip-pin ref in pyproject.toml, else 'main'."
        ),
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify the block is present without writing (exit 1 on drift).",
    )
    args = parser.parse_args()

    root = get_repo_root()
    ref = _resolve_ref(root, args.ref)
    settings_path = root / ".claude" / "settings.json"
    settings = _load_settings(settings_path)

    if settings is None:
        logger.error(
            "%s exists but is not parseable JSON — fix or remove it; refusing "
            "to overwrite.",
            settings_path,
        )
        return 1

    if args.check:
        if _is_current(settings, ref):
            logger.info(".claude/settings.json enables %s @ %s.", PLUGIN_KEY, ref)
            return 0
        logger.error(
            ".claude/settings.json does not enable %s @ %s — run "
            "install-forge-claude-settings.",
            PLUGIN_KEY,
            ref,
        )
        return 1

    if _is_current(settings, ref):
        logger.info(
            ".claude/settings.json already enables %s @ %s — no change.",
            PLUGIN_KEY,
            ref,
        )
        return 0

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(
        json.dumps(_merge(settings, ref), indent=2) + "\n", encoding="utf-8"
    )
    logger.info("Enabled %s @ %s in %s.", PLUGIN_KEY, ref, settings_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
