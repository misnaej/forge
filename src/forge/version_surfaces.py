"""The surfaces a forge install presents, read once for every checker.

One install exposes its version three ways — the pip ``forge-scripts``
distribution, the gitignored ``.githooks/.forge-hook-version`` sidecar the
hook installer writes, and the Claude Code plugin this checkout loads —
and they drift independently: a pull adds an entry point the install
lacks, a failed self-refresh leaves the hooks behind, a merge moves the
release a checkout pins while its plugin stays on the old one.
``forge-doctor`` reports the skew and ``forge-precommit``'s ``env_sync`` /
``plugin_sync`` steps act on it, so the readers live here once and every
consumer sees the same numbers and names the same remediation.

The plugin surface is this checkout's own install. ``forge-plugin-sync``
installs each checkout's release from a marketplace named after it
(``forge-vX-Y-Z``), and Claude Code records that install in
``installed_plugins.json`` against the checkout's path. That record is
what every plugin check reads; a slot shared between repos is never
consulted, because no checkout loads it any more.
"""

from __future__ import annotations

import json
import re
from importlib import metadata
from pathlib import Path
from typing import Final, NamedTuple

from forge.git_utils import FORGE_DIST_NAME
from forge.install_githooks import SIDECAR_NAME as HOOK_VERSION_SIDECAR
from forge.upgrade import find_pin


DIST_NAME: Final[str] = FORGE_DIST_NAME

# Claude Code's record of every plugin install on this machine: per plugin
# key, one entry per scope — ``user`` (every repo) or a ``projectPath``
# (one repo) — naming the cache directory that install loads. It is the
# only place that says which copy a given checkout uses.
INSTALLED_PLUGINS: Final[Path] = (
    Path.home() / ".claude" / "plugins" / "installed_plugins.json"
)

# Claude Code's user-scope settings: ``enabledPlugins`` here turns a plugin
# on in every repo on the machine, the other half of a global install.
USER_SETTINGS: Final[Path] = Path.home() / ".claude" / "settings.json"

# The manifest a plugin-shipping repo (forge itself) declares its release in.
MANIFEST_AREA: Final[str] = ".claude-plugin/plugin.json"

# The plugin every forge-specific reader below is about.
_FORGE: Final[str] = "forge"

# What a value read from another tool's file may look like before it is
# echoed in a verdict or interpolated into a remediation command. Plugin
# names, versions and refs all fit; anything else — whitespace, control
# characters, shell metacharacters — is replaced, so a hand-edited or
# planted record cannot make an advisory print a different command.
_SAFE_TEXT = re.compile(r"[A-Za-z0-9._/@+-]{1,100}")
UNPRINTABLE: Final[str] = "<unprintable>"

# Remediation per surface — the single command that re-converges that one
# onto the current line. Doctor prints it as advice; precommit prints it
# as the way past a block. One string, so the two can never disagree.
SKEW_REMEDIATION: Final[dict[str, str]] = {
    "pip package": "forge-upgrade --apply",
    "git hooks": "install-forge-githooks",
    "plugin": "forge-plugin-sync (then restart the session)",
}


def read_json(path: Path) -> tuple[dict, str | None]:
    """Read a JSON object file. Returns (data, error_message_or_None).

    Every file read this way is written by another tool — Claude Code's
    install records and settings, a plugin manifest — so its shape is not
    guaranteed. Anything but a readable JSON object is an error result,
    never an exception, which lets every caller call ``.get`` on the data
    unguarded.

    Args:
        path: Path to the JSON file to read.

    Returns:
        Tuple of (parsed JSON object, error message or None); the object
        is empty whenever there is an error.
    """
    if not path.is_file():
        return {}, f"missing: {path}"
    try:
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)
    except json.JSONDecodeError as exc:
        return {}, f"invalid JSON in {path}: {exc}"
    except (OSError, UnicodeDecodeError) as exc:
        return {}, f"unreadable: {path}: {exc}"
    if not isinstance(data, dict):
        return {}, f"not a JSON object: {path}"
    return data, None


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
    try:
        return sidecar.read_text(encoding="utf-8").strip() or None
    except (OSError, UnicodeDecodeError):
        return None


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


