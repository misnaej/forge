"""Shared git utilities for verification scripts.

Provides common helpers used by the forge CLIs: locating the repo root,
detecting modified files relative to main, and emitting CLI output
that bypasses ruff's T201 (bare-print) ban.
"""

import hashlib
import io
import json
import logging
import os
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


def next_version(latest_tag: str | None, bump: str) -> str:
    """Return the ``vX.Y.Z`` tag that follows *latest_tag* for a semver *bump*.

    Pure semver arithmetic — no git. Public API for tag-versioned
    (setuptools-scm) consumer repos composing a release flow off
    :func:`latest_v_tag`, and the version source for ``forge-release``.

    Args:
        latest_tag: Latest release tag (e.g. ``"v1.2.3"``; bare ``"1.2.3"``
            accepted). ``None`` — or a tag :func:`parse_semver` cannot
            read — is treated as a ``v0.0.0`` base, so a repo with no
            releases yet gets ``v0.1.0`` from a ``"minor"`` bump.
        bump: One of ``"major"``, ``"minor"``, ``"patch"``.

    Returns:
        The bumped tag, always ``v``-prefixed (``"v1.2.3"`` + ``"minor"``
        → ``"v1.3.0"``).

    Raises:
        ValueError: When *bump* is not a recognized increment name.
    """
    major, minor, patch = parse_semver(latest_tag or "") or (0, 0, 0)
    if bump == "major":
        return f"v{major + 1}.0.0"
    if bump == "minor":
        return f"v{major}.{minor + 1}.0"
    if bump == "patch":
        return f"v{major}.{minor}.{patch + 1}"
    msg = f"unknown bump {bump!r}: expected 'major', 'minor', or 'patch'"
    raise ValueError(msg)


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


def forge_install_command(extra: str | None = None) -> str:
    """Format the consumer-valid install command for forge-scripts.

    Args:
        extra: Optional forge-scripts extras group (e.g. ``"typecheck"``).

    Returns:
        ``pip install forge-scripts``, with the bracketed extras group
        (quoted for shell safety) when *extra* is given.
    """
    if extra is None:
        return "pip install forge-scripts"
    return f'pip install "forge-scripts[{extra}]"'


def missing_dependency_hint(package: str, *, extra: str | None = None) -> str:
    """Format a user-facing hint for a missing dependency.

    Single source of truth for the install command named in every
    missing-dependency message forge emits. The command must be valid
    from any consumer repo — never an editable ``-e ".[...]"`` form,
    which only works from a checkout of forge itself.

    Args:
        package: Distribution (pip) name of the missing dependency — the
            name a user recognizes from install output (e.g. ``vulture``,
            ``PyYAML``), not the import name where the two diverge.
        extra: forge-scripts extras group that provides the package, or
            ``None`` when it ships with the core install.

    Returns:
        One-line hint naming the package and its install command.
    """
    return f"`{package}` is not installed; run `{forge_install_command(extra)}`."


