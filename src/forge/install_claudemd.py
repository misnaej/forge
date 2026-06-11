"""install-forge-claude-md — sync the forge foundation into a consumer repo.

Foundation engineering principles live in ``FOUNDATION.md`` inside the
``forge-scripts`` pip package. To surface them to Claude Code at session
start, every consumer repo needs a copy on disk.

Layout (v1.1.3+): the foundation lives in a **separate file** at the
consumer repo root::

    FOUNDATION.md   ← managed by forge, do NOT edit, START/END markers
    CLAUDE.md       ← repo-owned; opens with `@FOUNDATION.md` directive
                      so Claude Code inlines the foundation at load time

Repo-specific rules live below the include directive in ``CLAUDE.md``
and are never touched by forge. This is a clean two-file split: the
foundation file is fully managed; ``CLAUDE.md`` is fully consumer-
owned.

Migration from the v1.1.2 inline-block layout is supported via
``--migrate``: the inline ``forge:foundation-managed`` block in
``CLAUDE.md`` is extracted into ``FOUNDATION.md`` and replaced with
the ``@FOUNDATION.md`` include directive.

Usage:
    install-forge-claude-md             # write/update FOUNDATION.md
    install-forge-claude-md --check     # exit 1 on drift, no writes
    install-forge-claude-md --quiet     # suppress 'already in sync' info logs
    install-forge-claude-md --migrate   # convert v1.1.2 inline-block layout
    install-forge-claude-md --force     # overwrite unmanaged FOUNDATION.md

Every invocation also runs an advisory upstream-version check. Forge
publishes on two release branches — ``main`` (minor versions only) and
``dev`` (every patch). The check tells consumers whether their pin is
behind on the channel they chose. Throttled to once per 24h via
``~/.cache/forge/upstream_check.json``; warning-only, never blocks.
See :func:`check_upstream`.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from datetime import UTC, datetime, timedelta
from importlib import metadata, resources
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from forge.git_utils import (
    _FORGE_GITHUB_REPO,
    configure_cli_logging,
    gh_api,
    parse_semver,
    repo_root,
)
from forge.run_context import is_non_interactive


if TYPE_CHECKING:
    from collections.abc import Callable


configure_cli_logging()
logger = logging.getLogger(__name__)


BLOCK_NAME = "forge:foundation-managed"
BLOCK_VERSION = 1
START_MARKER_RE = re.compile(
    rf"<!-- {re.escape(BLOCK_NAME)} v\d+ START -->",
    re.MULTILINE,
)
END_MARKER_RE = re.compile(
    rf"<!-- {re.escape(BLOCK_NAME)} v\d+ END -->",
    re.MULTILINE,
)
# The version-stamped comment is informational only — different installs
# (tagged release vs editable dev build) embed different version strings.
# Drift detection normalizes it away so the FOUNDATION text is the only
# thing that triggers a re-sync.
VERSION_LINE_RE = re.compile(
    r"<!-- DO NOT EDIT.*? Synced from forge-scripts [^>]*? "
    r"by install-forge-claude-md\..*?-->\n?",
    re.DOTALL,
)
INCLUDE_DIRECTIVE = "@FOUNDATION.md"
INCLUDE_DIRECTIVE_RE = re.compile(
    r"^\s*@FOUNDATION\.md\s*$",
    re.MULTILINE,
)
CLAUDEMD_SCAFFOLD = """# CLAUDE.md

<!-- Layout: this repo follows the forge engineering foundation. The
     foundation lives in FOUNDATION.md (managed by forge — do not edit
     there). Everything below layers on top. Repo rules win when they
     disagree with foundation. -->

@FOUNDATION.md

---

## Repo-specific rules

<!-- Add your repo-specific guidance below. install-forge-claude-md
     never touches this file beyond the initial scaffold. -->
"""


CLAUDE_SETTINGS_SCAFFOLD = """{
  "hooks": {
    "PreToolUse": [],
    "PostToolUse": []
  }
}
"""


CLAUDE_HOOKS_README = """# Consumer Claude Code hooks

This directory holds **consumer-specific** Claude Code hook scripts.
Forge ships its own hooks via the plugin manifest — those load
automatically from `${CLAUDE_PLUGIN_ROOT}/claude-hooks/...` and you do
not register them here.

## Path convention

Register every hook in `.claude/settings.json` with a **path rooted at
`${CLAUDE_PROJECT_DIR}`**, never a relative path. Relative paths break
when a hook fires from a subagent, a subdirectory, or any context where
the shell's cwd is not the repo root — you get errors like:

