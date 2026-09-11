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

Which plugin is installed, and from which commit, is Claude Code's own
state: ``installed_plugins.json`` records each install (its cache slot and
source commit) and ``known_marketplaces.json`` each registered marketplace
(its clone and tracked ref). A manifest that declares no version is keyed
on its commit, so the plugin verdict compares commits, not version
strings: a plugin-shipping repo against its base branch, a consumer — which
ships no manifest of its own — against the clone its pin resolves to.
"""

from __future__ import annotations

import json
import re
import shlex
from importlib import metadata
from pathlib import Path
from typing import Final, NamedTuple

from forge.config import load_config
from forge.git_utils import (
    FORGE_DIST_NAME,
    is_ancestor,
    parse_semver,
    paths_differ,
    plugin_manifest_declares_version,
    resolve_base_branch_ref,
    resolve_commit,
)
from forge.install_githooks import SIDECAR_NAME as HOOK_VERSION_SIDECAR
from forge.upgrade import find_pin


DIST_NAME: Final[str] = FORGE_DIST_NAME

# Registry Claude Code keeps of every marketplace it has cloned, mapping a
# marketplace name to its source repo, tracked ref and local clone path.
# Claude Code keeps one entry per name per user, so a repo's own pin is
# only served when the registration tracks that same ref.
KNOWN_MARKETPLACES: Final[Path] = (
    Path.home() / ".claude" / "plugins" / "known_marketplaces.json"
)

# Registry of installed plugins: per plugin key, one record per install
# scope (user, or a project path) naming its cache slot, recorded version
# and source commit. It — not the newest-named cache slot — says which
# copy a repo runs.
INSTALLED_PLUGINS: Final[Path] = (
    Path.home() / ".claude" / "plugins" / "installed_plugins.json"
)

# What Claude Code loads from a plugin tree: a change under any of these is
# a change to the running plugin; anything else in the repo is not.
PLUGIN_SURFACE: Final[tuple[str, ...]] = (
    ".claude-plugin",
    "agents",
    "skills",
    "claude-hooks",
)

# An abbreviated or full commit SHA — what a commit-keyed install records
# as its version.
_SHA_RE: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{7,40}")

# Remediation per surface — the single command that re-converges that one
# onto the current line. Doctor prints it as advice; precommit prints it
# as the way past a block. One string, so the two can never disagree.
SKEW_REMEDIATION: Final[dict[str, str]] = {
    "pip package": "forge-upgrade --apply",
    "git hooks": "install-forge-githooks",
    "plugin cache": "/plugin update forge@forge (then /reload-plugins)",
}

# A commit-keyed plugin behind its base: the marketplace clone has to move
# to the new commit before `/plugin update` has anything to install.
REFRESH_REMEDIATION: Final[str] = (
    "/plugin marketplace update {marketplace}, then /plugin update "
    "{plugin}@{marketplace} (then /reload-plugins)"
)

# A commit-keyed plugin whose clone already moved: a plain update installs
# the new commit, because a new commit is a new version.
UPDATE_REMEDIATION: Final[str] = (
    "/plugin update {plugin}@{marketplace} (then /reload-plugins)"
)

# The remediation for a cache slot holding stale *content* under a
# *declared* version. It cannot be ``/plugin update``: that command
# compares declared manifest versions, and an unmoved declared version is
# exactly the condition here — the update reports "already current" while
# the slot keeps whatever it was first filled with. Discarding the slot
# and *installing* (not updating) refills it from the clone.
STALE_CACHE_REMEDIATION: Final[str] = (
    "delete {slot}, reinstall with `claude plugin install "
    "{plugin}@{marketplace}`, and restart the session — `/plugin update` "
    "compares declared versions and reports no change"
)

# The machine's one registration tracks a different ref than this repo
# pins. Updating cannot fix that; only re-registering at the pinned ref
# does, and removal uninstalls the plugin everywhere it came from.
WRONG_REF_REMEDIATION: Final[str] = (
    "re-point this machine's `{marketplace}` marketplace to {ref}: "
    "`claude plugin marketplace remove {marketplace}`, "
    "`claude plugin marketplace add {slug}@{ref}`, then "
    "`claude plugin install {plugin}@{marketplace}` in every repo that uses "
    "it — the removal uninstalls {plugin} everywhere on this machine"
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


def find_install_dir(
    plugin_root: Path, *, preferred: Path | None = None
) -> Path | None:
    """Walk the Claude Code cache layout to find the active plugin install.

    Claude Code stores installed plugins under
    ``~/.claude/plugins/cache/<plugin>/<plugin>/<version>/`` — two levels
    nested below the cache slot, with one directory per cached version.
    Older versions and forks may flatten to one level or none. Walk
    up to two levels looking for the first directory that carries a
    ``.claude-plugin/plugin.json``. The slot an install record names wins;
    without one, the highest semver-shaped name does — a fallback only,
    because a commit-keyed slot is named by a SHA, which does not sort.

    Args:
        plugin_root: Cache slot for the plugin
            (``~/.claude/plugins/cache/<plugin>``).
        preferred: The slot ``installed_plugins.json`` records for the
            install being judged, when known.

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
    target = _safe_resolve(preferred) if preferred is not None else None
    if target is not None:
        for candidate in valid:
            if candidate.resolve() == target:
                return candidate
    return max(valid, key=lambda p: version_key(p.name))


