"""forge-upgrade — one-command consumer upgrade flow.

A small CLI that wraps the multi-step forge upgrade so a consumer
(or a Claude Code agent working in a consumer repo) can advance
``forge-scripts`` with one command instead of remembering six.

Two-phase by design because FOUNDATION §2 forbids Claude Code agents
from installing dependencies. Human-run scripts have a one-shot
shortcut via ``--apply``.

- **Phase 1** — ``forge-upgrade`` (or ``forge-upgrade --channel main``
  / ``--to vX.Y.Z``). Detects the current pin in the consumer's
  ``pyproject.toml``, rewrites it to the requested target, and prints
  the exact ``pip install`` command for the user to run.
- **Phase 2** — ``forge-upgrade --continue``. After the user has run
  the printed pip command, runs ``install-forge-bootstrap`` to
  re-sync managed artifacts and prints the Claude Code plugin update
  reminder.
- **One-shot** — ``forge-upgrade --apply``. For human-run setup
  scripts only: rewrites the pin, runs the force-reinstall pip
  command, then runs the bootstrap. The ``block_install_deps`` Claude
  hook refuses this flag for agents.

``forge-upgrade --check`` reports current vs latest without writing
anything.

Pin detection scope: searches the consumer's ``pyproject.toml`` for
a ``forge-scripts @ git+...`` line under any
``[project.optional-dependencies]`` table. Repos that pin via
``requirements.txt`` / ``setup.cfg`` / a lockfile get a clear "no
pyproject pin found" message; they continue to upgrade manually.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import shlex
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from forge.git_utils import _FORGE_GITHUB_REPO, configure_cli_logging, repo_root
from forge.install_bootstrap import main as _bootstrap_main
from forge.run_context import (
    AuthMode,
    git_auth_mode,
    is_non_interactive,
    progress_logger,
)


configure_cli_logging()
logger = logging.getLogger(__name__)


# Allowed characters in a `--to` ref. Defensive: a malicious / typoed
# value with shell metacharacters would render as text in the printed
# pip command — harmless inside forge but a poisoned-paste risk for
# anyone copying that command into a shell. Allowlist permits
# semver tags, branch names, and commit SHAs.
_REF_RE = re.compile(r"^[A-Za-z0-9._/-]+$")


def _ref_type(value: str) -> str:
    """Argparse type validator for ``--to``.

    Restricts the ref to ``[A-Za-z0-9._/-]+`` — enough for any valid
    git ref (semver tags, branch names, SHAs) and nothing that would
    survive a shell paste with side-effects.

    Args:
        value: Raw CLI string.

    Returns:
        The value when it matches the allowlist.

    Raises:
        argparse.ArgumentTypeError: When the value contains characters
            outside the allowlist.
    """
    if not _REF_RE.fullmatch(value) or ".." in value or value.startswith("/"):
        msg = (
            f"forge-upgrade: invalid --to ref {value!r}. Only "
            "letters, digits, and `._/-` are allowed; "
            "`..` and leading `/` are rejected."
        )
        raise argparse.ArgumentTypeError(msg)
    return value


# Matches the consumer's forge-scripts pin line inside pyproject.toml.
# Conservative on purpose — only the canonical PEP 631 form
# ``forge-scripts @ git+<url>`` is recognised. ``,`` and trailing
# whitespace are preserved on rewrite.
#
# ``url`` is non-greedy + quote-bounded so the engine extends it until
# the ref-anchor (``@<ref>``) finds a clean ``@`` that the ref token
# (``[^@"']+``) can follow. This makes the LAST ``@`` on the line the
# url / ref boundary — required so SSH URLs of the form
# ``git+ssh://git@github.com/<owner>/<repo>.git@<ref>`` (three ``@``
# characters: SSH user separator, host separator, ref separator) keep
# their hostname / owner / repo on rewrite.
_PIN_RE = re.compile(
    r'(?P<prefix>["\']forge-scripts\s*@\s*git\+)'
    r'(?P<url>[^"\']+?)'
    r'@(?P<ref>[^@"\']+)'
    r'(?P<suffix>["\'])',
)


@dataclass(frozen=True)
class Pin:
    """A forge-scripts pin parsed from a consumer's ``pyproject.toml``.

    Attributes:
        path: Repo-relative path of the file the pin was found in.
        line_no: 1-based line number where the pin appears.
        url: The ``git+...`` URL portion (no ref).
        ref: The current pin target — ``main`` / ``dev`` / ``vX.Y.Z``.
    """

    path: Path
    line_no: int
    url: str
    ref: str


def find_pin(repo_root: Path) -> Pin | None:
    """Locate the ``forge-scripts`` pin in *repo_root*'s ``pyproject.toml``.

    Args:
        repo_root: Consumer repo root.

    Returns:
        Parsed :class:`Pin` when a matching line is found; ``None`` when
        ``pyproject.toml`` is absent or carries no ``forge-scripts @
        git+...`` line.
    """
    pyproject = repo_root / "pyproject.toml"
    if not pyproject.is_file():
        return None
    for i, line in enumerate(pyproject.read_text().splitlines(), start=1):
        match = _PIN_RE.search(line)
        if match:
            return Pin(
                path=pyproject,
                line_no=i,
                url=match.group("url"),
                ref=match.group("ref"),
            )
    return None


def _rewrite_pin(pin: Pin, new_ref: str) -> str:
    """Return the file content with *pin*'s line rewritten to *new_ref*.

    Args:
        pin: The pin to update (carries the source file + line number).
        new_ref: Target ref — ``main`` / ``dev`` / a tag like ``v1.3.0``.

    Returns:
        New full file content. The line containing the pin gets its
        ``@<ref>`` portion replaced; all other lines are byte-identical
        to the original.
    """
    lines = pin.path.read_text().splitlines(keepends=True)
    # splitlines(keepends=True) returns each line with its trailing newline
    # preserved; the pin line keeps its newline too.
    raw = lines[pin.line_no - 1]
    rewritten = _PIN_RE.sub(
        lambda m: f"{m.group('prefix')}{m.group('url')}@{new_ref}{m.group('suffix')}",
        raw,
    )
    lines[pin.line_no - 1] = rewritten
    return "".join(lines)


def _git_url_for(auth_mode: AuthMode, ref: str) -> str:
    """Return the ``git+...`` URL pip should resolve for *ref* under *auth_mode*.

    Picks the URL form that the consumer's runtime can authenticate
    against. The actual auth happens inside pip's git subprocess and
    relies on the environment (SSH agent / git credential helper /
    ``GITHUB_TOKEN``); this function only chooses the scheme so the
    runner doesn't block on a credential prompt it can't answer.

    Args:
        auth_mode: One of ``"ssh"`` / ``"https-token"`` /
            ``"https-anonymous"`` / ``"none"``. The ``"none"`` value
            still renders an anonymous HTTPS URL — the caller decides
            whether to refuse.
        ref: Target ref (``main`` / ``dev`` / ``vX.Y.Z``).

    Returns:
        Either ``git+ssh://git@github.com/<repo>.git@<ref>`` (when
        ``auth_mode == "ssh"``) or
        ``git+https://github.com/<repo>.git@<ref>`` (every other mode).
    """
    if auth_mode == "ssh":
        return f"git+ssh://git@github.com/{_FORGE_GITHUB_REPO}.git@{ref}"
    return f"git+https://github.com/{_FORGE_GITHUB_REPO}.git@{ref}"


def _pip_command(ref: str, *, auth_mode: AuthMode = "https-anonymous") -> str:
    """Return the exact ``pip install`` line for a given pin ref.

    Args:
        ref: Target ref — ``main`` / ``dev`` / a tag like ``v1.3.0``.
        auth_mode: Auth context the rendered URL should match. Defaults
            to ``"https-anonymous"`` for callers that display the
            command as a hint (``--check``-mode printout in
            :func:`_run_phase1`, the "no pin found" message) rather
            than executing it. The ``--apply`` path detects the live
            auth context via :func:`forge.run_context.git_auth_mode`
            and forwards it explicitly.

    Returns:
        Single-line shell command. ``--force-reinstall --no-deps``
        busts pip's git cache when the ref is a moving branch
        (``@main`` / ``@dev``); kept for tagged refs too for
        consistency.
    """
    return (
        "pip install --upgrade --force-reinstall --no-deps "
        f'"forge-scripts @ {_git_url_for(auth_mode, ref)}"'
    )


def _resolve_target_ref_or_none(
    args: argparse.Namespace, current_ref: str | None
) -> str | None:
    """Resolve the target ref from CLI flags, falling back to current.

    Args:
        args: Parsed CLI args. ``--channel`` and ``--to`` are
            mutually exclusive at argparse level — at most one can be
            set.
        current_ref: Current pin's ref, or ``None`` when no pin exists.

    Returns:
        The ref string to write. ``--to`` is used when provided;
        ``--channel`` otherwise; the current ref when neither is given.
        ``None`` when no flag is given and no current pin exists.
    """
    if args.to:
        return args.to
    if args.channel:
        return args.channel
    return current_ref


def _resolve_target_ref(args: argparse.Namespace, current_ref: str | None) -> str:
    """Resolve the target ref or exit when undetermined.

    Args:
        args: Parsed CLI args.
        current_ref: Current pin's ref, or ``None`` when no pin exists.

    Returns:
        The resolved ref string.

    Raises:
        SystemExit: When no current pin exists and neither flag was
            given — the CLI can't know what to target.
    """
    resolved = _resolve_target_ref_or_none(args, current_ref)
    if resolved is None:
        sys.stderr.write(
            "forge-upgrade: no forge-scripts pin found in pyproject.toml and "
            "neither --to nor --channel given. Specify a target.\n",
        )
        raise SystemExit(2)
    return resolved


def _write_pyproject_atomic(path: Path, content: str) -> None:
    """Replace *path*'s contents with *content*, atomically.

    Writes to a sibling tempfile in the same directory then ``os.replace``s
    it into place. Same-directory ensures the rename is atomic on POSIX
    (it stays within one filesystem). Crash mid-write leaves the original
    file intact instead of truncated.

    Args:
        path: Target file to overwrite.
        content: New file content (full text).
    """
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(content)
        tmp.replace(path)
    except Exception:
        # Clean up the tempfile when the replace failed.
        if tmp.exists():
            tmp.unlink()
        raise


def _run_phase1(args: argparse.Namespace, root: Path) -> tuple[int, str | None]:
    """Phase 1 — detect the pin, rewrite it, print the pip command.

    ``--check`` short-circuits the rewrite even when no pin exists:
    reports the current state + the pip command the user would need to
    run manually.

    Args:
        args: Parsed CLI args.
        root: Consumer repo root.

    Returns:
        ``(exit_code, target_ref)``. ``target_ref`` is the resolved
        target written to the pin (or hinted in ``--check`` mode),
        ``None`` only when ``--check`` ran with no pin and no flag.
        Raises ``SystemExit(2)`` via :func:`_resolve_target_ref` when
        the non-``--check`` path can't resolve a target.
    """
    pin = find_pin(root)
    current_ref = pin.ref if pin else None

    if args.check:
        # In dry-run mode, surface what we can see + a default target hint
        # without exiting on missing pin / missing flags.
        if pin is None:
            logger.info("(no forge-scripts pin found in pyproject.toml)")
        else:
            logger.info("current pin: %s@%s", pin.url, pin.ref)
        target_hint = _resolve_target_ref_or_none(args, current_ref)
        if target_hint is None:
            logger.info("(no target — pass --channel or --to to see the pip command)")
            return 0, None
        logger.info("would upgrade to: %s", target_hint)
        logger.info("pip command: %s", _pip_command(target_hint))
        return 0, target_hint

    target_ref = _resolve_target_ref(args, current_ref)

    if pin is None:
        logger.warning(
            "no forge-scripts pin found in pyproject.toml — skipping rewrite. "
            "Run the pip command manually:\n  %s",
            _pip_command(target_ref),
        )
        logger.info("Then re-run: forge-upgrade --continue")
        return 0, target_ref

    if pin.ref != target_ref:
        _write_pyproject_atomic(pin.path, _rewrite_pin(pin, target_ref))
        logger.info(
            "✓ %s:%d  forge-scripts pin %s → %s",
            pin.path.name,
            pin.line_no,
            pin.ref,
            target_ref,
        )
    else:
        logger.info(
            "✓ %s:%d  forge-scripts pin already at %s",
            pin.path.name,
            pin.line_no,
            target_ref,
        )

    logger.info("")
    logger.info("Next: run the pip install command manually, then ")
    logger.info("`forge-upgrade --continue` to re-sync managed artifacts.")
    logger.info("")
    logger.info("  %s", _pip_command(target_ref))
    return 0, target_ref


# A ``## vX.Y.Z`` CHANGELOG version heading, and the ``### ⚠️ Upgrade
# notes`` lane within a version's section (up to the next ``### `` or EOF).
_CHANGELOG_VERSION_RE = re.compile(r"^## (v\d+\.\d+\.\d+)\b.*$", re.MULTILINE)
_UPGRADE_NOTES_RE = re.compile(
    r"^### ⚠️ Upgrade notes\s*?\n(.*?)(?=^### |\Z)",
    re.MULTILINE | re.DOTALL,
)


def _read_changelog() -> str | None:
    """Return forge's packaged ``CHANGELOG.md`` text, or ``None`` if unavailable.

    The changelog ships as package data (``src/forge/data/CHANGELOG.md``,
    a symlink to the repo root) so a consumer's ``forge-upgrade`` can read
    it offline — mirroring how ``FOUNDATION.md`` is shipped.

    Returns:
        The changelog contents, or ``None`` when the package data is
        missing (e.g. a partial install).
    """
    try:
        return resources.files("forge").joinpath("data/CHANGELOG.md").read_text("utf-8")
    except (OSError, ModuleNotFoundError):
        return None


def _consumer_upgrade_notes(
    changelog_text: str, *, max_versions: int = 3
) -> str | None:
    """Extract the most recent ``⚠️ Upgrade notes`` lanes from the changelog.

    Walks version sections newest-first and collects each one's
    ``### ⚠️ Upgrade notes`` block, up to *max_versions* that have one.
    Releases without the lane are additive/internal and carry no consumer
    action, so they are skipped.

    Args:
        changelog_text: Full ``CHANGELOG.md`` contents.
        max_versions: Hard cap on how many note-bearing versions to
            surface, newest-first — keeps output bounded when many
            versions accumulate. It is a blunt top-N, not a filter against
            the consumer's prior version (the header tells the reader to
            scan only entries newer than theirs).

    Returns:
        A formatted block (``vX.Y.Z:`` headers + their note lines), or
        ``None`` when no version carries upgrade notes.
    """
    headings = list(_CHANGELOG_VERSION_RE.finditer(changelog_text))
    chunks: list[str] = []
    for index, heading in enumerate(headings):
        if len(chunks) >= max_versions:
            break
        end = (
            headings[index + 1].start()
            if index + 1 < len(headings)
            else len(changelog_text)
        )
        section = changelog_text[heading.end() : end]
        note = _UPGRADE_NOTES_RE.search(section)
        if note:
            chunks.append(f"{heading.group(1)}:\n{note.group(1).strip()}")
    return "\n\n".join(chunks) if chunks else None


def _print_upgrade_notes() -> None:
    """Surface consumer-action upgrade notes after a successful upgrade.

    Reads the packaged changelog and prints the most recent
    ``⚠️ Upgrade notes`` lanes so the consumer knows what — if anything —
    they must change in their own repo. No-op when the changelog or its
    notes are absent.
    """
    text = _read_changelog()
    if text is None:
        return
    notes = _consumer_upgrade_notes(text)
    if notes is None:
        return
    logger.info("")
    logger.warning(
        "Consumer action — review these upgrade notes "
        "(any newer than your previous forge version):",
    )
    for line in notes.splitlines():
        logger.info("  %s", line)
    logger.info("")
    logger.info("Full release history: CHANGELOG.md in the forge repo.")


def _run_phase2() -> int:
    """Phase 2 — run install-forge-bootstrap; print plugin reminder.

    Wrapped in :func:`forge.run_context.progress_logger` so the substep
    boundary + total elapsed time appears in CI logs even though
    ``install-forge-bootstrap`` already emits its own per-step
    ``→ <slug>`` lines.

    Returns:
        Exit code from ``install-forge-bootstrap``. ``0`` plus a
        plugin-update reminder when bootstrap succeeds.
    """
    with progress_logger("bootstrap") as note:
        note("install-forge-bootstrap")
        saved_argv = sys.argv
        try:
            sys.argv = ["install-forge-bootstrap"]
            rc = _bootstrap_main()
        finally:
            sys.argv = saved_argv

    if rc == 0:
        logger.info("")
        logger.info("If you use Claude Code, finish the upgrade with:")
        logger.info("  /plugin update forge@forge")
        logger.info("  /reload-plugins")
        logger.info("Monitor changes still need a session restart.")
        _print_upgrade_notes()
    return rc


def _run_pip_install(
    ref: str,
    *,
    auth_mode: AuthMode,
    timeout_seconds: int | None,
) -> int:
    """Run the force-reinstall pip command, wrapped in a progress logger.

    The pip command uses ``--force-reinstall --no-deps`` so a moving
    branch ref (``@main`` / ``@dev``) actually re-fetches instead of
    silently freezing at the first install. Stdout / stderr stream
    straight through so the consumer sees pip's own progress in real
    time; the wrapping :func:`forge.run_context.progress_logger` adds
    start / done banners with elapsed time so the substep boundary is
    visible in CI logs.

    Args:
        ref: Target ref — ``main`` / ``dev`` / a tag like ``v1.3.0``.
        auth_mode: Selected by the caller from
            :func:`forge.run_context.git_auth_mode`. Picks the URL form
            (SSH vs. HTTPS) so the git subprocess inside pip can
            authenticate without prompting.
        timeout_seconds: Wall-clock cap on the pip subprocess. ``None``
            means no timeout (default for interactive workstation
            runs). A non-interactive caller should set this to a sane
            ceiling so a future hang fails fast and surfaces in the CI
            log instead of consuming the runner's max-runtime budget.

    Returns:
        Exit code from ``pip install``. Returns ``124`` when the
        timeout fires (matches the ``timeout(1)`` convention so CI
        diagnostics can distinguish "tool reported failure" from
        "watchdog killed it").
    """
    pip_cmd = _pip_command(ref, auth_mode=auth_mode)
    with progress_logger("pip install") as note:
        timeout_label = "none" if timeout_seconds is None else f"{timeout_seconds}s"
        note(f"auth={auth_mode} timeout={timeout_label}")
        note(pip_cmd)
        try:
            proc = subprocess.run(
                shlex.split(pip_cmd),
                check=False,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            note(
                f"timed out after {timeout_seconds}s "
                "(exit 124, matches GNU `timeout(1)`)"
            )
            return 124
    return proc.returncode


_DEFAULT_PIP_TIMEOUT_CI: int = 600


def _run_apply(args: argparse.Namespace, root: Path) -> int:
    """``--apply``: do phase 1 + run pip + do phase 2, in one command.

    Setup-script ergonomic for human-run rigs (Makefile / setup.sh /
    uv): a single command handles pin rewrite, force-reinstall, and
    artifact re-sync. **Forbidden for Claude Code agents per
    FOUNDATION §2** — the ``block_install_deps`` Claude hook refuses
    `forge-upgrade --apply` invocations.

    Consults :mod:`forge.run_context` for CI-awareness (per FOUNDATION
    §15):

    - :func:`is_non_interactive` decides the default pip timeout
      (``_DEFAULT_PIP_TIMEOUT_CI`` seconds in CI, no timeout
      interactively) so a CI hang fails fast and surfaces in the log.
      Override via ``--pip-timeout``.
    - :func:`git_auth_mode` picks the URL form the runner can actually
      authenticate against. When the detected mode is ``"none"`` AND
      we are non-interactive, abort with a clear error rather than
      block on a credential prompt against ``/dev/null``.

    Args:
        args: Parsed CLI args.
        root: Consumer repo root.

    Returns:
        ``0`` when all three phases succeed; non-zero from whichever
        step failed; ``2`` when the auth context is ``"none"`` in a
        non-interactive run.
    """
    rc, target_ref = _run_phase1(args, root)
    if rc != 0:
        return rc
    if target_ref is None:
        # Defensive: --apply rules out --check at argparse level, so
        # phase 1 either resolved a target or raised SystemExit. This
        # branch only fires if that contract changes.
        logger.error("--apply: phase 1 returned no target ref; aborting.")
        return 2

    auth_mode = git_auth_mode()
    non_interactive = is_non_interactive()
    if auth_mode == "none" and non_interactive:
        logger.error(
            "forge-upgrade --apply: no usable git auth detected "
            "(no SSH agent identity, no GITHUB_TOKEN / GH_TOKEN, no TTY). "
            "Aborting before pip blocks on a credential prompt. Set "
            "GITHUB_TOKEN, configure an SSH key, or run interactively.",
        )
        return 2

    timeout_seconds = args.pip_timeout
    if timeout_seconds is None and non_interactive:
        timeout_seconds = _DEFAULT_PIP_TIMEOUT_CI

    logger.info("")
    logger.info("Installing forge-scripts (--force-reinstall --no-deps)...")
    pip_rc = _run_pip_install(
        target_ref,
        auth_mode=auth_mode,
        timeout_seconds=timeout_seconds,
    )
    if pip_rc != 0:
        logger.error("pip install failed (exit %d) — aborting --apply.", pip_rc)
        return pip_rc

    logger.info("")
    logger.info("Re-syncing managed artifacts...")
    return _run_phase2()


def main() -> int:
    """One-command forge upgrade entry point.

    Returns:
        ``0`` on success; non-zero when the resolved target ref can't
        be determined or bootstrap re-sync fails.
    """
    parser = argparse.ArgumentParser(
        prog="forge-upgrade",
        description=(
            "Two-phase forge upgrade. Phase 1 (default): rewrite the "
            "forge-scripts pin in pyproject.toml + print the exact pip "
            "command. Phase 2 (--continue): after running pip, re-sync "
            "managed artifacts via install-forge-bootstrap."
        ),
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--channel",
        choices=("main", "dev"),
        help="Pin to a channel — `main` (slow, minor-only) or `dev` (every patch).",
    )
    group.add_argument(
        "--to",
        metavar="REF",
        type=_ref_type,
        help="Pin to a specific git ref (e.g. `v1.3.0`).",
    )
    parser.add_argument(
        "--continue",
        dest="phase2",
        action="store_true",
        help=(
            "Phase 2: run install-forge-bootstrap to re-sync managed "
            "artifacts. Use after the phase-1 pip command has been run."
        ),
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Dry-run: print what would change without rewriting the pin.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help=(
            "One-shot: rewrite the pin + run pip install --force-reinstall + "
            "re-sync managed artifacts. For human-run setup scripts only — "
            "Claude Code agents are blocked from this flag by "
            "block_install_deps (FOUNDATION §2)."
        ),
    )
    parser.add_argument(
        "--pip-timeout",
        type=int,
        default=None,
        metavar="SECONDS",
        help=(
            "Wall-clock cap on the pip subprocess during --apply. "
            f"Default: no timeout interactively, "
            f"{_DEFAULT_PIP_TIMEOUT_CI}s in CI (detected via "
            "FORGE_NON_INTERACTIVE / CI / stdin TTY). Returns exit "
            "124 on timeout (matches GNU timeout(1))."
        ),
    )
    args = parser.parse_args()

    root = repo_root()

    if args.phase2:
        if args.channel or args.to or args.check or args.apply:
            sys.stderr.write(
                "forge-upgrade: --continue cannot be combined with "
                "--channel / --to / --check / --apply.\n",
            )
            return 2
        return _run_phase2()

    if args.apply:
        if args.check:
            sys.stderr.write(
                "forge-upgrade: --apply and --check are mutually exclusive.\n",
            )
            return 2
        return _run_apply(args, root)

    rc, _target = _run_phase1(args, root)
    return rc


if __name__ == "__main__":
    sys.exit(main())
