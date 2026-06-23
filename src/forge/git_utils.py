"""Shared git utilities for verification scripts.

Provides common helpers used by the forge CLIs: locating the repo root,
detecting modified files relative to main, and emitting CLI output
that bypasses ruff's T201 (bare-print) ban.
"""

import io
import json
import logging
import re
import shutil
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path


logger = logging.getLogger(__name__)


# Canonical org/repo identifier for forge upstream. Single source of
# truth — every forge module that needs to talk to / link to the forge
# repo imports this constant. Carved out in FOUNDATION §2 as the one
# place where the org name may appear as a literal.
_FORGE_GITHUB_REPO = "misnaej/forge"


@lru_cache(maxsize=1)
def repo_root() -> Path:
    """Return the git repo root for the current working directory.

    Cached for the lifetime of the process — the repo root does not change
    mid-run, and audit scripts call this in hot loops (once per finding).

    Returns:
        Absolute ``Path`` to the repo root.

    Raises:
        SystemExit: If the current directory is not inside a git repo.
    """
    proc = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        sys.stderr.write("forge: not inside a git repo\n")
        raise SystemExit(1)
    return Path(proc.stdout.strip())


def configure_cli_logging() -> None:
    """Apply forge's canonical CLI logging setup.

    Sets the root logger to ``INFO`` with a bare-message formatter so CLI
    output to stdout/stderr looks like plain command output (no
    ``YYYY-MM-DD HH:MM:SS,mmm levelname`` prefix). Every forge CLI
    module calls this once at import time so library output is uniform
    across the package.

    Safe to call multiple times — ``logging.basicConfig`` is a no-op when
    handlers are already attached to the root logger.
    """
    logging.basicConfig(level=logging.INFO, format="%(message)s")


def emit(msg: str) -> None:
    """Write *msg* to stdout with a trailing newline.

    Routes through ``sys.stdout.write`` rather than ``print`` so CLI
    output that is part of the program's interface is not flagged by
    ruff's T201 (bare-print) rule.

    Args:
        msg: Line to emit.
    """
    sys.stdout.write(msg + "\n")


_SEMVER_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)")