```
/bin/sh: 1: .claude/hooks/<name>.sh: not found
```

### Right

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "${CLAUDE_PROJECT_DIR}/.claude/hooks/your_hook.sh",
            "timeout": 5
          }
        ]
      }
    ]
  }
}
```

### Wrong

```json
{ "type": "command", "command": ".claude/hooks/your_hook.sh" }
```

`${CLAUDE_PROJECT_DIR}` is populated by Claude Code with the absolute
path to the repo root regardless of shell cwd.

## Adding a hook

1. Drop the script under `.claude/hooks/<name>.sh`. Make it executable
   (`chmod +x`).
2. Register it in `.claude/settings.json` with the `${CLAUDE_PROJECT_DIR}`
   form shown above.
3. Restart Claude Code (or `/reload-plugins`) so the new hook is picked
   up.

Forge does not ship any consumer-specific hooks — only the directory
layout and this convention. The directory is created (empty) by
`install-forge-claude-md` so the path resolves on day one.
"""


def _foundation_text() -> str:
    """Return the bundled FOUNDATION.md text shipped with the pip package.

    Returns:
        The full content of ``src/forge/data/FOUNDATION.md`` as a string.
    """
    return resources.files("forge").joinpath("data/FOUNDATION.md").read_text()


def _forge_version() -> str:
    """Return the installed ``forge-scripts`` version, or ``unknown``.

    Returns:
        Semver string from ``importlib.metadata``, or ``"unknown"`` when
        the package isn't pip-installed (rare; tests).
    """
    try:
        return metadata.version("forge-scripts")
    except metadata.PackageNotFoundError:
        return "unknown"


def _build_foundation_file(*, foundation: str, version: str) -> str:
    """Render the full ``FOUNDATION.md`` content including markers.

    Args:
        foundation: Foundation content to embed verbatim.
        version: Installed forge version string (for traceability).

    Returns:
        Full file content, ready to write to ``FOUNDATION.md``.
    """
    return (
        f"<!-- {BLOCK_NAME} v{BLOCK_VERSION} START -->\n"
        f"<!-- DO NOT EDIT — managed by forge. Synced from forge-scripts "
        f"{version} by install-forge-claude-md.\n"
        f"     To upgrade: re-run install-forge-claude-md after pulling a "
        f"new forge version. -->\n\n"
        f"{foundation.rstrip()}\n\n"
        f"<!-- {BLOCK_NAME} v{BLOCK_VERSION} END -->\n"
    )


def _has_managed_markers(text: str) -> bool:
    """Return True if *text* contains a forge-managed START/END pair.

    Args:
        text: Text to check.

    Returns:
        True iff both START and END markers are present and START comes
        before END.
    """
    start = START_MARKER_RE.search(text)
    end = END_MARKER_RE.search(text)
    return bool(start and end and start.start() < end.start())


def _normalize(text: str) -> str:
    """Strip the version-stamped comment for drift comparison.

    Args:
        text: Raw ``FOUNDATION.md`` content (existing on disk or freshly
            rendered).

    Returns:
        *text* with the ``<!-- DO NOT EDIT ... -->`` banner removed. The
        remaining content (foundation body + START/END markers) is what
        we actually diff against, so version drift between dev/editable
        installs and tagged releases doesn't trigger spurious re-syncs.
    """
    return VERSION_LINE_RE.sub("", text)


def sync_foundation(
    foundation_path: Path,
    *,
    check_only: bool = False,
    force: bool = False,
) -> bool:
    """Write or update ``FOUNDATION.md`` with the shipped foundation text.

    Args:
        foundation_path: Path to the consumer repo's ``FOUNDATION.md``.
        check_only: Report drift without writing.
        force: Overwrite an existing ``FOUNDATION.md`` that lacks the
            managed START/END markers (consumer wrote their own file).

    Returns:
        True if the file changed (or would change in ``check_only`` mode);
        False if already in sync.
    """
    new_content = _build_foundation_file(
        foundation=_foundation_text(),
        version=_forge_version(),
    )
    if not foundation_path.exists():
        if check_only:
            return True
        foundation_path.write_text(new_content)
        logger.info("✓ created %s", foundation_path.name)
        return True

    existing = foundation_path.read_text()
    if not _has_managed_markers(existing) and not force:
        logger.warning(
            "! %s exists but has no forge-managed markers. "
            "Leaving alone (use --force to overwrite).",
            foundation_path.name,
        )
        return False

    if _normalize(new_content) == _normalize(existing):
        logger.info("✓ %s already in sync", foundation_path.name)
        return False

    if check_only:
        logger.warning(
            "! %s is out of sync with the installed forge version. "
            "Run install-forge-claude-md.",
            foundation_path.name,
        )
        return True

    foundation_path.write_text(new_content)
    logger.info("✓ updated %s", foundation_path.name)
    return True


def _claudemd_has_include(text: str) -> bool:
    """Return True if *text* has an ``@FOUNDATION.md`` include directive.

    Args:
        text: Full ``CLAUDE.md`` content.

    Returns:
        True iff at least one line consists of (optional whitespace)
        ``@FOUNDATION.md`` (optional trailing whitespace).
    """
    return bool(INCLUDE_DIRECTIVE_RE.search(text))


def scaffold_claudemd(claudemd_path: Path) -> bool:
    """Write a minimal scaffold ``CLAUDE.md`` if the file does not exist.

    Args:
        claudemd_path: Path to the consumer repo's ``CLAUDE.md``.

    Returns:
        True if a new file was written; False if it already exists.
    """
    if claudemd_path.exists():
        return False
    claudemd_path.write_text(CLAUDEMD_SCAFFOLD)
    logger.info("✓ created %s (scaffold)", claudemd_path.name)
    return True


def scaffold_claude_settings(settings_path: Path) -> bool:
    """Write a minimal ``.claude/settings.json`` if the file does not exist.

    The skeleton ships with empty ``PreToolUse`` / ``PostToolUse`` arrays
    so consumers can append their hook registrations without writing the
    whole file from scratch. Existing files are never touched — the
    consumer owns this file.

    Args:
        settings_path: Path to the consumer repo's ``.claude/settings.json``.

    Returns:
        True if a new file was written; False if it already exists.
    """
    if settings_path.exists():
        return False
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(CLAUDE_SETTINGS_SCAFFOLD)
    logger.info("✓ created %s (scaffold)", settings_path)
    return True


def ensure_claude_hooks_dir(hooks_dir: Path) -> bool:
    """Create ``.claude/hooks/`` with a README documenting the path convention.

    The README explains the ``${CLAUDE_PROJECT_DIR}/.claude/hooks/<name>.sh``
    pattern that hook registrations must follow. Without it, consumers
    typically copy a relative path (``.claude/hooks/<name>.sh``) and hit
    spurious ``not found`` errors whenever the hook fires from outside
    the repo-root cwd.

    Idempotent: re-running on an existing directory leaves the README
    alone if it's already present.

    Args:
        hooks_dir: Path to the consumer repo's ``.claude/hooks/``.

    Returns:
        True if the directory or its README was newly created; False if
        both already exist.
    """
    created = False
    if not hooks_dir.exists():
        hooks_dir.mkdir(parents=True)
        created = True
    readme_path = hooks_dir / "README.md"
    if not readme_path.exists():
        readme_path.write_text(CLAUDE_HOOKS_README)
        created = True
    if created:
        logger.info("✓ ensured %s with path-convention README", hooks_dir)
    return created


# ---------------------------------------------------------------------------
# Channel-aware upstream-version drift warning
# ---------------------------------------------------------------------------


# Forge upstream branch names — channels used by the dual-track release
# model. ``_FORGE_GITHUB_REPO`` lives in ``forge.git_utils`` (canonical).
_FORGE_MAIN_BRANCH = "main"
_FORGE_DEV_BRANCH = "dev"
_UPSTREAM_CACHE_FILENAME = "upstream_check.json"
_UPSTREAM_CACHE_TTL_HOURS_DEFAULT = 24


def _installed_forge_scripts_version() -> str | None:
    """Return the installed ``forge-scripts`` distribution version.

    Returns:
        Raw version string (e.g. ``"1.2.11"`` or ``"1.2.11.dev3+g..."``),
        or ``None`` if the package is not installed (consumer that
        adopted only the plugin layer).
    """
    try:
        return metadata.version("forge-scripts")
    except metadata.PackageNotFoundError:
        return None


def _plugin_entry_version(entry: object) -> str | None:
    """Pull the ``version`` field out of a single forge@forge entry.

    Args:
        entry: One element of the ``plugins.forge@forge`` value — either
            a dict (per-instance install record) or something else (ignored).

    Returns:
        The version string when *entry* is a dict carrying a non-empty
        ``version``; otherwise ``None``.
    """
    if isinstance(entry, dict) and entry.get("version"):
        return str(entry["version"])
    return None


def _installed_plugin_version(plugins_file: Path) -> str | None:
    """Read the installed Claude Code plugin version from the manifest.

    Args:
        plugins_file: Path to ``~/.claude/plugins/installed_plugins.json``.

    Returns:
        Version string for ``forge@forge``, or ``None`` if the file
        does not exist, is malformed, or does not list forge.
    """
    if not plugins_file.is_file():
        return None
    try:
        data = json.loads(plugins_file.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    forge_entries = data.get("plugins", {}).get("forge@forge")
    # Two manifest shapes seen in the wild: a list of per-instance
    # install records, or a single dict for a single install. Walk both
    # and return the most recent version field found.
    if isinstance(forge_entries, list):
        for entry in reversed(forge_entries):
            version = _plugin_entry_version(entry)
            if version is not None:
                return version
        return None
    return _plugin_entry_version(forge_entries)


def _read_configured_channel(settings_path: Path) -> str | None:
    """Return the marketplace ``ref`` consumers set to track a forge release channel.

    Navigates ``extraKnownMarketplaces.forge.source.ref`` in the
    consumer's Claude Code settings file.

    Args:
        settings_path: Path to ``~/.claude/settings.json``.

    Returns:
        The configured ref (typically ``"dev"`` or ``"main"``), or
        ``None`` when no ref is set or any read step fails (missing
        file, malformed JSON, missing/wrong-typed keys).
    """
    if not settings_path.is_file():
        return None
    try:
        node: object = json.loads(settings_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    # Each step refuses to .get(...) on a non-dict, which catches null
    # mid-chain (`{"extraKnownMarketplaces": null}`) and non-dict leaves
    # without raising. Walk: root → extraKnownMarketplaces → forge →
    # source → ref.
    for key in ("extraKnownMarketplaces", "forge", "source", "ref"):
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node if isinstance(node, str) and node else None


def _upstream_cache_path() -> Path:
    """Return the upstream-version-check cache file path.

    Honors ``XDG_CACHE_HOME``; falls back to ``~/.cache``.

    Returns:
        Absolute path to ``<cache>/forge/upstream_check.json``.
    """
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "forge" / _UPSTREAM_CACHE_FILENAME


class ChannelTags(NamedTuple):
    """Latest release tag on each of forge's two upstream branches.

    Forge publishes on ``main`` (minor versions only) and ``dev``
    (every patch). This class carries the resolved tag per branch so
    the upstream check can warn a consumer who is behind on whichever
    one they pinned. ``None`` per field means "skip the warning for
    that branch" (e.g. the API query failed).

    Attributes:
        main_tag: Highest semver tag on upstream ``main``, or ``None``.
        dev_tag: Highest semver tag on upstream ``dev``, or ``None``.
    """

    main_tag: str | None
    dev_tag: str | None


def _read_upstream_cache(
    cache_path: Path,
    ttl_hours: int,
) -> ChannelTags | None:
    """Return the cached channel tags if the cache is still fresh.

    Old single-channel caches (``{"latest_tag": ...}`` from before the
    dual-track rollout) are silently treated as a cache miss so the
    next call re-fetches under the new schema. This avoids forcing
    consumers to delete the cache file by hand.

    Args:
        cache_path: Cache file location.
        ttl_hours: Cache freshness window. Stale entries return ``None``.

    Returns:
        :class:`ChannelTags` when the cache exists, carries the new
        schema, and is younger than ``ttl_hours``. ``None`` otherwise
        (missing file, stale, parse failure, or pre-rollout schema).
    """
    if not cache_path.is_file():
        return None
    try:
        data = json.loads(cache_path.read_text())
        checked_at = datetime.fromisoformat(data["checked_at"])
    except (OSError, KeyError, ValueError):
        return None
    if datetime.now(UTC) - checked_at > timedelta(hours=ttl_hours):
        return None
    if "main_tag" not in data and "dev_tag" not in data:
        # Pre-rollout schema (carried only ``latest_tag``) — force re-fetch.
        return None
    return ChannelTags(
        main_tag=data.get("main_tag") or None,
        dev_tag=data.get("dev_tag") or None,
    )


def _write_upstream_cache(cache_path: Path, tags: ChannelTags) -> None:
    """Persist the channel-tag snapshot + check timestamp.

    Args:
        cache_path: Cache file location.
        tags: Channel-tag snapshot to store.
    """
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "checked_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "main_tag": tags.main_tag,
        "dev_tag": tags.dev_tag,
    }
    cache_path.write_text(json.dumps(payload) + "\n")


def _fetch_upstream_channel_tags() -> ChannelTags:
    """Query GitHub for the latest tag on each forge release channel.

    Three API calls (all covered by the 24h cache so the cost is
    amortised):

    1. ``GET /repos/misnaej/forge/tags?per_page=50`` — recent tags with
       their commit SHAs.
    2. ``GET /repos/misnaej/forge/commits?sha=main&per_page=50`` — the
       most recent main commits.
    3. ``GET /repos/misnaej/forge/commits?sha=dev&per_page=50`` — the
       most recent dev commits.

    For each branch, the latest tag is the first tag whose SHA appears
    in that branch's commit list. This correctly classifies hotfix
    tags landed on main (a non-``.0`` patch) without a tag-name
    heuristic.

    Returns:
        :class:`ChannelTags`. Either field may be ``None`` if the
        corresponding branch query fails or returns no overlap with
        the tag list. Total failure (no tags at all) returns
        ``ChannelTags(None, None)``.
    """
    # Window size — assumes the latest tag on each channel is within
    # the most-recent 50 commits / 50 tags. If forge ever has a long
    # untagged stretch, bump this. 50 covers ~6 months at forge's
    # current cadence.
    page_size = 50
    tags_json = gh_api(
        f"repos/{_FORGE_GITHUB_REPO}/tags?per_page={page_size}",
        "--jq",
        "[.[] | {name: .name, sha: .commit.sha}]",
    )
    if not tags_json:
        return ChannelTags(None, None)
    try:
        tags = json.loads(tags_json)
    except json.JSONDecodeError:
        return ChannelTags(None, None)

    def _branch_latest(branch: str) -> str | None:
        """Return the latest semver-shaped tag reachable from *branch*.

        Args:
            branch: Remote branch name (e.g. ``"main"`` or ``"dev"``)
                whose recent commits are scanned for tag matches.

        Returns:
            Tag name (e.g. ``"v1.2.3"``) for the most recent matching
            commit, or ``None`` when no semver tag is found in the
            scanned window.
        """
        commits_json = gh_api(
            f"repos/{_FORGE_GITHUB_REPO}/commits?sha={branch}&per_page={page_size}",
            "--jq",
            "[.[] | .sha]",
        )
        if not commits_json:
            return None
        try:
            commit_shas = set(json.loads(commits_json))
        except json.JSONDecodeError:
            return None
        # tags are newest-first per GitHub contract; first match wins.
        for entry in tags:
            if entry.get("sha") in commit_shas:
                return str(entry.get("name") or "") or None
        return None

    return ChannelTags(
        main_tag=_branch_latest(_FORGE_MAIN_BRANCH),
        dev_tag=_branch_latest(_FORGE_DEV_BRANCH),
    )


def _is_behind(installed: str | None, latest: str | None) -> bool:
    """Return ``True`` when *installed* is strictly older than *latest*.

    Args:
        installed: Installed version string, or ``None`` (treated as
            not-installed → never warns).
        latest: Latest upstream tag, or ``None`` (treated as
            unresolved → never warns).

    Returns:
        ``True`` iff both are parseable semver and installed < latest.
    """
    if not installed or not latest:
        return False
    installed_parsed = parse_semver(installed)
    latest_parsed = parse_semver(latest)
    if installed_parsed is None or latest_parsed is None:
        return False
    return installed_parsed < latest_parsed


def _render_channel_warning(
    *,
    installed: str,
    subject: str,
    tags: ChannelTags,
    upgrade_hint_main: str,
    upgrade_hint_dev: str,
) -> str | None:
    """Build the channel-aware warning text, or ``None`` when not behind.

    Honest about the cadence-vs-stability trade-off: both channels
    publish stable semver, so the wording never calls dev "beta" or
    "pre-release". Instead it labels the cadence ("every patch" vs
    "minor-only") and lets the consumer decide.

    When both channels report the same tag (e.g. immediately after a
    dev → main promotion), a single line is shown rather than two
    identical rows.

    Args:
        installed: Installed version string (e.g. forge-scripts version
            or plugin version).
        subject: Human label for the *installed* artifact — e.g.
            ``"forge-scripts"`` or ``"Claude plugin 'forge'"``.
        tags: Channel-tag snapshot from the upstream query.
        upgrade_hint_main: Shell or UI command to upgrade to ``main``.
        upgrade_hint_dev: Shell or UI command to upgrade to ``dev``.

    Returns:
        A multi-line warning string ready for ``logger.warning``, or
        ``None`` when *installed* is not behind either channel (or
        when no channel tags were resolved).
    """
    behind_main = _is_behind(installed, tags.main_tag)
    behind_dev = _is_behind(installed, tags.dev_tag)
    if not behind_main and not behind_dev:
        return None

    # Title reflects the channel-awareness: behind main = behind on the slow
    # channel they're on; behind dev only = newer patches available on the
    # fast channel (consumer is current on main).
    if behind_main:
        title = f"! {subject} is behind upstream."
    else:
        title = f"! {subject}: newer patches available on the dev channel."

    lines = [title, f"    installed: {installed}"]
    if tags.main_tag and tags.dev_tag and tags.main_tag == tags.dev_tag:
        lines.append(f"    latest (main = dev): {tags.main_tag}")
        lines.append(f"    upgrade: {upgrade_hint_main}")
    else:
        if tags.main_tag:
            lines.append(
                f"    main (slower, minor-only):   {tags.main_tag}   "
                "← fewer updates, longer bake time",
            )
        if tags.dev_tag:
            lines.append(
                f"    dev  (faster, every patch): {tags.dev_tag}   "
                "← every patch as it ships",
            )
        lines.append(
            "",
        )
        lines.append(
            "    Both channels publish stable semver — pick the cadence "
            "that fits your repo.",
        )
        if tags.main_tag:
            lines.append(f"    upgrade to main: {upgrade_hint_main}")
        if tags.dev_tag:
            lines.append(f"    upgrade to dev:  {upgrade_hint_dev}")
    # Branch-ref installs (`@main` / `@dev` pins) get a frozen pip cache
    # — the installed version's `.devN+gHASH` suffix gives them away.
    # Surface the freeze workaround explicitly.
    if ".dev" in installed and "+g" in installed:
        lines.append("")
        lines.append(
            "    Note: this looks like a branch-ref install "
            "(version carries `.devN+gHASH`). pip caches branch refs and "
            "won't refresh on its own. Run `forge-upgrade --apply` to "
            "upgrade.",
        )
    return "\n".join(lines)


def _append_channel_hint(warning: str, settings_file: Path) -> str:
    """Append a channel-switch hint to *warning* when a marketplace ref is set.

    The hint links the symptom ("plugin behind") to its likely cause
    (the upstream Claude Code marketplace ref-cache bug) by pointing
    at the workaround in ``docs/claude-code-plugin.md``. Consumers
    without an explicit channel ``ref`` cannot be hitting the
    cache-freeze symptom, so the hint is suppressed for them.

    Args:
        warning: The base plugin-behind warning text already composed
            by :func:`_render_channel_warning`.
        settings_file: Path to ``~/.claude/settings.json``.

    Returns:
        The warning with the hint appended when a marketplace ``ref``
        is configured; otherwise the warning unchanged.
    """
    configured_channel = _read_configured_channel(settings_file)
    if configured_channel is None:
        return warning
    return (
        f"{warning}\n\n"
        f"    Configured marketplace ref: '{configured_channel}'. "
        "If the plugin version stays pinned after `/plugin update "
        "forge@forge`, this is likely the marketplace ref-cache "
        "freeze — see the channel-switch section in "
        "`docs/claude-code-plugin.md` for the manual workaround."
    )