def require_cli(
    name: str,
    *,
    caller: str | None = None,
    extra: str | None = None,
    hint: str | None = None,
) -> None:
    """Abort with a clear install hint if *name* isn't on PATH.

    Foundation rule (FOUNDATION §2): forge-shipped CLIs (and external
    tools forge wraps) are required, not optional. Forge code fails
    loudly rather than silently substituting raw tools or producing
    degraded output.

    Args:
        name: Console-script name to check (e.g. ``"verify-forge-docstrings"``,
            ``"ruff"``, ``"gh"``).
        caller: Optional name of the CLI making the check (e.g.
            ``"forge-precommit"``). Used to prefix the error so the user
            knows which tool reported the missing dependency. Defaults
            to ``"forge"``.
        extra: forge-scripts extras group that provides *name* (e.g.
            ``"typecheck"`` for ``pyrefly``). Use for tools gated behind
            a forge-scripts optional-dependency group; omit for tools the
            core install carries.
        hint: Full replacement for the default install line. Takes
            precedence over *extra*. Use for external tools with no
            forge-scripts relationship (e.g. ``gh``).

    Raises:
        SystemExit: If *name* is not on PATH. Exit code is 2 (config error).
    """
    if shutil.which(name) is not None:
        return
    prefix = caller or "forge"
    line = hint or (
        f"Run `{forge_install_command(extra)}` (or your repo's equivalent) and retry."
    )
    sys.stderr.write(
        f"{prefix}: required CLI '{name}' not on PATH.\n  {line}\n",
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


def _run_git(*args: str, cwd: Path | None = None) -> str:
    """Run a git command and return stdout.

    Args:
        *args: Git command arguments.
        cwd: Directory to run git in. Defaults to the cached process-wide
            :func:`repo_root` when omitted.

    Returns:
        Stdout from the git command, or empty string on failure.
    """
    result = subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        cwd=cwd if cwd is not None else repo_root(),
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def run_git(
    *args: str,
    cwd: Path | None = None,
    check: bool = True,
    log_errors: bool = True,
) -> str:
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
        log_errors: When ``False``, suppress the failure log line and
            just raise — for callers that tolerate an expected failure
            (e.g. a raced tag push) and own the messaging themselves.

    Returns:
        Trimmed stdout.

    Raises:
        subprocess.CalledProcessError: When ``check=True`` and git exits
            non-zero. Git's captured stderr is logged before the raise —
            without it, CI logs show only a bare exit code and the actual
            git message ("unable to auto-detect email address", …) is
            invisible. Invariant for callers: never pass a
            credential-bearing arg or URL (e.g. a token-embedded remote)
            — a failure would echo it verbatim into CI logs.
    """
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=check,
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        if log_errors and detail:
            logger.exception(
                "git %s failed (exit %d): %s", " ".join(args), exc.returncode, detail
            )
        raise
    return proc.stdout.strip()


# The identity injected when a runner has none (fresh CI). Module-level
# so tests can exercise the exact production flags against real git.
_FALLBACK_IDENTITY = (
    "-c",
    "user.name=forge-release",
    "-c",
    "user.email=forge-release@users.noreply.github.com",
)


def _fallback_identity_args(repo_root: Path) -> list[str]:
    """Return ``-c`` identity flags when git has no usable tagger identity.

    ``git var GIT_COMMITTER_IDENT`` evaluates the same chain ``git tag``
    does (config, ``GIT_COMMITTER_*`` env, auto-detection), so any real
    identity wins and the fallback only fires where tagging would
    otherwise die with exit 128 (fresh CI runners). On failure that
    subcommand writes nothing to stdout (its message goes to stderr), so
    an empty probe result means "no identity" — a property of ``git
    var``, not of :func:`run_git`'s ``check=False`` mode.

    Args:
        repo_root: Repo root for the probe.

    Returns:
        ``["-c", "user.name=…", "-c", "user.email=…"]`` when no identity
        is available, else an empty list.
    """
    if run_git("var", "GIT_COMMITTER_IDENT", cwd=repo_root, check=False):
        return []
    return list(_FALLBACK_IDENTITY)


def create_annotated_tag(
    repo_root: Path,
    tag: str,
    *,
    commit: str = "HEAD",
    force: bool = False,
) -> None:
    """Create annotated *tag* at *commit*, surviving identity-less runners.

    The one tag-creation seam shared by every forge CLI that cuts an
    annotated tag (``forge-release``, ``forge-next-prep --tag``,
    ``forge-check-main-tags --fix``). Injects a fallback tagger identity
    only when git has none (see :func:`_fallback_identity_args`) — an
    annotated tag requires one, and a fresh CI runner configures none.

    Args:
        repo_root: Repo root.
        tag: Tag name to create (also used as the tag message).
        commit: Commit-ish to tag.
        force: Pass ``-f`` to relocate an existing tag.

    Raises:
        subprocess.CalledProcessError: When git fails for any other
            reason (stderr is logged by :func:`run_git`).
    """
    # `--` pins tag/commit as positionals so a `-`-prefixed value can
    # never be parsed as a git option, independent of caller validation.
    args = ["tag", *(["-f"] if force else []), "-a", "-m", tag, "--", tag, commit]
    run_git(*_fallback_identity_args(repo_root), *args, cwd=repo_root)


def resolve_current_branch(repo_root: Path) -> tuple[str, str] | None:
    """Return the current branch name and where it came from, or ``None``.

    The one branch-name resolver for per-PR logic (FOUNDATION §12):
    ``git branch --show-current`` wins when non-empty; on a detached
    HEAD — a CI ``pull_request`` checkout of ``refs/pull/N/merge`` —
    it is empty, so the PR source branch is read from the
    ``GITHUB_HEAD_REF`` env var instead (``GITHUB_REF_NAME`` is
    ``N/merge`` on that event, not the branch name).

    Args:
        repo_root: Git repo root.

    Returns:
        ``(branch, source)`` with source ``"local"`` or
        ``"GITHUB_HEAD_REF"``, or ``None`` when neither yields a name.
    """
    branch = run_git("branch", "--show-current", cwd=repo_root, check=False).strip()
    if branch:
        return branch, "local"
    head_ref = os.environ.get("GITHUB_HEAD_REF", "").strip()
    if head_ref:
        return head_ref, "GITHUB_HEAD_REF"
    return None


def ref_exists(repo_root: Path, ref: str) -> bool:
    """Return whether *ref* resolves to a commit in the repo.

    The shared ref-existence probe — candidate order and fallback policy
    live in :func:`resolve_base_branch_ref`, the single diff-base
    resolver every diff-scoped caller routes through. The ``^{commit}``
    peel makes it verify commit-ish-ness, not just name existence.

    Args:
        repo_root: Git repo root.
        ref: Any ref or revision expression.

    Returns:
        ``True`` when ``git rev-parse --verify`` resolves *ref* to a
        commit.
    """
    return bool(
        run_git(
            "rev-parse",
            "--verify",
            "--quiet",
            f"{ref}^{{commit}}",
            cwd=repo_root,
            check=False,
        )
    )


def merge_in_progress(repo_root: Path) -> bool:
    """Return whether *repo_root* has an in-progress (uncommitted) merge.

    Resolves ``MERGE_HEAD`` via ``git rev-parse --git-path`` rather than a
    hardcoded ``.git/MERGE_HEAD`` so linked worktrees and submodules (where
    the git dir lives elsewhere) are handled; ``--git-path`` may return a
    relative or absolute path, and the ``/`` join degrades correctly for
    both. Mid-merge, diff-vs-merge-base checks misattribute the base's
    changes to the branch (HEAD is still pre-merge), so callers use this to
    suppress that class of false positive.

    Args:
        repo_root: Git repo root.

    Returns:
        ``True`` when ``MERGE_HEAD`` exists (mid ``git merge``, before the
        merge commit).
    """
    git_path = run_git(
        "rev-parse", "--git-path", "MERGE_HEAD", cwd=repo_root, check=False
    )
    return bool(git_path) and (repo_root / git_path).exists()


def resolve_base_branch_ref(root: Path | None, base_branch: str) -> str | None:
    """Return the ref diff-scoped checks should compare against, origin-first.

    The single home for forge's diff-base policy (every diff-scoped
    caller — ``get_modified_files``, ``forge.changelog``,
    ``forge.precommit``, ``forge.smart_test`` — routes here): prefer
    ``origin/<base_branch>``, the authoritative merge target a PR
    actually lands on, and fall back to the local ``<base_branch>`` only
    when the remote ref is absent (offline clone, no remote). A local
    base branch is a convenience cache that silently rots when the
    developer doesn't pull; diffing against it makes already-merged
    commits look branch-added. Not a replacement for the origin-*only*
    probes in the promotion checks (``verify_changelog_history``,
    ``verify_main_tags``, ``release``) — those must see published state
    and deliberately have no local fallback.

    A *base_branch* starting with ``-`` is rejected outright: git would
    parse it as a flag, not a ref (option injection via a crafted
    ``[tool.forge].base_branch``), and no real branch name starts with a
    dash.

    Args:
        root: Git repo root; ``None`` uses the cached process-wide
            :func:`repo_root`.
        base_branch: Configured base-branch name.

    Returns:
        ``origin/<base_branch>`` or ``<base_branch>``, whichever
        resolves first; ``None`` when neither does or *base_branch* is
        empty or flag-shaped.
    """
    if not base_branch or base_branch.startswith("-"):
        return None
    if root is None:
        root = repo_root()
    for ref in (f"origin/{base_branch}", base_branch):
        if ref_exists(root, ref):
            return ref
    return None


def merge_base_with_head(root: Path | None, base_branch: str) -> str:
    """Return the merge-base SHA of ``HEAD`` and the resolved base ref.

    Thin companion to :func:`resolve_base_branch_ref` for callers that
    want the divergence point rather than the ref itself — the
    origin-first policy stays in exactly one place.

    Args:
        root: Git repo root; ``None`` uses the cached process-wide
            :func:`repo_root`.
        base_branch: Configured base-branch name.

    Returns:
        The merge-base SHA, or ``""`` when no base ref resolves or the
        merge-base computation fails (unrelated histories).
    """
    ref = resolve_base_branch_ref(root, base_branch)
    if ref is None:
        return ""
    return _run_git("merge-base", ref, "HEAD", cwd=root)


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


# Paths treated as release-channel curated content: excluded from the
# release fingerprint so a release branch that finalizes them does not
# break tree-equality with the tagged dev release. The @main CHANGELOG is
# condensed per promotion (release-process.md §5), so it is the one file a
# correct release branch always diverges on.
_RELEASE_EQUAL_IGNORE = ("CHANGELOG.md",)


def release_tree_fingerprint(repo_root: Path, ref: str) -> str | None:
    """Return a content fingerprint of *ref*'s tree, ignoring ``CHANGELOG.md``.

    Like :func:`get_tree_sha`, but two refs whose trees differ **only** in
    ``CHANGELOG.md`` share a fingerprint. forge's ``@main`` CHANGELOG is
    curated and condensed per promotion — authored in the
    ``release/vX.Y.Z`` branch — so a release branch's tree never
    byte-matches the tagged ``dev`` release's tree, yet it is the *same
    release*. The rolling-next guard
    (:func:`forge.verify_plugin_version._is_release_commit`) and the
    main-tag aligner (``forge-check-main-tags``) compare on this
    fingerprint so curated-CHANGELOG divergence is tolerated while any
    other file difference still counts (the match stays release-exact).

    The value is the SHA-256 of ``git ls-tree -r <ref>`` (mode, type, blob
    SHA, path per file) with the ``CHANGELOG.md`` entry removed. Excluding
    one path from a recursive blob listing — rather than diffing two refs —
    keeps the result usable as a dict key, so callers can index many base
    commits by fingerprint in a single pass.

    Args:
        repo_root: Working directory for the git invocation.
        ref: Any commit-ish (``HEAD``, a tag, ``origin/main``, a SHA).

    Returns:
        A 64-char hex fingerprint, or ``None`` when *ref* does not resolve
        or its tree has no files outside ``CHANGELOG.md``.
    """
    raw = run_git("ls-tree", "-r", ref, cwd=repo_root, check=False)
    if not raw:
        return None
    kept = [
        line
        for line in raw.splitlines()
        if line.partition("\t")[2] not in _RELEASE_EQUAL_IGNORE
    ]
    if not kept:
        # Tree resolves only to ignored paths (e.g. a repo tracking nothing
        # but CHANGELOG.md). Returning a hash of "" would make every such
        # tree falsely release-equal; treat it as no usable release tree.
        return None
    return hashlib.sha256("\n".join(kept).encode()).hexdigest()


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
    repo_root: Path | None = None,
    base_branch: str = "main",
) -> list[str]:
    """Get list of modified files from git.

    Detects files modified in the current branch compared to the base
    branch, including branch commits, staged files, and unstaged changes.

    Strategy:
        - Feature branch: all files modified vs the base ref from
          :func:`resolve_base_branch_ref` — ``origin/<base_branch>``
          first, local ``<base_branch>`` as offline fallback
          (branch commits + staged + unstaged)
        - Base branch: files modified vs previous commit

    Args:
        suffix: File suffix to filter by. Defaults to '.py'.
        prefix: Optional path prefix(es) to filter by. Either a single
            string or a tuple of acceptable prefixes (e.g.,
            ``("test/", "tests/")`` to accept either test-dir layout).
        repo_root: Directory to run git in. Defaults to the process-wide
            cached :func:`repo_root`. Pass an explicit root when the caller
            already holds one — required for in-process callers like
            ``forge-precommit`` steps, where the cached global may point at
            a different repo than the one under check (e.g. in tests).
        base_branch: Branch the feature-branch diff compares against.
            Callers with a loaded ``[tool.forge]`` config pass
            ``cfg.base_branch`` (``forge.config.select_diff_files`` does);
            the default matches the config default.

    Returns:
        Deduplicated list of modified file paths matching the filters.
    """
    current_branch = _run_git("branch", "--show-current", cwd=repo_root)

    if current_branch and current_branch != base_branch:
        base = resolve_base_branch_ref(repo_root, base_branch)
        if base is not None:
            logger.info(
                "Checking files modified in '%s' compared to '%s'...",
                current_branch,
                base,
            )

            # Branch commits + staged + unstaged
            branch_files = _parse_files(
                _run_git("diff", "--name-only", f"{base}...HEAD", cwd=repo_root),
                suffix=suffix,
                prefix=prefix,
            )
            staged_files = _parse_files(
                _run_git("diff", "--name-only", "--cached", cwd=repo_root),
                suffix=suffix,
                prefix=prefix,
            )
            unstaged_files = _parse_files(
                _run_git("diff", "--name-only", cwd=repo_root),
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
                _run_git("diff", "--name-only", "HEAD~1", cwd=repo_root),
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
    repo_root: Path | None = None,
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
        repo_root: Directory to run ``git ls-files`` in. Defaults to the
            process-wide cached :func:`repo_root`. Pass an explicit root
            when the caller already holds one (so the tracked-set query
            targets *that* repo, not the cached global — the seam that lets
            the selector be exercised against a temp git repo in tests).

    Returns:
        Sorted, deduplicated list of tracked file paths matching the filters.
    """
    out = _run_git("ls-files", cwd=repo_root)
    return sorted(set(_parse_files(out, suffix=suffix, prefix=prefix)))


