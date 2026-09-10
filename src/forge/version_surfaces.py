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

A consumer repo ships no manifest, so it has no declared version to
compare — the module also reads Claude Code's
``known_marketplaces.json`` registry to resolve the marketplace clone a
consumer's pin points at, and judges the cache slot by comparing that
clone's content against what is loaded instead.
"""

from __future__ import annotations

import json
from importlib import metadata
from pathlib import Path
from typing import Final, NamedTuple

from forge.git_utils import FORGE_DIST_NAME, parse_semver
from forge.install_githooks import SIDECAR_NAME as HOOK_VERSION_SIDECAR
from forge.upgrade import find_pin


DIST_NAME: Final[str] = FORGE_DIST_NAME

# Registry Claude Code keeps of every marketplace it has cloned, mapping a
# marketplace name to its source repo and the local clone path. Reading it
# is what lets a consumer — which ships no manifest of its own — say what
# content it actually pinned.
KNOWN_MARKETPLACES: Final[Path] = (
    Path.home() / ".claude" / "plugins" / "known_marketplaces.json"
)

# Remediation per surface — the single command that re-converges that one
# onto the current line. Doctor prints it as advice; precommit prints it
# as the way past a block. One string, so the two can never disagree.
SKEW_REMEDIATION: Final[dict[str, str]] = {
    "pip package": "forge-upgrade --apply",
    "git hooks": "install-forge-githooks",
    "plugin cache": "/plugin update forge@forge (then /reload-plugins)",
}

# The remediation for a cache slot holding stale *content*. It cannot be
# ``/plugin update``: that command compares declared manifest versions, and
# a frozen declared version is exactly the condition here — the update
# reports "already current" while the slot keeps whatever it was first
# filled with. Only discarding the slot (or moving the clone it is filled
# from) converges.
STALE_CACHE_REMEDIATION: Final[str] = (
    "delete ~/.claude/plugins/cache/{plugin}/ and restart the session "
    "(move the marketplace ref first if the clone is behind too) — "
    "`/plugin update` compares declared versions and reports no change"
)


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
    return install_version(install_dir)


def install_version(install_dir: Path) -> str:
    """Version a plugin install directory reports.

    Args:
        install_dir: Directory carrying ``.claude-plugin/plugin.json``.

    Returns:
        The manifest's ``version`` field, falling back to the directory
        name — Claude Code names each cache slot after the version it was
        filled for, so the name answers even when the manifest does not.
    """
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


def marketplace_clone(repo_slug: str) -> Path | None:
    """Local clone Claude Code keeps for the marketplace serving *repo_slug*.

    Claude Code records every marketplace it has fetched in
    ``~/.claude/plugins/known_marketplaces.json``, keyed by marketplace
    name, each entry carrying ``source.repo`` (``owner/repo``) and
    ``installLocation`` (a real git checkout at the ref the consumer
    registered). Matching on the source repo rather than the marketplace
    name keeps the lookup tied to what the pin names.

    Args:
        repo_slug: ``owner/repo`` the consumer's pin points at.

    Returns:
        The clone directory, or ``None`` when the registry is absent,
        unreadable, carries no entry for *repo_slug*, or names a path
        that no longer exists. Every degradation is "unknown", never an
        exception — this reader runs inside an advisory.
    """
    try:
        data, err = read_json(KNOWN_MARKETPLACES)
    except OSError:
        return None
    if err is not None or not isinstance(data, dict):
        return None
    for entry in data.values():
        if not isinstance(entry, dict):
            continue
        source = entry.get("source")
        if not isinstance(source, dict) or source.get("repo") != repo_slug:
            continue
        location = entry.get("installLocation")
        if isinstance(location, str) and Path(location).is_dir():
            return Path(location)
    return None


def _repo_slug(url: str) -> str | None:
    """Return the ``owner/repo`` a git pin URL names.

    Args:
        url: The ``git+...`` URL portion of a pin (no ref).

    Returns:
        ``"owner/repo"``, or ``None`` when the URL carries no such pair.
    """
    cleaned = url.strip().removeprefix("git+").removesuffix(".git")
    if "://" in cleaned:
        cleaned = cleaned.split("://", 1)[1].partition("/")[2]
    elif ":" in cleaned:  # scp-style git@host:owner/repo
        cleaned = cleaned.rsplit(":", 1)[1]
    owner, _, repo = cleaned.strip("/").rpartition("/")
    if not owner or not repo:
        return None
    return f"{owner.rsplit('/', 1)[-1]}/{repo}"


def _hook_names(plugin_dir: Path) -> frozenset[str]:
    """Names of the Claude Code hooks a plugin directory ships.

    Args:
        plugin_dir: Root of a plugin tree (clone or cache install).

    Returns:
        The file names under ``claude-hooks/``, empty when the directory
        is absent — the hook set is the comparison surface because a
        missing hook is what a consumer actually feels.
    """
    hooks = plugin_dir / "claude-hooks"
    if not hooks.is_dir():
        return frozenset()
    return frozenset(entry.name for entry in hooks.iterdir() if entry.is_file())


class PluginCacheStatus(NamedTuple):
    """What the Claude Code plugin cache says relative to what ships it.

    Attributes:
        state: ``"no-manifest"``, ``"uncached"``, ``"unparsed"``,
            ``"current"``, ``"behind"``, or ``"stale-content"`` — the
            consumer verdict, where the slot's declared version is not
            behind but its content is.
        plugin_name: Name the manifest declares, falling back to the repo
            directory's own name.
        cached: Version in the cache, when there is one.
        declared: Version the manifest declares — or, on the consumer
            branch, the ref the repo pins.
        missing_hooks: Hooks the pinned content ships that the cache slot
            does not; populated only for ``"stale-content"``.
    """

    state: str
    plugin_name: str
    cached: str | None
    declared: str | None
    missing_hooks: tuple[str, ...] = ()


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

    A repo that ships no manifest of its own is a *consumer*, and the
    population that actually suffers a frozen cache. It gets the branch in
    :func:`_consumer_cache_status`, which compares content rather than
    version strings.

    Args:
        repo_root: Repo whose ``.claude-plugin/plugin.json`` ships the plugin.

    Returns:
        A :class:`PluginCacheStatus`; ``"behind"`` and ``"stale-content"``
        are the findings.
    """
    manifest = repo_root / ".claude-plugin" / "plugin.json"
    if not manifest.is_file():
        return _consumer_cache_status(repo_root)
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