def parse_semver(version: str) -> tuple[int, int, int] | None:
    """Parse the leading ``X.Y.Z`` (optional ``v`` prefix) of a version string.

    Tolerates suffixes (``-rc1``, ``+build``, ``.devN+gHASH`` from
    setuptools-scm) — only the major / minor / patch triple matters for
    forge's version comparisons.

    Single source of truth: ``forge.verify_plugin_version``,
    ``forge.next_prep``, and ``forge.install_claudemd`` all import this
    helper instead of carrying their own copies.

    Args:
        version: Version string from ``importlib.metadata.version``, a
            git tag, or ``plugin.json``.

    Returns:
        ``(major, minor, patch)`` tuple, or ``None`` if no leading
        ``X.Y.Z`` is parseable.
    """
    match = _SEMVER_RE.match(version.strip())
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def latest_v_tag(root: Path) -> str | None:
    """Return the highest ``v*`` git tag by semver sort, or ``None`` if none.

    Resolves the latest release **globally** — ``git tag --list "v*"
    --sort=-v:refname`` — independent of ``HEAD``'s ancestry. This is the
    single source of truth for "latest release tag", shared by the
    rolling-next pre-commit guard (``verify-forge-plugin-version``) and
    the auto-tagger (``forge-next-prep``). A branch-independent resolution
    is required in the dual-track (dev/main) model: a release tagged on
    one branch is not in the other's history, so an ancestry-scoped
    ``git describe`` would disagree with the auto-tagger and let a stale
    manifest slip past the guard.

    Args:
        root: Repo root (cwd for the git invocation).

    Returns:
        Tag name like ``"v1.2.9"``, or ``None`` when no ``v*`` tags exist.
    """
    proc = subprocess.run(
        ["git", "tag", "--list", "v*", "--sort=-v:refname"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    out = proc.stdout.strip()
    if not out:
        return None
    return out.splitlines()[0]


def require_cli(name: str, *, caller: str | None = None) -> None:
    """Abort with a clear install hint if *name* isn't on PATH.

    Foundation rule (FOUNDATION §2): forge-shipped CLIs (and external
    tools forge wraps) are required, not optional. Forge code fails
    loudly rather than silently substituting raw tools or producing
    degraded output.

    Args:
        name: Console-script name to check (e.g. ``"verify-forge-ruff"``,
            ``"ruff"``, ``"gh"``).
        caller: Optional name of the CLI making the check (e.g.
            ``"forge-precommit"``). Used to prefix the error so the user
            knows which tool reported the missing dependency. Defaults
            to ``"forge"``.

    Raises:
        SystemExit: If *name* is not on PATH. Exit code is 2 (config error).
    """
    if shutil.which(name) is not None:
        return
    prefix = caller or "forge"
    sys.stderr.write(
        f"{prefix}: required CLI '{name}' not on PATH.\n"
        f'  Run `pip install -e ".[dev]"` (or your repo\'s equivalent) '
        "and retry.\n",
    )
    raise SystemExit(2)


def write_step_log(repo_root: Path, name: str, output: str) -> Path:
    """Write *output* to ``code_health/<name>.log`` under *repo_root*.

    Shared helper for every forge phase CLI. Ensures every step writes
    its log the same way — same path, same trailing-newline convention,
    same parent-dir creation — so agents can read
    ``code_health/<step>.log`` regardless of which CLI produced it.

    Args:
        repo_root: Git repo root.
        name: Step name (slug, no extension). Becomes
            ``code_health/<name>.log``. Any path separators are stripped
            defensively so a slug like ``"../etc"`` cannot escape the
            ``code_health/`` directory — even though every current
            caller passes a hard-coded literal.
        output: Log content. A trailing newline is added if missing.

    Returns:
        The full path to the written log file.
    """
    safe_name = Path(name).name
    log_path = repo_root / "code_health" / f"{safe_name}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    text = output if output.endswith("\n") else output + "\n"
    log_path.write_text(text)
    return log_path


@contextmanager
def capturing_to_step_log(repo_root: Path, name: str) -> Iterator[None]:
    """Tee root-logger output into ``code_health/<name>.log`` for the block.

    Phase CLIs whose output is built up across many ``logger.info`` calls
    (rather than a single concatenated string) wrap their ``main()`` body
    in this context manager. Every record emitted on the root logger is
    accumulated in memory, then written to ``code_health/<name>.log`` on
    exit. Stdout output is unaffected — the user still sees the same
    interactive feedback.

    Pairs with :func:`write_step_log` for CLIs that DO build an explicit
    string: both ultimately produce the same on-disk artifact.

    Args:
        repo_root: Git repo root.
        name: Step slug (no extension). Becomes ``code_health/<name>.log``.

    Yields:
        Nothing — the CLI body runs unchanged inside the ``with`` block.
    """
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.setLevel(logging.DEBUG)
    root = logging.getLogger()
    # Force INFO so info records reach handlers even when the root logger was
    # left at its default WARNING (e.g. under pytest, where basicConfig is
    # a no-op because pytest already attached a handler).
    saved_level = root.level
    if saved_level > logging.INFO or saved_level == logging.NOTSET:
        root.setLevel(logging.INFO)
    root.addHandler(handler)
    try:
        yield
    finally:
        root.removeHandler(handler)
        root.setLevel(saved_level)
        write_step_log(repo_root, name, buf.getvalue())


def gh_api(*args: str, timeout: int = 10) -> str | None:
    """Run ``gh api`` with *args* and return stripped stdout, or ``None``.

    Forge's canonical wrapper for advisory GitHub API calls. Failure
    of any kind — missing ``gh``, no network, auth error, timeout,
    non-zero exit, empty stdout — collapses to ``None``. Every caller
    treats the helper as best-effort and skips the feature when
    ``None`` is returned. Use :func:`require_cli` when a strict
    dependency on ``gh`` is needed; ``gh_api`` is the right primitive
    for everything else.

    Args:
        *args: Trailing arguments after ``gh api`` (e.g. an endpoint
            path + ``--jq`` expression).
        timeout: Hard timeout in seconds. Defaults to 10 — short
            enough to not block git hooks or CLI flows.

    Returns:
        Trimmed stdout on success; ``None`` on any failure.
    """
    try:
        proc = subprocess.run(
            ["gh", "api", *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    out = proc.stdout.strip()
    return out or None


def _run_git(*args: str) -> str:
    """Run a git command and return stdout.

    Args:
        *args: Git command arguments.

    Returns:
        Stdout from the git command, or empty string on failure.
    """
    result = subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        cwd=repo_root(),
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def run_git(*args: str, cwd: Path | None = None, check: bool = True) -> str:
    """Run ``git`` with *args* in *cwd* and return stripped stdout.

    The explicit-``cwd`` git runner shared by the release CLIs
    (``forge-next-prep``, ``forge-check-main-tags``). Distinct from
    :func:`_run_git`, which always targets the cached process-wide
    :func:`repo_root` and swallows errors — release tooling operates on
    a caller-supplied root and needs ``check=True`` to surface push /
    tag failures rather than silently continuing.

    Args:
        *args: Argv tail (without the leading ``git``).
        cwd: Working directory for the git invocation; defaults to the
            current directory.
        check: When ``True``, raise on a non-zero exit.

    Returns:
        Trimmed stdout.

    Raises:
        subprocess.CalledProcessError: When ``check=True`` and git exits
            non-zero.
    """
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=check,
    )
    return proc.stdout.strip()


def get_tree_sha(repo_root: Path, ref: str) -> str | None:
    """Return the git **tree** SHA of *ref*, or ``None`` when unresolvable.

    Tree (not commit) identity is forge's deterministic join between a
    release tag and the squash commit that reproduces it on another
    branch: a promotion squashes dev's history into a new commit whose
    *tree* equals the tagged dev commit's tree even though the commit
    SHA, parents, and message differ. Shared by the rolling-next guard
    (:func:`forge.verify_plugin_version._is_release_commit`) and the
    main-tag aligner (``forge-check-main-tags``).

    Args:
        repo_root: Working directory for the git invocation.
        ref: Any commit-ish (``HEAD``, a tag, ``origin/main``, a SHA).

    Returns:
        The 40-char tree SHA, or ``None`` when *ref* does not resolve.
    """
    proc = subprocess.run(
        ["git", "rev-parse", f"{ref}^{{tree}}"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    out = proc.stdout.strip()
    return out or None


def read_plugin_version_at_ref(repo_root: Path, ref: str) -> str | None:
    """Return ``plugin.json["version"]`` at *ref*, or ``None`` when absent.

    Reads the manifest out of the git object store at an arbitrary ref
    (``origin/dev``, a tag, a SHA) without a checkout. A missing manifest
    is common in non-plugin repos, so callers treat ``None`` as "not a
    plugin repo / nothing to compare".

    Args:
        repo_root: Working directory for the git invocation.
        ref: Any git refspec.

    Returns:
        Bare version string when ``.claude-plugin/plugin.json`` exists at
        *ref* and parses, ``None`` otherwise.
    """
    proc = subprocess.run(
        ["git", "show", f"{ref}:.claude-plugin/plugin.json"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return None
    try:
        return str(json.loads(proc.stdout)["version"])
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


def read_local_plugin_version(repo_root: Path) -> str | None:
    """Return the working-tree ``.claude-plugin/plugin.json["version"]``.

    The on-disk counterpart to :func:`read_plugin_version_at_ref`. Single
    source for "read the local manifest version", shared by the
    rolling-next guard and ``forge-next-prep``.

    Args:
        repo_root: Repo root.

    Returns:
        Bare semver string (e.g. ``"1.2.10"``), or ``None`` when the
        manifest is missing, unparseable, or the version field is absent
        / not semver-shaped.
    """
    plugin = repo_root / ".claude-plugin" / "plugin.json"
    if not plugin.is_file():
        return None
    try:
        data = json.loads(plugin.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    version = data.get("version")
    if not isinstance(version, str) or parse_semver(version) is None:
        return None
    return version


def _parse_files(
    output: str,
    *,
    suffix: str,
    prefix: str | tuple[str, ...] | None,
) -> list[str]:
    """Parse git diff output into a filtered file list.

    Args:
        output: Raw git diff output (newline-separated file paths).
        suffix: File suffix to filter by (e.g., '.py').
        prefix: Optional path prefix(es) to filter by. Either a single
            string (e.g., ``"tests/"``) or a tuple of acceptable prefixes
            (e.g., ``("test/", "tests/")`` to match either layout).

    Returns:
        List of file paths matching the filters.
    """
    if not output:
        return []
    files = [line.strip() for line in output.split("\n") if line.strip()]
    files = [f for f in files if f.endswith(suffix)]
    if prefix:
        files = [f for f in files if f.startswith(prefix)]
    return files


def get_modified_files(
    *,
    suffix: str = ".py",
    prefix: str | tuple[str, ...] | None = None,
) -> list[str]:
    """Get list of modified files from git.

    Detects files modified in the current branch compared to main,
    including branch commits, staged files, and unstaged changes.

    Strategy:
        - Feature branch: all files modified vs main/origin/main
          (branch commits + staged + unstaged)
        - Main branch: files modified vs previous commit

    Args:
        suffix: File suffix to filter by. Defaults to '.py'.
        prefix: Optional path prefix(es) to filter by. Either a single
            string or a tuple of acceptable prefixes (e.g.,
            ``("test/", "tests/")`` to accept either test-dir layout).

    Returns:
        Deduplicated list of modified file paths matching the filters.
    """
    current_branch = _run_git("branch", "--show-current")

    if current_branch and current_branch != "main":
        # Try main, then origin/main as base branch
        for base in ("main", "origin/main"):
            if not _run_git("rev-parse", "--verify", base):
                continue

            logger.info(
                "Checking files modified in '%s' compared to '%s'...",
                current_branch,
                base,
            )

            # Branch commits + staged + unstaged
            branch_files = _parse_files(
                _run_git("diff", "--name-only", f"{base}...HEAD"),
                suffix=suffix,
                prefix=prefix,
            )
            staged_files = _parse_files(
                _run_git("diff", "--name-only", "--cached"),
                suffix=suffix,
                prefix=prefix,
            )
            unstaged_files = _parse_files(
                _run_git("diff", "--name-only"),
                suffix=suffix,
                prefix=prefix,
            )

            all_files = branch_files + staged_files + unstaged_files
            if all_files:
                return sorted(set(all_files))

    # Fallback: compare to previous commit
    logger.info("Checking files modified compared to previous commit...")
    return sorted(
        set(
            _parse_files(
                _run_git("diff", "--name-only", "HEAD~1"),
                suffix=suffix,
                prefix=prefix,
            ),
        ),
    )


SCOPE_ALL = "all"
SCOPE_DIFF = "diff"
# The two file-selection scopes shared by the scope-aware pre-commit steps and
# their CLIs (ruff, docstrings, test-naming). Defined once here — co-located
# with the two file-source functions the scopes pick between — so the resolver
# and every `--scope` argparse choice reference one vocabulary.
VALID_SCOPES = (SCOPE_ALL, SCOPE_DIFF)


def get_tracked_files(
    *,
    suffix: str = ".py",
    prefix: str | tuple[str, ...] | None = None,
) -> list[str]:
    """Get all git-tracked files matching the suffix/prefix filters.

    The whole-repo counterpart to :func:`get_modified_files`: the file
    source for precommit steps running in ``scope = "all"`` mode, which
    check the entire tracked tree rather than the diff vs main.

    Args:
        suffix: File suffix to filter by. Defaults to '.py'.
        prefix: Optional path prefix(es) to filter by. Either a single
            string or a tuple of acceptable prefixes (e.g.,
            ``("test/", "tests/")`` to accept either test-dir layout).

    Returns:
        Sorted, deduplicated list of tracked file paths matching the filters.
    """
    return sorted(
        set(_parse_files(_run_git("ls-files"), suffix=suffix, prefix=prefix)),
    )
