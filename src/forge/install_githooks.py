"""install-forge-githooks — wire forge's git hooks into a repo.

Writes thin wrapper hooks under ``.githooks/`` and points
``core.hooksPath`` at them. Three hooks are installed; each is a
one-line wrapper that calls a forge-shipped CLI:

- ``pre-commit``  → ``forge-precommit "$@"``
- ``post-merge``  → ``forge-post-merge "$@"``
- ``post-checkout`` → ``forge-post-checkout "$@"``

The wrapper-pattern contract: forge owns the *logic* (in the CLIs);
the consumer owns the hook *file*. Consumers extend a wrapper by
adding repo-specific shell lines after the forge CLI call. The
installer detects consumer-modified wrappers via a body-hash field
in the managed marker and skips them on auto-refresh — only an
unmodified wrapper is rewritten.

All three are written with a versioned ``# forge:githook-managed
v<N> forge-version=X.Y.Z body-sha=<hex>`` marker. Re-running this
CLI upgrades managed hooks that are still pristine; modified
wrappers survive (use ``--force`` to override).
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import re
import subprocess
import sys
from dataclasses import dataclass
from importlib import metadata
from typing import TYPE_CHECKING

from forge.git_utils import configure_cli_logging, repo_root


if TYPE_CHECKING:
    from pathlib import Path


configure_cli_logging()
logger = logging.getLogger(__name__)


HOOK_VERSION = 2
MANAGED_MARKER_PREFIX = "# forge:githook-managed"
FORGE_VERSION_KEY = "forge-version"
BODY_SHA_KEY = "body-sha"
_BODY_SHA_LEN = 12
_UNKNOWN_FORGE_VERSION = "0.0.0"

# Pattern matching the full managed marker line. Captures the hook-
# version digit and every ``key=value`` field. ``forge-version`` is
# always present; ``body-sha`` is present only for v2+ markers (v1
# hooks predate body-hash detection).
_MARKER_RE = re.compile(
    r"^"
    + re.escape(MANAGED_MARKER_PREFIX)
    + r"\s+v(?P<hook_version>\d+)"
    + r"(?:\s+(?P<fields>\S.*))?$",
)


def _installed_forge_version() -> str:
    """Return the installed ``forge-scripts`` version, or ``0.0.0`` if absent.

    Returns:
        Bare version string from ``importlib.metadata``, or the sentinel
        ``"0.0.0"`` when the package isn't on the runtime path. The
        sentinel lets the staleness preamble degrade gracefully on
        consumer machines that haven't installed forge yet (e.g. a
        contributor running the script straight from a clone).
    """
    try:
        return metadata.version("forge-scripts")
    except metadata.PackageNotFoundError:
        return _UNKNOWN_FORGE_VERSION


def _compute_body_sha(body: str) -> str:
    """Return a short hex SHA-256 digest of *body* for marker embedding.

    Args:
        body: The hook body text (the script content after the
            staleness preamble — i.e. what the consumer might
            customize). Whitespace at the ends is stripped before
            hashing so trailing-newline drift across editors does
            not flip the hash.

    Returns:
        First ``_BODY_SHA_LEN`` hex characters of the SHA-256 digest.
        Truncation is fine here: this hash is a tamper-evidence
        marker, not a security primitive.
    """
    digest = hashlib.sha256(body.strip().encode("utf-8")).hexdigest()
    return digest[:_BODY_SHA_LEN]


def managed_marker(forge_version: str, body_sha: str | None = None) -> str:
    """Render the managed-hook marker line for *forge_version*.

    Args:
        forge_version: Bare version string (e.g. ``"1.2.13"``).
        body_sha: Body hash that v2+ markers embed for tamper detection.
            ``None`` renders a legacy v1-style marker (used only by
            the v1-content reconstructor in migration detection).

    Returns:
        Full marker line. The v2 shape is
        ``"# forge:githook-managed v2 forge-version=X.Y.Z body-sha=<hex>"``.
        ``_is_managed`` keys on the prefix only, so older hooks written
        without the ``body-sha=`` field remain recognised and get
        migrated on the next install.
    """
    base = (
        f"{MANAGED_MARKER_PREFIX} v{HOOK_VERSION} {FORGE_VERSION_KEY}={forge_version}"
    )
    if body_sha is None:
        return base
    return f"{base} {BODY_SHA_KEY}={body_sha}"


def _parse_marker(content: str) -> dict[str, str] | None:
    """Extract the managed marker's fields from full hook content.

    Args:
        content: Full file content (as read from disk).

    Returns:
        Dict with at least ``hook_version`` and (when present) the
        marker's ``forge-version`` and ``body-sha`` fields. Returns
        ``None`` when no managed marker line is found — the file is
        not forge-managed.
    """
    for line in content.splitlines():
        match = _MARKER_RE.match(line)
        if match is None:
            continue
        parsed: dict[str, str] = {"hook_version": match.group("hook_version")}
        fields = match.group("fields") or ""
        for field in fields.split():
            if "=" in field:
                key, _, value = field.partition("=")
                parsed[key] = value
        return parsed
    return None


# Embedded verbatim into every managed hook. The whole block runs
# under `set +e` so a missing python3, an old `sort` without -V, or
# any other PATH oddity can never break the underlying git command —
# the staleness check is advisory, never load-bearing. `sort -V` is
# the POSIX-safe way to compare semver strings without requiring
# Python at the comparison site; on the rare platform where it is
# unavailable the entire check silently no-ops.
_STALENESS_PREAMBLE_TEMPLATE = """\
# Forge staleness check: warn (don't block) when the installed
# forge-scripts version is newer than the version that wrote this
# hook. Whole block is `set +e`-guarded so a missing python3, an
# old `sort`, or any other PATH oddity can never break the
# underlying git command.
forge_hook_version="__FORGE_VERSION__"
set +e
forge_installed_version=$(python3 -c \
    'from importlib.metadata import version; print(version("forge-scripts"))' \
    2>/dev/null)
