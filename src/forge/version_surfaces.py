"""The three surfaces a forge install presents, read once for every checker.

One install exposes its version three ways — the pip ``forge-scripts``
distribution, the gitignored ``.githooks/.forge-hook-version`` sidecar the
hook installer writes, and the cached Claude Code plugin — and they drift
independently: a pull adds an entry point the install lacks, a failed
self-refresh leaves the hooks behind, a merged plugin release sits unloaded
in the cache. ``forge-doctor`` reports the skew as an advisory and
``forge-precommit``'s ``env_sync`` / ``plugin_sync`` steps block on it, so
the readers live here once and both consumers see the same numbers and
name the same remediation.
"""

from __future__ import annotations

import json
from importlib import metadata
from pathlib import Path
from typing import Final, NamedTuple

from forge.git_utils import FORGE_DIST_NAME, parse_semver
from forge.install_githooks import SIDECAR_NAME as HOOK_VERSION_SIDECAR


DIST_NAME: Final[str] = FORGE_DIST_NAME

# Remediation per surface — the single command that re-converges that one
# onto the current line. Doctor prints it as advice; precommit prints it
# as the way past a block. One string, so the two can never disagree.
SKEW_REMEDIATION: Final[dict[str, str]] = {
    "pip package": "forge-upgrade --apply",
    "git hooks": "install-forge-githooks",
    "plugin cache": "/plugin update forge@forge (then /reload-plugins)",
}


def read_json(path: Path) -> tuple[dict, str | None]:
    """Read a JSON file. Returns (data, error_message_or_None).

    Args:
        path: Path to the JSON file to read.

    Returns:
        Tuple of (parsed JSON data dict, error message or None).
    """
    if not path.is_file():
        return {}, f"missing: {path}"
    try:
        with path.open() as fh:
            return json.load(fh), None
    except json.JSONDecodeError as exc:
        return {}, f"invalid JSON in {path}: {exc}"


def version_key(name: str) -> tuple[int, ...]:
    """Return a sortable key for a version-shaped directory name.

    Args:
        name: Directory name (typically a bare semver like ``"1.13.0"``;
            falls back to a tuple of zeros when the name isn't
            version-shaped so the comparison degrades gracefully).

    Returns:
        Tuple of integers — ``(1, 13, 0)`` for ``"1.13.0"``,
        ``(0,)`` for any non-numeric name. Comparing tuples
        component-wise gives correct semver ordering (``1.13`` > ``1.9``,
        which lexicographic string compare gets wrong).
    """
    parts = name.split(".")
    try:
        return tuple(int(p) for p in parts)
    except ValueError:
        return (0,)


def find_plugin_cache(plugin_name: str) -> Path | None:
    r"""Locate a Claude Code plugin cache directory by name.

    Only checks the canonical ``~/.claude/plugins/cache/<plugin>`` path
    that Claude Code populates on ``/plugin install``. The marketplace
    source dir (``~/.claude/plugins/marketplaces/...``) is intentionally
    not searched here: marketplace dir names are ``<org>-<plugin>``,
    and embedding an org prefix in this lookup would tie the lookup to a
    single ``<org>/<plugin>`` source.

    Args:
        plugin_name: Plugin identifier (e.g. ``"forge"``).

    Returns:
        Absolute path to the plugin cache if found, otherwise ``None`` —
        also for a path-shaped name (``/``, ``\\``, ``..``), which is
        never a plugin identifier.
    """
    # The name comes from a repo's own manifest; a path-shaped value must
    # not walk the lookup out of the cache directory.
    if (
        not plugin_name
        or "/" in plugin_name
        or "\\" in plugin_name
        or ".." in plugin_name
    ):
        return None
    cache = Path.home() / ".claude" / "plugins" / "cache" / plugin_name
    return cache if cache.is_dir() else None


def find_install_dir(plugin_root: Path) -> Path | None:
    """Walk the Claude Code cache layout to find the active plugin install.

    Claude Code stores installed plugins under
    ``~/.claude/plugins/cache/<plugin>/<plugin>/<version>/`` — two levels
    nested below the cache slot, with one directory per cached version.
    Older versions and forks may flatten to one level or none. Walk
    up to two levels looking for the first directory that carries a
    ``.claude-plugin/plugin.json``; when multiple versions are
    present, pick the one with the highest semver-shaped name.

    Args:
        plugin_root: Cache slot for the plugin
            (``~/.claude/plugins/cache/<plugin>``).

    Returns:
        Path of the directory carrying ``.claude-plugin/plugin.json`` (the
        install root for diagnostics), or ``None`` when no valid layout is
        found at any depth.
    """
    candidates: list[Path] = []
    for depth_glob in (".claude-plugin", "*/.claude-plugin", "*/*/.claude-plugin"):
        candidates.extend(plugin_root.glob(depth_glob))
    valid = [c.parent for c in candidates if (c / "plugin.json").is_file()]
    if not valid:
        return None
    return max(valid, key=lambda p: version_key(p.name))


def pip_version() -> str | None:
    """Version of the installed ``forge-scripts`` package, or None if absent."""
    try:
        return metadata.version(DIST_NAME)
    except metadata.PackageNotFoundError:
        return None


