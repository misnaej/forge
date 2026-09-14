"""Tests for ``forge.commit`` — the ``forge-commit`` stage/commit/push CLI.

Two layers, tested separately. ``plan_commit`` is a pure function
(``RepoState``/``CommitRequest`` in, ``CommitPlan``/``Refusal`` out) —
every refusal and every happy path is tested directly, no git involved.
``main()``'s real-git suite drives the CLI end to end against ephemeral
work/bare repo pairs (``tests.conftest.init_single_track_repo``), with a
genuine local git pre-commit hook standing in for the real gate
``forge-commit`` defers to (FOUNDATION §7 — hooks see only the command an
agent types, never a CLI's own git calls).

# MOCKING STRATEGY: the planner tests build ``RepoState``/``CommitRequest``
values directly — pure input/output, no seam to fake. The real-git tests
fake nothing about git itself: ``commit.repo_root`` is replaced with the
ephemeral work tree (the standard ``monkeypatch.setattr(<module>,
"repo_root", lambda: tmp_path)`` pattern used across the suite) so every
downstream call threads the ephemeral repo, and ``GIT_CONFIG_GLOBAL`` /
``GIT_CONFIG_SYSTEM`` are scrubbed to ``/dev/null`` for the duration of
each real-git test — ``commit.py``'s own in-process git calls inherit
``os.environ`` (unlike this suite's own setup calls, which always pass
the already-scrubbed ``tests.conftest.GIT_ENV``), so without the scrub a
developer's machine-wide ``core.hooksPath`` or ``commit.gpgsign`` could
change these tests' outcome. One test
(``test_main_ledger_write_failure_still_pushed_exits_1``) replaces
``commit.append_ledger_line`` with a raiser: it is the one failure mode
nothing about real filesystem state can trigger without also breaking
the earlier pre-commit-record read ``plan_commit`` depends on.
"""

from __future__ import annotations

import os
import subprocess
from typing import TYPE_CHECKING

import pytest

from forge import commit, git_utils
from forge.commit import CommitPlan, CommitRequest, Refusal, RepoState, plan_commit
from forge.ledger import parse_ledger
from tests.conftest import GIT_ENV, commit_all, init_single_track_repo, timing_log


if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# plan_commit — pure planner unit tests (no git)
# ---------------------------------------------------------------------------


def _state(**overrides: object) -> RepoState:
    """Build a ``RepoState`` with feature-branch defaults, *overrides* applied.

    Args:
        **overrides: Field values overriding the defaults.

    Returns:
        The built state.
    """
    defaults: dict[str, object] = {"branch": "feat/x", "base_branch": "main"}
    defaults.update(overrides)
    return RepoState(**defaults)  # type: ignore[arg-type]


def _normal_request(**overrides: object) -> CommitRequest:
    """Build a normal-mode ``CommitRequest`` with a valid subject, *overrides* applied.

    Args:
        **overrides: Field values overriding the defaults.

    Returns:
        The built request.
    """
    defaults: dict[str, object] = {"message": "feat: add x", "stage_all": True}
    defaults.update(overrides)
    return CommitRequest(**defaults)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("branch", "expected_substr"),
    [
        pytest.param(None, "detached HEAD", id="detached-head"),
        pytest.param("main", "protected base branch", id="on-base-branch"),
    ],
)
def test_plan_commit_refuses_missing_or_protected_branch(
    branch: str | None, expected_substr: str
) -> None:
    """No checked-out branch, or the base branch itself, refuses before anything else.

    ``_branch_refusal`` is the first check ``plan_commit`` runs; both of
    its branches (detached HEAD, base branch) refuse regardless of the
    rest of the request.

    Args:
        branch: The state's checked-out branch (``None`` = detached).
        expected_substr: Substring the refusal reason must contain.
    """
    outcome = plan_commit(_normal_request(), _state(branch=branch))
    assert isinstance(outcome, Refusal)
    assert expected_substr in outcome.reason


