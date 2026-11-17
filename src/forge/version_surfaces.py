"""The surfaces a forge install presents, read once for every checker.

One install exposes its version two ways — the pip ``forge-scripts``
distribution, and the gitignored ``.githooks/.forge-hook-version``
sidecar the hook installer writes — and they drift independently: a pull
adds an entry point the install lacks, a failed self-refresh leaves the
hooks behind. ``forge-doctor`` reports the skew and
``forge-precommit``'s ``env_sync`` step acts on it, so the readers live
here once and every consumer sees the same numbers and names the same
remediation.

The agent integration is **not** a surface here: it is generated from
this same package rather than installed separately, so it cannot carry a
version of its own. Whether the generated files match what the package
ships is `install-forge-claude --check`'s question, reported by
``forge-doctor`` as ``claude:integration``.
"""

from __future__ import annotations

import json
import re
from importlib import metadata
from pathlib import Path
from typing import Final

from forge.git_utils import FORGE_DIST_NAME
from forge.install_githooks import SIDECAR_NAME as HOOK_VERSION_SIDECAR


DIST_NAME: Final[str] = FORGE_DIST_NAME

# Claude Code's record of every plugin install on this machine: per plugin
# key, one entry per scope — ``user`` (every repo) or a ``projectPath``
# (one repo) — naming the cache directory that install loads. It is the
# only place that says which copy a given checkout uses.

# Claude Code's user-scope settings: ``enabledPlugins`` here turns a plugin
# on in every repo on the machine, the other half of a global install.

# The manifest a plugin-shipping repo (forge itself) declares its release in.

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


# Prefix of the marketplace a checkout's own forge install comes from:
# ``forge-v<major>-<minor>-<patch>``. Claude Code keeps one marketplace per
# name per machine, so while every version shared the name ``forge``,
# checkouts on different versions overwrote each other's source. A name
# per version cannot collide, and a name always means the same content.

_RELEASE_REF = re.compile(r"v(\d+\.\d+\.\d+)")
# Exact, unlike parse_semver: the version becomes a directory name and a
# `claude` argument, and a git hook acts on whatever a branch's manifest says.
_RELEASE_VERSION = re.compile(r"\d+\.\d+\.\d+")


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
