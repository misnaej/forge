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

Which cached copy to judge is itself a question: one machine holds a
copy per version some repo fetched, and Claude Code records which copy
each repo uses in ``installed_plugins.json``. The module reads that
record so a repo is judged on its own copy rather than on whichever is
newest.
"""

from __future__ import annotations

import hashlib
import json
from importlib import metadata
from pathlib import Path
from typing import Final, NamedTuple

from forge.claude_settings_schema import MARKETPLACE_KEY, read_marketplace_ref
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

# Claude Code's record of every plugin install on this machine: per plugin
# key, one entry per scope — ``user`` (every repo) or a ``projectPath``
# (one repo) — naming the cache directory that install loads. It is the
# only place that says which of several cached copies a given repo uses.
INSTALLED_PLUGINS: Final[Path] = (
    Path.home() / ".claude" / "plugins" / "installed_plugins.json"
)

# The plugin tree areas whose content decides what a session runs. The
# manifest is compared too, minus its ``version``: fragments mode parks
# that field at the latest tag, so trees that differ only there are the
# same content.
CONTENT_AREAS: Final[tuple[str, ...]] = ("agents", "skills", "claude-hooks")
MANIFEST_AREA: Final[str] = ".claude-plugin/plugin.json"

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

# The remediation when the machine-wide marketplace registration points at
# a different ref than this repo pins. Every repo on the machine updates
# from that one registration, so ``/plugin update`` fetches from the wrong
# ref and no per-repo command converges; only re-pointing the
# registration does — and that moves the source for every repo.
SOURCE_MISMATCH_REMEDIATION: Final[str] = (
    "`/plugin update` cannot fix this — it fetches from the registered "
    "source. Re-point the machine-wide marketplace at the ref this repo "
    "pins (`/plugin marketplace remove {plugin}`, add it again at that "
    "ref, then `/plugin install {plugin}@{plugin}` — forge's "
    "docs/claude-code-plugin.md, 'Changing the marketplace ref'). The "
    "registration is shared, so this changes the source for every repo "
    "on this machine"
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
    for entry in _marketplace_entries(repo_slug):
        location = entry.get("installLocation")
        if isinstance(location, str) and Path(location).is_dir():
            return Path(location)
    return None


def registered_marketplace_ref(repo_slug: str) -> str | None:
    """Ref the machine-wide marketplace registration for *repo_slug* tracks.

    The registry is per machine, not per repo: the first repo to register
    a marketplace sets the ``source.ref`` every other repo then updates
    from, whatever ref those repos pin.

    Args:
        repo_slug: ``owner/repo`` the consumer's pin points at.

    Returns:
        The first matching entry's ``source.ref``, or ``None`` when the
        registry is absent or unreadable, no entry serves *repo_slug*, or
        the entry names no ref (it then tracks the default branch, which
        this reader cannot name — "unknown", not a mismatch).
    """
    for entry in _marketplace_entries(repo_slug):
        source = entry.get("source")
        ref = source.get("ref") if isinstance(source, dict) else None
        return ref if isinstance(ref, str) and ref else None
    return None


def _marketplace_entries(repo_slug: str) -> list[dict[str, object]]:
    """Registry entries whose source is the GitHub repo *repo_slug*.

    Args:
        repo_slug: ``owner/repo`` to match against ``source.repo``.

    Returns:
        Matching entries in registry order; empty when the registry is
        absent, unreadable or not a JSON object.
    """
    try:
        data, err = read_json(KNOWN_MARKETPLACES)
    except OSError:
        return []
    if err is not None or not isinstance(data, dict):
        return []
    return [
        entry
        for entry in data.values()
        if isinstance(entry, dict)
        and isinstance(entry.get("source"), dict)
        and entry["source"].get("repo") == repo_slug
    ]


class PluginInstall(NamedTuple):
    """One install record from ``installed_plugins.json``.

    Attributes:
        scope: The record's scope — ``"user"`` for a machine-wide install,
            otherwise the per-repo scope Claude Code wrote (``"project"``
            or ``"local"``).
        version: The version the record names, falling back to what the
            install directory's manifest declares.
        install_dir: The cache directory the install loads from.
    """

    scope: str
    version: str
    install_dir: Path


class PluginInstalls(NamedTuple):
    """Which installed copies of a plugin bear on one repo.

    Attributes:
        repo: The record whose ``projectPath`` is this repo, if any.
        user: The first user-scope record, if any — reported, never used
            in place of the repo's own: whether it or the repo's record
            loads is Claude Code's decision, and this reader cannot see it.
        install_dir: The copy to judge — the repo record's, else the
            newest cached copy.
        fallback: ``True`` when ``install_dir`` is the newest cached copy
            because no record for this repo exists, so the verdict may
            describe another repo's copy.
    """

    repo: PluginInstall | None
    user: PluginInstall | None
    install_dir: Path | None
    fallback: bool


def plugin_records(
    plugin: str, plugins_file: Path | None = None
) -> list[dict[str, object]]:
    """Raw install records Claude Code keeps for *plugin*.

    Two shapes are seen on disk for one plugin key — a list of records,
    one per scope, or a single record — and both are accepted.

    Args:
        plugin: A full ``name@marketplace`` key, matched exactly, or a bare
            plugin name, matching that name from any marketplace.
        plugins_file: Record file to read; defaults to
            :data:`INSTALLED_PLUGINS`.

    Returns:
        The records as dicts in file order; empty when the file is absent,
        unreadable or malformed — never an exception, since every caller
        runs inside an advisory.
    """
    path = INSTALLED_PLUGINS if plugins_file is None else plugins_file
    try:
        data, err = read_json(path)
    except (OSError, UnicodeDecodeError):
        return []
    plugins = data.get("plugins") if err is None and isinstance(data, dict) else None
    if not isinstance(plugins, dict):
        return []
    records: list[dict[str, object]] = []
    for key, value in plugins.items():
        if not isinstance(key, str) or (
            key != plugin and key.partition("@")[0] != plugin
        ):
            continue
        entries = value if isinstance(value, list) else [value]
        records.extend(entry for entry in entries if isinstance(entry, dict))
    return records


def plugin_installs(repo_root: Path, plugin_name: str) -> PluginInstalls:
    """Find the installed copies of *plugin_name* that bear on *repo_root*.

    A machine holds one cached copy per version some repo fetched, and
    each repo keeps the copy it last fetched; judging a repo by the newest
    copy reported every repo as current whatever it actually ran.

    Args:
        repo_root: Repo whose own install record is wanted.
        plugin_name: Bare plugin name (e.g. ``"forge"``).

    Returns:
        A :class:`PluginInstalls`. Records whose install directory no
        longer exists are ignored.
    """
    target = _resolved(repo_root)
    repo: PluginInstall | None = None
    user: PluginInstall | None = None
    for record in plugin_records(plugin_name):
        install = _install_from_record(record)
        if install is None:
            continue
        if install.scope == "user":
            user = user or install
            continue
        project = record.get("projectPath")
        if repo is None and isinstance(project, str):
            repo = install if _resolved(Path(project)) == target else None
    if repo is not None:
        return PluginInstalls(repo, user, repo.install_dir, fallback=False)
    cache_root = find_plugin_cache(plugin_name)
    newest = find_install_dir(cache_root) if cache_root is not None else None
    return PluginInstalls(None, user, newest, fallback=newest is not None)


def _install_from_record(record: dict[str, object]) -> PluginInstall | None:
    """Build a :class:`PluginInstall` from one raw record.

    Args:
        record: One element of a plugin key's records.

    Returns:
        The install, or ``None`` when the record names no existing
        install directory.
    """
    location = record.get("installPath")
    if not isinstance(location, str) or not location:
        return None
    install_dir = Path(location)
    if not install_dir.is_dir():
        return None
    scope = record.get("scope")
    version = record.get("version")
    return PluginInstall(
        scope=scope if isinstance(scope, str) else "",
        version=str(version) if version else install_version(install_dir),
        install_dir=install_dir,
    )


def _resolved(path: Path) -> Path:
    """Return *path* resolved, or unchanged when resolution fails.

    Args:
        path: Path to resolve.

    Returns:
        The resolved path — so a symlinked checkout matches the path
        Claude Code recorded — or *path* itself on an ``OSError``.
    """
    try:
        return path.resolve()
    except OSError:
        return path


def _repo_slug(url: str) -> str | None:
    """Return the ``owner/repo`` a git pin URL names.

    Splitting on ``://`` drops any embedded credentials with the host, so
    a token in the pin never reaches the returned slug. A path with more
    than two segments is returned whole rather than trimmed to its last
    two: every caller resolves a GitHub pin, and a longer path simply
    fails to match, which degrades the check to "unknown" — the safe
    direction. Guessing which segment to drop would instead match the
    wrong repository.

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
    return f"{owner}/{repo}"