def _keyed_records(
    plugin: str, plugins_file: Path | None = None
) -> list[tuple[str, dict[str, object]]]:
    """Install records for *plugin*, each with the key it is filed under.

    Two shapes are seen on disk for one plugin key — a list of records,
    one per scope, or a single record — and both are accepted. The key
    says which marketplace a record came from.

    Args:
        plugin: A full ``name@marketplace`` key, matched exactly, or a bare
            plugin name, matching that name from any marketplace.
        plugins_file: Record file to read; defaults to
            :data:`INSTALLED_PLUGINS`.

    Returns:
        ``(key, record)`` pairs in file order; empty when the file is
        absent, unreadable or malformed — never an exception, since every
        caller runs inside a check.
    """
    path = INSTALLED_PLUGINS if plugins_file is None else plugins_file
    data, err = read_json(path)
    plugins = data.get("plugins") if err is None else None
    if not isinstance(plugins, dict):
        return []
    records: list[tuple[str, dict[str, object]]] = []
    for key, value in plugins.items():
        if not isinstance(key, str) or (
            key != plugin and key.partition("@")[0] != plugin
        ):
            continue
        entries = value if isinstance(value, list) else [value]
        records.extend((key, entry) for entry in entries if isinstance(entry, dict))
    return records


# Prefix of the marketplace a checkout's own forge install comes from:
# ``forge-v<major>-<minor>-<patch>``. Claude Code keeps one marketplace per
# name per machine, so while every version shared the name ``forge``,
# checkouts on different versions overwrote each other's source. A name
# per version cannot collide, and a name always means the same content.
PINNED_MARKETPLACE_PREFIX: Final[str] = "forge-v"

_RELEASE_REF = re.compile(r"v(\d+\.\d+\.\d+)")
# Exact, unlike parse_semver: the version becomes a directory name and a
# `claude` argument, and a git hook acts on whatever a branch's manifest says.
_RELEASE_VERSION = re.compile(r"\d+\.\d+\.\d+")


def pinned_marketplace(version: str) -> str:
    """Return the per-version marketplace name for forge *version*.

    Args:
        version: A release version, ``X.Y.Z``.

    Returns:
        ``forge-vX-Y-Z`` — dots become dashes, so the name is one token.
    """
    return PINNED_MARKETPLACE_PREFIX + version.replace(".", "-")


def target_plugin_version(repo_root: Path) -> str | None:
    """Return the forge release this checkout should run as its plugin.

    A plugin repo (forge itself) runs the version its own manifest names:
    under fragments mode that is the latest release tag, except on a
    release-assembly commit, whose manifest names the release about to be
    tagged. A consumer runs the release its ``forge-scripts`` pin names.

    Args:
        repo_root: Checkout root.

    Returns:
        ``X.Y.Z``, or ``None`` when no release is named — no manifest
        version (or one that is not exactly ``X.Y.Z``) and no pin, or a
        pin on a branch, which names no fixed content to install.
    """
    manifest = repo_root / MANIFEST_AREA
    if manifest.is_file():
        data, err = read_json(manifest)
        version = data.get("version") if err is None else None
        if isinstance(version, str) and _RELEASE_VERSION.fullmatch(version):
            return version
        return None
    pin = find_pin(repo_root)
    match = _RELEASE_REF.fullmatch(pin.ref) if pin is not None else None
    return match.group(1) if match else None


class CheckoutInstall(NamedTuple):
    """One plugin install Claude Code recorded.

    Attributes:
        key: The ``<plugin>@<marketplace>`` key the record is filed under.
        scope: ``local`` or ``project`` for one checkout's install, ``user``
            for one that loads in every checkout.
        version: The installed version — the record's, falling back to
            what the install directory's manifest declares.
        install_dir: The cache directory the install loads from.
    """

    key: str
    scope: str
    version: str
    install_dir: Path


