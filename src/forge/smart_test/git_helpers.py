"""Git plumbing for smart-test change detection.

A thin layer over :func:`forge.git_utils.run_git`: resolve the ref a
changeset should be measured against, and enumerate the Python files it
touched (the committed delta vs that base plus staged and unstaged
working-tree edits). Kept separate from the import-graph walk so the
"what changed?" question has a single home, and so the dependency layer
stays a pure function of a file set.
"""

from __future__ import annotations

import fnmatch
from typing import TYPE_CHECKING

from forge.config import load_config
from forge.git_utils import ref_exists, resolve_base_branch_ref, run_git


if TYPE_CHECKING:
    from pathlib import Path


def _ref_exists(repo_root: Path, ref: str) -> bool:
    """Return whether *ref* resolves to a commit in the repo.

    Thin alias over the shared :func:`forge.git_utils.ref_exists` probe;
    kept so this module's call sites stay stable.

    Args:
        repo_root: Git repo root.
        ref: Any ref or revision expression.

    Returns:
        ``True`` when ``git rev-parse --verify`` resolves *ref*.
    """
    return ref_exists(repo_root, ref)


def resolve_base_ref(repo_root: Path, override: str | None = None) -> str:
    """Resolve the ref to diff ``HEAD`` against for change detection.

    A feature branch's change set is its delta from where it diverged —
    the integration branch — so ``dev_branch`` is tried before
    ``base_branch``, each resolved origin-first (then local fallback) by
    the canonical :func:`forge.git_utils.resolve_base_branch_ref`. A
    plain ``HEAD`` is the last resort (yields only working-tree edits
    when nothing else resolves, e.g. a fresh clone with no remote).

    Args:
        repo_root: Git repo root.
        override: Explicit base ref from the caller (``--base``); used
            verbatim when it resolves, bypassing auto-detection. A
            ``-``-prefixed value is ignored (git would parse it as a
            flag, not a ref — same option-injection guard as
            :func:`forge.git_utils.resolve_base_branch_ref`; ``--base``
            may come from a CI wrapper, not only a human).

    Returns:
        A resolvable ref name.
    """
    if override and not override.startswith("-") and _ref_exists(repo_root, override):
        return override
    cfg = load_config(repo_root)
    for branch in (cfg.dev_branch, cfg.base_branch):
        ref = resolve_base_branch_ref(repo_root, branch)
        if ref is not None:
            return ref
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
    or module should still be selected). Conservative by design.

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


def changed_non_python_files(
    repo_root: Path, base_ref: str, *, ignore_globs: tuple[str, ...] = ()
) -> set[str]:
    """Return changed non-``.py`` files the selector cannot map to tests.

    The safe-fallback guarantee (FOUNDATION §17 / forge-docs/smart-test.md)
    rests on this: any change the import graph cannot reason about must
    escalate the run to ``full``. Same four change sources as
    :func:`changed_python_files`; paths matching *ignore_globs* (doc-only
    and metadata files that cannot affect test outcomes) are excluded.

    Args:
        repo_root: Git repo root.
        base_ref: Ref to diff against (see :func:`resolve_base_ref`).
        ignore_globs: ``fnmatch`` patterns for unmappable-but-harmless
            paths (e.g. ``*.md``).

    Returns:
        Repo-relative non-Python changed paths after ignores; a non-empty
        result means the caller must run the full suite.
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
        files.update(
            line for line in out.splitlines() if line and not line.endswith(".py")
        )
    return {
        rel
        for rel in files
        if not any(fnmatch.fnmatch(rel, pat) for pat in ignore_globs)
    }