def check_upstream(
    *,
    plugins_file: Path | None = None,
    settings_file: Path | None = None,
    cache_ttl_hours: int = _UPSTREAM_CACHE_TTL_HOURS_DEFAULT,
    fetch: Callable[[], ChannelTags] = _fetch_upstream_channel_tags,
) -> None:
    """Warn (only) when the installed forge is behind either release channel.

    Channel-aware: queries the latest tag on both forge ``main`` (slow,
    minor-only) and ``dev`` (fast, every patch). A consumer pinned to
    ``main`` is not nagged about dev-only patches; a consumer pinned
    to ``dev`` sees the latest patch. The warning text explains the
    cadence trade-off so consumers can pick the channel that fits.

    When the consumer has an explicit marketplace ``ref`` configured
    in ``~/.claude/settings.json`` and the plugin warning fires, an
    extra line points at the channel-switch workaround documented in
    ``docs/claude-code-plugin.md`` — the symptom (plugin behind)
    matches the upstream Claude Code marketplace ref-cache bug whose
    workaround consumers otherwise have to discover by hand.

    Throttled to once per ``cache_ttl_hours`` via
    ``~/.cache/forge/upstream_check.json``. Network / gh-auth failures
    are non-fatal — a single ``[forge] upstream check skipped: …`` line
    is logged and the function returns without raising.

    No-ops in non-interactive contexts (CI / no-TTY, per FOUNDATION
    §15) — the warning is advisory for human dev loops and adds a
    network call that has no value to a CI runner.

    Args:
        plugins_file: Path to ``~/.claude/plugins/installed_plugins.json``.
            Default reads the real file under ``~/.claude``.
        settings_file: Path to ``~/.claude/settings.json``. Default
            reads the real file under ``~/.claude``.
        cache_ttl_hours: Throttle window. Tests pass a small value to
            bypass throttling.
        fetch: Override the upstream-query callable for tests. Must
            return :class:`ChannelTags`.
    """
    # CI-aware bypass per FOUNDATION §15: the warning is dev-loop
    # advisory; in a non-interactive context (GitHub Actions et al.)
    # it spams the log with text the runner cannot act on and adds a
    # `gh api` network call to the install path. Skip cleanly.
    if is_non_interactive():
        return

    if plugins_file is None:
        plugins_file = Path.home() / ".claude" / "plugins" / "installed_plugins.json"
    if settings_file is None:
        settings_file = Path.home() / ".claude" / "settings.json"

    cache_path = _upstream_cache_path()
    tags = _read_upstream_cache(cache_path, ttl_hours=cache_ttl_hours)
    if tags is None:
        tags = fetch()
        if tags.main_tag is None and tags.dev_tag is None:
            logger.info(
                "[forge] upstream check skipped: gh api failed or unreachable",
            )
            return
        # Partial fetches (one channel resolved, the other None) ARE
        # cached. A transient failure on the missing side will silence
        # that channel's warnings for up to ``cache_ttl_hours`` — judged
        # acceptable for an advisory check. The next post-TTL run
        # re-queries both.
        _write_upstream_cache(cache_path, tags)

    forge_scripts_version = _installed_forge_scripts_version()
    plugin_version = _installed_plugin_version(plugins_file)

    # Recommend the forge-upgrade wrapper, not raw pip. The wrapper
    # rewrites the pyproject.toml pin + runs `pip install --force-
    # reinstall --no-deps` + reruns install-forge-bootstrap so managed
    # artifacts (FOUNDATION.md, docs/cli-reference.md, .githooks/) stay
    # in sync. Raw `pip install` skips all that and trains downstream
    # agents to bypass the wrapper.
    upgrade_main = "forge-upgrade --apply --channel main"
    upgrade_dev = "forge-upgrade --apply --channel dev"
    plugin_upgrade = (
        "run `/plugin update forge@forge`, then `/reload-plugins` "
        "(or restart the session via Cmd+R / new conversation) so the "
        "new agents register. Monitor changes still need a full restart."
    )

    if forge_scripts_version:
        warning = _render_channel_warning(
            installed=forge_scripts_version,
            subject="forge-scripts",
            tags=tags,
            upgrade_hint_main=upgrade_main,
            upgrade_hint_dev=upgrade_dev,
        )
        if warning is not None:
            logger.warning(warning)

    if plugin_version:
        warning = _render_channel_warning(
            installed=plugin_version,
            subject="Claude plugin 'forge'",
            tags=tags,
            upgrade_hint_main=plugin_upgrade,
            upgrade_hint_dev=plugin_upgrade,
        )
        if warning is not None:
            logger.warning(_append_channel_hint(warning, settings_file))