class InstallRecord(NamedTuple):
    """One ``installed_plugins.json`` entry — a copy Claude Code runs.

    Attributes:
        install_dir: The cache slot the entry points at, when it names one.
        version: The version Claude Code recorded: a semver string, or a
            12-character commit SHA for a manifest that declares none.
        commit: The source commit (``gitCommitSha``), when recorded.
    """

    install_dir: Path | None
    version: str | None
    commit: str | None


def installed_record(
    plugin_key: str,
    repo_root: Path | None = None,
    *,
    plugins_file: Path | None = None,
) -> InstallRecord | None:
    """Return the install record of *plugin_key* that applies to *repo_root*.

    A plugin can be installed once per scope — user-wide, or per project —
    and each install is a separate cache slot, possibly a different
    commit. Judging a repo means judging the copy recorded for that repo,
    so the project's own record wins, then the user-scope one; another
    project's install says nothing about this one. Every degradation
    (missing, unreadable, or foreign-shaped registry) is "no record",
    never an exception — the callers are advisories and gates that must
    not crash on Claude Code's internal state.

    Args:
        plugin_key: ``<plugin>@<marketplace>``.
        repo_root: Repo being judged; ``None`` takes the last record that
            carries a version, whatever its scope.
        plugins_file: Registry to read; defaults to :data:`INSTALLED_PLUGINS`.

    Returns:
        The applicable record, or ``None`` when there is none.
    """
    try:
        data, err = read_json(plugins_file or INSTALLED_PLUGINS)
    except OSError:
        return None
    if err is not None or not isinstance(data, dict):
        return None
    plugins = data.get("plugins")
    entries = plugins.get(plugin_key) if isinstance(plugins, dict) else None
    # Two shapes seen in the wild: a list of per-scope records, or one dict.
    if isinstance(entries, dict):
        entries = [entries]
    if not isinstance(entries, list):
        return None
    chosen = _pick_record([e for e in entries if isinstance(e, dict)], repo_root)
    if chosen is None:
        return None
    install = chosen.get("installPath")
    version = chosen.get("version")
    commit = chosen.get("gitCommitSha")
    return InstallRecord(
        Path(install) if isinstance(install, str) and install else None,
        str(version) if version else None,
        str(commit) if commit else None,
    )


def _pick_record(records: list[dict], repo_root: Path | None) -> dict | None:
    """Choose the record that applies to *repo_root* (see :func:`installed_record`).

    Args:
        records: The plugin's per-scope install records.
        repo_root: The repo to match by ``projectPath``, or ``None`` to fall
            back to the last record carrying a version.

    Returns:
        The matching record, or ``None`` when no record applies.
    """
    if repo_root is None:
        with_version = [r for r in records if r.get("version")]
        return with_version[-1] if with_version else None
    here = repo_root.resolve()
    mine = [
        r
        for r in records
        if isinstance(r.get("projectPath"), str)
        and _safe_resolve(Path(r["projectPath"])) == here
    ]
    if mine:
        return mine[-1]
    user = [r for r in records if r.get("scope") == "user"]
    return user[-1] if user else None


