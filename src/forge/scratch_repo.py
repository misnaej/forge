"""forge-scratch-repo — isolated scratch copies and scratch repos for agents.

Agents that only look (reviewers, test advisors) or only write tests
still need to try things: plant a bug to see whether a test catches it,
or build a small repo with commits to probe how git behaves. Doing that
in the shared checkout risks other sessions' work, and the guard hooks
refuse the git operations it would take there. This CLI is the
sanctioned place for it (FOUNDATION §11 "Probing"):

- ``snapshot`` copies the current repository into a fresh directory as
  a one-commit repo — the tree at ``--ref`` (default ``HEAD``), or with
  ``--worktree`` the tracked files as they are right now, uncommitted
  edits included (for mutation probes).
- ``build`` makes a small repo from a JSON spec of commits and branches
  (for git-behaviour probes).

Both print the new directory's absolute path; callers address it by that
path or with ``git -C <path>``, never with ``cd … &&`` (a failed ``cd``
leaves the next command running in the real checkout).

The source checkout is only ever read. Every git call this CLI makes into
the scratch directory disables hooks and signing and uses a fixed
identity, and runs with git's per-process environment overrides removed,
so a ``GIT_DIR`` inherited from the caller cannot redirect the writes
into the real repository. The new directory is always freshly created
and never lands inside a git work tree unless that location is ignored —
a scratch copy inside the checkout would show up as untracked work.
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from forge.git_utils import (
    configure_cli_logging,
    emit,
    git_env_overrides_removed,
    run_git,
)


configure_cli_logging()
logger = logging.getLogger(__name__)

# Flags for every git call into a scratch directory: no hooks (a scratch
# repo has no pre-commit gate to honour, and a global hooks path must not
# run against throwaway content), no signing prompt, one fixed identity.
_SCRATCH_GIT = (
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "commit.gpgsign=false",
    "-c",
    "tag.gpgsign=false",
    "-c",
    "user.name=forge-scratch",
    "-c",
    "user.email=forge-scratch@users.noreply.github.com",
)
_DEFAULT_BRANCH = "main"


class ScratchError(Exception):
    """A request this CLI refuses; the message says why."""


def _scratch_git(dest: Path, *args: str) -> str:
    """Run git inside the scratch directory *dest* with the scratch flags.

    Args:
        dest: The scratch repository.
        *args: Argv tail after the scratch ``-c`` flags.

    Returns:
        Trimmed stdout.
    """
    return run_git(*_SCRATCH_GIT, *args, cwd=dest)


def _work_tree_of(path: Path) -> Path | None:
    """Return the top of the git work tree containing *path*, if any.

    Args:
        path: An existing directory.

    Returns:
        The work-tree root, or ``None`` when *path* is not inside one.
    """
    top = run_git(
        "rev-parse", "--show-toplevel", cwd=path, check=False, log_errors=False
    )
    return Path(top) if top else None


def _check_parent(parent: Path) -> Path:
    """Validate the directory the scratch copy will be created in.

    Args:
        parent: Requested parent directory.

    Returns:
        The resolved parent.

    Raises:
        ScratchError: When *parent* does not exist, or lies inside a git
            work tree at a location that is not gitignored.
    """
    resolved = parent.expanduser().resolve()
    if not resolved.is_dir():
        msg = f"parent directory does not exist: {resolved}"
        raise ScratchError(msg)
    top = _work_tree_of(resolved)
    if top is None:
        # Not in a work tree — but possibly inside a repository's .git
        # directory, where rev-parse finds a git dir and no work tree.
        if run_git(
            "rev-parse", "--git-dir", cwd=resolved, check=False, log_errors=False
        ):
            msg = (
                f"refusing to create a scratch copy inside a .git directory: {resolved}"
            )
            raise ScratchError(msg)
        return resolved
    rel = resolved.relative_to(top.resolve())
    # check-ignore prints the path when it is ignored and nothing otherwise.
    ignored = rel != Path() and bool(
        run_git(
            "check-ignore",
            "--",
            f"{rel.as_posix()}/",
            cwd=top,
            check=False,
            log_errors=False,
        )
    )
    if not ignored:
        msg = (
            f"refusing to create a scratch copy inside the git work tree {top} "
            f"({resolved} is not gitignored) — it would show up as untracked work "
            "in that checkout. Omit --parent to use the system temp dir, or "
            "point it at an ignored directory."
        )
        raise ScratchError(msg)
    return resolved


def _new_dest(parent: Path) -> Path:
    """Create a fresh, empty scratch directory under *parent*.

    Args:
        parent: A validated parent directory.

    Returns:
        The new directory's absolute path.
    """
    return Path(tempfile.mkdtemp(prefix="forge-scratch-", dir=parent)).resolve()


def _source_root() -> Path:
    """Return the work tree the caller is in — the snapshot source.

    Returns:
        The work-tree root.

    Raises:
        ScratchError: When the current directory is not in a git work tree.
    """
    top = _work_tree_of(Path.cwd())
    if top is None:
        msg = "snapshot must run inside a git checkout (the source to copy)"
        raise ScratchError(msg)
    return top


def _refuse_option_like(value: str, what: str) -> None:
    """Refuse a ref or branch name git would read as an option.

    Args:
        value: The ref, branch or start point from the caller.
        what: What *value* is, for the message.

    Raises:
        ScratchError: When *value* is empty or starts with ``-``.
    """
    if not value or value.startswith("-"):
        msg = f"{what} must be a ref name, not an option: {value!r}"
        raise ScratchError(msg)


def _export_ref(source: Path, ref: str, dest: Path) -> None:
    """Write the tree of *ref* in *source* into *dest*.

    Args:
        source: Source work tree (read only).
        ref: Commit-ish to export.
        dest: Empty destination directory.

    Raises:
        ScratchError: When *ref* does not name a commit in *source*.
    """
    _refuse_option_like(ref, "--ref")
    proc = subprocess.run(
        ["git", "archive", "--format=tar", f"{ref}^{{tree}}"],
        cwd=source,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        msg = f"cannot export {ref!r}: {proc.stderr.decode(errors='replace').strip()}"
        raise ScratchError(msg)
    # The "tar" filter refuses members that would land outside *dest*; it
    # keeps symlinks with absolute targets, which a repo may legitimately
    # track (the stricter "data" filter rejects them).
    with tarfile.open(fileobj=io.BytesIO(proc.stdout)) as archive:
        archive.extractall(dest, filter="tar")


def _copy_worktree(source: Path, dest: Path) -> None:
    """Copy *source*'s tracked files, as they are on disk now, into *dest*.

    Uncommitted edits are included; a tracked file deleted from the work
    tree is skipped, as is a submodule (a directory, not a file), and so
    is a file reached through a symlinked directory — it may live outside
    the checkout. A tracked symlink is copied as a link: one with an
    absolute target still points where it did, so writes through it in
    the copy land there.

    Args:
        source: Source work tree (read only).
        dest: Empty destination directory.
    """
    listing = run_git("ls-files", "-z", cwd=source)
    root = source.resolve()
    for rel in filter(None, listing.split("\0")):
        src = source / rel
        if src.parent.resolve() != (root / rel).parent:
            continue
        if not src.is_symlink() and not src.is_file():
            continue
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target, follow_symlinks=False)


def _commit_all(dest: Path, message: str) -> None:
    """Stage everything in *dest* (ignored paths too) and commit it.

    Args:
        dest: The scratch repository.
        message: Commit message.
    """
    # --force: a tracked file that also matches .gitignore must not be
    # silently dropped from the copy.
    _scratch_git(dest, "add", "--all", "--force")
    _scratch_git(dest, "commit", "--quiet", "--allow-empty", "-m", message)


def snapshot(*, ref: str | None, worktree: bool, parent: Path) -> Path:
    """Create a one-commit scratch copy of the current checkout.

    Args:
        ref: Commit-ish to export; ``None`` means ``HEAD`` unless
            *worktree* is set.
        worktree: Copy tracked files as they are on disk, uncommitted
            edits included, instead of a committed tree.
        parent: Directory to create the copy in.

    Returns:
        The scratch repository's absolute path.
    """
    source = _source_root()
    dest = _new_dest(_check_parent(parent))
    try:
        if worktree:
            _copy_worktree(source, dest)
            described = f"working tree of {source}"
        else:
            _export_ref(source, ref or "HEAD", dest)
            described = f"{ref or 'HEAD'} of {source}"
        _scratch_git(dest, "init", "--quiet", "--initial-branch", _DEFAULT_BRANCH)
        _commit_all(dest, f"forge-scratch snapshot: {described}")
    except BaseException:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    return dest


def _spec_path(dest: Path, rel: str) -> Path:
    """Resolve a spec file path inside *dest*, refusing any escape.

    Args:
        dest: The scratch repository.
        rel: A relative POSIX path from the spec.

    Returns:
        The absolute path inside *dest*.

    Raises:
        ScratchError: For an absolute path, a ``..`` component, or any
            ``.git`` component.
    """
    pure = PurePosixPath(rel)
    if pure.is_absolute() or not pure.parts or {"..", ".git"} & set(pure.parts):
        msg = f"spec path must be relative, inside the repo and outside .git: {rel!r}"
        raise ScratchError(msg)
    return dest.joinpath(*pure.parts)


def _load_spec(text: str) -> list[dict[str, Any]]:
    """Parse and shape-check a build spec.

    Args:
        text: JSON text — a list of commit objects.

    Returns:
        The commit objects.

    Raises:
        ScratchError: When the spec is not a non-empty list of objects
            each carrying a string ``message``.
    """
    try:
        spec = json.loads(text)
    except json.JSONDecodeError as exc:
        msg = f"spec is not valid JSON: {exc}"
        raise ScratchError(msg) from exc
    if not isinstance(spec, list) or not spec:
        msg = "spec must be a non-empty JSON list of commits"
        raise ScratchError(msg)
    for i, commit in enumerate(spec):
        if not isinstance(commit, dict) or not isinstance(commit.get("message"), str):
            msg = f"spec commit {i} must be an object with a string 'message'"
            raise ScratchError(msg)
    return spec


def _switch_branch(dest: Path, commit: dict[str, Any], *, first: bool) -> None:
    """Put *dest* on the branch a spec commit names, creating it if needed.

    Args:
        dest: The scratch repository.
        commit: One spec commit (``branch`` and ``from`` are optional).
        first: Whether this is the first commit (the branch is unborn).
    """
    branch = commit.get("branch")
    start = commit.get("from")
    if not branch:
        return
    _refuse_option_like(str(branch), "spec branch")
    if start is not None:
        _refuse_option_like(str(start), "spec 'from'")
    if first:
        _scratch_git(dest, "symbolic-ref", "HEAD", f"refs/heads/{branch}")
        return
    exists = run_git(
        "rev-parse",
        "--verify",
        "--quiet",
        f"refs/heads/{branch}",
        cwd=dest,
        check=False,
        log_errors=False,
    )
    if exists:
        _scratch_git(dest, "checkout", "--quiet", branch)
    else:
        _scratch_git(
            dest, "checkout", "--quiet", "-b", branch, *([start] if start else [])
        )


def build(spec_text: str, *, parent: Path) -> Path:
    """Create a scratch repository from a JSON spec of commits.

    Each commit object carries ``message`` and optionally ``branch`` (the
    branch to commit on, created on first use), ``from`` (the start point
    for a new branch; default: the current commit), ``write`` (an object
    of path → file content) and ``delete`` (a list of paths). Commits are
    made in list order; the repo is left on the last commit's branch.

    Args:
        spec_text: The spec as JSON text.
        parent: Directory to create the repository in.

    Returns:
        The scratch repository's absolute path.
    """
    commits = _load_spec(spec_text)
    dest = _new_dest(_check_parent(parent))
    try:
        _scratch_git(dest, "init", "--quiet", "--initial-branch", _DEFAULT_BRANCH)
        for i, commit in enumerate(commits):
            _switch_branch(dest, commit, first=i == 0)
            for rel, content in (commit.get("write") or {}).items():
                target = _spec_path(dest, rel)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(str(content))
            for rel in commit.get("delete") or []:
                _spec_path(dest, rel).unlink(missing_ok=True)
            _commit_all(dest, commit["message"])
    except BaseException:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    return dest


def _parser() -> argparse.ArgumentParser:
    """Build the argument parser.

    Returns:
        The configured parser.
    """
    parser = argparse.ArgumentParser(
        prog="forge-scratch-repo",
        description=(
            "Create an isolated scratch copy or scratch repo for experiments "
            "(FOUNDATION §11 'Probing'). Prints the new directory's absolute path."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    snap = sub.add_parser("snapshot", help="One-commit copy of the current checkout.")
    mode = snap.add_mutually_exclusive_group()
    mode.add_argument("--ref", help="Commit-ish to copy (default: HEAD).")
    mode.add_argument(
        "--worktree",
        action="store_true",
        help="Copy tracked files as they are now, uncommitted edits included.",
    )
    bld = sub.add_parser("build", help="Repo built from a JSON spec of commits.")
    bld.add_argument("--spec", required=True, help="Spec file path, or '-' for stdin.")
    for p in (snap, bld):
        p.add_argument(
            "--parent",
            type=Path,
            default=Path(tempfile.gettempdir()),
            help="Directory to create the copy in (default: system temp dir).",
        )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the CLI.

    Args:
        argv: Arguments (default: ``sys.argv[1:]``).

    Returns:
        ``0`` on success, ``1`` when the request is refused or git fails.
    """
    args = _parser().parse_args(argv)
    try:
        with git_env_overrides_removed():
            if args.command == "snapshot":
                dest = snapshot(
                    ref=args.ref, worktree=args.worktree, parent=args.parent
                )
            else:
                text = (
                    sys.stdin.read()
                    if args.spec == "-"
                    else Path(args.spec).read_text()
                )
                dest = build(text, parent=args.parent)
    except (ScratchError, subprocess.CalledProcessError, OSError) as exc:
        # A refusal is an expected outcome with a complete message, not a
        # crash — no traceback.
        sys.stderr.write(f"forge-scratch-repo: {exc}\n")
        return 1
    else:
        emit(str(dest))
        return 0


if __name__ == "__main__":
    sys.exit(main())