@pytest.mark.parametrize(
    ("state_kwargs", "request_kwargs", "expected_substr"),
    [
        pytest.param(
            {"in_merge": True},
            {"message": None, "paths": ("a.py",)},
            "a merge is in progress",
            id="merge-with-named-paths",
        ),
        pytest.param(
            {},
            {"message": None, "wip_sync": True, "paths": ("a.py",)},
            "do not name paths",
            id="wip-sync-with-named-paths",
        ),
        pytest.param(
            {},
            {"message": None, "stage_all": False, "paths": ()},
            "nothing selected",
            id="normal-with-nothing-selected",
        ),
    ],
)
def test_plan_commit_refuses_a_selection_its_mode_cannot_honour(
    state_kwargs: dict[str, object],
    request_kwargs: dict[str, object],
    expected_substr: str,
) -> None:
    """Each mode enforces its own staging-selection rule.

    A merge in progress refuses named paths (stage the resolutions
    instead), ``--wip-sync`` refuses named paths (it always stages
    everything), and plain normal mode refuses selecting nothing at all.

    Args:
        state_kwargs: Overrides for the ``RepoState``.
        request_kwargs: Overrides for the ``CommitRequest``.
        expected_substr: Substring the refusal reason must contain.
    """
    outcome = plan_commit(CommitRequest(**request_kwargs), _state(**state_kwargs))  # type: ignore[arg-type]
    assert isinstance(outcome, Refusal)
    assert expected_substr in outcome.reason


@pytest.mark.parametrize(
    ("message", "wip_sync", "stage_all", "wip_sync_env", "expected_substr"),
    [
        pytest.param(
            "wip-sync: sync",
            False,
            True,
            False,
            "needs --wip-sync",
            id="subject-without-flag",
        ),
        pytest.param(
            "feat: normal",
            True,
            False,
            False,
            "needs a subject starting with",
            id="flag-without-subject",
        ),
        pytest.param(
            "feat: normal",
            False,
            True,
            True,
            "FORGE_WIP_SYNC=1 is set",
            id="inherited-env-without-flag",
        ),
    ],
)
def test_plan_commit_refuses_a_wip_sync_subject_flag_mismatch(
    message: str,
    *,
    wip_sync: bool,
    stage_all: bool,
    wip_sync_env: bool,
    expected_substr: str,
) -> None:
    """The ``wip-sync:`` subject and ``--wip-sync`` flag must agree, in every direction.

    Covers a ``wip-sync:`` subject with no flag, the flag with an
    ordinary subject, and an inherited ``FORGE_WIP_SYNC=1`` environment
    variable with neither.

    Args:
        message: Commit subject.
        wip_sync: The request's ``--wip-sync`` flag.
        stage_all: The request's ``--all`` flag (only relevant in normal
            mode, to clear the selection check first).
        wip_sync_env: Whether ``FORGE_WIP_SYNC=1`` is already set.
        expected_substr: Substring the refusal reason must contain.
    """
    request = CommitRequest(message=message, stage_all=stage_all, wip_sync=wip_sync)
    outcome = plan_commit(request, _state(wip_sync_env=wip_sync_env))
    assert isinstance(outcome, Refusal)
    assert expected_substr in outcome.reason


def test_plan_commit_allows_a_paired_wip_sync_subject_and_flag() -> None:
    """A ``wip-sync:`` subject with ``--wip-sync`` set pairs correctly and is allowed.

    Positive counterpart to the mismatch cases above. Wip-sync mode also
    skips the pre-commit evidence check entirely, so no timing log is
    needed for this to succeed.
    """
    request = CommitRequest(message="wip-sync: checkpoint before merge", wip_sync=True)
    outcome = plan_commit(request, _state())
    assert outcome == CommitPlan(
        message="wip-sync: checkpoint before merge", mode="wip-sync"
    )


def test_plan_commit_refuses_a_missing_message_in_normal_mode() -> None:
    """No message at all — the first refusal check.

    This is ``_message_refusal``'s very first check, ahead of other checks.
    Selection is otherwise valid (``stage_all=True``) so only the missing
    message refuses.
    """
    request = CommitRequest(message=None, stage_all=True)
    outcome = plan_commit(request, _state())
    assert isinstance(outcome, Refusal)
    assert outcome.reason == "no commit message — pass -m or -F"


def test_plan_commit_refuses_ai_attribution_in_normal_mode() -> None:
    """AI attribution in the message is rejected."""
    request = _normal_request(message="feat: add x\n\nCo-Authored-By: Someone <a@b.c>")
    outcome = plan_commit(request, _state())
    assert isinstance(outcome, Refusal)
    assert "AI attribution" in outcome.reason


def test_plan_commit_refuses_ai_attribution_in_merge_mode() -> None:
    """The same attribution check applies to a merge-finish commit."""
    request = CommitRequest(
        message="Merge branch 'x'\n\nCo-Authored-By: Someone <a@b.c>"
    )
    outcome = plan_commit(request, _state(in_merge=True))
    assert isinstance(outcome, Refusal)
    assert "AI attribution" in outcome.reason