def get_untracked_files(
    *,
    suffix: str = ".py",
    prefix: str | tuple[str, ...] | None = None,
    repo_root: Path | None = None,
) -> list[str]:
    """Get untracked, non-gitignored files matching the suffix/prefix filters.

    The complement to :func:`get_tracked_files`: files present on disk but
    absent from the index and **not** gitignored (``git ls-files --others
    --exclude-standard``) — the "forgot to ``git add``" set. Sole use is
    warning when a first-party source file is silently skipped by a
    tracked-set scan (issue #164). A gitignored file is *deliberately*
    out of scope (issue #161) and is never listed here — that is exactly
    what ``--exclude-standard`` filters out.

    Args:
        suffix: File suffix to filter by. Defaults to '.py'.
        prefix: Optional path prefix(es) to filter by. Either a single
            string or a tuple of acceptable prefixes (e.g.,
            ``("test/", "tests/")`` to accept either test-dir layout).
        repo_root: Directory to run ``git ls-files`` in. Defaults to the
            process-wide cached :func:`repo_root`. Pass an explicit root
            when the caller already holds one (the seam that lets the
            query be exercised against a temp git repo in tests).

    Returns:
        Sorted, deduplicated list of untracked (non-ignored) file paths
        matching the filters.
    """
    out = _run_git("ls-files", "--others", "--exclude-standard", cwd=repo_root)
    return sorted(set(_parse_files(out, suffix=suffix, prefix=prefix)))


