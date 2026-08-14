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
#   - resync.load_config: stubbed with a real ForgeConfig(base_branch="main",
#     dev_branch="main") instead of reading pyproject.toml.
#   - resync.subprocess.run: faked with the shared FakeProc to simulate
#     `gh pr list` / `gh pr create` / `forge-precommit --only ...` responses.
#   - resync._bootstrap_run: stubbed to avoid a real bootstrap pass.
#   - resync._provenance_evidence: stubbed at the function boundary in
#     `_publish_resync` tests (its own subprocess.run seam is exercised
#     directly in its dedicated test section instead).
#   - A stateful counter fake for `_working_tree_dirty` where a test needs
#     the pre-bootstrap and post-bootstrap dirty checks to disagree.

from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING

import pytest

from forge import resync
from forge.config import ForgeConfig
from tests.conftest import FakeProc


if TYPE_CHECKING:
    from pathlib import Path


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
    monkeypatch: pytest.MonkeyPatch,
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
        resync.subprocess,
        "run",
        lambda *_a, **_kw: FakeProc(0, stdout=payload),
    )
    assert resync._open_resync_pr_url() == "https://github.com/x/y/pull/1"


def test_open_resync_pr_url_none_when_no_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No open PR's head carries the resync prefix → None."""
    payload = json.dumps(
        [{"headRefName": "feat/other", "url": "https://github.com/x/y/pull/2"}],
    )
    monkeypatch.setattr(
        resync.subprocess,
        "run",
        lambda *_a, **_kw: FakeProc(0, stdout=payload),
    )
    assert resync._open_resync_pr_url() is None


def test_open_resync_pr_url_none_when_gh_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`gh pr list` failing (rc != 0) degrades to None rather than raising."""
    monkeypatch.setattr(
        resync.subprocess,
        "run",
        lambda *_a, **_kw: FakeProc(1, stderr="boom"),
    )
    assert resync._open_resync_pr_url() is None


def test_open_resync_pr_url_none_on_malformed_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`gh pr list` returning malformed JSON (rc 0) degrades to None."""
    monkeypatch.setattr(
        resync.subprocess,
        "run",
        lambda *_a, **_kw: FakeProc(0, stdout="not json"),
    )
    assert resync._open_resync_pr_url() is None


def test_open_resync_pr_url_none_on_empty_pr_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty PR list (`[]`) yields None."""
    monkeypatch.setattr(
        resync.subprocess,
        "run",
        lambda *_a, **_kw: FakeProc(0, stdout="[]"),
    )
    assert resync._open_resync_pr_url() is None


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
        resync.subprocess,
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
        resync.subprocess,
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

    monkeypatch.setattr(resync.subprocess, "run", _fake_run)

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
        lambda: dedup_calls.append("x") or None,
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
        lambda: "https://github.com/x/y/pull/1",
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
        lambda: "https://github.com/x/y/pull/1",
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
    monkeypatch.setattr(resync, "_open_resync_pr_url", lambda: None)
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
    monkeypatch.setattr(resync, "_open_resync_pr_url", lambda: None)
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
        `ForgeConfig(base_branch="main", dev_branch="main")`.
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
    monkeypatch.setattr(resync, "_open_resync_pr_url", lambda: None)
    monkeypatch.setattr(resync, "_run_bootstrap", lambda: 0)
    monkeypatch.setattr(
        resync,
        "load_config",
        lambda _root: ForgeConfig(base_branch="main", dev_branch="main"),
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