def test_plan_commit_refuses_ai_attribution_in_wip_sync_mode() -> None:
    """The same attribution check applies to a ``--wip-sync`` checkpoint commit."""
    request = CommitRequest(
        message="wip-sync: checkpoint\n\nCo-Authored-By: Someone <a@b.c>", wip_sync=True
    )
    outcome = plan_commit(request, _state())
    assert isinstance(outcome, Refusal)
    assert "AI attribution" in outcome.reason


def test_plan_commit_refuses_non_conventional_subject_in_normal_mode() -> None:
    """A subject not shaped ``<type>(<scope>)?!?: <subject>`` refuses in normal mode."""
    request = _normal_request(message="just a message, no type prefix")
    outcome = plan_commit(request, _state())
    assert isinstance(outcome, Refusal)
    assert "not conventional-commit format" in outcome.reason


def test_plan_commit_allows_a_non_conventional_subject_in_merge_mode() -> None:
    """Merge mode allows any subject — conventional-commit check is normal-mode only.

    Tests that merge mode lets through non-conventional subjects, with a
    valid fresh pre-commit record so only the subject shape is tested.
    """
    request = CommitRequest(message="just a message, no type prefix")
    state = _state(
        in_merge=True,
        timing_log=timing_log("ruff SKIP"),
        verdicts={"precommit_timing": "fresh"},
    )
    outcome = plan_commit(request, state)
    assert outcome == CommitPlan(message="just a message, no type prefix", mode="merge")


def test_plan_commit_refuses_when_a_pre_commit_step_failed() -> None:
    """A FAIL marker in the timing log refuses, naming the failed step."""
    state = _state(
        timing_log=timing_log("ruff FAIL"),
        verdicts={"precommit_timing": "fresh", "ruff": "fresh"},
    )
    outcome = plan_commit(_normal_request(), state)
    assert isinstance(outcome, Refusal)
    assert "pre-commit steps failed: ruff" in outcome.reason


def test_plan_commit_refuses_a_stale_verdict_for_an_executed_step() -> None:
    """A step log stale (even if timing log is fresh) causes refusal."""
    state = _state(
        timing_log=timing_log("ruff PASS"),
        verdicts={"precommit_timing": "fresh", "ruff": "stale"},
    )
    outcome = plan_commit(_normal_request(), state)
    assert isinstance(outcome, Refusal)
    assert "pre-commit logs are not fresh for this tree: ruff" in outcome.reason


def test_plan_commit_allows_a_skipped_step_even_when_its_own_verdict_is_stale() -> None:
    """A SKIP row made no claim about this tree, so its own staleness is irrelevant."""
    state = _state(
        timing_log=timing_log("ruff SKIP"),
        verdicts={"precommit_timing": "fresh", "ruff": "stale"},
    )
    outcome = plan_commit(_normal_request(), state)
    assert outcome == CommitPlan(message="feat: add x", mode="normal")


def test_plan_commit_allows_a_warn_step_with_a_fresh_verdict() -> None:
    """WARN never blocks by itself — non-blocking by the step's own contract."""
    state = _state(
        timing_log=timing_log("ruff WARN"),
        verdicts={"precommit_timing": "fresh", "ruff": "fresh"},
    )
    outcome = plan_commit(_normal_request(), state)
    assert outcome == CommitPlan(message="feat: add x", mode="normal")


def test_plan_commit_allows_a_pass_step_whose_verdict_is_environment_only() -> None:
    """Environment-only steps (verdict ``n/a``) are trusted like ``fresh``."""
    state = _state(
        timing_log=timing_log("env_sync PASS"),
        verdicts={"precommit_timing": "fresh", "env_sync": "n/a"},
    )
    outcome = plan_commit(_normal_request(), state)
    assert outcome == CommitPlan(message="feat: add x", mode="normal")


@pytest.mark.parametrize(
    ("timing_log_value", "verdicts", "expected_substr"),
    [
        pytest.param(None, {}, "no pre-commit record", id="missing-record"),
        pytest.param(
            timing_log("ruff SKIP"),
            {"precommit_timing": "stale"},
            "describes a different tree",
            id="stale-record",
        ),
    ],
)
def test_plan_commit_refuses_a_missing_or_stale_pre_commit_record(
    timing_log_value: str | None, verdicts: dict[str, str], expected_substr: str
) -> None:
    """Missing or stale pre-commit timing log causes refusal.

    Args:
        timing_log_value: The state's ``timing_log`` field.
        verdicts: The state's ``verdicts`` mapping.
        expected_substr: Substring the refusal reason must contain.
    """
    state = _state(timing_log=timing_log_value, verdicts=verdicts)
    outcome = plan_commit(_normal_request(), state)
    assert isinstance(outcome, Refusal)
    assert expected_substr in outcome.reason