def path_escapes_repo(repo_root: Path, path: str) -> bool:
    """Return True if *path* resolves outside *repo_root*.

    The one repo-containment predicate shared by every caller that must not
    hand a tool a path escaping the repo: it resolves ``repo_root / path``
    (following ``..``, absolute-path replacement, and symlinks) and checks
    the result is neither the repo root itself nor a descendant of it. Only
    the *action* on a True result differs by caller — the diff-scope selector
    (:func:`forge.config.select_diff_files`) drops the offender, while
    ``fix_ruff._validate_paths`` raises on it (untrusted argv is a fail-loud
    boundary). Keeping the test itself in one place means a future edge case
    (a new symlink or platform quirk) is fixed once, not per call site.

    Args:
        repo_root: Git repo root the *path* is interpreted relative to.
        path: Candidate path (repo-relative string, or absolute).

    Returns:
        True when the resolved path lies outside *repo_root*.
    """
    repo_real = repo_root.resolve()
    candidate = (repo_root / path).resolve()
    return candidate != repo_real and repo_real not in candidate.parents


def stage_modified_paths(repo_root: Path, pathspecs: list[str]) -> list[str]:
    """``git add`` tracked files modified within *pathspecs*.

    Pathspec-scoped on purpose: only modifications under the given pathspecs
    are re-staged, so a step that mutates a file (ruff reformat, a doc
    regenerator) folds *its own* change into the commit without sweeping in
    unrelated unstaged edits elsewhere in the working tree. Both
    :mod:`forge.fix_ruff` (source dirs) and the doc-regen pre-commit step
    (specific doc paths) share this one git-add-back helper.

    Args:
        repo_root: Git repo root (the subprocess ``cwd``).
        pathspecs: Paths relative to *repo_root* limiting which modifications
            are eligible for re-staging.

    Returns:
        Newly-staged file paths (relative to repo root). Empty when no
        in-scope file changed or *repo_root* is not a git repo.
    """
    if not (repo_root / ".git").exists():
        return []
    proc = subprocess.run(
        ["git", "diff", "--name-only", "--", *pathspecs],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return []
    files = [line for line in proc.stdout.splitlines() if line.strip()]
    if not files:
        return []
    subprocess.run(["git", "add", "--", *files], cwd=repo_root, check=False)
    return files
