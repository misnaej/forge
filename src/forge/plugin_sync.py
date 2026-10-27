"""forge-plugin-sync — give this checkout its own install of its forge release.

Claude Code keeps one marketplace per name per machine. While forge's
marketplace was always named ``forge``, every repo's settings fed that one
registration, so repos on different releases overwrote each other's source
and ``/plugin update`` fetched whichever release the last repo had named.
This command installs forge for one checkout from a marketplace named after
the release (``forge-vX-Y-Z``, :func:`forge.version_surfaces.pinned_marketplace`),
so the version a repo pins is the only thing that decides what it runs.

The marketplace is a directory under the user cache holding a one-plugin
``marketplace.json`` that pins the release tag *and* its commit. Its
content is a pure function of the release, so every checkout on that
release shares it, and registering it again from another checkout is a
no-op for Claude Code. Every change goes through Claude Code's own
``claude plugin`` commands with ``--scope local``: no Claude Code file is
edited here, no other checkout's install is touched, and no cache slot is
deleted.

The ``plugin_sync`` pre-commit step and ``forge-doctor`` are the read-only
twins of this command; all three judge "current" with
:func:`forge.version_surfaces.checkout_plugin_current`.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Final

from forge.git_utils import (
    _FORGE_GITHUB_REPO,
    configure_cli_logging,
    network_git_env,
    repo_root,
    run_git,
)
from forge.run_context import is_ci
from forge.version_surfaces import (
    checkout_installs,
    checkout_plugin_current,
    pinned_marketplace,
    plugin_records,
    read_json,
    target_plugin_version,
)


configure_cli_logging()
logger = logging.getLogger(__name__)

# A first install clones forge from GitHub; three minutes covers a slow
# link without letting a hung clone stall a git hook indefinitely.
_CLAUDE_TIMEOUT_S: Final[int] = 180
_LS_REMOTE_TIMEOUT_S: Final[int] = 30
_MESSAGE_CAP: Final[int] = 300
_COMMIT = re.compile(r"[0-9a-f]{40}")

EXIT_OK: Final[int] = 0
EXIT_FAILED: Final[int] = 1
EXIT_UNKNOWN: Final[int] = 2


def marketplace_dir(name: str) -> Path:
    """Return the shared directory holding marketplace *name*.

    Args:
        name: A per-version marketplace name.

    Returns:
        ``$XDG_CACHE_HOME/forge/marketplaces/<name>`` (``~/.cache`` when
        the variable is unset).
    """
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "forge" / "marketplaces" / name


def marketplace_manifest(version: str, commit: str) -> dict[str, object]:
    """Return the ``marketplace.json`` content for forge *version*.

    Args:
        version: Release, ``X.Y.Z``.
        commit: The 40-hex commit the release tag points at.

    Returns:
        A one-plugin marketplace pinning both the tag and its commit, so a
        moved tag cannot change what the name installs.
    """
    owner = _FORGE_GITHUB_REPO.partition("/")[0]
    return {
        "name": pinned_marketplace(version),
        "owner": {"name": owner},
        "plugins": [
            {
                "name": "forge",
                "source": {
                    "source": "github",
                    "repo": _FORGE_GITHUB_REPO,
                    "ref": f"v{version}",
                    "sha": commit,
                },
            }
        ],
    }


def write_marketplace(version: str, commit: str) -> Path:
    """Write the per-version marketplace and return its directory.

    The write is a rename over the old file, so a checkout syncing at the
    same moment reads either the whole old file or the whole new one.

    Args:
        version: Release, ``X.Y.Z``.
        commit: The release tag's commit.

    Returns:
        The marketplace directory, ready for ``claude plugin marketplace
        add``.
    """
    root = marketplace_dir(pinned_marketplace(version))
    meta = root / ".claude-plugin"
    meta.mkdir(parents=True, exist_ok=True)
    text = json.dumps(marketplace_manifest(version, commit), indent=2) + "\n"
    fd, tmp = tempfile.mkstemp(dir=meta, prefix=".marketplace-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    Path(tmp).replace(meta / "marketplace.json")
    return root


def resolve_commit(version: str) -> str | None:
    """Ask GitHub which commit release tag ``v<version>`` points at.

    Args:
        version: Release, ``X.Y.Z``.

    Returns:
        The commit (the peeled target of an annotated tag), or ``None``
        when the remote cannot be reached or holds no such tag.
    """
    tag = f"refs/tags/v{version}"
    try:
        listing = run_git(
            "ls-remote",
            f"https://github.com/{_FORGE_GITHUB_REPO}.git",
            tag,
            f"{tag}^{{}}",
            env=network_git_env(None),
            log_errors=False,
            timeout=_LS_REMOTE_TIMEOUT_S,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    refs: dict[str, str] = {}
    for line in listing.splitlines():
        sha, _, ref = line.partition("\t")
        if _COMMIT.fullmatch(sha):
            refs[ref] = sha
    return refs.get(f"{tag}^{{}}") or refs.get(tag)


def _printable(text: str) -> str:
    """Return *text* with control characters escaped, capped in length.

    Args:
        text: A message Claude Code printed.

    Returns:
        One line safe to echo into a terminal.
    """
    escaped = "".join(ch if ch.isprintable() else f"\\x{ord(ch):02x}" for ch in text)
    return escaped[:_MESSAGE_CAP]


def _claude(args: list[str], cwd: Path) -> tuple[bool, str]:
    """Run ``claude plugin <args> --json`` in *cwd*.

    Args:
        args: Arguments after ``claude plugin``.
        cwd: The checkout — ``--scope local`` applies to the working
            directory's project.

    Returns:
        ``(succeeded, message)``: success needs exit 0 and an ``ok``
        outcome on the last JSON line; the message is Claude Code's own,
        made printable.
    """
    try:
        proc = subprocess.run(
            ["claude", "plugin", *args, "--json"],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            timeout=_CLAUDE_TIMEOUT_S,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return False, f"`claude plugin {args[0]}` timed out after {_CLAUDE_TIMEOUT_S}s"
    except OSError as exc:
        return False, _printable(str(exc))
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    try:
        result = json.loads(lines[-1]) if lines else {}
    except json.JSONDecodeError:
        result = {}
    if not isinstance(result, dict):
        result = {}
    message = str(result.get("message") or proc.stderr.strip() or proc.stdout.strip())
    ok = proc.returncode == 0 and result.get("outcome") == "ok"
    return ok, _printable(message)


def _refusal(root: Path) -> str | None:
    """Name a setup this command must not install alongside, if any.

    Two enabled plugins named ``forge`` make Claude Code drop hooks, so a
    second forge must be turned off before this checkout gets its own.

    Args:
        root: Checkout root.

    Returns:
        What to fix, or ``None`` when nothing conflicts.
    """
    if any(record.get("scope") == "user" for record in plugin_records("forge")):
        return (
            "forge is also installed for your whole user account, which loads "
            "in every repo beside this checkout's own copy. Remove it: "
            "`claude plugin uninstall forge@forge --scope user`."
        )
    data, err = read_json(root / ".claude" / "settings.json")
    enabled = data.get("enabledPlugins") if err is None else None
    if isinstance(enabled, dict):
        shared = sorted(
            key
            for key, value in enabled.items()
            if isinstance(key, str) and key.startswith("forge@") and value is True
        )
        if shared:
            return (
                f".claude/settings.json enables {', '.join(shared)} for everyone "
                "who opens this repo, beside this checkout's own copy. Set it to "
                "false — `install-forge-claude-settings` writes that for you."
            )
    return None


def _run_install_steps(root: Path, directory: Path, name: str) -> bool:
    """Register the per-version marketplace and install the plugin from it.

    Args:
        root: Checkout root.
        directory: The marketplace directory written for the release.
        name: The pinned marketplace name (``forge@<name>`` is installed).

    Returns:
        ``True`` when both Claude Code steps succeeded; ``False`` at the first
        refusal, after logging it.
    """
    for step in (
        ["marketplace", "add", str(directory), "--scope", "local"],
        ["install", f"forge@{name}", "--scope", "local"],
    ):
        ok, message = _claude(step, root)
        if not ok:
            logger.error(
                "forge-plugin-sync: `claude plugin %s` failed: %s", step[0], message
            )
            return False
    return True


def _remove_stale_installs(root: Path, name: str) -> None:
    """Uninstall this checkout's older local forge installs.

    Args:
        root: Checkout root.
        name: The pinned marketplace name; its install is kept.

    Returns:
        None. A failed removal is logged as a warning and does not stop the sync.
    """
    for install in checkout_installs(root):
        if install.scope == "local" and install.key != f"forge@{name}":
            ok, message = _claude(["uninstall", install.key, "--scope", "local"], root)
            if not ok:
                logger.warning(
                    "forge-plugin-sync: could not remove this checkout's older %s: %s",
                    install.key,
                    message,
                )


def _install(root: Path, version: str, name: str) -> int:
    """Write the release marketplace, install it, and verify the result.

    Args:
        root: Checkout root.
        version: The target plugin release version.
        name: The pinned marketplace name.

    Returns:
        :data:`EXIT_OK` when the install is recorded for this checkout;
        :data:`EXIT_FAILED` when a Claude Code step fails or nothing is
        recorded afterwards; :data:`EXIT_UNKNOWN` when the release commit
        cannot be read.
    """
    commit = resolve_commit(version)
    if commit is None:
        logger.error(
            "forge-plugin-sync: could not read the commit of release v%s from "
            "GitHub (offline, or the tag is not published yet).",
            version,
        )
        return EXIT_UNKNOWN
    directory = write_marketplace(version, commit)
    if not _run_install_steps(root, directory, name):
        return EXIT_FAILED
    _remove_stale_installs(root, name)
    if not checkout_plugin_current(root, version):
        logger.error(
            "forge-plugin-sync: Claude Code reported success, but no install of "
            "forge %s is recorded for this checkout.",
            version,
        )
        return EXIT_FAILED
    logger.warning(
        "forge-plugin-sync: installed forge %s for this checkout — restart the "
        "Claude Code session to load it.",
        version,
    )
    return EXIT_OK


def sync(root: Path, *, check: bool = False, quiet: bool = False) -> int:
    """Bring *root*'s own forge plugin install onto its target release.

    Args:
        root: Checkout root.
        check: Report only; never call Claude Code.
        quiet: Print nothing when already current or when ``claude`` is
            absent — for git hooks. Changes and failures always print.

    Returns:
        :data:`EXIT_OK` when current (or a change was made, or ``claude``
        is absent); :data:`EXIT_FAILED` when not current under *check*, on
        a conflicting install, or when Claude Code refused;
        :data:`EXIT_UNKNOWN` when the target release or its commit cannot
        be determined.
    """
    version = target_plugin_version(root)
    if version is None:
        logger.error(
            "forge-plugin-sync: no forge release to install — pin forge-scripts "
            "to a release tag (`forge-scripts @ git+...@vX.Y.Z`)."
        )
        return EXIT_UNKNOWN
    name = pinned_marketplace(version)
    if checkout_plugin_current(root, version):
        if not quiet:
            logger.info(
                "forge-plugin-sync: forge %s is this checkout's plugin.", version
            )
        return EXIT_OK
    if check:
        logger.warning(
            "forge-plugin-sync: this checkout has no install of forge %s — run "
            "`forge-plugin-sync`.",
            version,
        )
        return EXIT_FAILED
    refusal = _refusal(root)
    if refusal is not None:
        logger.error("forge-plugin-sync: %s", refusal)
        return EXIT_FAILED
    if shutil.which("claude") is None:
        if not quiet:
            logger.info("forge-plugin-sync: `claude` is not installed — skipped.")
        return EXIT_OK
    return _install(root, version, name)


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and run :func:`sync` on the current repo.

    Args:
        argv: Argument list; ``None`` reads :data:`sys.argv`.

    Returns:
        The exit code from :func:`sync`, or :data:`EXIT_OK` in CI.
    """
    parser = argparse.ArgumentParser(
        prog="forge-plugin-sync",
        description=(
            "Install this checkout's own copy of its forge release as its "
            "Claude Code plugin, from a per-version marketplace, so repos on "
            "different releases never overwrite each other."
        ),
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="report only; exit 1 when this checkout is not on its release",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="print only changes and failures (for git hooks)",
    )
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    if is_ci():
        return EXIT_OK
    return sync(repo_root(), check=args.check, quiet=args.quiet)


if __name__ == "__main__":
    sys.exit(main())