def _safe_resolve(path: Path) -> Path | None:
    """Resolve *path*, or ``None`` when it cannot be (e.g. a NUL byte).

    Paths here come from Claude Code's registry files; a malformed one must
    read as "no match", never crash a gate that runs on every commit.

    Args:
        path: A path read from local registry state.

    Returns:
        The resolved path, or ``None``.
    """
    try:
        return path.resolve()
    except (OSError, ValueError):
        return None


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


class MarketplaceClone(NamedTuple):
    """A marketplace Claude Code has registered on this machine.

    Attributes:
        name: The registration key — the ``<marketplace>`` in plugin keys.
        path: The local clone.
        ref: The ref the registration tracks; ``None`` for the default
            branch.
    """

    name: str
    path: Path
    ref: str | None


def marketplace_clone(repo_slug: str) -> MarketplaceClone | None:
    """The registered marketplace serving *repo_slug*, with its clone and ref.

    Claude Code records every marketplace it has fetched in
    ``~/.claude/plugins/known_marketplaces.json``, keyed by marketplace
    name, each entry carrying ``source.repo`` (``owner/repo``), an optional
    ``source.ref``, and ``installLocation`` (a real git checkout of that
    ref). Matching on the source repo rather than the marketplace name
    keeps the lookup tied to what the pin names. The ref is returned, not
    assumed: the registration is machine-wide, so it may track another
    repo's pin.

    Args:
        repo_slug: ``owner/repo`` the consumer's pin points at.

    Returns:
        The registration, or ``None`` when the registry is absent,
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
    for name, entry in data.items():
        if not isinstance(entry, dict):
            continue
        source = entry.get("source")
        if not isinstance(source, dict) or source.get("repo") != repo_slug:
            continue
        location = entry.get("installLocation")
        if isinstance(location, str) and Path(location).is_dir():
            ref = source.get("ref")
            return MarketplaceClone(
                str(name), Path(location), ref if isinstance(ref, str) and ref else None
            )
    return None


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
            ``"current"``, ``"behind"``, ``"stale-content"`` (a consumer's
            installed commit is not the one its pin serves), or
            ``"wrong-ref"`` (the machine's registration tracks another ref
            than the consumer pins).
        plugin_name: Name the manifest declares, falling back to the repo
            directory's own name.
        cached: What is installed: the cached version, or for a
            commit-keyed plugin the installed commit (12 characters); for
            ``"wrong-ref"``, the ref the registration tracks.
        declared: What should be installed: the manifest's version, the
            base ref a commit-keyed plugin is judged against, or on the
            consumer branch the ref the repo pins.
        missing_hooks: Hooks the pinned content ships that the cache slot
            does not; detail for ``"stale-content"``.
        remedy: The command that converges this verdict, when one is
            specific to it.
    """

    state: str
    plugin_name: str
    cached: str | None
    declared: str | None
    missing_hooks: tuple[str, ...] = ()
    remedy: str = ""


def plugin_cache_status(repo_root: Path) -> PluginCacheStatus:
    """Compare the cached plugin against what ships it.

    One reader for a question two checks ask: the ``plugin_sync``
    pre-commit step, which may refuse a commit, and ``forge-doctor``,
    which reports an advisory. Both need the same verdict and the same
    remediation; only what they do with it differs.

    Three branches, by what identifies the plugin. A manifest that
    declares a version is judged by that version. A manifest that declares
    none is keyed on its commit, so :func:`_commit_identity_status` judges
    the installed commit against the base branch. A repo with no manifest
    is a *consumer*: :func:`_consumer_cache_status` judges the installed
    commit against the clone its pin resolves to. Comparing the cache
    against the pip package's version instead — as the doctor once did —
    asks about two numbers that are apart by design.

    Args:
        repo_root: Repo whose ``.claude-plugin/plugin.json`` ships the
            plugin, or a consumer repo.

    Returns:
        A :class:`PluginCacheStatus`; ``"behind"``, ``"stale-content"``
        and ``"wrong-ref"`` are the findings.
    """
    manifest = repo_root / ".claude-plugin" / "plugin.json"
    if not manifest.is_file():
        return _consumer_cache_status(repo_root)
    data, _err = read_json(manifest)
    plugin_name = str(data.get("name") or repo_root.name)
    if not plugin_manifest_declares_version(repo_root):
        return _commit_identity_status(repo_root, plugin_name)
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