# ---------------------------------------------------------------------------
# Existing CLAUDE.md helpers
# ---------------------------------------------------------------------------


def warn_claudemd_missing_include(claudemd_path: Path) -> None:
    """Log a warning when ``CLAUDE.md`` lacks the ``@FOUNDATION.md`` include.

    Foundation rules only reach Claude when ``CLAUDE.md`` references
    ``FOUNDATION.md``. Missing the include is silent failure — the user
    gets foundation content on disk that the agent never reads.

    Args:
        claudemd_path: Path to the consumer repo's ``CLAUDE.md``.
    """
    if not claudemd_path.exists():
        return
    if _claudemd_has_include(claudemd_path.read_text()):
        return
    logger.warning(
        "! %s exists but does not include `@FOUNDATION.md`. "
        "Foundation rules will NOT reach Claude Code until you add "
        "`@FOUNDATION.md` near the top of the file (or run "
        "install-forge-claude-md --migrate if you have an inline-block "
        "layout from v1.1.2 or earlier).",
        claudemd_path.name,
    )


def migrate_inline_block(claudemd_path: Path) -> bool:
    """Convert a v1.1.2-style inline-block ``CLAUDE.md`` to the split layout.

    Detects the ``forge:foundation-managed`` block in ``CLAUDE.md``,
    removes it, and replaces it with the ``@FOUNDATION.md`` include
    directive. ``FOUNDATION.md`` itself is written separately by
    ``sync_foundation``; this function only edits ``CLAUDE.md``.

    Args:
        claudemd_path: Path to the consumer repo's ``CLAUDE.md``.

    Returns:
        True if ``CLAUDE.md`` was rewritten; False if no inline block was
        found (already migrated, or never had one).
    """
    if not claudemd_path.exists():
        logger.info(
            "%s does not exist — nothing to migrate. The scaffold will be "
            "written instead.",
            claudemd_path.name,
        )
        return False
    existing = claudemd_path.read_text()
    if not _has_managed_markers(existing):
        if _claudemd_has_include(existing):
            logger.info("✓ %s already on split layout", claudemd_path.name)
            return False
        logger.warning(
            "! %s has neither an inline managed block nor an "
            "`@FOUNDATION.md` include. Add the include manually near the "
            "top of the file.",
            claudemd_path.name,
        )
        return False

    start_match = START_MARKER_RE.search(existing)
    end_match = END_MARKER_RE.search(existing)
    if start_match is None or end_match is None:  # _has_managed_markers verified.
        return False
    pre = existing[: start_match.start()]
    end_pos = end_match.end()
    if existing[end_pos : end_pos + 1] == "\n":
        # Consume the newline that terminates the END marker line so the
        # replacement doesn't leave a blank line where the marker was.
        end_pos += 1
    post = existing[end_pos:]

    # Replace the block with the include directive + a separator. Strip
    # any leading blank lines in `post` so we don't stack newlines.
    replacement = f"{INCLUDE_DIRECTIVE}\n\n---\n\n"
    post = post.lstrip("\n")
    new_content = f"{pre}{replacement}{post}"
    claudemd_path.write_text(new_content)
    logger.info(
        "✓ migrated %s: inline block replaced with `%s`",
        claudemd_path.name,
        INCLUDE_DIRECTIVE,
    )
    return True