def _installs(
    plugin: str, plugins_file: Path | None
) -> list[tuple[CheckoutInstall, str | None]]:
    """Every recorded install of *plugin* whose directory still exists.

    A record can outlive its directory — a cache cleared by hand, or a
    marketplace whose source folder is gone — and such a record loads
    nothing, so it is skipped rather than reported.

    Args:
        plugin: As for :func:`_keyed_records`.
        plugins_file: Record file to read; defaults to
            :data:`INSTALLED_PLUGINS`.

    Returns:
        ``(install, projectPath)`` pairs in file order; the path is
        ``None`` when the record names none.
    """
    found: list[tuple[CheckoutInstall, str | None]] = []
    for key, record in _keyed_records(plugin, plugins_file):
        location = record.get("installPath")
        if not isinstance(location, str) or not location:
            continue
        install_dir = Path(location)
        if not install_dir.is_dir():
            continue
        scope = record.get("scope")
        version = record.get("version")
        project = record.get("projectPath")
        install = CheckoutInstall(
            key=key,
            scope=scope if isinstance(scope, str) else "",
            version=str(version) if version else install_version(install_dir),
            install_dir=install_dir,
        )
        found.append((install, project if isinstance(project, str) else None))
    return found


def checkout_installs(
    repo_root: Path, plugins_file: Path | None = None, *, plugin: str = _FORGE
) -> list[CheckoutInstall]:
    """Return every install of *plugin* recorded for *repo_root* alone.

    User-scope records are left out: they belong to every checkout, and
    :func:`user_installs` reports them.

    Args:
        repo_root: Checkout root.
        plugins_file: Record file to read; defaults to
            :data:`INSTALLED_PLUGINS`.
        plugin: Bare plugin name.

    Returns:
        The installs in file order; records whose install directory no
        longer exists are ignored.
    """
    target = _resolved(repo_root)
    return [
        install
        for install, project in _installs(plugin, plugins_file)
        if install.scope != "user"
        and project is not None
        and _resolved(Path(project)) == target
    ]


def user_installs(
    plugin: str = _FORGE, plugins_file: Path | None = None
) -> list[CheckoutInstall]:
    """Return every user-scope install of *plugin* whose directory exists.

    Args:
        plugin: Bare plugin name.
        plugins_file: Record file to read; defaults to
            :data:`INSTALLED_PLUGINS`.

    Returns:
        The installs in file order.
    """
    return [
        install
        for install, _ in _installs(plugin, plugins_file)
        if install.scope == "user"
    ]


def resolve_install(
    repo_root: Path, plugin: str = _FORGE, plugins_file: Path | None = None
) -> CheckoutInstall | None:
    """Return the install of *plugin* that answers for *repo_root*.

    Forge is judged on this checkout's own install only: a copy recorded
    for another checkout or for the whole user account is not what this
    checkout was set up to load. When the checkout holds several forge
    installs, the one :func:`checkout_plugin_current` counts — the target
    release from that release's own marketplace — wins, then any at the
    target version, then the first recorded. Another plugin has no
    per-checkout release to aim at, so its own checkout record wins and a
    user-scope record stands in when there is none.

    Args:
        repo_root: Checkout root.
        plugin: Bare plugin name.
        plugins_file: Record file to read; defaults to
            :data:`INSTALLED_PLUGINS`.

    Returns:
        The install, or ``None`` when nothing qualifies.
    """
    own = checkout_installs(repo_root, plugins_file, plugin=plugin)
    if plugin == _FORGE:
        target = target_plugin_version(repo_root)
        pinned = f"{_FORGE}@{pinned_marketplace(target)}" if target else None
        ranked = sorted(
            own,
            key=lambda install: (install.key != pinned, install.version != target),
        )
        return ranked[0] if ranked else None
    if own:
        return own[0]
    return next(iter(user_installs(plugin, plugins_file)), None)