def _commit_identity_status(repo_root: Path, plugin_name: str) -> PluginCacheStatus:
    """Judge a commit-keyed plugin against the base branch of the repo shipping it.

    Every commit is a plugin version, so "behind" means the base branch
    changed what Claude Code loads (:data:`PLUGIN_SURFACE`) since the
    installed commit — not merely that it moved. The check never guesses:
    an installed commit this clone does not know, or one that is not an
    ancestor of the base (a newer install, a fork), is no finding.

    Args:
        repo_root: Repo whose version-less manifest ships the plugin.
        plugin_name: Name the manifest declares.

    Returns:
        ``"current"`` or ``"behind"`` (with the refresh-and-update
        remedy), ``"uncached"`` when this repo has no install record, and
        ``"unparsed"`` when the commits cannot be compared.
    """
    marketplace = own_marketplace_name(repo_root, plugin_name)
    record = installed_record(f"{plugin_name}@{marketplace}", repo_root)
    installed = _installed_commit(record)
    if not installed:
        return PluginCacheStatus("uncached", plugin_name, None, None)
    base_ref = resolve_base_branch_ref(repo_root, load_config(repo_root).base_branch)
    base_sha = resolve_commit(repo_root, base_ref) if base_ref else None
    if base_ref is None or base_sha is None:
        return PluginCacheStatus("unparsed", plugin_name, installed[:12], None)
    declared = f"{base_ref} ({base_sha[:7]})"
    if not is_ancestor(repo_root, installed, base_sha):
        return PluginCacheStatus("unparsed", plugin_name, installed[:12], declared)
    behind = paths_differ(repo_root, installed, base_sha, PLUGIN_SURFACE)
    return PluginCacheStatus(
        "behind" if behind else "current",
        plugin_name,
        installed[:12],
        declared,
        remedy=REFRESH_REMEDIATION.format(marketplace=marketplace, plugin=plugin_name),
    )


def own_marketplace_name(repo_root: Path, plugin_name: str) -> str:
    """Name of the marketplace a plugin-shipping repo publishes, else the plugin's.

    Args:
        repo_root: Repo root to read ``.claude-plugin/marketplace.json`` from.
        plugin_name: Fallback name when the repo ships no marketplace manifest.

    Returns:
        The marketplace's declared name, or *plugin_name* when absent.
    """
    data, err = read_json(repo_root / ".claude-plugin" / "marketplace.json")
    name = data.get("name") if err is None and isinstance(data, dict) else None
    return str(name) if name else plugin_name


