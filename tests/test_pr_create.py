"""Tests for forge.pr_create — publish a PR from the branch it verifies.

# MOCKING STRATEGY: every scenario runs against a real ephemeral git repo
# (``tests.conftest.init_git_repo``) so the branch/HEAD the gate reads are
# real git state, not asserted text — that is the whole point of the
# module (FOUNDATION §12, module docstring). ``subprocess.run`` is
# replaced with a thin dispatcher that forwards anything but a ``gh``
# argv to the real implementation and records ``gh`` argvs instead of
# executing them — the seam is which binary the argv names, never the
# whole subprocess surface, so `git` calls the gate depends on
# (``rev-parse``, ``branch --show-current``) still run for real.
# `classify` and `emergency_consume` are patched at the `forge.pr_create`
# namespace (FOUNDATION §5) with plain callables/lambdas returning a
# canned verdict — the classifier and the sentinel each have their own
# test module; only the gate's dispatch on their result is under test
# here.
"""

from __future__ import annotations

import subprocess
import sys
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from forge import pr_create
from tests.conftest import GIT_ENV, commit_all, init_git_repo


if TYPE_CHECKING:
    from pathlib import Path


def _head_sha(repo: Path) -> str:
    """Return *repo*'s current HEAD commit's full sha.

    Args:
        repo: Git checkout to read HEAD from.

    Returns:
        The full HEAD sha.
    """
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        env=GIT_ENV,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _write_wrapup(repo: Path, sha: str, *, mode_line: str | None = None) -> None:
    """Write a `code_health/pr_wrapup.md` naming *sha*, optionally with a mode line.

    Args:
        repo: Repo root to write under — matches `WRAPUP_PATH`'s
            repo-relative resolution.
        sha: Commit sha (full or short) to embed in the `verified-at:` line.
        mode_line: Optional `wrapup-mode: ...` header line to append.
    """
    code_health = repo / "code_health"
    code_health.mkdir(parents=True, exist_ok=True)
    lines = ["# PR Wrap-up", "", f"verified-at: {sha}"]
    if mode_line is not None:
        lines.append(mode_line)
    (code_health / "pr_wrapup.md").write_text("\n".join(lines) + "\n")