if [ -n "${forge_installed_version}" ] \
    && [ "${forge_installed_version}" != "${forge_hook_version}" ] \
    && [ "$(printf '%s\\n%s\\n' "${forge_hook_version}" "${forge_installed_version}" \
        | sort -V 2>/dev/null | tail -n1)" = "${forge_installed_version}" ]; then
    msg="[forge] hook generated by forge ${forge_hook_version};"
    msg="${msg} installed is ${forge_installed_version}."
    echo "${msg}" >&2
    echo "[forge] run \\`install-forge-githooks --refresh\\` to regenerate." >&2
fi
set -e"""


@dataclass(frozen=True)
class HookSpec:
    """A git hook the installer maintains.

    Attributes:
        name: Hook filename (e.g. ``"pre-commit"``).
        body: Bash payload that runs under the managed marker. The
            installer prepends a shebang, the marker, a comment block,
            and ``set -euo pipefail``.
    """

    name: str
    body: str


HOOKS: tuple[HookSpec, ...] = (
    HookSpec(name="pre-commit", body='forge-precommit "$@"'),
    HookSpec(name="post-merge", body='forge-post-merge "$@"'),
    HookSpec(name="post-checkout", body='forge-post-checkout "$@"'),
)


# Body shapes that pre-v1.12.0 (hook v1) forge wrote into post-merge
# and post-checkout. Used to detect pristine v1 wrappers during the
# v1→v2 auto-migration: a file matching one of these bodies (after
# stripping the staleness preamble) was forge-generated and is safe
# to rewrite without consumer customization concern. A v1 file whose
# body differs from these strings carries consumer modifications and
# is backed up before overwrite.
#
# pre-commit's v1 body equals its v2 body (already a one-liner from
# the start), so it does not appear here — pre-commit migrates by
# marker bump alone.
_V1_HOOK_BODIES: dict[str, str] = {
    "post-merge": (
        "# Foundation drift check after `git pull`. Verifies FOUNDATION.md\n"
        "# matches the installed forge version and warns if CLAUDE.md is\n"
        "# missing the `@FOUNDATION.md` include directive. The CLI is\n"
        "# REQUIRED (FOUNDATION §2): hard-fail if forge-scripts isn't\n"
        '# installed so the contributor knows to run `pip install -e ".[dev]"`.\n'
        "if ! command -v install-forge-claude-md >/dev/null 2>&1; then\n"
        '    echo "[forge] post-merge: install-forge-claude-md not on PATH." >&2\n'
        '    echo "[forge] Run \\`pip install -e \\".[dev]\\"\\` and retry." >&2\n'
        "    exit 1\n"
        "fi\n"
        "install-forge-claude-md --check --quiet || \\\n"
        '    echo "  → run \\`install-forge-claude-md\\` to sync."\n'
        "# Auto-refresh managed git hooks when forge itself has been upgraded\n"
        "# since they were last written. Backgrounded + output-redirected so\n"
        "# the rewrite happens AFTER this hook process exits — bash reads\n"
        "# scripts in chunks and a self-rewrite mid-execution risks reading\n"
        "# stale or partial content past the current buffer boundary.\n"
        "# Idempotent: no-op when nothing changed. --refresh forces regen\n"
        "# regardless of content match; --quiet suppresses INFO.\n"
        "if command -v install-forge-githooks >/dev/null 2>&1; then\n"
        "    ( install-forge-githooks --refresh --quiet || true ) "
        ">/dev/null 2>&1 &\n"
        "fi"
    ),
    "post-checkout": (
        "# Branch-switch / clone: same foundation drift check as post-merge.\n"
        "# Args: <prev_head> <new_head> <branch_flag>. We only fire when\n"
        "# the HEAD actually moved (branch_flag=1) to avoid running on\n"
        "# every file-level checkout.\n"
        'if [ "${3:-0}" != "1" ]; then\n'
        "    exit 0\n"
        "fi\n"
        "if ! command -v install-forge-claude-md >/dev/null 2>&1; then\n"
        "    echo '[forge] post-checkout: install-forge-claude-md not on PATH.'"
        " >&2\n"
        '    echo "[forge] Run \\`pip install -e \\".[dev]\\"\\` and retry." >&2\n'
        "    exit 1\n"
        "fi\n"
        "install-forge-claude-md --check --quiet || \\\n"
        '    echo "  → run \\`install-forge-claude-md\\` to sync."'
    ),
}


def _hook_content(spec: HookSpec, forge_version: str) -> str:
    """Render the full file content for *spec*.

    Args:
        spec: Hook spec to render.
        forge_version: Forge version to embed in the managed marker and
            in the staleness preamble. Hooks generated by an older forge
            will emit a stderr warning on every run once the installed
            forge becomes newer.

    Returns:
        Full bash script text including shebang, marker (with body
        SHA), staleness preamble, and body.
    """
    body_sha = _compute_body_sha(spec.body)
    preamble = _STALENESS_PREAMBLE_TEMPLATE.replace("__FORGE_VERSION__", forge_version)
    return (
        "#!/usr/bin/env bash\n"
        f"{managed_marker(forge_version, body_sha)}\n"
        f"# {spec.name} hook. Generated by install-forge-githooks.\n"
        "# Edit freely; reinstalling forge respects consumer edits\n"
        "# (auto-refresh leaves modified wrappers alone). Use --force\n"
        "# to override and rewrite to the canonical one-liner.\n"
        "set -euo pipefail\n"
        f"{preamble}\n"
        f"{spec.body}\n"
    )


def _is_managed(hook: Path) -> bool:
    """Return True if *hook* carries any forge-managed marker.

    Args:
        hook: Path to the hook script.

    Returns:
        True iff the file exists and contains ``MANAGED_MARKER_PREFIX``.
        An older-version marker is still considered managed so re-running
        the installer can upgrade it.
    """
    if not hook.is_file():
        return False
    return MANAGED_MARKER_PREFIX in hook.read_text()


def _hook_body_from_content(content: str) -> str:
    """Return the body portion of a managed hook file's full content.

    Args:
        content: Full file content (shebang + marker + comments +
            preamble + body).

    Returns:
        Just the body (everything after the staleness preamble's
        terminator), with whitespace stripped at the ends. When the
        preamble terminator is missing (older / hand-edited file),
        falls back to the substring after the marker line. The
        return value is what gets hashed and compared to detect
        consumer modification.
    """
    preamble_terminator = "\nset -e\n"
    idx = content.find(preamble_terminator)
    if idx != -1:
        return content[idx + len(preamble_terminator) :].strip()
    # Fallback: locate the marker line and return everything after
    # the next blank-ish line. Conservative — used only for legacy
    # files that lack the standard preamble shape.
    for line in content.splitlines():
        if MANAGED_MARKER_PREFIX in line:
            after = content.split(line, 1)[1]
            return after.strip()
    return content.strip()


def _wrapper_is_unmodified(hook: Path, spec: HookSpec) -> bool:
    """Return True when *hook* still carries the body forge originally wrote.

    The check supports both v2 (body-sha in marker) and v1 (no
    body-sha; body matched against the corresponding entry in
    :data:`_V1_HOOK_BODIES`).

    Args:
        hook: Path to the hook file.
        spec: The canonical spec for this hook (used to identify the
            v1 body shape during migration detection).

    Returns:
        True when the hook is pristine — the body matches what forge
        last wrote. False when the consumer added repo-specific
        steps, when the file is hand-modified, or when the file is
        not forge-managed at all.
    """
    if not hook.is_file():
        return False
    content = hook.read_text()
    marker = _parse_marker(content)
    if marker is None:
        return False
    body = _hook_body_from_content(content)
    body_sha = marker.get(BODY_SHA_KEY)
    if body_sha is not None:
        return _compute_body_sha(body) == body_sha
    # Legacy v1 marker (no body-sha). Compare body to the known v1
    # shape for this hook name. pre-commit's v1 body matches its v2
    # body exactly, so fall through to that comparison for it.
    expected = _V1_HOOK_BODIES.get(spec.name, spec.body)
    return body.strip() == expected.strip()


def _backup_hook(hook: Path, forge_version: str) -> Path:
    """Save the current hook content to a versioned backup file.

    Args:
        hook: Hook path to back up.
        forge_version: Version label embedded in the backup filename
            so consumers can see which migration triggered the save.

    Returns:
        Path of the written backup file. Existing backups for the
        same forge version are overwritten — multiple ``--refresh``
        runs at the same forge version converge to one backup, not
        a per-run trail.
    """
    backup = hook.with_name(f"{hook.name}.before-forge-v{forge_version}.bak")
    backup.write_text(hook.read_text())
    return backup


def _write_hook(
    hook: Path,
    spec: HookSpec,
    forge_version: str,
    *,
    force: bool,
    refresh: bool = False,
) -> bool:
    """Write *spec* to *hook*, honoring the wrapper-pattern contract.

    Args:
        hook: Destination path.
        spec: Hook spec describing what to write.
        forge_version: Forge version to embed in the marker + staleness
            preamble. Threaded explicitly so callers (and tests) can
            simulate older installs without monkey-patching
            ``importlib.metadata``.
        force: Overwrite even when the existing file is consumer-modified.
        refresh: Rewrite even when current content already matches
            (used by ``--refresh`` and by the post-merge auto-refresh,
            which wants the file regenerated whenever forge has been
            upgraded). Consumer-modified wrappers are still respected
            unless *force* is also True.

    Returns:
        True if the file was (re)written; False if it was left alone
        because it is consumer-modified and *force* is False, or
        because the existing content already matches and *refresh* is
        False.
    """
    content = _hook_content(spec, forge_version)
    if hook.exists() and not _is_managed(hook) and not force:
        logger.info(
            "✓ .githooks/%s exists and is user-customized — leaving alone "
            "(use --force to overwrite)",
            spec.name,
        )
        return False
    if hook.exists() and hook.read_text() == content and not refresh:
        logger.info("✓ .githooks/%s already up to date", spec.name)
        return False
    if hook.exists() and _is_managed(hook) and not _wrapper_is_unmodified(hook, spec):
        if not force:
            logger.info(
                "✓ .githooks/%s carries consumer modifications — leaving alone "
                "(use --force to overwrite; backup will be saved)",
                spec.name,
            )
            return False
        backup = _backup_hook(hook, forge_version)
        logger.warning(
            "! .githooks/%s was consumer-modified; saved %s before --force overwrite",
            spec.name,
            backup.name,
        )
    elif hook.exists() and _is_managed(hook):
        marker = _parse_marker(hook.read_text()) or {}
        if marker.get("hook_version") != str(HOOK_VERSION):
            backup = _backup_hook(hook, forge_version)
            logger.info(
                "→ .githooks/%s migrating from hook v%s to v%d; saved %s",
                spec.name,
                marker.get("hook_version", "?"),
                HOOK_VERSION,
                backup.name,
            )
    hook.parent.mkdir(exist_ok=True)
    hook.write_text(content)
    hook.chmod(0o755)
    logger.info(
        "✓ wrote .githooks/%s (v%d, forge %s)", spec.name, HOOK_VERSION, forge_version
    )
    return True


def _set_hooks_path(repo: Path, *, force: bool) -> None:
    """Set ``core.hooksPath`` to ``.githooks``.

    Warns and refuses to overwrite an existing value that points
    somewhere else (e.g. ``.husky``) unless *force* is True.

    Args:
        repo: Git repo root.
        force: Overwrite an existing non-``.githooks`` value.
    """
    proc = subprocess.run(
        ["git", "-C", str(repo), "config", "--get", "core.hooksPath"],
        capture_output=True,
        text=True,
        check=False,
    )
    current = proc.stdout.strip()
    if current == ".githooks":
        logger.info("✓ core.hooksPath already .githooks")
        return
    if current and not force:
        logger.warning(
            "! core.hooksPath is currently %r (likely another hook manager). "
            "Leaving alone. Re-run with --force to overwrite.",
            current,
        )
        return
    subprocess.run(
        ["git", "-C", str(repo), "config", "core.hooksPath", ".githooks"],
        check=True,
    )
    logger.info("✓ core.hooksPath → .githooks")


def main() -> int:
    """CLI entry point.

    Returns:
        ``0`` on success; non-zero on configuration errors.
    """
    parser = argparse.ArgumentParser(
        prog="install-forge-githooks",
        description=(
            "Install forge's managed git hooks (pre-commit, post-merge, "
            "post-checkout) and set core.hooksPath. Idempotent. "
            "Use --force to overwrite user-customized hooks or an existing "
            "core.hooksPath value."
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Overwrite user-customized hook files and any existing "
            "non-.githooks core.hooksPath value."
        ),
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help=(
            "Rewrite managed hook files unconditionally (used by the "
            "post-merge auto-refresh to pick up a new forge version). "
            "Does not override user-customized hooks — pair with --force "
            "for that."
        ),
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress INFO logs (used by the post-merge auto-refresh).",
    )
    args = parser.parse_args()

    if args.quiet:
        logger.setLevel(logging.WARNING)

    forge_version = _installed_forge_version()
    root = repo_root()
    githooks_dir = root / ".githooks"
    for spec in HOOKS:
        _write_hook(
            githooks_dir / spec.name,
            spec,
            forge_version,
            force=args.force,
            refresh=args.refresh,
        )
    _set_hooks_path(root, force=args.force)
    logger.info(
        "\nNext: `git commit` fires forge-precommit. "
        "`git pull` / `git checkout` will warn when CLAUDE.md drifts.",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
