"""Tests for ``forge.resync``."""

# MOCKING STRATEGY: forge-resync shells out to git and gh, and re-enters
# install-forge-bootstrap in-process; every seam is stubbed so no real
# git/gh/bootstrap process ever runs.
#   - resync.run_git: replaced with a recorder that returns canned stdout
#     per subcommand (never touches a real repo).
#   - resync.create_commit: replaced with a recorder (or a raiser, to
#     exercise the commit-failure path) so the actual commit step never
#     shells out to git either.
#   - resync.require_cli: replaced with a no-op (or a raiser, to exercise
#     the missing-gh abort) so PATH lookups never matter.
#   - resync.repo_root: pinned to a tmp_path sandbox.
#   - resync.load_config: stubbed with a real ForgeConfig(base_branch="main")
#     instead of reading pyproject.toml.
#   - resync.subprocess.run: faked with the shared FakeProc to simulate
#     `gh pr list` / `gh pr create` / `forge-precommit --only ...` responses.
#   - resync._bootstrap_run: stubbed to avoid a real bootstrap pass.
#   - resync._provenance_evidence: stubbed at the function boundary in
#     `_publish_resync` tests (its own subprocess.run seam is exercised
#     directly in its dedicated test section instead).
#   - A stateful counter fake for `_working_tree_dirty` where a test needs
#     the pre-bootstrap and post-bootstrap dirty checks to disagree.
#   - `_regenerate` unit tests fake `resync.subprocess.run` directly (per-call
#     argv inspection distinguishes the plain generator call from its
#     `--check` re-run); the generator argv comes from the real
#     `forge_cli_argv` against the installed forge-scripts.
#   - `_resolve_conflicts` tests build REAL git merge-conflict states (no
#     mocked git plumbing — mirrors `tests/test_rebump.py`'s local repo
#     helpers) and patch only `resync._regenerate` with a recording stub, so
#     the conflict-classification logic runs against a genuine unmerged index.

from __future__ import annotations

import json
import subprocess
import sys
from typing import TYPE_CHECKING

import pytest

from forge import git_utils, resync
from forge.config import ForgeConfig
from tests.conftest import GIT_ENV, FakeProc, commit_all, init_git_repo


if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Local repo-building helpers (mirrors tests/test_rebump.py ~100-160)
# ---------------------------------------------------------------------------


def _checkout_new_branch(repo: Path, name: str) -> None:
    """Create and check out branch *name* from the current ``HEAD``.

    Args:
        repo: Repo root.
        name: New branch name.
    """
    subprocess.run(
        ["git", "checkout", "-q", "-b", name], cwd=repo, env=GIT_ENV, check=True
    )


def _checkout(repo: Path, name: str) -> None:
    """Check out existing branch *name*.

    Args:
        repo: Repo root.
        name: Branch to switch to.
    """
    subprocess.run(["git", "checkout", "-q", name], cwd=repo, env=GIT_ENV, check=True)


