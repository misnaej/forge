"""Shared test fixtures for forge.

Lives at ``tests/conftest.py`` so pytest auto-discovers it. Exposes the
real-git helpers ``GIT_ENV``, ``init_git_repo`` and ``init_single_track_repo``
(ephemeral repos for the git-touching suites), the subprocess fakes
``FakeProc``, ``CapturedCalls`` and the ``make_fake_run`` factory — used by
tests that monkeypatch ``subprocess.run`` in any of the forge CLIs — and
``page_json``, one ``gh api --paginate`` page renderer shared by every
suite that fakes ``gh_comments.gh_api``.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest


if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


# Shared git author/committer identity for real-git tests, so commits and
# annotated tags find an identity without a ~/.gitconfig. PATH is forwarded
# so the git binary resolves.
GIT_ENV: dict[str, str] = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
    "PATH": os.environ.get("PATH", ""),
    # Read no configuration from the machine. A developer's global config
    # commonly sets init.defaultBranch=main and an identity; a CI runner
    # has neither, which is how these suites passed locally and failed on
    # the runner. Every branch name and identity the tests rely on is set
    # explicitly below.
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
}


@pytest.fixture(autouse=True)
def _git_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give every test a git identity in its own environment.

    ``GIT_ENV`` covers the git commands the tests run themselves, but a
    test that calls forge code which shells out to git — tagging a
    release, say — inherits the process environment instead. A
    workstation has a global identity there and a CI runner does not, so
    the call fails only on the runner. These variables are a fallback:
    where a real identity is configured, git prefers it.
    """
    for key, value in (
        ("GIT_AUTHOR_NAME", "t"),
        ("GIT_AUTHOR_EMAIL", "t@t"),
        ("GIT_COMMITTER_NAME", "t"),
        ("GIT_COMMITTER_EMAIL", "t@t"),
    ):
        monkeypatch.setenv(key, value)


def init_git_repo(repo: Path) -> None:
    """Initialize a minimal git repo with one empty commit on ``main``.

    Shared by the real-git suites (``git_utils``, ``verify_plugin_version``)
    so the ephemeral-repo boilerplate lives in one place.

    Args:
        repo: Directory to initialize. Must already exist.
    """
    for cmd in (
        ["git", "init", "-q", "-b", "main"],
        # Write the identity into the repo, not just this call's env: the
        # suites run plenty of later `git` calls without `env=GIT_ENV`,
        # and those inherit whatever the machine has. A workstation has a
        # global identity and a CI runner does not, which is how these
        # tests passed locally and failed on the runner.
        ["git", "config", "user.name", "t"],
        ["git", "config", "user.email", "t@t"],
        ["git", "commit", "-q", "--allow-empty", "-m", "initial"],
    ):
        subprocess.run(cmd, cwd=repo, env=GIT_ENV, check=True)


def commit_all(repo: Path, message: str) -> None:
    """Stage everything and commit with *message*.

    Shared by the real-git suites (``git_utils``, ``rebump``) — one
    stage-and-commit helper instead of per-file copies.

    Args:
        repo: Repo root.
        message: Commit message.
    """
    subprocess.run(["git", "add", "-A"], cwd=repo, env=GIT_ENV, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", message], cwd=repo, env=GIT_ENV, check=True
    )


def init_single_track_repo(base: Path) -> tuple[Path, Path]:
    """Initialize a paired work/bare single-track git repository under *base*.

    ``base/work`` on ``main`` only, wired to ``base/origin.git`` with
    ``main`` pushed. Shared by the ``forge-release`` suites so the
    work-tree/bare-origin plumbing lives in one place; callers layer
    their repo-shape payload (files, tags, branches) on top.

    Args:
        base: Parent directory; must already exist. ``work`` and
            ``origin.git`` are created inside it.

    Returns:
        A ``(work, bare)`` tuple of the work-tree and bare-repo paths.
    """
    work = base / "work"
    bare = base / "origin.git"
    work.mkdir()
    bare.mkdir()
    init_git_repo(work)
    subprocess.run(
        ["git", "init", "--bare", "-q", "-b", "main"],
        cwd=bare,
        env=GIT_ENV,
        check=True,
    )
    subprocess.run(
        ["git", "remote", "add", "origin", str(bare)],
        cwd=work,
        env=GIT_ENV,
        check=True,
    )
    subprocess.run(
        ["git", "push", "-q", "origin", "main"], cwd=work, env=GIT_ENV, check=True
    )
    return work, bare


def _detach_head(repo: Path) -> None:
    """Detach HEAD in *repo* at its current commit (empties ``--show-current``).

    Shared by the ``changelog`` / ``precommit`` real-git suites that exercise
    the CI ``pull_request`` detached-HEAD path (empty local branch name,
    ``GITHUB_HEAD_REF`` fallback).

    Args:
        repo: Git repo working tree.
    """
    subprocess.run(
        ["git", "checkout", "-q", "--detach", "HEAD"], cwd=repo, env=GIT_ENV, check=True
    )


def tag_exists(repo: Path, tag: str) -> bool:
    """Return whether *repo* (work tree or bare) carries *tag*.

    Args:
        repo: Repo to check.
        tag: Tag name to look for.

    Returns:
        ``True`` when git reports the tag in that repo.
    """
    result = subprocess.run(
        ["git", "tag", "--list", tag],
        cwd=repo,
        env=GIT_ENV,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip() == tag


@dataclass
class FakeProc:
    """Minimal ``subprocess.CompletedProcess`` stand-in.

    Attributes:
        returncode: Simulated exit code.
        stdout: Simulated standard output.
        stderr: Simulated standard error.
    """

    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


@dataclass
class CapturedCalls:
    """Holder for ``subprocess.run`` argvs intercepted by a fake.

    Attributes:
        calls: List of argv lists captured in invocation order.
        telemetry_flags: List of ``telemetry`` kwargs captured in invocation
            order, for fakes (e.g. ``run_pytest`` stand-ins) that also record
            a per-call telemetry toggle. Empty unless a caller appends to it.
        labels: List of ``label`` kwargs captured in invocation order, for
            fakes that also record the telemetry run label (#376). Empty
            unless a caller appends to it.
    """

    calls: list[list[str]] = field(default_factory=list)
    telemetry_flags: list[bool] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)


def page_json(*items: object) -> str:
    """Render one ``gh api --paginate --jq '[...]'`` output page.

    Shared by every suite that fakes ``gh_comments.gh_api`` (comment
    listings for the squash-comment and wrap-up CLIs) — a page is a
    single JSON array line, as ``gh`` emits per page.

    Args:
        *items: Mappings or bare values the fake endpoint should return.

    Returns:
        A single JSON array line.
    """
    return json.dumps(list(items))


def make_fake_run(
    *,
    stdout: str = "",
    returncode: int = 0,
    captured: CapturedCalls | None = None,
) -> Callable[..., FakeProc]:
    """Return a ``subprocess.run`` replacement that records argvs.

    Args:
        stdout: ``stdout`` to return on every invocation.
        returncode: ``returncode`` to return on every invocation.
        captured: Optional ``CapturedCalls`` to push the argv into.
            If ``None``, calls are not retained.

    Returns:
        A callable compatible with ``subprocess.run`` signatures used by
        forge code. Ignores ``**kwargs`` (cwd, capture_output, etc.).
    """

    def _fake_run(cmd: list[str], **kwargs: object) -> FakeProc:
        del kwargs
        if captured is not None:
            captured.calls.append(cmd)
        return FakeProc(returncode=returncode, stdout=stdout)

    return _fake_run