def checkout_plugin_current(
    repo_root: Path, version: str, plugins_file: Path | None = None
) -> bool:
    """Say whether *repo_root* has its own install of forge *version*.

    The one judgment ``forge-plugin-sync``, ``forge-doctor`` and the
    ``plugin_sync`` pre-commit step share: current means a record for
    this checkout under the per-version marketplace, at that version.

    Args:
        repo_root: Checkout root.
        version: Target release, ``X.Y.Z``.
        plugins_file: Record file to read; defaults to
            :data:`INSTALLED_PLUGINS`.

    Returns:
        ``True`` when such a record exists.
    """
    want = f"{_FORGE}@{pinned_marketplace(version)}"
    return any(
        install.key == want and install.version == version
        for install in checkout_installs(repo_root, plugins_file)
    )


def enabled_forge_keys(*settings_paths: Path) -> list[str]:
    """Return every ``forge@…`` key the given settings files leave turned on.

    Files are read in order and a later file's value for a key replaces an
    earlier one's — the way Claude Code layers a checkout's
    ``settings.local.json`` over its ``settings.json`` — so a key one file
    enables and a later one sets ``false`` is off.

    Args:
        *settings_paths: Claude Code ``settings.json``-shaped files, lowest
            precedence first.

    Returns:
        The keys whose final ``enabledPlugins`` value is ``true``, sorted;
        a file that is absent, unreadable or sets nothing contributes
        nothing.
    """
    final: dict[str, object] = {}
    for path in settings_paths:
        data, err = read_json(path)
        enabled = data.get("enabledPlugins") if err is None else None
        if isinstance(enabled, dict):
            final.update(
                (key, value)
                for key, value in enabled.items()
                if isinstance(key, str) and key.startswith(f"{_FORGE}@")
            )
    return sorted(key for key, value in final.items() if value is True)


def global_forge_keys(
    plugins_file: Path | None = None, settings_file: Path | None = None
) -> list[str]:
    """Return every forge key installed or enabled for the whole user account.

    A user-scope forge loads in every repo, beside the checkout's own copy,
    and two enabled plugins named ``forge`` make Claude Code drop hooks —
    so both ``forge-doctor`` and ``forge-plugin-sync`` ask this one
    question. A user-scope record counts whether or not its directory
    still exists: Claude Code may fetch it again, and removing the record
    is the fix either way.

    Args:
        plugins_file: Record file to read; defaults to
            :data:`INSTALLED_PLUGINS`.
        settings_file: User settings to read; defaults to
            :data:`USER_SETTINGS`.

    Returns:
        The keys, sorted and without duplicates; empty when there are none.
    """
    recorded = {
        key
        for key, record in _keyed_records(_FORGE, plugins_file)
        if record.get("scope") == "user"
    }
    settings = USER_SETTINGS if settings_file is None else settings_file
    return sorted(recorded | set(enabled_forge_keys(settings)))


def _resolved(path: Path) -> Path:
    """Return *path* resolved, or unchanged when resolution fails.

    Args:
        path: Path to resolve.

    Returns:
        The resolved path — so a symlinked checkout matches the path
        Claude Code recorded — or *path* itself when it cannot be resolved:
        an ``OSError``, or a ``ValueError`` for a path carrying a NUL byte,
        which a hand-edited record can hold.
    """
    try:
        return path.resolve()
    except (OSError, ValueError):
        return path


def safe_text(value: object) -> str | None:
    """Return *value* as text if it matches :data:`_SAFE_TEXT`, else a placeholder.

    Args:
        value: A name, version or ref read from another tool's file — any
            JSON value, since the file's shape is not guaranteed.

    Returns:
        ``None`` for ``None``, the value's text unchanged when every
        character is allowed, otherwise :data:`UNPRINTABLE`.
    """
    if value is None:
        return None
    text = str(value)
    return text if _SAFE_TEXT.fullmatch(text) else UNPRINTABLE