def main() -> int:
    """CLI entry point.

    Returns:
        ``0`` on success or already-in-sync. ``1`` in ``--check`` mode
        when ``FOUNDATION.md`` drift is detected.
    """
    parser = argparse.ArgumentParser(
        prog="install-forge-claude-md",
        description=(
            "Sync the forge foundation into this repo. Writes/updates "
            "FOUNDATION.md (managed by forge); scaffolds CLAUDE.md with "
            "an `@FOUNDATION.md` include if it doesn't exist; creates "
            "`.claude/hooks/` with a README documenting the "
            "`${CLAUDE_PROJECT_DIR}/.claude/hooks/<name>.sh` path "
            "convention; writes a minimal `.claude/settings.json` if "
            "missing. Existing consumer-owned files are never touched."
        ),
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help=(
            "Exit non-zero if FOUNDATION.md drifts from the installed "
            "forge version. Also warns if CLAUDE.md exists without the "
            "`@FOUNDATION.md` include."
        ),
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress 'already in sync' info logs (intended for git hooks).",
    )
    parser.add_argument(
        "--migrate",
        action="store_true",
        help=(
            "Convert a v1.1.2-or-earlier inline-block CLAUDE.md to the "
            "split layout (FOUNDATION.md + @FOUNDATION.md include)."
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Overwrite an existing FOUNDATION.md that lacks the "
            "forge-managed markers. Use sparingly."
        ),
    )
    args = parser.parse_args()

    if args.quiet:
        logger.setLevel(logging.WARNING)

    root = repo_root()
    foundation_path = root / "FOUNDATION.md"
    claudemd_path = root / "CLAUDE.md"

    if args.migrate:
        migrate_inline_block(claudemd_path)

    changed = sync_foundation(
        foundation_path,
        check_only=args.check,
        force=args.force,
    )

    if not args.check and not claudemd_path.exists():
        scaffold_claudemd(claudemd_path)
    else:
        warn_claudemd_missing_include(claudemd_path)

    # Consumer Claude Code hooks scaffold: ensure .claude/hooks/ exists
    # with the path-convention README, and write a minimal settings.json
    # if missing. Documents the `${CLAUDE_PROJECT_DIR}/.claude/hooks/<n>.sh`
    # pattern so consumers don't ship broken relative paths. No-op when
    # the files already exist — consumer owns them.
    if not args.check:
        ensure_claude_hooks_dir(root / ".claude" / "hooks")
        scaffold_claude_settings(root / ".claude" / "settings.json")

    # Upstream-version drift warning. Runs in both default and --check
    # modes so the post-checkout / post-merge hooks (which call --check)
    # surface "you're N versions behind" automatically. Warning-only:
    # never changes the exit code, never auto-installs.
    check_upstream()

    if args.check and changed:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