# ---------------------------------------------------------------------------
# read_state
# ---------------------------------------------------------------------------


def test_read_state_branch_is_none_when_only_github_head_ref_resolves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A branch known only via CI's ``GITHUB_HEAD_REF`` reads as detached.

    ``read_state``'s own docstring: there is no local branch to commit on
    in that case, so ``RepoState.branch`` must be ``None`` even though
    ``resolve_current_branch`` did return a name — the ``source`` tag is
    what ``read_state`` discards it on, not the name itself.
    """
    monkeypatch.setattr(
        commit, "resolve_current_branch", lambda _root: ("pr-branch", "GITHUB_HEAD_REF")
    )
    state = commit.read_state(tmp_path)
    assert state.branch is None


# ---------------------------------------------------------------------------
# main() — real-git suite
# ---------------------------------------------------------------------------


_HOOK_SEEN_NAME = "hook_saw_wip_sync.txt"


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
        name: Branch to check out.
    """
    subprocess.run(["git", "checkout", "-q", name], cwd=repo, env=GIT_ENV, check=True)


def _head_sha(repo: Path) -> str:
    """Return *repo*'s current ``HEAD`` commit sha.

    Args:
        repo: Repo root.

    Returns:
        The full sha.
    """
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        env=GIT_ENV,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _bare_ref_sha(bare: Path, branch: str) -> str | None:
    """Get the sha for a branch in a bare repo.

    Args:
        bare: Bare repo root.
        branch: Branch name to resolve.

    Returns:
        The full sha, or ``None``.
    """
    proc = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=bare,
        env=GIT_ENV,
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.stdout.strip() or None


def _install_recording_hook(work: Path) -> None:
    """Install a recording pre-commit hook for testing.

    Runs unmocked so wip-sync env-propagation and hook-block scenarios
    exercise genuine git hook semantics.

    Args:
        work: Work-tree repo root; the hook writes into
            ``work / "hook_saw_wip_sync.txt"``.
    """
    seen = work / _HOOK_SEEN_NAME
    hook_path = work / ".git" / "hooks" / "pre-commit"
    hook_path.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "${{FORGE_WIP_SYNC:-<unset>}}" > "{seen}"\n'
        'if [ -n "${HOOK_MSG:-}" ]; then echo "$HOOK_MSG"; echo "$HOOK_MSG" >&2; fi\n'
        'exit "${HOOK_EXIT:-0}"\n'
    )
    hook_path.chmod(0o755)


def _seed_fresh_timing_log(work: Path, *rows: str) -> None:
    """Write a ``precommit_timing.log`` fresh for *work*'s CURRENT working tree.

    Must run as the LAST setup step in any scenario that calls it:
    ``working_tree_sha`` hashes every non-ignored file, including
    untracked ones (only ``code_health/`` itself is excluded), so
    anything written to *work* afterward makes the log read ``stale``
    instead of ``fresh``.

    Args:
        work: Work-tree repo root.
        *rows: ``"<name> <marker>"`` rows; defaults to one SKIP row —
            ``plan_commit``'s evidence check never inspects a SKIP step's
            individual freshness, so a bare SKIP row clears the check for
            scenarios that don't care about it otherwise.
    """
    body = timing_log(*(rows or ("ruff SKIP",)), stamp=None)
    git_utils.write_step_log(work, "precommit_timing", body)


