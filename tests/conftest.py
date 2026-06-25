"""Shared test fixtures for forge.

Lives at ``tests/conftest.py`` so pytest auto-discovers it. Exposes a
canonical ``FakeProc`` stand-in and a ``fake_subprocess_run`` factory
that captures call argvs — used by tests that monkeypatch
``subprocess.run`` in any of the forge CLIs.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from typing import TYPE_CHECKING


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
}


def init_git_repo(repo: Path) -> None:
    """Initialize a minimal git repo with one empty commit on ``main``.

    Shared by the real-git suites (``git_utils``, ``verify_plugin_version``,
    ``verify_main_tags``) so the ephemeral-repo boilerplate lives in one
    place.

    Args:
        repo: Directory to initialize. Must already exist.
    """
    for cmd in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "commit", "-q", "--allow-empty", "-m", "initial"],
    ):
        subprocess.run(cmd, cwd=repo, env=GIT_ENV, check=True)


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
    """

    calls: list[list[str]] = field(default_factory=list)


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