def content_digests(plugin_dir: Path) -> dict[str, str]:
    """Hash each content area of a plugin tree.

    Comparing names alone called a rewritten agent, or a hook fixed under
    the same name, current; hashing the bytes does not. The two trees
    compared are a git clone and a cache copy, so only the areas a session
    loads are hashed — the clone's ``.git/`` and anything else outside
    them never count.

    Args:
        plugin_dir: Root of a plugin tree (clone or cache install).

    Returns:
        A sha256 hex digest per area in :data:`CONTENT_AREAS`, plus one
        for :data:`MANIFEST_AREA` with its ``version`` field removed. An
        absent area hashes as empty.
    """
    digests = {area: _area_digest(plugin_dir / area) for area in CONTENT_AREAS}
    digests[MANIFEST_AREA] = _manifest_digest(plugin_dir / MANIFEST_AREA)
    return digests


def _area_digest(area: Path) -> str:
    """Hash every file under *area* by relative path and bytes.

    Args:
        area: Directory to hash.

    Returns:
        The sha256 hex digest. Bytecode caches are skipped: a session that
        runs a Python hook writes them into the cache copy, and they say
        nothing about the content shipped.
    """
    digest = hashlib.sha256()
    if not area.is_dir():
        return digest.hexdigest()
    files = sorted(
        path
        for path in area.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    )
    for path in files:
        digest.update(path.relative_to(area).as_posix().encode())
        digest.update(b"\0")
        try:
            digest.update(path.read_bytes())
        except OSError:
            digest.update(b"<unreadable>")
        digest.update(b"\0")
    return digest.hexdigest()