def _new_repo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    branch: str | None = "feat/work",
    hook: bool = True,
) -> tuple[Path, Path]:
    """Build a work/bare repo pair wired for ``commit.main()``'s real-git suite.

    Gitignores ``code_health/`` and ``.plan/`` (the directories
    ``_record`` writes into, so a ``--all``/``--wip-sync`` ``git add -A``
    never sweeps forge-commit's own bookkeeping into the commit it is
    making) and the recording hook's own ``hook_saw_wip_sync.txt``
    output (written mid-commit, after staging already ran — but a later
    ``--all``/``--wip-sync`` commit in the same work tree would otherwise
    sweep a prior run's leftover file straight in), scrubs global/system
    git config for the calling process (so
    a developer's machine-wide ``core.hooksPath`` or ``commit.gpgsign``
    can never change ``commit.py``'s own in-process git calls, which —
    unlike this helper's own setup calls — inherit ``os.environ``),
    installs the recording pre-commit hook, checks out *branch*, and
    pins ``commit.repo_root`` to the work tree. Does NOT seed a timing
    log — callers needing one call :func:`_seed_fresh_timing_log` as
    their own LAST setup step, after everything else they create.

    Args:
        tmp_path: Pytest tmp dir; ``work``/``origin.git`` are created
            inside it.
        monkeypatch: Patches ``commit.repo_root`` and the git config env.
        branch: Feature branch to check out; ``None`` stays on ``main``.
        hook: Install the recording pre-commit hook.

    Returns:
        ``(work, bare)`` paths.
    """
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    work, bare = init_single_track_repo(tmp_path)
    (work / ".gitignore").write_text(f"code_health/\n.plan/\n{_HOOK_SEEN_NAME}\n")
    commit_all(work, "chore: gitignore code_health and .plan")
    subprocess.run(
        ["git", "push", "-q", "origin", "main"], cwd=work, env=GIT_ENV, check=True
    )
    if branch is not None:
        _checkout_new_branch(work, branch)
    if hook:
        _install_recording_hook(work)
    monkeypatch.setattr(commit, "repo_root", lambda: work)
    return work, bare