def _stub_gh(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Route `gh` argvs to a recorder while leaving every other command real.

    Patches the shared `subprocess` module object (FOUNDATION §5 — the
    namespace `forge.pr_create` looks `run` up in), so both `pr_create`'s
    own `gh pr create` call and every `git` call the gate makes through
    `forge.git_utils` pass through the same dispatcher. Only a `gh` argv
    is intercepted; a stray real invocation would mean the gate reached
    publication when a test expected it to refuse first.

    Args:
        monkeypatch: Pytest's monkeypatch fixture, used to scope the patch
            to the calling test.

    Returns:
        The list `gh` argvs are appended to, in call order.
    """
    real_run = subprocess.run
    calls: list[list[str]] = []

    def _fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if cmd[0] == "gh":
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0)
        return real_run(cmd, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(pr_create.subprocess, "run", _fake_run)
    return calls


def _run_main(monkeypatch: pytest.MonkeyPatch, repo: Path, *extra_args: str) -> int:
    """Point `forge-pr-create` at *repo* and run it with a minimal argv.

    Args:
        monkeypatch: Pytest's monkeypatch fixture.
        repo: Checkout `repo_root()` should resolve to.
        *extra_args: Additional argv tokens appended after the required
            `--base`/`--title`/`--body-file` trio.

    Returns:
        `pr_create.main()`'s exit code.
    """
    monkeypatch.setattr(pr_create, "repo_root", lambda: repo)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "forge-pr-create",
            "--base",
            "main",
            "--title",
            "t",
            "--body-file",
            "body.md",
            *extra_args,
        ],
    )
    return pr_create.main()


def test_pr_create_publishes_when_wrapup_names_own_head(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A wrap-up naming the checkout's own HEAD lets the CLI publish it.

    Regression bound for the module's whole reason to exist: the branch
    published is read from the checkout the process stands in, never
    inferred from anything textual. Asserting the recorded `gh` call's
    `--head` equals the checkout's real branch name is the load-bearing
    check — a regression back to text-inference could still exit 0 while
    naming the wrong branch.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    init_git_repo(repo)
    subprocess.run(
        ["git", "checkout", "-q", "-b", "feature/publish-me"],
        cwd=repo,
        env=GIT_ENV,
        check=True,
    )
    (repo / "file.txt").write_text("x")
    commit_all(repo, "add file")
    _write_wrapup(repo, _head_sha(repo))
    calls = _stub_gh(monkeypatch)

    assert _run_main(monkeypatch, repo) == 0
    assert len(calls) == 1
    head_index = calls[0].index("--head")
    assert calls[0][head_index + 1] == "feature/publish-me"


def test_pr_create_refuses_without_a_wrapup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No authored wrap-up at all refuses publication."""
    repo = tmp_path / "repo"
    repo.mkdir()
    init_git_repo(repo)
    calls = _stub_gh(monkeypatch)

    assert _run_main(monkeypatch, repo) == 2
    assert calls == []


def test_pr_create_refuses_wrapup_naming_a_different_commit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A wrap-up whose `verified-at:` names a commit other than HEAD refuses.

    Distinguishes "wrong tree verified" from "nothing verified" — the
    wrap-up exists here, so a gate that only checked file presence would
    wrongly allow this.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    init_git_repo(repo)
    _write_wrapup(repo, "0" * 40)
    calls = _stub_gh(monkeypatch)

    assert _run_main(monkeypatch, repo) == 2
    assert calls == []


def test_pr_create_refuses_light_wrapup_when_classifier_disagrees(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A `wrapup-mode: light` wrap-up refuses when the re-run classifier disagrees.

    The light escape is earned at publish time, not asserted by the
    wrap-up's author — a diff that grew past light-code between authoring
    and publishing must not slip through on a stale self-declaration.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    init_git_repo(repo)
    _write_wrapup(repo, _head_sha(repo), mode_line="wrapup-mode: light")
    monkeypatch.setattr(
        pr_create, "classify", lambda _root, _base, _pr: SimpleNamespace(mode="full")
    )
    calls = _stub_gh(monkeypatch)

    assert _run_main(monkeypatch, repo) == 2
    assert calls == []


def test_pr_create_publishes_light_wrapup_when_classifier_agrees(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A `wrapup-mode: light` wrap-up publishes when the classifier still agrees.

    Complement of the disagreement case above — proves the re-check is a
    real gate (can say yes) rather than one that only ever refuses light
    wrap-ups.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    init_git_repo(repo)
    _write_wrapup(repo, _head_sha(repo), mode_line="wrapup-mode: light")
    monkeypatch.setattr(
        pr_create,
        "classify",
        lambda _root, _base, _pr: SimpleNamespace(mode="light-code"),
    )
    calls = _stub_gh(monkeypatch)

    assert _run_main(monkeypatch, repo) == 0
    assert len(calls) == 1


def test_pr_create_refuses_emergency_wrapup_when_sentinel_not_armed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A `wrapup-mode: emergency` wrap-up refuses when no bypass is armed.

    Traceability (the HEAD match) is never deferred, only verification —
    an emergency declaration still needs a spendable sentinel behind it,
    not just the header line.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    init_git_repo(repo)
    _write_wrapup(repo, _head_sha(repo), mode_line="wrapup-mode: emergency")
    monkeypatch.setattr(pr_create, "emergency_consume", lambda _root: 1)
    calls = _stub_gh(monkeypatch)

    assert _run_main(monkeypatch, repo) == 2
    assert calls == []


@pytest.mark.parametrize(
    "passthrough",
    [
        pytest.param(["--head", "other-branch"], id="separated"),
        pytest.param(["--head=other-branch"], id="equals-joined"),
        pytest.param(["-Hother-branch"], id="attached-short"),
        pytest.param(["--repo", "other/org"], id="owned-flag-repo"),
    ],
)
def test_passthrough_cannot_override_the_verified_branch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, passthrough: list[str]
) -> None:
    """A passthrough token setting an owned flag refuses, in every form gh accepts.

    The exploit: `gh`'s parser takes the LAST occurrence of a repeated
    flag, so a passthrough `--head other` appended after this command's
    own `--head <verified-branch>` would win — verification would pass
    against the real checkout while the branch actually published is
    whatever the passthrough named. `--base`/`--repo` are owned for the
    same reason (the light escape judged against one base, the PR opened
    against another; the whole publication redirected). Checking both the
    exit code and that `gh` was never invoked matters: a refusal for an
    unrelated reason (e.g. a parser error) could exit 2 without proving
    the passthrough itself was caught.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    init_git_repo(repo)
    subprocess.run(
        ["git", "checkout", "-q", "-b", "feature/publish-me"],
        cwd=repo,
        env=GIT_ENV,
        check=True,
    )
    (repo / "file.txt").write_text("x")
    commit_all(repo, "add file")
    _write_wrapup(repo, _head_sha(repo))
    calls = _stub_gh(monkeypatch)

    assert _run_main(monkeypatch, repo, "--", *passthrough) == 2
    assert calls == []


def test_passthrough_still_allows_flags_the_command_does_not_own(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An ordinary passthrough flag the command doesn't own still reaches `gh`.

    Without this, a filter broad enough to catch the owned-flag exploit
    above could quietly also swallow legitimate passthrough use, and
    nothing would notice — this is the complement that proves the filter
    is scoped to `OWNED_FLAGS`, not to "anything after `--`".
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    init_git_repo(repo)
    subprocess.run(
        ["git", "checkout", "-q", "-b", "feature/publish-me"],
        cwd=repo,
        env=GIT_ENV,
        check=True,
    )
    (repo / "file.txt").write_text("x")
    commit_all(repo, "add file")
    _write_wrapup(repo, _head_sha(repo))
    calls = _stub_gh(monkeypatch)

    assert _run_main(monkeypatch, repo, "--", "--label", "bug") == 0
    assert len(calls) == 1
    assert "--label" in calls[0]
    assert "bug" in calls[0]


def test_only_the_wrap_up_header_verifies_not_a_quoted_stamp(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A `verified-at:` quoted below the header must not stand in for the header's own.

    A wrap-up legitimately quotes an older reporter round's stamp further
    down, naming an earlier commit. The exploit: scanning the whole file
    for any `verified-at:` let that quoted, earlier-round stamp — which
    happens to name the checkout's current HEAD from a prior verification
    pass — satisfy the check even though the wrap-up's OWN header names a
    different (stale) commit. This is the exact input the old shell
    implementation refused and the Python rewrite initially did not, so it
    pins a real regression rather than a hypothetical one.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    init_git_repo(repo)
    stale_sha = _head_sha(repo)
    (repo / "file.txt").write_text("x")
    commit_all(repo, "second commit")
    head_sha = _head_sha(repo)
    code_health = repo / "code_health"
    code_health.mkdir(parents=True, exist_ok=True)
    (code_health / "pr_wrapup.md").write_text(
        "\n".join(
            [
                "# PR Wrap-up",
                "",
                f"verified-at: {stale_sha}",
                "",
                "Quoted from an earlier reporter round:",
                f"verified-at: {head_sha}   (PR #1, branch feature/publish-me)",
            ]
        )
        + "\n"
    )
    calls = _stub_gh(monkeypatch)

    assert _run_main(monkeypatch, repo) == 2
    assert calls == []