def _manifest_digest(manifest: Path) -> str:
    """Hash a plugin manifest with its ``version`` field removed.

    Args:
        manifest: The ``plugin.json`` to hash.

    Returns:
        The sha256 hex digest of the canonical JSON, or of the raw bytes
        when the file is not a JSON object; an absent or unreadable file
        hashes as empty.
    """
    try:
        raw = manifest.read_bytes()
    except OSError:
        return hashlib.sha256().hexdigest()
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return hashlib.sha256(raw).hexdigest()
    if not isinstance(data, dict):
        return hashlib.sha256(raw).hexdigest()
    data.pop("version", None)
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _repo_marketplace_ref(repo_root: Path) -> str | None:
    """Marketplace ref this repo's own ``.claude/settings.json`` pins.

    Args:
        repo_root: Repo whose project settings are read.

    Returns:
        The ref, or ``None`` when the file is absent, unreadable or sets
        none.
    """
    try:
        data, err = read_json(repo_root / ".claude" / "settings.json")
    except (OSError, UnicodeDecodeError):
        return None
    if err is not None or not isinstance(data, dict):
        return None
    return read_marketplace_ref(data)


class PluginCacheStatus(NamedTuple):
    """What the Claude Code plugin cache says relative to what ships it.

    Attributes:
        state: ``"no-manifest"``, ``"uncached"``, ``"unparsed"``,
            ``"current"``, ``"behind"``, ``"stale-content"`` — the
            consumer verdict, where the slot's declared version is not
            behind but its content is — or ``"source-mismatch"``, the
            consumer verdict where the machine-wide marketplace tracks a
            different ref than the repo pins.
        plugin_name: Name the manifest declares, falling back to the repo
            directory's own name.
        cached: Version in the cache, when there is one.
        declared: Version the manifest declares — or, on the consumer
            branch, the ref the repo pins.
        stale_areas: Content areas (:data:`CONTENT_AREAS` and
            :data:`MANIFEST_AREA`) whose cached copy differs from the
            pinned content; populated only for ``"stale-content"``.
        source_ref: Ref this repo pins for the marketplace; populated only
            for ``"source-mismatch"``.
        registered_ref: Ref the machine-wide marketplace registration
            tracks; populated only for ``"source-mismatch"``.
        fallback: ``True`` when no install record names this repo, so
            ``cached`` describes the newest cached copy rather than this
            repo's own.
    """

    state: str
    plugin_name: str
    cached: str | None
    declared: str | None
    stale_areas: tuple[str, ...] = ()
    source_ref: str | None = None
    registered_ref: str | None = None
    fallback: bool = False


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

    Either branch judges this repo's own installed copy when Claude Code
    has a record of one, and the newest cached copy otherwise — flagged in
    the result, since that copy may be another repo's.

    Args:
        repo_root: Repo whose ``.claude-plugin/plugin.json`` ships the plugin.

    Returns:
        A :class:`PluginCacheStatus`; ``"behind"``, ``"stale-content"`` and
        ``"source-mismatch"`` are the findings.
    """
    manifest = repo_root / ".claude-plugin" / "plugin.json"
    if not manifest.is_file():
        return _consumer_cache_status(repo_root)
    data, _err = read_json(manifest)
    plugin_name = str(data.get("name") or repo_root.name)
    declared = str(data["version"]) if data.get("version") else None
    own = plugin_installs(repo_root, plugin_name).repo
    if own is not None:
        cached: str | None = own.version
    else:
        cached = plugin_cache_version(find_plugin_cache(plugin_name))
    fallback = own is None and cached is not None
    if cached is None:
        return PluginCacheStatus("uncached", plugin_name, None, declared)
    cached_t = parse_semver(cached)
    declared_t = parse_semver(declared or "")
    if cached_t is None or declared_t is None:
        return PluginCacheStatus(
            "unparsed", plugin_name, cached, declared, fallback=fallback
        )
    state = "current" if cached_t >= declared_t else "behind"
    return PluginCacheStatus(state, plugin_name, cached, declared, fallback=fallback)


def _consumer_cache_status(repo_root: Path) -> PluginCacheStatus:
    """Compare a consumer's active cache slot against the ref it pinned.

    A consumer ships no manifest, so there is no declared version to
    compare — and under tag-per-merge with assemble-later release, the
    declared version would not identify the content anyway: no tagged
    tree's manifest equals its own tag, so materially different trees
    share one cache slot. What the consumer *did* declare is the pin, and
    the marketplace clone that pin resolves to is a real checkout of the
    pinned content. Comparing its content against the cache slot's, area
    by area, asks the question the version string cannot answer, and
    names what differs.

    The clone only holds the pinned content when the machine-wide
    marketplace tracks the ref this repo pins. When it tracks another, the
    comparison would judge the slot against the wrong tree, so the
    mismatch is the verdict instead.

    Args:
        repo_root: Consumer repo root — searched for a ``forge-scripts``
            pin.

    Returns:
        ``"source-mismatch"`` when the registered marketplace tracks a
        different ref than the repo pins, ``"stale-content"`` when any
        content area differs from the pinned content, ``"current"`` when
        none does, ``"uncached"`` when nothing is installed, and
        ``"no-manifest"`` when the pin or the clone cannot be resolved.
    """
    pin = find_pin(repo_root)
    slug = _repo_slug(pin.url) if pin is not None else None
    if pin is None or slug is None:
        return PluginCacheStatus("no-manifest", repo_root.name, None, None)
    clone = marketplace_clone(slug)
    source_dir = find_install_dir(clone) if clone is not None else None
    data, _err = (
        read_json(source_dir / ".claude-plugin" / "plugin.json")
        if source_dir is not None
        else ({}, None)
    )
    # A forge-scripts pin always names the forge plugin, so its marketplace
    # name is the right fallback when the clone cannot say.
    plugin_name = str(data.get("name") or MARKETPLACE_KEY)
    source_ref = _repo_marketplace_ref(repo_root) or pin.ref
    registered_ref = registered_marketplace_ref(slug)
    if source_ref and registered_ref and source_ref != registered_ref:
        return PluginCacheStatus(
            "source-mismatch",
            plugin_name,
            None,
            pin.ref,
            source_ref=source_ref,
            registered_ref=registered_ref,
        )
    if source_dir is None:
        return PluginCacheStatus("no-manifest", repo_root.name, None, None)
    installs = plugin_installs(repo_root, plugin_name)
    if installs.install_dir is None:
        return PluginCacheStatus("uncached", plugin_name, None, pin.ref)
    cached = install_version(installs.install_dir)
    source, loaded = content_digests(source_dir), content_digests(installs.install_dir)
    stale = tuple(sorted(area for area in source if source[area] != loaded[area]))
    return PluginCacheStatus(
        "stale-content" if stale else "current",
        plugin_name,
        cached,
        pin.ref,
        stale_areas=stale,
        fallback=installs.fallback,
    )