def test_main_normal_commit_with_named_paths_happy_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A normal ``-m`` + named-paths commit stages, commits, pushes and records.

    Exercises the full success path end to end: ``HEAD`` moves, the bare
    remote gets the new commit with upstream set (``feat/work`` had
    none), ``.plan/CONTINUATION.md`` gets an activity line,
    ``code_health/commit_history.log`` parses with the expected fields,
    and stdout carries the commit sha and the subagent-edit receipt.
    """
    work, bare = _new_repo(tmp_path, monkeypatch)
    before = _head_sha(work)
    (work / "hello.txt").write_text("hello\n")
    _seed_fresh_timing_log(work)

    exit_code = commit.main(["-m", "feat: add hello file", "hello.txt"])

    assert exit_code == commit.EXIT_OK
    after = _head_sha(work)
    assert after != before
    assert _bare_ref_sha(bare, "feat/work") == after
    upstream = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "feat/work@{u}"],
        cwd=work,
        env=GIT_ENV,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert upstream == "origin/feat/work"

    continuation = (work / ".plan" / "CONTINUATION.md").read_text()
    assert f"{after[:7]} feat: add hello file" in continuation

    ledger_text = (work / "code_health" / "commit_history.log").read_text()
    rows = parse_ledger(ledger_text, tail_key="subject")
    assert rows[-1]["sha"] == after[:7]
    assert rows[-1]["branch"] == "feat/work"
    assert rows[-1]["mode"] == "normal"
    assert rows[-1]["files"] == "1"
    assert rows[-1]["pushed"] == "yes"
    assert rows[-1]["subject"] == "feat: add hello file"

    out = capsys.readouterr().out
    assert f"committed: {after[:7]}" in out
    assert "subagent edits:" in out


def test_main_normal_commit_with_file_message_happy_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``-F <file>`` commit uses the file's content as the commit message, verbatim.

    Mirrors ``test_main_normal_commit_with_named_paths_happy_path`` but
    exercises the ``-F``/``--file`` message source instead of ``-m`` —
    the file holds a multi-line message (subject, blank line, two body
    lines), and the landed commit's full message must equal it exactly,
    not just its subject.
    """
    work, _bare = _new_repo(tmp_path, monkeypatch)
    (work / "hello.txt").write_text("hello\n")
    message = (
        "feat: add hello file\n\nA body line explaining why.\nAnd a second body line."
    )
    message_file = tmp_path / "message.txt"
    message_file.write_text(message + "\n")
    _seed_fresh_timing_log(work)

    exit_code = commit.main(["-F", str(message_file), "hello.txt"])

    assert exit_code == commit.EXIT_OK
    committed_message = subprocess.run(
        ["git", "log", "-1", "--format=%B", "HEAD"],
        cwd=work,
        env=GIT_ENV,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert committed_message == message


def test_main_unreadable_message_file_exits_nothing_committed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``-F`` with unreadable file fails immediately.

    Tests ``OSError`` handling before ``read_state``/``plan_commit``,
    so it needs no work/bare repo or pre-commit record.
    """
    monkeypatch.setattr(commit, "repo_root", lambda: tmp_path)
    missing = tmp_path / "does-not-exist-message.txt"

    exit_code = commit.main(["-F", str(missing)])

    assert exit_code == commit.EXIT_NOTHING_COMMITTED
    out = capsys.readouterr().out
    assert "forge-commit: cannot read the message file:" in out


def test_main_pre_commit_hook_block_leaves_nothing_committed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Pre-commit hook block prints output verbatim and exits 2.

    The git hook is the real per-commit gate; it still runs after the
    fast pre-commit-record refusal. The hook's message is echoed to both
    stdout and stderr, and ``_commit``'s exception handler re-emits both.
    """
    work, _bare = _new_repo(tmp_path, monkeypatch)
    before = _head_sha(work)
    (work / "hello.txt").write_text("hello\n")
    _seed_fresh_timing_log(work)
    monkeypatch.setenv("HOOK_EXIT", "1")
    monkeypatch.setenv("HOOK_MSG", "synthetic-hook-block-message")

    exit_code = commit.main(["-m", "feat: add hello file", "hello.txt"])

    assert exit_code == commit.EXIT_NOTHING_COMMITTED
    assert _head_sha(work) == before
    out = capsys.readouterr().out
    assert out.count("synthetic-hook-block-message") == 2
    assert "the commit was blocked" in out


def test_main_wip_sync_checkpoint_stages_everything_env_scoped_never_pushes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--wip-sync`` stages all, scopes env var, never pushes.

    Checkpoint commit stays local; env var reaches the hook but doesn't
    leak to the process. Untracked file is swept in; bare remote untouched.
    Wip-sync mode skips pre-commit evidence check entirely.
    """
    work, bare = _new_repo(tmp_path, monkeypatch)
    (work / "untracked.txt").write_text("wip content\n")
    monkeypatch.delenv("FORGE_WIP_SYNC", raising=False)
    assert "FORGE_WIP_SYNC" not in os.environ

    exit_code = commit.main(["--wip-sync", "-m", "wip-sync: checkpoint before merge"])

    assert exit_code == commit.EXIT_OK
    assert "FORGE_WIP_SYNC" not in os.environ
    hook_saw = (work / _HOOK_SEEN_NAME).read_text().strip()
    assert hook_saw == "1"
    committed_files = subprocess.run(
        ["git", "show", "--name-only", "--format=", "HEAD"],
        cwd=work,
        env=GIT_ENV,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert "untracked.txt" in committed_files
    assert _bare_ref_sha(bare, "feat/work") is None

    ledger_text = (work / "code_health" / "commit_history.log").read_text()
    rows = parse_ledger(ledger_text, tail_key="subject")
    assert rows[-1]["mode"] == "wip-sync"
    assert rows[-1]["pushed"] == "skipped"
    continuation = (work / ".plan" / "CONTINUATION.md").read_text()
    assert "wip-sync: checkpoint before merge" in continuation


def test_main_merge_finish_without_message_uses_prepared_merge_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Merge finish without ``-m`` uses the prepared merge message.

    Builds a real non-conflicting merge so MERGE_HEAD/MERGE_MSG exist
    as they would mid-merge; the commit has two parents and carries the
    message that merge_message reads.
    """
    work, _bare = _new_repo(tmp_path, monkeypatch)
    _checkout(work, "main")
    _checkout_new_branch(work, "other")
    (work / "other.txt").write_text("other change\n")
    commit_all(work, "feat: other change")
    _checkout(work, "feat/work")
    (work / "mine.txt").write_text("my change\n")
    commit_all(work, "feat: my change")

    subprocess.run(
        ["git", "merge", "--no-commit", "--no-ff", "other"],
        cwd=work,
        env=GIT_ENV,
        check=True,
    )
    expected_message = git_utils.merge_message(work)
    assert expected_message is not None
    _seed_fresh_timing_log(work)

    exit_code = commit.main([])

    assert exit_code == commit.EXIT_OK
    parents = subprocess.run(
        ["git", "rev-list", "--parents", "-n", "1", "HEAD"],
        cwd=work,
        env=GIT_ENV,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert len(parents) == 3  # commit sha + two parent shas
    body = subprocess.run(
        ["git", "log", "-1", "--format=%B", "HEAD"],
        cwd=work,
        env=GIT_ENV,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert body == expected_message
    assert "Merge branch 'other'" in body


def test_main_push_failure_after_commit_exits_1_commit_stays_local(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A push failure after a successful commit exits 1; the local commit is intact.

    The origin remote is repointed at a nonexistent path (mirrors
    ``test_fetch_quietly_returns_false_for_an_unreachable_remote`` in
    ``tests/test_git_utils.py``), so the push genuinely fails rather than
    being mocked. Also proves ``_record``'s ``committed.pushed is not
    False`` gate: a failed push must skip the ``.plan/CONTINUATION.md``
    write entirely (not merely leave it stale), and the ledger row it
    does still write must record the failure, not silently read as an
    unattempted push.
    """
    work, _bare = _new_repo(tmp_path, monkeypatch)
    before = _head_sha(work)
    subprocess.run(
        ["git", "remote", "set-url", "origin", str(tmp_path / "does-not-exist.git")],
        cwd=work,
        env=GIT_ENV,
        check=True,
    )
    (work / "hello.txt").write_text("hello\n")
    _seed_fresh_timing_log(work)

    exit_code = commit.main(["-m", "feat: add hello file", "hello.txt"])

    assert exit_code == commit.EXIT_LATE_FAILURE
    after = _head_sha(work)
    assert after != before
    out = capsys.readouterr().out
    assert f"committed: {after[:7]}" in out
    assert "push FAILED" in out
    assert not (work / ".plan" / "CONTINUATION.md").exists()
    ledger_text = (work / "code_health" / "commit_history.log").read_text()
    rows = parse_ledger(ledger_text, tail_key="subject")
    assert rows[-1]["pushed"] == "failed"


def test_main_push_only_on_base_branch_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--push-only`` on base branch refuses — branch check still applies.

    Even though ``--push-only`` bypasses ``plan_commit``, it still
    calls ``_branch_refusal`` directly, catching the base branch.
    """
    _work, bare = _new_repo(tmp_path, monkeypatch, branch=None)
    before_bare = _bare_ref_sha(bare, "main")

    exit_code = commit.main(["--push-only"])

    assert exit_code == commit.EXIT_NOTHING_COMMITTED
    assert _bare_ref_sha(bare, "main") == before_bare
    out = capsys.readouterr().out
    assert "protected base branch" in out


def test_main_push_only_pushes_an_existing_commit_without_consulting_pre_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--push-only`` pushes existing commits, bypassing ``plan_commit``.

    No ``precommit_timing.log`` is ever written in this scenario — a
    normal commit here would refuse for a missing pre-commit record, but
    ``--push-only`` calls ``_push_only``, which only checks the branch,
    never ``plan_commit``'s evidence gate.
    """
    work, bare = _new_repo(tmp_path, monkeypatch)
    (work / "local.txt").write_text("local only\n")
    commit_all(work, "feat: local only commit")
    local_sha = _head_sha(work)
    assert _bare_ref_sha(bare, "feat/work") is None

    exit_code = commit.main(["--push-only"])

    assert exit_code == commit.EXIT_OK
    assert _bare_ref_sha(bare, "feat/work") == local_sha


def test_main_no_push_commits_locally_leaves_bare_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--no-push`` commits locally, records it, leaves remote untouched.

    ``pushed=None`` (never attempted) still gets a CONTINUATION line
    (the module docstring's "after a push, or with ``--no-push``") and a
    ``pushed=skipped`` ledger row.
    """
    work, bare = _new_repo(tmp_path, monkeypatch)
    (work / "hello.txt").write_text("hello\n")
    _seed_fresh_timing_log(work)

    exit_code = commit.main(["--no-push", "-m", "feat: local only change", "hello.txt"])

    assert exit_code == commit.EXIT_OK
    after = _head_sha(work)
    assert _bare_ref_sha(bare, "feat/work") is None

    ledger_text = (work / "code_health" / "commit_history.log").read_text()
    rows = parse_ledger(ledger_text, tail_key="subject")
    assert rows[-1]["pushed"] == "skipped"
    continuation = (work / ".plan" / "CONTINUATION.md").read_text()
    assert f"{after[:7]} feat: local only change" in continuation


def test_main_on_base_branch_refuses_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Committing on ``main`` itself is refused before anything is touched.

    No feature branch is checked out — ``_new_repo(branch=None)`` stays
    on ``main``, the default ``[tool.forge].base_branch``.
    """
    work, bare = _new_repo(tmp_path, monkeypatch, branch=None)
    before_local = _head_sha(work)
    before_bare = _bare_ref_sha(bare, "main")
    (work / "hello.txt").write_text("hello\n")

    exit_code = commit.main(["-m", "feat: try to commit on main", "hello.txt"])

    assert exit_code == commit.EXIT_NOTHING_COMMITTED
    assert _head_sha(work) == before_local
    assert _bare_ref_sha(bare, "main") == before_bare
    out = capsys.readouterr().out
    assert "protected base branch" in out


def test_main_stages_only_named_paths_not_stray_untracked_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Named paths are staged; untracked files elsewhere are excluded.

    Pins the changelog contract for commits made on the author's behalf:
    an untracked ``changelog.d/`` fragment left in the tree must not
    silently ride along in a commit that named only the real change —
    slug, type and bump level choose the released version, so only the
    author decides whether a fragment ships.
    """
    work, _bare = _new_repo(tmp_path, monkeypatch)
    (work / "real_change.py").write_text("x = 1\n")
    changelog_dir = work / "changelog.d"
    changelog_dir.mkdir()
    (changelog_dir / "stray.patch.md").write_text("bump: patch\n\nstray fragment\n")
    _seed_fresh_timing_log(work)

    exit_code = commit.main(["-m", "feat: add real change", "real_change.py"])

    assert exit_code == commit.EXIT_OK
    committed_files = subprocess.run(
        ["git", "show", "--name-only", "--format=", "HEAD"],
        cwd=work,
        env=GIT_ENV,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert committed_files == ["real_change.py"]
    status = subprocess.run(
        ["git", "status", "--porcelain", "changelog.d/stray.patch.md"],
        cwd=work,
        env=GIT_ENV,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert status.startswith("??")


def test_main_all_with_nothing_staged_refuses_head_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--all`` on clean tree stages nothing — refused after evidence check.

    Distinct from ``test_plan_commit_refuses_a_selection_its_mode_cannot_honour``'s
    ``normal-with-nothing-selected`` case (an empty *request*, no git
    involved): here the request DOES select ``--all`` and the pre-commit
    record IS fresh, so ``plan_commit`` allows the commit through — it is
    ``_stage``'s real ``git add -A`` finding nothing new to add that
    refuses, one check later than the planner's own selection rule can
    ever reach.
    """
    work, _bare = _new_repo(tmp_path, monkeypatch)
    before = _head_sha(work)
    _seed_fresh_timing_log(work)

    exit_code = commit.main(["--all", "-m", "feat: x"])

    assert exit_code == commit.EXIT_NOTHING_COMMITTED
    assert _head_sha(work) == before
    out = capsys.readouterr().out
    assert "forge-commit: nothing staged — nothing committed" in out


def test_main_staging_a_nonexistent_path_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Git-unknown path fails staging with git's error, nothing committed.

    ``_stage``'s ``git add -- <paths>`` raises ``CalledProcessError`` for
    a pathspec matching no files; ``_run`` catches it and refuses before
    ever reaching ``_commit``.
    """
    work, _bare = _new_repo(tmp_path, monkeypatch)
    before = _head_sha(work)
    _seed_fresh_timing_log(work)

    exit_code = commit.main(["-m", "feat: x", "does-not-exist.py"])

    assert exit_code == commit.EXIT_NOTHING_COMMITTED
    assert _head_sha(work) == before
    out = capsys.readouterr().out
    assert "forge-commit: staging failed — nothing committed:" in out


def test_main_ledger_write_failure_still_pushed_exits_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Ledger write failure exits 1 even though push succeeded.

    Ledger write is mocked to fail after a successful push; the raiser
    call count proves the failure is from the ledger write, not another
    source of exit 1.
    """
    work, bare = _new_repo(tmp_path, monkeypatch)
    before = _head_sha(work)
    (work / "hello.txt").write_text("hello\n")
    _seed_fresh_timing_log(work)

    raiser_calls = 0

    def _raise_disk_full(*_a: object, **_kw: object) -> None:
        nonlocal raiser_calls
        raiser_calls += 1
        msg = "disk full"
        raise OSError(msg)

    monkeypatch.setattr(commit, "append_ledger_line", _raise_disk_full)

    exit_code = commit.main(["-m", "feat: add hello file", "hello.txt"])

    assert exit_code == commit.EXIT_LATE_FAILURE
    assert raiser_calls == 1
    out = capsys.readouterr().out
    assert "could not write code_health/commit_history.log" in out
    after = _head_sha(work)
    assert after != before
    assert _bare_ref_sha(bare, "feat/work") == after
