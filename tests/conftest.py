"""Shared test fixtures for forge.

Lives at ``tests/conftest.py`` so pytest auto-discovers it. Exposes the
real-git helpers ``GIT_ENV``, ``init_git_repo`` and ``init_single_track_repo``
(ephemeral repos for the git-touching suites), the subprocess fakes
``FakeProc``, ``CapturedCalls`` and the ``make_fake_run`` factory — used by
tests that monkeypatch ``subprocess.run`` in any of the forge CLIs —
``make_fake_push_branch`` and ``make_fake_push_tag``, the analogous
factories for a module's imported ``git_utils.push_branch`` /
``git_utils.push_tag`` names, ``page_json``, one ``gh api --paginate``
page renderer shared by every suite that fakes ``gh_comments.gh_api``, the
``# produced-at:`` provenance-stamp helpers (``PRODUCED_AT_RE``,
``log_body``) shared by every suite asserting on a ``code_health/*.log``
writer (FOUNDATION §13), and ``timing_log``, the ``precommit_timing.log``
body builder shared by the wrap-up-compose and evidence-pack suites.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest

from forge.git_utils import PushResult


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


# The ``# produced-at:`` provenance stamp every ``code_health/*.log`` writer
# prepends as line 1 (FOUNDATION §13, git_utils.build_stamp). One pattern
# here so every suite asserting on the shape agrees on it, and can pull the
# ``tree`` / ``head`` / ``when`` fields out of a captured stamp line.
PRODUCED_AT_RE = re.compile(
    r"# produced-at: tree=(?P<tree>[0-9a-f]{40}|unknown) "
    r"head=(?P<head>\S+) (?P<when>\S+)"
)


# The fixed placeholder stamp `timing_log` prepends by default: a reader
# that only cares about the marker rows (not real tree freshness) must
# never mistake the stamp line for a step row.
_UNKNOWN_TIMING_STAMP = (
    "# produced-at: tree=unknown head=abc1234 2026-01-01T00:00:00+00:00"
)


def timing_log(*rows: str, stamp: str | None = _UNKNOWN_TIMING_STAMP) -> str:
    """Build a ``precommit_timing.log`` body from ``"<name> <marker>"`` specs.

    Shared by every suite reading a timing log's step rows
    (``pr_wrapup_compose``'s ``_code_quality_rows``, ``pr_evidence``'s
    ``_timing_snapshot``) — one row-shape builder instead of two
    near-identical copies.

    Args:
        *rows: Each a ``"<name> <marker>"`` pair (marker is one of SKIP,
            PASS, WARN, FAIL).
        stamp: The ``# produced-at:`` line to prepend, or ``None`` to
            omit it. The default placeholder suits a reader that only
            parses marker rows; a caller judging real tree freshness
            passes a real :func:`forge.git_utils.produced_at_stamp` line.

    Returns:
        A timing-log body matching ``precommit._format_timing_log``'s shape.
    """
    lines = []
    if stamp is not None:
        lines.append(stamp)
    lines.append("forge-precommit per-step timing (newest run overwrites)")
    lines.append("")
    total = 0.0
    for spec in rows:
        name, marker = spec.split()
        lines.append(f"{name:<28} {1.0:>7.1f}s  {marker}")
        total += 1.0
    lines.append("")
    lines.append(f"{'total':<28} {total:>7.1f}s")
    return "\n".join(lines)


def log_body(path: Path) -> str:
    """Return *path*'s content with a leading ``# produced-at:`` stamp stripped.

    Every ``code_health/*.log`` writer now prepends a provenance stamp as
    line 1 (FOUNDATION §13). Tests asserting a log's exact caller-supplied
    body read through this helper instead of comparing raw file content,
    so the assertion holds whether or not the stamp has landed yet —
    state-tolerant on purpose: only a line 1 actually starting with the
    marker is stripped, so a log written before the stamp existed (or by
    a writer this feature doesn't touch) round-trips unchanged.

    Args:
        path: Log file to read.

    Returns:
        The file's content with a leading stamp line (and its trailing
        newline) removed, or the full content unchanged when line 1 is
        not a ``# produced-at:`` stamp.
    """
    text = path.read_text(encoding="utf-8")
    first_line, sep, rest = text.partition("\n")
    if sep and first_line.startswith("# produced-at:"):
        return rest
    return text


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


def make_fake_push_branch(
    *,
    ok: bool = True,
    stderr: str = "",
    calls: list[tuple[object, object, dict[str, object]]] | None = None,
) -> Callable[..., PushResult]:
    """Return a ``git_utils.push_branch``-shaped fake recording its call args.

    Shared by every suite that monkeypatches a module's imported
    ``push_branch`` name directly (``resync``, ``changelog_fragments``)
    instead of faking the ``subprocess.run`` call underneath it.
    ``PushResult`` is already a plain data object — ``git_utils``'s own
    Null-result shape — so this only wraps it in a recorder, never
    reimplements it.

    Args:
        ok: Simulated push success.
        stderr: Simulated git stderr (only meaningful when ``ok=False``).
        calls: Optional list to append each ``(root, branch, kwargs)``
            call into; omit when a test doesn't need to inspect the call.

    Returns:
        A callable matching ``push_branch``'s ``(root, branch, **kwargs)``
        call shape.
    """

    def _fake_push_branch(root: object, branch: object, **kwargs: object) -> PushResult:
        if calls is not None:
            calls.append((root, branch, kwargs))
        return PushResult(ok=ok, returncode=0 if ok else 1, stderr=stderr)

    return _fake_push_branch


def make_fake_push_tag(
    *,
    ok: bool = True,
    stderr: str = "",
    calls: list[tuple[object, object, dict[str, object]]] | None = None,
) -> Callable[..., PushResult]:
    """Return a ``git_utils.push_tag``-shaped fake recording its call args.

    The tag sibling of :func:`make_fake_push_branch`, for suites that
    monkeypatch a module's imported ``push_tag`` name. Both seams take
    ``(root, ref, **kwargs)`` and return the same ``PushResult``, so this
    delegates rather than restating the recorder — only the name the test
    reads differs.

    Args:
        ok: Simulated push success.
        stderr: Simulated git stderr (only meaningful when ``ok=False``).
        calls: Optional list to append each ``(root, tag, kwargs)`` call
            into; omit when a test doesn't need to inspect the call.

    Returns:
        A callable matching ``push_tag``'s ``(root, tag, **kwargs)`` call
        shape.
    """
    return make_fake_push_branch(ok=ok, stderr=stderr, calls=calls)