def _consumer_cache_status(repo_root: Path) -> PluginCacheStatus:
    """Compare a consumer's active cache slot against the ref it pinned.

    A consumer ships no manifest, so there is no declared version to
    compare — and under tag-per-merge with assemble-later release, the
    declared version would not identify the content anyway: no tagged
    tree's manifest equals its own tag, so materially different trees
    share one cache slot. What the consumer *did* declare is the pin, and
    the marketplace clone that pin resolves to is a real checkout of the
    pinned content. Comparing its hook set against the cache slot's asks
    the question the version string cannot answer, and names the concrete
    harm: hooks that are not loaded.

    Args:
        repo_root: Consumer repo root — searched for a ``forge-scripts``
            pin.

    Returns:
        ``"stale-content"`` when the slot is missing hooks the pinned
        content ships, ``"current"`` when it is not, ``"uncached"`` when
        nothing is installed, and ``"no-manifest"`` when the pin or the
        clone cannot be resolved at all.
    """
    pin = find_pin(repo_root)
    slug = _repo_slug(pin.url) if pin is not None else None
    clone = marketplace_clone(slug) if slug is not None else None
    source_dir = find_install_dir(clone) if clone is not None else None
    if pin is None or source_dir is None:
        return PluginCacheStatus("no-manifest", repo_root.name, None, None)
    data, _err = read_json(source_dir / ".claude-plugin" / "plugin.json")
    plugin_name = str(data.get("name") or repo_root.name)
    cache_root = find_plugin_cache(plugin_name)
    install_dir = find_install_dir(cache_root) if cache_root is not None else None
    if install_dir is None:
        return PluginCacheStatus("uncached", plugin_name, None, pin.ref)
    cached = install_version(install_dir)
    missing = tuple(sorted(_hook_names(source_dir) - _hook_names(install_dir)))
    if not missing:
        return PluginCacheStatus("current", plugin_name, cached, pin.ref)
    return PluginCacheStatus("stale-content", plugin_name, cached, pin.ref, missing)
