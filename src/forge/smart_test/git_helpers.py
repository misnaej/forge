"""Git plumbing for smart-test change detection.

A thin layer over :func:`forge.git_utils.run_git`: resolve the ref a
changeset should be measured against, and enumerate the Python files it
touched (the committed delta vs that base plus staged and unstaged
working-tree edits). Kept separate from the import-graph walk so the
"what changed?" question has a single home, and so the dependency layer
stays a pure function of a file set.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from forge.config import load_config
from forge.git_utils import run_git


if TYPE_CHECKING:
    from pathlib import Path


def _ref_exists(repo_root: Path, ref: str) -> bool:
    """Return whether *ref* resolves to a commit in the repo.

    Args:
        repo_root: Git repo root.
        ref: Any ref or revision expression.

    Returns:
        ``True`` when ``git rev-parse --verify`` resolves *ref*.
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


def resolve_base_ref(repo_root: Path, override: str | None = None) -> str:
    """Resolve the ref to diff ``HEAD`` against for change detection.

    A feature branch's change set is its delta from where it diverged —
    the integration branch — so the remote-tracking ``origin/<dev_branch>``
    is preferred, then its local name, then the base branch, then a plain
    ``HEAD`` (which yields only working-tree edits when nothing else
    resolves, e.g. a fresh clone with no remote).

    Args:
        repo_root: Git repo root.
        override: Explicit base ref from the caller (``--base``); used
            verbatim when it resolves, bypassing auto-detection.

    Returns:
        A resolvable ref name.
    """
    if override and _ref_exists(repo_root, override):
        return override
    cfg = load_config(repo_root)
    candidates = (
        f"origin/{cfg.dev_branch}",
        cfg.dev_branch,
        f"origin/{cfg.base_branch}",
        cfg.base_branch,
    )
    for candidate in candidates:
        if _ref_exists(repo_root, candidate):
            return candidate
    return "HEAD"


def head_commit_message(repo_root: Path) -> str:
    """Return ``HEAD``'s full commit message (subject + body).

    Used by ``--from-commit-message`` to read a depth directive (e.g.
    ``[depth-2]`` / ``[full]``) a CI job left in the commit. Returns an
    empty string when there is no commit yet.

    Args:
        repo_root: Git repo root.

    Returns:
        The commit message, or ``""`` when unavailable.
    """
    return run_git("log", "-1", "--format=%B", cwd=repo_root, check=False)


def changed_python_files(repo_root: Path, base_ref: str) -> set[str]:
    """Return repo-relative ``.py`` files changed vs *base_ref*.

    Unions four sources so every file the changeset could affect is
    covered regardless of commit state: the committed delta since the
    merge-base with *base_ref* (the three-dot ``base...HEAD`` semantics,
    so unrelated base-branch commits don't inflate the set), unstaged and
    staged working-tree edits, and **untracked** files (a brand-new test
    or module should still be selected). Conservative by design (#8).

    Args:
        repo_root: Git repo root.
        base_ref: Ref to diff against (see :func:`resolve_base_ref`).

    Returns:
        Repo-relative paths ending in ``.py``; empty when nothing changed.
    """
    merge_base = run_git("merge-base", base_ref, "HEAD", cwd=repo_root, check=False)
    diff_base = merge_base or base_ref
    arg_sets = (
        ("diff", "--name-only", diff_base, "HEAD"),
        ("diff", "--name-only"),
        ("diff", "--name-only", "--cached"),
        ("ls-files", "--others", "--exclude-standard"),
    )
    files: set[str] = set()
    for args in arg_sets:
        out = run_git(*args, cwd=repo_root, check=False)
        files.update(line for line in out.splitlines() if line.endswith(".py"))
    return files