def _consumer_cache_status(repo_root: Path) -> PluginCacheStatus:
    """Judge a consumer's installed commit against the ref it pinned.

    A consumer ships no manifest, so it has no declared version to compare
    — and a declared version would not identify the content anyway: under
    tag-per-merge, distinct commits can share one. What the consumer did
    declare is the pin. Two questions follow, in order. Does this
    machine's registration serve that ref at all? Claude Code keeps one
    registration per marketplace name per user, so another repo can hold
    it at a different ref, and then no update in this repo can help. And
    is the installed commit the one the clone is at?

    Args:
        repo_root: Consumer repo root — searched for a ``forge-scripts``
            pin.

    Returns:
        ``"wrong-ref"``, ``"stale-content"``, ``"current"``, ``"uncached"``
        when this repo has no install record, ``"unparsed"`` when the
        commits cannot be read, and ``"no-manifest"`` when the pin or the
        clone cannot be resolved at all.
    """
    pin = find_pin(repo_root)
    slug = _repo_slug(pin.url) if pin is not None else None
    clone = marketplace_clone(slug) if slug is not None else None
    source_dir = find_install_dir(clone.path) if clone is not None else None
    if pin is None or slug is None or clone is None or source_dir is None:
        return PluginCacheStatus("no-manifest", repo_root.name, None, None)
    data, _err = read_json(source_dir / ".claude-plugin" / "plugin.json")
    plugin_name = str(data.get("name") or repo_root.name)
    names = {"marketplace": clone.name, "plugin": plugin_name}
    if not _clone_serves_ref(clone, pin.ref):
        # Registry- and clone-derived values land in shell commands a person
        # may paste — quote them.
        values = {**names, "ref": pin.ref, "slug": slug}
        shell = {key: shlex.quote(value) for key, value in values.items()}
        return PluginCacheStatus(
            "wrong-ref",
            plugin_name,
            clone.ref or "(default branch)",
            pin.ref,
            remedy=WRONG_REF_REMEDIATION.format(**shell),
        )
    record = installed_record(f"{plugin_name}@{clone.name}", repo_root)
    if record is None:
        return PluginCacheStatus("uncached", plugin_name, None, pin.ref)
    installed = _installed_commit(record)
    head = resolve_commit(clone.path, "HEAD")
    if not installed or head is None:
        return PluginCacheStatus(
            "unparsed", plugin_name, installed[:12] if installed else None, pin.ref
        )
    if head.startswith(installed) or installed.startswith(head):
        return PluginCacheStatus("current", plugin_name, installed[:12], pin.ref)
    return PluginCacheStatus(
        "stale-content",
        plugin_name,
        installed[:12],
        pin.ref,
        _missing_hooks(source_dir, record.install_dir),
        remedy=(
            _stale_slot_remedy(record, names)
            if plugin_manifest_declares_version(source_dir)
            else UPDATE_REMEDIATION.format(**names)
        ),
    )


def _stale_slot_remedy(record: InstallRecord, names: dict[str, str]) -> str:
    """The slot-discarding remedy for a declared-version install, quoted.

    Args:
        record: The stale install record — its slot is what to delete.
        names: ``marketplace`` and ``plugin`` names for the reinstall.

    Returns:
        :data:`STALE_CACHE_REMEDIATION` naming the exact slot when the
        record names one, else the plugin's cache directory.
    """
    slot = (
        str(record.install_dir)
        if record.install_dir is not None
        else f"~/.claude/plugins/cache/{names['marketplace']}/{names['plugin']}/"
    )
    return STALE_CACHE_REMEDIATION.format(
        slot=shlex.quote(slot),
        plugin=shlex.quote(names["plugin"]),
        marketplace=shlex.quote(names["marketplace"]),
    )


def _installed_commit(record: InstallRecord | None) -> str | None:
    """The commit an install record names, or ``None`` when it names none.

    ``gitCommitSha`` when it is commit-shaped; otherwise the recorded
    version, only if commit-shaped (a commit-keyed install records its SHA
    there). A semver version is not a commit — comparing one against a SHA
    would report every install as stale — and a short or junk value could
    prefix-match any commit, reporting a stale install as current.

    Args:
        record: The install record, or ``None``.

    Returns:
        A full or abbreviated commit SHA, or ``None``.
    """
    if record is None:
        return None
    for candidate in (record.commit, record.version):
        if candidate and _SHA_RE.fullmatch(candidate):
            return candidate
    return None


def _clone_serves_ref(clone: MarketplaceClone, ref: str) -> bool:
    """Whether the registered clone is at *ref* — by name, or by commit.

    Args:
        clone: The registered marketplace clone to check.
        ref: The ref the consumer pins.

    Returns:
        True when the clone's tracked ref matches *ref*, or its ``HEAD``
        resolves to the same commit as *ref*.
    """
    if clone.ref == ref:
        return True
    pinned = resolve_commit(clone.path, ref)
    return pinned is not None and pinned == resolve_commit(clone.path, "HEAD")


def _missing_hooks(source_dir: Path, install_dir: Path | None) -> tuple[str, ...]:
    """Hooks the pinned tree ships that the installed slot lacks (detail only).

    Args:
        source_dir: The pinned marketplace clone's source tree.
        install_dir: The installed cache slot, or ``None`` when unknown.

    Returns:
        Sorted names of hooks present in *source_dir* but missing from
        *install_dir*; empty when *install_dir* is ``None`` or not a
        directory.
    """
    if install_dir is None or not install_dir.is_dir():
        return ()
    return tuple(sorted(_hook_names(source_dir) - _hook_names(install_dir)))