def hook_sidecar_version(repo_root: Path) -> str | None:
    """Forge version recorded in the git-hook sidecar, or None when absent.

    Reads the gitignored ``.githooks/.forge-hook-version`` sidecar written by
    ``install-forge-githooks`` (the tracked hook *marker* deliberately omits
    the version to stay byte-stable across bumps — see that CLI). A repo whose
    hooks aren't forge-managed simply has no sidecar and is skipped.

    Args:
        repo_root: Directory whose ``.githooks/`` is inspected.

    Returns:
        The recorded version string (may carry a ``.devN+g<sha>`` suffix), or
        None when the sidecar is missing or empty.
    """
    sidecar = repo_root / ".githooks" / HOOK_VERSION_SIDECAR
    if not sidecar.is_file():
        return None
    return sidecar.read_text(encoding="utf-8").strip() or None


def plugin_cache_version(plugin_root: Path | None) -> str | None:
    """Version of the cached Claude Code plugin install, or None when absent.

    Prefers the ``version`` field of the installed ``plugin.json`` (robust to
    a flattened cache layout) and falls back to the cache directory name.

    Args:
        plugin_root: Cache slot for the plugin, or None when uncached.

    Returns:
        The cached plugin's version string, or None when no install is found.
    """
    if plugin_root is None:
        return None
    install_dir = find_install_dir(plugin_root)
    if install_dir is None:
        return None
    data, err = read_json(install_dir / ".claude-plugin" / "plugin.json")
    if err is None and data.get("version"):
        return str(data["version"])
    return install_dir.name


def editable_install_origin() -> Path | None:
    """Return the checkout an editable ``forge-scripts`` install points at.

    Reads the distribution's PEP 610 ``direct_url.json``: an editable
    install records ``{"dir_info": {"editable": true}, "url":
    "file:///…"}``. Parallel dev clones each own a conda env, and nothing
    else tells a developer that the env on ``PATH`` was built from a
    *different* clone — so the pre-commit gate compares this path to the
    repo it runs in.

    Returns:
        The resolved local path, or ``None`` when the distribution is
        missing, the file is absent or malformed, or the install is not
        editable (a git/index install has no clone to compare against).
    """
    data = _direct_url()
    if data is None:
        return None
    dir_info = data.get("dir_info")
    url = data.get("url")
    if not isinstance(dir_info, dict) or not dir_info.get("editable"):
        return None
    if not isinstance(url, str) or not url.startswith("file://"):
        return None
    return Path(url.removeprefix("file://")).resolve()


def _direct_url() -> dict[str, object] | None:
    """Return the distribution's parsed ``direct_url.json``, or ``None``.

    Returns:
        The JSON object, or ``None`` when the distribution is missing, the
        file is absent or empty, or it is not a JSON object.
    """
    try:
        raw = metadata.distribution(DIST_NAME).read_text("direct_url.json")
    except metadata.PackageNotFoundError:
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


class PluginCacheStatus(NamedTuple):
    """What the Claude Code plugin cache says relative to a repo's manifest.

    Attributes:
        state: ``"no-manifest"``, ``"uncached"``, ``"unparsed"``,
            ``"current"`` or ``"behind"``.
        plugin_name: Name the manifest declares, falling back to the repo
            directory's own name.
        cached: Version in the cache, when there is one.
        declared: Version the manifest declares, when it declares one.
    """

    state: str
    plugin_name: str
    cached: str | None
    declared: str | None


def plugin_cache_status(repo_root: Path) -> PluginCacheStatus:
    """Compare the cached plugin against the manifest that ships it.

    One reader for a question two checks ask: the ``plugin_sync``
    pre-commit step, which may refuse a commit, and ``forge-doctor``,
    which reports an advisory. Both need the same verdict and the same
    remediation; only what they do with it differs.

    The manifest is the plugin's version. Comparing the cache against the
    pip package's instead — as the doctor once did — asks about two
    numbers that are parked apart by design, which produced a warning no
    command could clear.

    Args:
        repo_root: Repo whose ``.claude-plugin/plugin.json`` ships the plugin.

    Returns:
        A :class:`PluginCacheStatus`; only ``"behind"`` is a finding.
    """
    manifest = repo_root / ".claude-plugin" / "plugin.json"
    if not manifest.is_file():
        return PluginCacheStatus("no-manifest", repo_root.name, None, None)
    data, _err = read_json(manifest)
    plugin_name = str(data.get("name") or repo_root.name)
    declared = str(data["version"]) if data.get("version") else None
    cached = plugin_cache_version(find_plugin_cache(plugin_name))
    if cached is None:
        return PluginCacheStatus("uncached", plugin_name, None, declared)
    cached_t = parse_semver(cached)
    declared_t = parse_semver(declared or "")
    if cached_t is None or declared_t is None:
        return PluginCacheStatus("unparsed", plugin_name, cached, declared)
    state = "current" if cached_t >= declared_t else "behind"
    return PluginCacheStatus(state, plugin_name, cached, declared)