def _merge_no_commit(repo: Path, branch: str) -> int:
    """Run ``git merge --no-ff --no-commit`` against *branch*, return its exit code.

    Args:
        repo: Repo root.
        branch: Branch to merge into the current one.

    Returns:
        The merge subprocess's return code (0 clean, non-zero conflicted).
    """
    result = subprocess.run(
        ["git", "merge", "--no-ff", "--no-commit", branch],
        cwd=repo,
        env=GIT_ENV,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode


def _staged_paths(repo: Path) -> list[str]:
    """Return the repo-relative paths currently staged relative to ``HEAD``.

    Args:
        repo: Repo root.

    Returns:
        Sorted staged (index vs HEAD) paths.
    """
    out = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=repo,
        env=GIT_ENV,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return sorted(line for line in out.splitlines() if line.strip())


# ---------------------------------------------------------------------------
# _forge_version
# ---------------------------------------------------------------------------


def test_forge_version_strips_local_suffix(monkeypatch: pytest.MonkeyPatch) -> None:
    """The local-build suffix (``+g<sha>...``) is stripped for a stable branch name."""
    monkeypatch.setattr(
        resync.metadata,
        "version",
        lambda _name: "2.7.1+g1a2b3c.d20260101",
    )
    assert resync._forge_version() == "2.7.1"


def test_forge_version_unknown_on_package_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """forge-scripts not installed as a distribution → `"unknown"`."""

    def _raise(_name: str) -> str:
        raise resync.metadata.PackageNotFoundError

    monkeypatch.setattr(resync.metadata, "version", _raise)
    assert resync._forge_version() == "unknown"


# ---------------------------------------------------------------------------
# _working_tree_dirty
# ---------------------------------------------------------------------------


def test_working_tree_dirty_true_on_porcelain_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-empty `git status --porcelain` output means the tree is dirty."""
    monkeypatch.setattr(resync, "run_git", lambda *_a, **_kw: " M foo.py\n")
    assert resync._working_tree_dirty(tmp_path) is True


def test_working_tree_dirty_false_when_clean(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty porcelain output means the tree is clean."""
    monkeypatch.setattr(resync, "run_git", lambda *_a, **_kw: "")
    assert resync._working_tree_dirty(tmp_path) is False


# ---------------------------------------------------------------------------
# _open_resync_pr_url
# ---------------------------------------------------------------------------


def test_open_resync_pr_url_finds_matching_prefix(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A PR whose head matches the resync branch prefix returns its URL."""
    payload = json.dumps(
        [
            {
                "headRefName": "chore/forge-resync-2.7.0",
                "url": "https://github.com/x/y/pull/1",
            },
            {"headRefName": "feat/other", "url": "https://github.com/x/y/pull/2"},
        ],
    )
    monkeypatch.setattr(
        git_utils.subprocess,
        "run",
        lambda *_a, **_kw: FakeProc(0, stdout=payload),
    )
    assert resync._open_resync_pr_url(tmp_path) == "https://github.com/x/y/pull/1"


def test_open_resync_pr_url_none_when_no_match(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No open PR's head carries the resync prefix → None."""
    payload = json.dumps(
        [{"headRefName": "feat/other", "url": "https://github.com/x/y/pull/2"}],
    )
    monkeypatch.setattr(
        git_utils.subprocess,
        "run",
        lambda *_a, **_kw: FakeProc(0, stdout=payload),
    )
    assert resync._open_resync_pr_url(tmp_path) is None


def test_open_resync_pr_url_none_when_gh_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`gh pr list` failing (rc != 0) degrades to None rather than raising."""
    monkeypatch.setattr(
        git_utils.subprocess,
        "run",
        lambda *_a, **_kw: FakeProc(1, stderr="boom"),
    )
    assert resync._open_resync_pr_url(tmp_path) is None


def test_open_resync_pr_url_none_on_malformed_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`gh pr list` returning malformed JSON (rc 0) degrades to None."""
    monkeypatch.setattr(
        git_utils.subprocess,
        "run",
        lambda *_a, **_kw: FakeProc(0, stdout="not json"),
    )
    assert resync._open_resync_pr_url(tmp_path) is None


def test_open_resync_pr_url_none_on_empty_pr_list(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An empty PR list (`[]`) yields None."""
    monkeypatch.setattr(
        git_utils.subprocess,
        "run",
        lambda *_a, **_kw: FakeProc(0, stdout="[]"),
    )
    assert resync._open_resync_pr_url(tmp_path) is None


# ---------------------------------------------------------------------------
# _run_bootstrap
# ---------------------------------------------------------------------------


def test_run_bootstrap_delegates_to_shared_reentry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_run_bootstrap` returns the shared re-entry helper's exit code.

    The argv-swap mechanics live in
    `forge.install_bootstrap.run_in_process` (covered in
    `test_install_bootstrap.py`); this wrapper only adds the progress
    banner and passes the code through.
    """
    monkeypatch.setattr(resync, "_bootstrap_run", lambda: 0)
    assert resync._run_bootstrap() == 0


def test_run_bootstrap_returns_nonzero_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing bootstrap exit code is passed through unchanged."""
    monkeypatch.setattr(resync, "_bootstrap_run", lambda: 3)
    assert resync._run_bootstrap() == 3


# ---------------------------------------------------------------------------
# _provenance_evidence
# ---------------------------------------------------------------------------


def test_provenance_evidence_pass_states_byte_verified_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A clean gate run (rc 0) reports passed with the verbatim gate output.

    SCENARIO: every provenance gate exits 0.
    MOCK SETUP: `subprocess.run` replaced with a fake returning
        `FakeProc(0, stdout="all good")`.
    EXPECTED BEHAVIOR: `passed` is `True`; the evidence block carries the
        "byte-verified" language and the verbatim stdout.
    """
    monkeypatch.setattr(
        git_utils.subprocess,
        "run",
        lambda *_a, **_kw: FakeProc(0, stdout="all good"),
    )

    passed, block = resync._provenance_evidence(tmp_path)

    assert passed is True
    assert "byte-verified" in block
    assert "all good" in block


def test_provenance_evidence_fail_emits_full_review_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing gate run (rc != 0) reports failed with a full-review warning.

    SCENARIO: a provenance gate exits non-zero, with output on both
        stdout and stderr.
    MOCK SETUP: `subprocess.run` replaced with a fake returning
        `FakeProc(1, stdout="drift found", stderr="mismatch")`.
    EXPECTED BEHAVIOR: `passed` is `False`; the evidence block warns
        "full review" and carries both the stdout and stderr text.
    """
    monkeypatch.setattr(
        git_utils.subprocess,
        "run",
        lambda *_a, **_kw: FakeProc(1, stdout="drift found", stderr="mismatch"),
    )

    passed, block = resync._provenance_evidence(tmp_path)

    assert passed is False
    assert "full review" in block
    assert "drift found" in block
    assert "mismatch" in block


def test_provenance_evidence_argv_and_kwargs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gate subprocess is invoked with the joined step list and expected kwargs.

    SCENARIO: a single provenance gate run, argv/kwargs captured rather
        than the return value.
    MOCK SETUP: `subprocess.run` replaced with a recorder appending
        `(cmd, kwargs)` and returning `FakeProc(0)`.
    EXPECTED BEHAVIOR: called exactly once, with the `forge-precommit
        --only <joined steps>` argv and `capture_output=True`,
        `check=False`, `text=True`, `cwd=<root>` — `text=True` is
        load-bearing: without it stdout/stderr are bytes and `.strip()`
        would raise inside `_provenance_evidence`.
    """
    calls: list[tuple[list[str], dict[str, object]]] = []

    def _fake_run(cmd: list[str], **kwargs: object) -> FakeProc:
        calls.append((cmd, kwargs))
        return FakeProc(0)

    monkeypatch.setattr(git_utils.subprocess, "run", _fake_run)

    resync._provenance_evidence(tmp_path)

    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv == [
        "forge-precommit",
        "--only",
        ",".join(resync.PROVENANCE_GATE_STEPS),
    ]
    assert kwargs["capture_output"] is True
    assert kwargs["check"] is False
    assert kwargs["text"] is True
    assert kwargs["cwd"] == tmp_path


def test_provenance_evidence_truncates_oversized_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gate output past the cap is truncated with a marker; the fence survives.

    SCENARIO: a passing gate run whose stdout exceeds
        `git_utils.EVIDENCE_OUTPUT_CAP`.
    MOCK SETUP: `subprocess.run` replaced with a fake returning
        `FakeProc(0, stdout=<oversized payload>)`.
    EXPECTED BEHAVIOR: the evidence block carries the "… (truncated)"
        marker, does not contain the full original output, and the
        four-backtick fence is intact.
    """
    oversized = "x" * (git_utils.EVIDENCE_OUTPUT_CAP + 500)
    monkeypatch.setattr(
        git_utils.subprocess,
        "run",
        lambda *_a, **_kw: FakeProc(0, stdout=oversized),
    )

    passed, block = resync._provenance_evidence(tmp_path)

    assert passed is True
    assert "… (truncated)" in block
    assert oversized not in block
    assert "````" in block


# ---------------------------------------------------------------------------
# _regenerate
# ---------------------------------------------------------------------------


def test_regenerate_success_runs_generator_then_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A clean generator run followed by an agreeing --check returns True.

    SCENARIO: the generator exits 0, and re-running it with --check
        afterward also exits 0 (its output matches the regenerated file).
    MOCK SETUP: `subprocess.run` replaced with a recorder appending every
        argv and returning `FakeProc(0)`.
    EXPECTED BEHAVIOR: both the plain generator argv and the `--check`
        argv run, in that order, launched via the running install's own
        module (``sys.executable -m ...``, never a bare PATH name — see
        `forge_cli_argv`); the function returns True and logs an info
        line naming the regenerated path.
    """
    calls: list[list[str]] = []
    monkeypatch.setattr(
        resync.subprocess,
        "run",
        lambda cmd, **_kw: calls.append(cmd) or FakeProc(0),
    )

    with caplog.at_level("INFO"):
        result = resync._regenerate(
            tmp_path, "FOUNDATION.md", ("install-forge-claude-md",)
        )

    assert result is True
    assert calls == [
        [sys.executable, "-m", "forge.install_claudemd"],
        [sys.executable, "-m", "forge.install_claudemd", "--check"],
    ]
    assert any("regenerated FOUNDATION.md" in r.getMessage() for r in caplog.records)


def test_regenerate_generator_failure_skips_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failing generator returns False without ever running --check.

    SCENARIO: the generator itself exits non-zero.
    MOCK SETUP: `subprocess.run` replaced with a recorder returning
        `FakeProc(1, stderr="boom")` unconditionally.
    EXPECTED BEHAVIOR: only the plain generator argv runs — `--check` is
        never invoked; the function returns False and logs an error
        naming the exit code.
    """
    calls: list[list[str]] = []
    monkeypatch.setattr(
        resync.subprocess,
        "run",
        lambda cmd, **_kw: calls.append(cmd) or FakeProc(1, stderr="boom"),
    )

    with caplog.at_level("ERROR"):
        result = resync._regenerate(
            tmp_path, "FOUNDATION.md", ("install-forge-claude-md",)
        )

    assert result is False
    assert calls == [[sys.executable, "-m", "forge.install_claudemd"]]
    assert any("failed" in r.getMessage() for r in caplog.records)


def test_regenerate_check_disagreement_returns_false(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A generator that runs clean but whose --check disagrees still fails.

    SCENARIO: the generator exits 0, but the immediate `--check` re-run
        reports drift (non-zero) — the regenerated content did not stick,
        or the generator is non-idempotent.
    MOCK SETUP: `require_cli` no-op; `subprocess.run` replaced with a
        fake distinguishing the two calls by whether `--check` is in argv:
        `FakeProc(0)` for the plain call, `FakeProc(1, stdout="drift")`
        for `--check`.
    EXPECTED BEHAVIOR: both calls run; the function returns False and
        logs an error naming the disagreement.
    """

    def _fake_run(cmd: list[str], **_kw: object) -> FakeProc:
        if "--check" in cmd:
            return FakeProc(1, stdout="drift")
        return FakeProc(0)

    monkeypatch.setattr(resync, "require_cli", lambda *_a, **_kw: None)
    monkeypatch.setattr(resync.subprocess, "run", _fake_run)

    with caplog.at_level("ERROR"):
        result = resync._regenerate(
            tmp_path, "FOUNDATION.md", ("install-forge-claude-md",)
        )

    assert result is False
    assert any("disagrees" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# _publish_resync
# ---------------------------------------------------------------------------


def test_publish_resync_happy_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The full branch/commit/push/PR sequence runs, then returns to the start branch.

    SCENARIO: happy-path resync publish — dirty tree already regenerated,
        no existing PR.
    MOCK SETUP: `run_git` recorder captures every git invocation and
        reports `"main"` for `branch --show-current`; `create_commit`
        replaced with a recorder capturing `(root, message)`;
        `subprocess.run` (gh) records its argv and returns a `FakeProc`
        carrying the created PR's URL on stdout.
    EXPECTED BEHAVIOR: `switch -c chore/forge-resync-<ver>-no-version`,
        `add -A`, a `create_commit` call with the "chore: resync..."
        message (marker included), `push -u origin <branch>`, a `gh pr
        create --base main ...` call, then a final `switch` back to
        `"main"`; returns 0.
    """
    git_calls: list[list[str]] = []

    def _fake_run_git(*args: str, **_kw: object) -> str:
        git_calls.append(list(args))
        if args[:2] == ("branch", "--show-current"):
            return "main"
        return ""

    commit_calls: list[tuple[object, ...]] = []

    def _fake_create_commit(*args: object) -> None:
        commit_calls.append(args)

    gh_calls: list[list[str]] = []

    def _fake_subprocess_run(cmd: list[str], **_kw: object) -> FakeProc:
        gh_calls.append(cmd)
        return FakeProc(0, stdout="https://github.com/x/y/pull/9")

    monkeypatch.setattr(resync, "run_git", _fake_run_git)
    monkeypatch.setattr(resync, "create_commit", _fake_create_commit)
    monkeypatch.setattr(resync.subprocess, "run", _fake_subprocess_run)
    monkeypatch.setattr(
        resync, "_provenance_evidence", lambda *_a, **_kw: (True, "<evidence>")
    )

    with caplog.at_level("INFO"):
        rc = resync._publish_resync(tmp_path, "2.7.0", "main")

    assert rc == 0
    branch = f"chore/forge-resync-2.7.0-{resync.NO_VERSION_BRANCH_TOKEN}"
    assert ["switch", "-c", branch] in git_calls
    assert ["add", "-A"] in git_calls
    commit_message = (
        f"chore: resync forge-managed artifacts (2.7.0) "
        f"{resync.NO_VERSION_COMMIT_MARKER}"
    )
    assert commit_calls == [(tmp_path, commit_message)]
    assert ["push", "-u", "origin", branch] in git_calls
    assert git_calls[-1] == ["switch", "main"]  # returns to start branch

    assert len(gh_calls) == 1
    gh_argv = gh_calls[0]
    assert gh_argv[:3] == ["gh", "pr", "create"]
    assert "--base" in gh_argv
    assert gh_argv[gh_argv.index("--base") + 1] == "main"
    assert "--head" in gh_argv
    assert gh_argv[gh_argv.index("--head") + 1] == branch

    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "resync PR opened" in msgs


def test_publish_resync_gh_create_failure_leaves_branch_pushed_returns_1(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`gh pr create` failing still leaves the pushed branch and switches back.

    SCENARIO: the branch push succeeds but `gh pr create` exits non-zero.
    MOCK SETUP: `run_git` recorder reports `"main"` for `show-current`;
        `create_commit` replaced with a no-op recorder; `subprocess.run`
        (gh) returns `FakeProc(1, stderr="boom")`.
    EXPECTED BEHAVIOR: returns 1, an error naming the branch is logged,
        and the `finally`-block switch-back to `"main"` still runs.
    """
    git_calls: list[list[str]] = []

    def _fake_run_git(*args: str, **_kw: object) -> str:
        git_calls.append(list(args))
        if args[:2] == ("branch", "--show-current"):
            return "main"
        return ""

    commit_calls: list[tuple[object, ...]] = []

    monkeypatch.setattr(resync, "run_git", _fake_run_git)
    monkeypatch.setattr(
        resync, "create_commit", lambda *args: commit_calls.append(args)
    )
    monkeypatch.setattr(
        resync.subprocess,
        "run",
        lambda *_a, **_kw: FakeProc(1, stderr="boom"),
    )
    monkeypatch.setattr(
        resync, "_provenance_evidence", lambda *_a, **_kw: (True, "<evidence>")
    )

    with caplog.at_level("ERROR"):
        rc = resync._publish_resync(tmp_path, "2.7.0", "main")

    assert rc == 1
    branch = f"chore/forge-resync-2.7.0-{resync.NO_VERSION_BRANCH_TOKEN}"
    assert any(branch in r.getMessage() for r in caplog.records)
    assert git_calls[-1] == ["switch", "main"]  # finally still switches back


def test_publish_resync_git_failure_still_switches_back_and_propagates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A git step failing mid-publish still switches back before propagating.

    SCENARIO: `create_commit` raises `subprocess.CalledProcessError` (e.g.
        nothing to commit / hook rejection).
    MOCK SETUP: `run_git` recorder reports `"main"` for `show-current` and
        would otherwise no-op; `create_commit` replaced with a raiser.
    EXPECTED BEHAVIOR: `subprocess.CalledProcessError` propagates out of
        `_publish_resync` (the `Raises:` contract), and the `finally`
        block's switch-back to `"main"` still fires before it does.
    """
    git_calls: list[list[str]] = []

    def _fake_run_git(*args: str, **_kw: object) -> str:
        git_calls.append(list(args))
        if args[:2] == ("branch", "--show-current"):
            return "main"
        return ""

    def _fake_create_commit(_root: object, message: str) -> None:
        raise subprocess.CalledProcessError(1, ["commit", "-m", message])

    monkeypatch.setattr(resync, "run_git", _fake_run_git)
    monkeypatch.setattr(resync, "create_commit", _fake_create_commit)

    with pytest.raises(subprocess.CalledProcessError):
        resync._publish_resync(tmp_path, "2.7.0", "main")

    assert git_calls[-1] == ["switch", "main"]  # finally still switches back


def test_publish_resync_switch_create_failure_propagates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`switch -c` itself failing still switches back before propagating.

    SCENARIO: the initial `switch -c <branch>` raises (e.g. the branch
        already exists from a stale prior run).
    MOCK SETUP: `run_git` recorder reports `"main"` for `show-current`,
        raises on the `switch -c` call; `create_commit` replaced with a
        no-op recorder (never reached, since `switch -c` fails first).
    EXPECTED BEHAVIOR: `subprocess.CalledProcessError` propagates out of
        `_publish_resync`, and — because `switch -c` is inside the `try`
        (the widened-try fix) — the `finally` block's switch-back to
        `"main"` still fires, even though no branch was ever created.
    """
    git_calls: list[list[str]] = []

    def _fake_run_git(*args: str, **_kw: object) -> str:
        git_calls.append(list(args))
        if args[:2] == ("branch", "--show-current"):
            return "main"
        if args[:2] == ("switch", "-c"):
            raise subprocess.CalledProcessError(1, list(args))
        return ""

    commit_calls: list[tuple[object, ...]] = []

    monkeypatch.setattr(resync, "run_git", _fake_run_git)
    monkeypatch.setattr(
        resync, "create_commit", lambda *args: commit_calls.append(args)
    )

    with pytest.raises(subprocess.CalledProcessError):
        resync._publish_resync(tmp_path, "2.7.0", "main")

    assert git_calls[-1] == ["switch", "main"]  # finally still switches back
    assert commit_calls == []


def test_publish_resync_switches_back_even_when_start_branch_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A detached-HEAD start (empty `--show-current`) skips the final switch cleanly."""
    git_calls: list[list[str]] = []

    def _fake_run_git(*args: str, **_kw: object) -> str:
        git_calls.append(list(args))
        if args[:2] == ("branch", "--show-current"):
            return ""
        return ""

    commit_calls: list[tuple[object, ...]] = []

    monkeypatch.setattr(resync, "run_git", _fake_run_git)
    monkeypatch.setattr(
        resync, "create_commit", lambda *args: commit_calls.append(args)
    )
    monkeypatch.setattr(
        resync.subprocess,
        "run",
        lambda *_a, **_kw: FakeProc(0, stdout="https://github.com/x/y/pull/1"),
    )
    monkeypatch.setattr(
        resync, "_provenance_evidence", lambda *_a, **_kw: (True, "<evidence>")
    )

    rc = resync._publish_resync(tmp_path, "2.7.0", "main")
    assert rc == 0
    # A plain 2-element ["switch", "<branch>"] is the switch-BACK call;
    # ["switch", "-c", branch] (3 elements) is the initial branch-create.
    switch_backs = [c for c in git_calls if c[:1] == ["switch"] and len(c) == 2]
    assert switch_backs == []


def test_publish_resync_body_includes_pass_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A passing provenance gate's evidence block is appended to the PR body.

    SCENARIO: `_provenance_evidence` reports a pass.
    MOCK SETUP: `run_git` recorder reports `"main"` for `show-current`;
        `create_commit` no-op; `_provenance_evidence` stubbed to return
        `(True, "<pass block>")`; `subprocess.run` (gh) recorder captures
        the `gh pr create` argv.
    EXPECTED BEHAVIOR: the `--body` argv contains the standard `_PR_BODY`
        text followed by the pass evidence block, and `_publish_resync`
        still returns 0.
    """

    def _fake_run_git(*args: str, **_kw: object) -> str:
        if args[:2] == ("branch", "--show-current"):
            return "main"
        return ""

    gh_calls: list[list[str]] = []

    def _fake_subprocess_run(cmd: list[str], **_kw: object) -> FakeProc:
        gh_calls.append(cmd)
        return FakeProc(0, stdout="https://github.com/x/y/pull/9")

    monkeypatch.setattr(resync, "run_git", _fake_run_git)
    monkeypatch.setattr(resync, "create_commit", lambda *_a: None)
    monkeypatch.setattr(resync.subprocess, "run", _fake_subprocess_run)
    monkeypatch.setattr(
        resync,
        "_provenance_evidence",
        lambda *_a, **_kw: (True, "<pass block>"),
    )

    rc = resync._publish_resync(tmp_path, "2.7.0", "main")

    assert rc == 0
    assert len(gh_calls) == 1
    body = gh_calls[0][gh_calls[0].index("--body") + 1]
    assert resync._PR_BODY in body
    assert "<pass block>" in body
    assert body.index(resync._PR_BODY) < body.index("<pass block>")


def test_publish_resync_body_includes_fail_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Failing provenance gate appends evidence block; PR still opens.

    SCENARIO: `_provenance_evidence` reports a failure.
    MOCK SETUP: `run_git` recorder reports `"main"` for `show-current`;
        `create_commit` no-op; `_provenance_evidence` stubbed to return
        `(False, "<fail block>")`; `subprocess.run` (gh) recorder
        captures the `gh pr create` argv.
    EXPECTED BEHAVIOR: the `--body` argv contains the fail evidence
        block, and `_publish_resync` still returns 0 — a gate failure
        never blocks PR creation, it only flags the body for full review.
        A warning naming the gate failure is logged.
    """

    def _fake_run_git(*args: str, **_kw: object) -> str:
        if args[:2] == ("branch", "--show-current"):
            return "main"
        return ""

    gh_calls: list[list[str]] = []

    def _fake_subprocess_run(cmd: list[str], **_kw: object) -> FakeProc:
        gh_calls.append(cmd)
        return FakeProc(0, stdout="https://github.com/x/y/pull/9")

    monkeypatch.setattr(resync, "run_git", _fake_run_git)
    monkeypatch.setattr(resync, "create_commit", lambda *_a: None)
    monkeypatch.setattr(resync.subprocess, "run", _fake_subprocess_run)
    monkeypatch.setattr(
        resync,
        "_provenance_evidence",
        lambda *_a, **_kw: (False, "<fail block>"),
    )

    with caplog.at_level("WARNING"):
        rc = resync._publish_resync(tmp_path, "2.7.0", "main")

    assert rc == 0
    assert len(gh_calls) == 1
    body = gh_calls[0][gh_calls[0].index("--body") + 1]
    assert "<fail block>" in body
    assert any(
        "provenance gates did not pass" in r.getMessage() for r in caplog.records
    )


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def test_main_aborts_on_missing_gh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing `gh` binary aborts before any git/bootstrap work runs.

    SCENARIO: `require_cli` raises `SystemExit(1)` because `gh` is absent.
    MOCK SETUP: `repo_root` → sandbox; `require_cli` replaced with a
        raiser; `run_git` records whether it is ever called.
    EXPECTED BEHAVIOR: `SystemExit(1)` propagates out of `main()`;
        nothing else runs.
    """
    monkeypatch.setattr(resync, "repo_root", lambda: tmp_path)

    def _raise_missing_cli(*_a: object, **_kw: object) -> None:
        raise SystemExit(1)

    monkeypatch.setattr(resync, "require_cli", _raise_missing_cli)
    called: list[str] = []
    monkeypatch.setattr(
        resync,
        "run_git",
        lambda *_a, **_kw: called.append("run_git") or "",
    )
    monkeypatch.setattr(resync.sys, "argv", ["forge-resync"])

    with pytest.raises(SystemExit) as exc:
        resync.main()
    assert exc.value.code == 1
    assert called == []


def test_main_aborts_on_dirty_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A dirty working tree aborts before the dedup guard / bootstrap run.

    SCENARIO: `git status --porcelain` reports pending changes.
    MOCK SETUP: `repo_root` → sandbox; `require_cli` → no-op; `run_git` →
        dirty porcelain output; `_open_resync_pr_url` / `_run_bootstrap`
        record whether they were ever called.
    EXPECTED BEHAVIOR: returns 1; dedup guard and bootstrap never run.
    """
    monkeypatch.setattr(resync, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(resync, "require_cli", lambda *_a, **_kw: None)
    monkeypatch.setattr(resync, "run_git", lambda *_a, **_kw: " M foo.py\n")

    dedup_calls: list[str] = []
    bootstrap_calls: list[str] = []
    monkeypatch.setattr(
        resync,
        "_open_resync_pr_url",
        lambda _root: dedup_calls.append("x") or None,
    )
    monkeypatch.setattr(
        resync,
        "_run_bootstrap",
        lambda: bootstrap_calls.append("x") or 0,
    )
    monkeypatch.setattr(resync.sys, "argv", ["forge-resync"])

    with caplog.at_level("ERROR"):
        rc = resync.main()
    assert rc == 1
    assert dedup_calls == []
    assert bootstrap_calls == []


def test_main_requires_forge_precommit_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`main()` gates on `forge-precommit` being installed, alongside `gh`.

    SCENARIO: both required CLIs are present.
    MOCK SETUP: `require_cli` replaced with a recorder capturing each
        `name` argument; `_open_resync_pr_url` short-circuits the rest of
        `main()` so no further seams need stubbing.
    EXPECTED BEHAVIOR: `require_cli` is called with `"forge-precommit"`
        (the provenance-gate binary `_provenance_evidence` shells out to)
        in addition to `"gh"`.
    """
    monkeypatch.setattr(resync, "repo_root", lambda: tmp_path)
    required: list[str] = []
    monkeypatch.setattr(
        resync,
        "require_cli",
        lambda name, **_kw: required.append(name),
    )
    monkeypatch.setattr(resync, "run_git", lambda *_a, **_kw: "")
    monkeypatch.setattr(
        resync,
        "_open_resync_pr_url",
        lambda _root: "https://github.com/x/y/pull/1",
    )
    monkeypatch.setattr(resync.sys, "argv", ["forge-resync"])

    resync.main()

    assert "forge-precommit" in required
    assert "gh" in required


def test_main_dedup_guard_short_circuits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An already-open resync PR short-circuits before bootstrap/publish run."""
    monkeypatch.setattr(resync, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(resync, "require_cli", lambda *_a, **_kw: None)
    monkeypatch.setattr(resync, "run_git", lambda *_a, **_kw: "")
    monkeypatch.setattr(
        resync,
        "_open_resync_pr_url",
        lambda _root: "https://github.com/x/y/pull/1",
    )
    bootstrap_calls: list[str] = []
    publish_calls: list[str] = []
    monkeypatch.setattr(
        resync,
        "_run_bootstrap",
        lambda: bootstrap_calls.append("x") or 0,
    )
    monkeypatch.setattr(
        resync,
        "_publish_resync",
        lambda *_a, **_kw: publish_calls.append("x") or 0,
    )
    monkeypatch.setattr(resync.sys, "argv", ["forge-resync"])

    with caplog.at_level("INFO"):
        rc = resync.main()
    assert rc == 0
    assert bootstrap_calls == []
    assert publish_calls == []


def test_main_bootstrap_failure_propagates_exit_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failing bootstrap short-circuits `main()` with its own exit code."""
    monkeypatch.setattr(resync, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(resync, "require_cli", lambda *_a, **_kw: None)
    monkeypatch.setattr(resync, "run_git", lambda *_a, **_kw: "")
    monkeypatch.setattr(resync, "_open_resync_pr_url", lambda _root: None)
    monkeypatch.setattr(resync, "_run_bootstrap", lambda: 3)
    publish_calls: list[str] = []
    monkeypatch.setattr(
        resync,
        "_publish_resync",
        lambda *_a, **_kw: publish_calls.append("x") or 0,
    )
    monkeypatch.setattr(resync.sys, "argv", ["forge-resync"])

    with caplog.at_level("ERROR"):
        rc = resync.main()
    assert rc == 3
    assert publish_calls == []


def test_main_no_diff_after_bootstrap_exits_clean(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Bootstrap regen producing no diff → "in sync", exit 0, no publish."""
    monkeypatch.setattr(resync, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(resync, "require_cli", lambda *_a, **_kw: None)
    monkeypatch.setattr(resync, "run_git", lambda *_a, **_kw: "")  # always clean
    monkeypatch.setattr(resync, "_open_resync_pr_url", lambda _root: None)
    monkeypatch.setattr(resync, "_run_bootstrap", lambda: 0)
    publish_calls: list[str] = []
    monkeypatch.setattr(
        resync,
        "_publish_resync",
        lambda *_a, **_kw: publish_calls.append("x") or 0,
    )
    monkeypatch.setattr(resync.sys, "argv", ["forge-resync"])

    with caplog.at_level("INFO"):
        rc = resync.main()
    assert rc == 0
    assert publish_calls == []
    assert any("in sync" in r.getMessage() for r in caplog.records)


def test_main_diff_after_bootstrap_publishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bootstrap regen producing a diff calls `_publish_resync` with the right args.

    SCENARIO: clean tree pre-bootstrap, dirty tree post-bootstrap (regen
        actually changed managed artifacts).
    MOCK SETUP: `repo_root` → sandbox; `require_cli` → no-op; a stateful
        counter fake for `run_git("status", "--porcelain", ...)` reports
        clean on the first call (pre-bootstrap check) and dirty on the
        second (post-bootstrap check); `_open_resync_pr_url` → None;
        `_run_bootstrap` → 0; `load_config` → a real
        `ForgeConfig(base_branch="main")`.
    EXPECTED BEHAVIOR: `_publish_resync` is called exactly once with
        `(root, forge_version, "main")`.
    """
    status_calls = {"n": 0}

    def _fake_run_git(*args: str, **_kw: object) -> str:
        if args[:2] == ("status", "--porcelain"):
            status_calls["n"] += 1
            return "" if status_calls["n"] == 1 else " M FOUNDATION.md\n"
        return ""

    monkeypatch.setattr(resync, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(resync, "require_cli", lambda *_a, **_kw: None)
    monkeypatch.setattr(resync, "run_git", _fake_run_git)
    monkeypatch.setattr(resync, "_open_resync_pr_url", lambda _root: None)
    monkeypatch.setattr(resync, "_run_bootstrap", lambda: 0)
    monkeypatch.setattr(
        resync,
        "load_config",
        lambda _root: ForgeConfig(base_branch="main"),
    )
    monkeypatch.setattr(resync, "_forge_version", lambda: "2.7.0")

    publish_args: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        resync,
        "_publish_resync",
        lambda *a, **_kw: publish_args.append(a) or 0,
    )
    monkeypatch.setattr(resync.sys, "argv", ["forge-resync"])

    rc = resync.main()
    assert rc == 0
    assert publish_args == [(tmp_path, "2.7.0", "main")]


# ---------------------------------------------------------------------------
# _resolve_conflicts
# ---------------------------------------------------------------------------


def test_resolve_conflicts_no_merge_in_progress_returns_2(tmp_path: Path) -> None:
    """A clean repo with no merge underway refuses — nothing to resolve."""
    init_git_repo(tmp_path)
    assert resync._resolve_conflicts(tmp_path, dry_run=False) == 2


def test_resolve_conflicts_merge_clean_nothing_conflicted_returns_2(
    tmp_path: Path,
) -> None:
    """A merge in progress that auto-merged cleanly (no conflicts) refuses.

    `--no-ff --no-commit` against two branches touching distinct files
    merges with rc 0 but still leaves `MERGE_HEAD` in place (the commit
    step never ran) — `merge_in_progress` is True while `unmerged_paths`
    is empty.
    """
    init_git_repo(tmp_path)
    _checkout_new_branch(tmp_path, "other")
    (tmp_path / "other.txt").write_text("other\n")
    commit_all(tmp_path, "other work")
    _checkout(tmp_path, "main")
    _checkout_new_branch(tmp_path, "feat/x")
    (tmp_path / "feat.txt").write_text("feat\n")
    commit_all(tmp_path, "feat work")

    assert _merge_no_commit(tmp_path, "other") == 0

    assert resync._resolve_conflicts(tmp_path, dry_run=False) == 2


def test_resolve_conflicts_foreign_path_only_refuses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A single non-generated conflicted path refuses, naming it; the stub never runs.

    SCENARIO: the only conflicted path is `foo.txt` — not a forge-generated
        artifact.
    MOCK SETUP: `resync._regenerate` replaced with a recording stub.
    EXPECTED BEHAVIOR: returns 2; the stub is never called; an error
        naming `foo.txt` is logged.
    """
    init_git_repo(tmp_path)
    (tmp_path / "foo.txt").write_text("base\n")
    commit_all(tmp_path, "add foo")
    _checkout_new_branch(tmp_path, "other")
    (tmp_path / "foo.txt").write_text("other\n")
    commit_all(tmp_path, "other edit")
    _checkout(tmp_path, "main")
    _checkout_new_branch(tmp_path, "feat/x")
    (tmp_path / "foo.txt").write_text("feat\n")
    commit_all(tmp_path, "feat edit")

    assert _merge_no_commit(tmp_path, "other") != 0

    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(resync, "_regenerate", lambda *a: calls.append(a) or True)

    with caplog.at_level("ERROR"):
        rc = resync._resolve_conflicts(tmp_path, dry_run=False)

    assert rc == 2
    assert calls == []
    assert any("foo.txt" in r.getMessage() for r in caplog.records)


def test_resolve_conflicts_mixed_generated_and_foreign_refuses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A conflict set mixing a generated and a foreign path refuses entirely.

    SCENARIO: `FOUNDATION.md` (generated) and `foo.txt` (foreign) both
        conflict.
    MOCK SETUP: `resync._regenerate` replaced with a recording stub.
    EXPECTED BEHAVIOR: returns 2; the stub is never called — even the
        generated path is left untouched, matching `forge-rebump`'s
        all-or-nothing refusal contract.
    """
    init_git_repo(tmp_path)
    (tmp_path / "FOUNDATION.md").write_text("base found\n")
    (tmp_path / "foo.txt").write_text("base foo\n")
    commit_all(tmp_path, "add files")
    _checkout_new_branch(tmp_path, "other")
    (tmp_path / "FOUNDATION.md").write_text("other found\n")
    (tmp_path / "foo.txt").write_text("other foo\n")
    commit_all(tmp_path, "other edit")
    _checkout(tmp_path, "main")
    _checkout_new_branch(tmp_path, "feat/x")
    (tmp_path / "FOUNDATION.md").write_text("feat found\n")
    (tmp_path / "foo.txt").write_text("feat foo\n")
    commit_all(tmp_path, "feat edit")

    assert _merge_no_commit(tmp_path, "other") != 0

    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(resync, "_regenerate", lambda *a: calls.append(a) or True)

    rc = resync._resolve_conflicts(tmp_path, dry_run=False)

    assert rc == 2
    assert calls == []


def test_resolve_conflicts_dry_run_only_generated_reports_without_acting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """dry_run reports the resolvable verdict without regenerating or staging.

    SCENARIO: only `FOUNDATION.md` conflicts; `dry_run=True`.
    MOCK SETUP: `resync._regenerate` replaced with a recording stub.
    EXPECTED BEHAVIOR: returns 0; the stub is never called; the path is
        left genuinely unresolved (`git add` never ran — a merge conflict
        already makes ``git diff --cached`` list the path, so the real
        assertion is that `unmerged_paths` still reports it); an info
        line names `FOUNDATION.md`.
    """
    init_git_repo(tmp_path)
    (tmp_path / "FOUNDATION.md").write_text("base found\n")
    commit_all(tmp_path, "add FOUNDATION.md")
    _checkout_new_branch(tmp_path, "other")
    (tmp_path / "FOUNDATION.md").write_text("other found\n")
    commit_all(tmp_path, "other edit")
    _checkout(tmp_path, "main")
    _checkout_new_branch(tmp_path, "feat/x")
    (tmp_path / "FOUNDATION.md").write_text("feat found\n")
    commit_all(tmp_path, "feat edit")

    assert _merge_no_commit(tmp_path, "other") != 0

    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(resync, "_regenerate", lambda *a: calls.append(a) or True)

    with caplog.at_level("INFO"):
        rc = resync._resolve_conflicts(tmp_path, dry_run=True)

    assert rc == 0
    assert calls == []
    assert git_utils.unmerged_paths(tmp_path) == ["FOUNDATION.md"]
    assert any("FOUNDATION.md" in r.getMessage() for r in caplog.records)


def test_resolve_conflicts_happy_path_regenerates_and_stages_each(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every conflicted generated path is regenerated with its own argv and staged.

    SCENARIO: `FOUNDATION.md` and `docs/cli-reference.md` both conflict —
        both are forge-generated.
    MOCK SETUP: `resync._regenerate` replaced with a recording stub
        returning True for every path.
    EXPECTED BEHAVIOR: returns 0; the stub is called once per conflicted
        path, each time with that path's own :func:`regen_commands` argv;
        both paths end up staged.
    """
    init_git_repo(tmp_path)
    (tmp_path / "FOUNDATION.md").write_text("base found\n")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "cli-reference.md").write_text("base cli\n")
    commit_all(tmp_path, "add generated files")
    _checkout_new_branch(tmp_path, "other")
    (tmp_path / "FOUNDATION.md").write_text("other found\n")
    (tmp_path / "docs" / "cli-reference.md").write_text("other cli\n")
    commit_all(tmp_path, "other edit")
    _checkout(tmp_path, "main")
    _checkout_new_branch(tmp_path, "feat/x")
    (tmp_path / "FOUNDATION.md").write_text("feat found\n")
    (tmp_path / "docs" / "cli-reference.md").write_text("feat cli\n")
    commit_all(tmp_path, "feat edit")

    assert _merge_no_commit(tmp_path, "other") != 0

    calls: list[tuple[Path, str, tuple[str, ...]]] = []

    def _fake_regenerate(root: Path, path: str, argv: tuple[str, ...]) -> bool:
        calls.append((root, path, argv))
        return True

    monkeypatch.setattr(resync, "_regenerate", _fake_regenerate)

    rc = resync._resolve_conflicts(tmp_path, dry_run=False)

    assert rc == 0
    expected = resync.regen_commands(tmp_path)
    called_paths = {path for _root, path, _argv in calls}
    assert called_paths == {"FOUNDATION.md", "docs/cli-reference.md"}
    for root, path, argv in calls:
        assert root == tmp_path
        assert argv == expected[path]
    assert _staged_paths(tmp_path) == ["FOUNDATION.md", "docs/cli-reference.md"]


def test_resolve_conflicts_regenerate_failure_returns_2(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `_regenerate` failure on any conflicted path aborts with 2.

    SCENARIO: `FOUNDATION.md` conflicts; the regenerate stub reports failure.
    MOCK SETUP: `resync._regenerate` replaced with a stub returning False.
    EXPECTED BEHAVIOR: `_resolve_conflicts` returns 2.
    """
    init_git_repo(tmp_path)
    (tmp_path / "FOUNDATION.md").write_text("base found\n")
    commit_all(tmp_path, "add FOUNDATION.md")
    _checkout_new_branch(tmp_path, "other")
    (tmp_path / "FOUNDATION.md").write_text("other found\n")
    commit_all(tmp_path, "other edit")
    _checkout(tmp_path, "main")
    _checkout_new_branch(tmp_path, "feat/x")
    (tmp_path / "FOUNDATION.md").write_text("feat found\n")
    commit_all(tmp_path, "feat edit")

    assert _merge_no_commit(tmp_path, "other") != 0

    monkeypatch.setattr(resync, "_regenerate", lambda *_a: False)

    rc = resync._resolve_conflicts(tmp_path, dry_run=False)

    assert rc == 2


# ---------------------------------------------------------------------------
# main() — --resolve-conflicts / --dry-run argv handling
# ---------------------------------------------------------------------------


def test_main_resolve_conflicts_dispatches_before_gh_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--resolve-conflicts` short-circuits before the gh/forge-precommit preflight.

    SCENARIO: `--resolve-conflicts` passed on argv.
    MOCK SETUP: `repo_root` → sandbox; `require_cli` replaced with a
        recorder capturing every `name` it is called with;
        `_resolve_conflicts` replaced with a recorder.
    EXPECTED BEHAVIOR: `_resolve_conflicts` is called with
        `(root, dry_run=False)` and its return value passes straight
        through; `require_cli` is never called (not even for `"gh"`).
    """
    monkeypatch.setattr(resync, "repo_root", lambda: tmp_path)
    required: list[str] = []
    monkeypatch.setattr(
        resync, "require_cli", lambda name, **_kw: required.append(name)
    )
    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        resync,
        "_resolve_conflicts",
        lambda root, *, dry_run: calls.append((root, dry_run)) or 0,
    )
    monkeypatch.setattr(resync.sys, "argv", ["forge-resync", "--resolve-conflicts"])

    rc = resync.main()

    assert rc == 0
    assert calls == [(tmp_path, False)]
    assert required == []


def test_main_dry_run_without_resolve_conflicts_exits_via_parser_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bare `--dry-run` is rejected by `parser.error`, not silently inert.

    SCENARIO: `--dry-run` passed without `--resolve-conflicts`.
    MOCK SETUP: none — argparse's own `parser.error` fires before any
        resync logic runs.
    EXPECTED BEHAVIOR: `SystemExit(2)` propagates out of `main()`.
    """
    monkeypatch.setattr(resync.sys, "argv", ["forge-resync", "--dry-run"])

    with pytest.raises(SystemExit) as exc:
        resync.main()

    assert exc.value.code == 2
