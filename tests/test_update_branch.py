"""Tests for forge.update_branch — base sync with mechanical conflict resolution."""

# MOCKING STRATEGY: git runs for real against a bare `origin` in tmp_path (no
# network). Faked only where the real tool is out of scope: the generator
# (`regenerate_artifact`, `regen_commands`) and the version-slot resolver
# (`rebump`), each replaced at update_branch's own import name with a
# deterministic writer. The pre-commit gate is a stub hook script that
# records `FORGE_MERGE_SYNC`.

from __future__ import annotations

import subprocess
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from forge import update_branch
from forge.git_utils import unmerged_paths
from forge.rebump import RebumpOutcome
from forge.update_branch import UpdateRefusedError
from tests.conftest import GIT_ENV, commit_all, git_stdout, init_git_repo


if TYPE_CHECKING:
    from pathlib import Path


_SHARED = "one\ntwo\nthree\n"


def _repo_with_origin(tmp_path: Path) -> tuple[Path, Path]:
    """Build a checkout on `feature/x` whose `origin/main` is a real bare remote.

    Args:
        tmp_path: Pytest's per-test temporary directory.

    Returns:
        The checkout root and the bare origin's path.
    """
    origin = tmp_path / "origin.git"
    origin.mkdir()
    git_stdout(origin, "init", "-q", "--bare", "-b", "main")
    repo = tmp_path / "repo"
    repo.mkdir()
    init_git_repo(repo)
    (repo / "shared.txt").write_text(_SHARED)
    commit_all(repo, "add shared")
    git_stdout(repo, "remote", "add", "origin", str(origin))
    git_stdout(repo, "push", "-q", "origin", "main")
    git_stdout(repo, "checkout", "-q", "-b", "feature/x")
    return repo, origin


def _advance_origin(tmp_path: Path, origin: Path, files: dict[str, str]) -> None:
    """Push one commit writing *files* to `origin/main` from a second clone.

    Args:
        tmp_path: Pytest's per-test temporary directory.
        origin: The bare remote to push to.
        files: Repo-relative path to new content.
    """
    other = tmp_path / "other"
    if other.exists():
        git_stdout(other, "pull", "-q", "origin", "main")
    else:
        git_stdout(tmp_path, "clone", "-q", str(origin), str(other))
        git_stdout(other, "config", "user.name", "t")
        git_stdout(other, "config", "user.email", "t@t")
    for name, text in files.items():
        (other / name).parent.mkdir(parents=True, exist_ok=True)
        (other / name).write_text(text)
    commit_all(other, "base moves")
    git_stdout(other, "push", "-q", "origin", "main")


def _conflicted_repo(tmp_path: Path) -> tuple[Path, Path]:
    """Build a repo where the branch and `origin/main` edit the same line.

    Args:
        tmp_path: Pytest's per-test temporary directory.

    Returns:
        The checkout root and the bare origin's path.
    """
    repo, origin = _repo_with_origin(tmp_path)
    (repo / "shared.txt").write_text("one\nbranch\nthree\n")
    commit_all(repo, "branch edits shared")
    _advance_origin(tmp_path, origin, {"shared.txt": "one\nmain\nthree\n"})
    return repo, origin


def _merge_in_progress(repo: Path) -> bool:
    """Return whether a merge is paused in a checkout (MERGE_HEAD present).

    Args:
        repo: The checkout root to inspect.

    Returns:
        True when `.git/MERGE_HEAD` exists in `repo`.
    """
    return (repo / ".git" / "MERGE_HEAD").exists()


def _parents(repo: Path) -> list[str]:
    """Return the parent SHAs of the HEAD commit of a checkout.

    Args:
        repo: The checkout root to inspect.

    Returns:
        The parent SHAs of HEAD, in order.
    """
    return git_stdout(repo, "log", "-1", "--format=%P").split()


def _lessons(repo: Path) -> list[Path]:
    """Return the intake lesson files written under a checkout, sorted.

    Args:
        repo: The checkout root to inspect.

    Returns:
        Paths of `docs/intake/lessons/*.md` in `repo`, sorted by name.
    """
    return sorted((repo / "docs" / "intake" / "lessons").glob("*.md"))


def test_start_merges_pushes_and_is_idempotent(tmp_path: Path) -> None:
    """SCENARIO: base advanced with an unrelated file; run twice.

    EXPECTED BEHAVIOR: first run commits a two-parent merge and pushes it
    (origin's branch is local HEAD); second run is up to date and adds no
    commit.
    """
    repo, origin = _repo_with_origin(tmp_path)
    _advance_origin(tmp_path, origin, {"unrelated.txt": "moved\n"})

    assert update_branch.start(repo, None, push_after=True) == 0

    assert len(_parents(repo)) == 2
    head = git_stdout(repo, "rev-parse", "HEAD").strip()
    assert git_stdout(origin, "rev-parse", "feature/x").strip() == head
    assert (repo / "unrelated.txt").read_text() == "moved\n"

    assert update_branch.start(repo, None, push_after=True) == 0
    assert git_stdout(repo, "rev-parse", "HEAD").strip() == head


def test_start_logs_the_calling_tool_when_pushing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """SCENARIO: clean merge pushed to the bare origin.

    EXPECTED BEHAVIOR: the push log names `forge-update-branch`, not the
    `forge-commit` default.
    """
    repo, origin = _repo_with_origin(tmp_path)
    _advance_origin(tmp_path, origin, {"unrelated.txt": "moved\n"})

    with caplog.at_level("INFO"):
        assert update_branch.start(repo, None, push_after=True) == 0

    assert "forge-update-branch: pushed" in caplog.text


def test_start_does_not_take_a_base_revert_as_already_seen(tmp_path: Path) -> None:
    """SCENARIO: base reverts a file to a version the branch held before forking.

    Base sets `shared.txt` to Y, the branch forks there and edits it to Z,
    base reverts it to the original X. X is in the branch's history only
    through base commits.
    EXPECTED BEHAVIOR: exit 3, merge in progress, `shared.txt` still
    unmerged. Without the `MERGE_HEAD..HEAD` range limit the rule would
    take the branch side silently.
    """
    repo, origin = _repo_with_origin(tmp_path)
    _advance_origin(tmp_path, origin, {"shared.txt": "one\nY\nthree\n"})
    git_stdout(repo, "fetch", "-q", "origin", "main")
    git_stdout(repo, "merge", "-q", "--ff-only", "origin/main")
    (repo / "shared.txt").write_text("one\nZ\nthree\n")
    commit_all(repo, "branch edits shared")
    _advance_origin(tmp_path, origin, {"shared.txt": _SHARED})

    assert (
        update_branch.start(repo, None, push_after=False) == update_branch.EXIT_STOPPED
    )

    assert _merge_in_progress(repo)
    assert unmerged_paths(repo) == ["shared.txt"]


def test_start_checkpoints_a_dirty_tree_before_merging(tmp_path: Path) -> None:
    """SCENARIO: uncommitted work present when the base moved.

    EXPECTED BEHAVIOR: a `wip-sync:` commit is the merge's first parent
    and the tree ends clean.
    """
    repo, origin = _repo_with_origin(tmp_path)
    (repo / "scratch.txt").write_text("wip\n")
    _advance_origin(tmp_path, origin, {"unrelated.txt": "moved\n"})

    assert update_branch.start(repo, None, push_after=False) == 0

    first_parent = git_stdout(repo, "log", "-1", "--format=%s", "HEAD^1").strip()
    assert first_parent.startswith("wip-sync:")
    assert git_stdout(repo, "status", "--porcelain").strip() == ""


def test_start_takes_the_branch_side_for_a_parent_already_squash_merged(
    tmp_path: Path,
) -> None:
    """SCENARIO: stacked branch C (off parent P) after P was squash-merged to main.

    P edits line two of `shared.txt`, C edits it further, main gets P's
    content as an unrelated-ancestry commit. Git sees both sides change
    the same line; P's version is already in C's history.
    EXPECTED BEHAVIOR: no stop, no intake lesson, C's version kept. Without
    the already-seen rule the run exits 3 and `shared.txt` holds markers.
    """
    repo, origin = _repo_with_origin(tmp_path)
    git_stdout(repo, "checkout", "-q", "-b", "feature/parent")
    (repo / "shared.txt").write_text("one\nparent\nthree\n")
    commit_all(repo, "parent edit")
    git_stdout(repo, "checkout", "-q", "-b", "feature/child")
    (repo / "shared.txt").write_text("one\nchild\nthree\n")
    commit_all(repo, "child edit")
    _advance_origin(tmp_path, origin, {"shared.txt": "one\nparent\nthree\n"})

    assert update_branch.start(repo, None, push_after=False) == 0

    assert (repo / "shared.txt").read_text() == "one\nchild\nthree\n"
    assert not _merge_in_progress(repo)
    assert _lessons(repo) == []
    assert "Kept the branch side" in git_stdout(repo, "log", "-1", "--format=%B")


def test_start_stops_on_a_genuine_conflict_and_records_a_lesson(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """SCENARIO: both sides change the same line; no earlier history explains it.

    EXPECTED BEHAVIOR: exit 3, merge left in progress, one intake lesson
    written, log names `--continue`.
    """
    repo, _ = _conflicted_repo(tmp_path)

    with caplog.at_level("WARNING", logger=update_branch.__name__):
        code = update_branch.start(repo, None, push_after=False)

    assert code == update_branch.EXIT_STOPPED
    assert _merge_in_progress(repo)
    lessons = _lessons(repo)
    assert len(lessons) == 1
    assert "shared.txt" in lessons[0].read_text()
    assert "--continue" in caplog.text


def _stub_gate(repo: Path, tmp_path: Path) -> Path:
    """Install a pre-commit hook recording `FORGE_MERGE_SYNC`.

    Args:
        repo: Repo to install the hook in.
        tmp_path: Directory for the hooks dir and the record file.

    Returns:
        The file the hook writes to.
    """
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    record = tmp_path / "gate.txt"
    hook = hooks / "pre-commit"
    hook.write_text(f'#!/bin/sh\necho "${{FORGE_MERGE_SYNC:-unset}}" > "{record}"\n')
    hook.chmod(0o755)
    git_stdout(repo, "config", "core.hooksPath", str(hooks))
    return record


@pytest.mark.parametrize(
    ("resolved", "gate_seen"),
    [
        pytest.param("one\nmerged\nthree\n", "0", id="hand-edited-is-gated"),
        pytest.param("one\nbranch\nthree\n", "1", id="one-side-exactly-is-ungated"),
        pytest.param("one\nmain\nthree\n", "1", id="other-side-exactly-is-ungated"),
    ],
)
def test_resume_commits_a_hand_resolved_merge(
    tmp_path: Path, resolved: str, gate_seen: str
) -> None:
    """SCENARIO: stop on a conflict, edit the file without `git add`, `--continue`.

    MOCK SETUP: stub pre-commit hook records `FORGE_MERGE_SYNC`.
    EXPECTED BEHAVIOR: marker-free file is staged by resume, merge commit
    created and pushed. New content runs the gate (`FORGE_MERGE_SYNC=0`); content
    equal to a side commits ungated (`FORGE_MERGE_SYNC=1`). Without the
    stage step the commit would see an unmerged index; without the env
    split both cases would record the same value.

    Args:
        resolved: Hand-written content for the conflicted file.
        gate_seen: Expected `FORGE_MERGE_SYNC` value the hook recorded.
    """
    repo, origin = _conflicted_repo(tmp_path)
    assert update_branch.start(repo, None, push_after=False) == 3
    record = _stub_gate(repo, tmp_path)
    (repo / "shared.txt").write_text(resolved)

    assert update_branch.resume(repo, None, push_after=True) == 0

    assert len(_parents(repo)) == 2
    assert not _merge_in_progress(repo)
    assert record.read_text().strip() == gate_seen
    head = git_stdout(repo, "rev-parse", "HEAD").strip()
    assert git_stdout(origin, "rev-parse", "feature/x").strip() == head


def test_resume_gates_a_hand_edit_even_when_the_env_defers_the_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SCENARIO: `FORGE_MERGE_SYNC=1` exported in the caller's environment.

    MOCK SETUP: stub pre-commit hook records `FORGE_MERGE_SYNC`.
    EXPECTED BEHAVIOR: a hand-edited resume still runs the gate with the
    deferral pinned off (hook records `0`, not the inherited `1`).
    """
    monkeypatch.setenv("FORGE_MERGE_SYNC", "1")
    repo, _ = _conflicted_repo(tmp_path)
    assert update_branch.start(repo, None, push_after=False) == 3
    record = _stub_gate(repo, tmp_path)
    (repo / "shared.txt").write_text("one\nmerged\nthree\n")

    assert update_branch.resume(repo, None, push_after=False) == 0

    assert record.read_text().strip() == "0"


def test_resume_stages_a_file_with_a_bare_equals_line(tmp_path: Path) -> None:
    """SCENARIO: hand-resolved file holds a `=======` line (RST underline).

    EXPECTED BEHAVIOR: not mistaken for a conflict marker; resume stages it
    and commits the merge.
    """
    repo, _ = _conflicted_repo(tmp_path)
    assert update_branch.start(repo, None, push_after=False) == 3
    (repo / "shared.txt").write_text("one\nTitle\n=======\nthree\n")

    assert update_branch.resume(repo, None, push_after=False) == 0

    assert not _merge_in_progress(repo)
    assert "=======" in (repo / "shared.txt").read_text()


def _foreign_merge(repo: Path) -> None:
    """Leave a merge of an unrelated branch in progress.

    Args:
        repo: The checkout on `feature/x` to merge `side` into.
    """
    git_stdout(repo, "checkout", "-q", "-b", "side", "main")
    (repo / "side.txt").write_text("side\n")
    commit_all(repo, "side work")
    git_stdout(repo, "checkout", "-q", "feature/x")
    (repo / "mine.txt").write_text("mine\n")
    commit_all(repo, "mine work")
    subprocess.run(
        ["git", "merge", "--no-commit", "--no-ff", "side"],
        cwd=repo,
        env=GIT_ENV,
        check=True,
        capture_output=True,
    )


def _staged_outside(repo: Path) -> None:
    """Stop on a conflict, resolve it, and also stage an unrelated edit.

    Args:
        repo: The conflicted checkout to resolve and stage in.
    """
    assert update_branch.start(repo, None, push_after=False) == 3
    (repo / "shared.txt").write_text("one\nmerged\nthree\n")
    (repo / "extra.txt").write_text("sneaked in\n")
    git_stdout(repo, "add", "extra.txt")


@pytest.mark.parametrize(
    ("setup", "remedy"),
    [
        pytest.param(None, "forge-update-branch", id="no-merge-in-progress"),
        pytest.param(_foreign_merge, "--base", id="merge-not-of-base"),
        pytest.param(_staged_outside, "git restore --staged", id="staged-outside"),
    ],
)
def test_continue_refuses_and_names_a_remedy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    setup: object,
    remedy: str,
) -> None:
    """SCENARIO: `--continue` in a state it must not commit.

    EXPECTED BEHAVIOR: exit 1 through `main`, nothing committed, the
    refusal names the remedy possible in that state.

    Args:
        setup: Callable that leaves the repo in the state under test, or None.
        remedy: Substring the refusal must name.
    """
    repo, _ = _conflicted_repo(tmp_path)
    if callable(setup):
        setup(repo)
    head = git_stdout(repo, "rev-parse", "HEAD").strip()
    # repo_root() is process-cached; pin it to this test's repo.
    monkeypatch.setattr(update_branch, "repo_root", lambda: repo)

    with caplog.at_level("WARNING", logger=update_branch.__name__):
        code = update_branch.main(["--continue", "--no-push"])

    assert code == 1
    assert remedy in caplog.text
    assert git_stdout(repo, "rev-parse", "HEAD").strip() == head


def test_start_regenerates_a_generated_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SCENARIO: both sides change a generated artifact; nothing else conflicts.

    MOCK SETUP: `regen_commands` names `gen.txt`; `regenerate_artifact`
    writes deterministic content.
    EXPECTED BEHAVIOR: no stop; file holds the regenerated content; merge
    commit names it. Without regeneration the run stops with exit 3.
    """
    repo, origin = _repo_with_origin(tmp_path)
    (repo / "gen.txt").write_text("base\n")
    commit_all(repo, "add gen")
    git_stdout(repo, "push", "-q", "origin", "feature/x")
    git_stdout(repo, "checkout", "-q", "main")
    git_stdout(repo, "merge", "-q", "--ff-only", "feature/x")
    git_stdout(repo, "push", "-q", "origin", "main")
    git_stdout(repo, "checkout", "-q", "feature/x")
    (repo / "gen.txt").write_text("branch\n")
    commit_all(repo, "branch gen")
    _advance_origin(tmp_path, origin, {"gen.txt": "main\n"})

    def _fake_regenerate(root: Path, path: str, _argv: tuple[str, ...]) -> bool:
        (root / path).write_text("generated\n")
        return True

    monkeypatch.setattr(update_branch, "regen_commands", lambda _r: {"gen.txt": ("g",)})
    monkeypatch.setattr(update_branch, "regenerate_artifact", _fake_regenerate)

    assert update_branch.start(repo, None, push_after=False) == 0

    assert (repo / "gen.txt").read_text() == "generated\n"
    assert "Regenerated: gen.txt" in git_stdout(repo, "log", "-1", "--format=%B")


def test_start_resolves_the_version_slot_through_rebump(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SCENARIO: both sides bump the manifest version; nothing else conflicts.

    MOCK SETUP: `rebump` replaced with a fake that writes the manifest and
    stages it, returning an outcome without refusal.
    EXPECTED BEHAVIOR: no stop; commit message names the version slot.
    """
    slot = update_branch.PLUGIN_PATH
    repo, origin = _repo_with_origin(tmp_path)
    (repo / slot).parent.mkdir(parents=True)
    (repo / slot).write_text('{"version": "1.0.0"}\n')
    commit_all(repo, "add manifest")
    git_stdout(repo, "checkout", "-q", "main")
    git_stdout(repo, "merge", "-q", "--ff-only", "feature/x")
    git_stdout(repo, "push", "-q", "origin", "main")
    git_stdout(repo, "checkout", "-q", "feature/x")
    (repo / slot).write_text('{"version": "1.1.0"}\n')
    commit_all(repo, "branch bump")
    _advance_origin(tmp_path, origin, {slot: '{"version": "1.2.0"}\n'})

    def _fake_rebump(root: Path) -> SimpleNamespace:
        (root / slot).write_text('{"version": "1.3.0"}\n')
        git_stdout(root, "add", slot)
        return SimpleNamespace(refusal=None, staged=[slot])

    monkeypatch.setattr(update_branch, "rebump", _fake_rebump)

    assert update_branch.start(repo, None, push_after=False) == 0

    assert '"1.3.0"' in (repo / slot).read_text()
    assert f"Version slot: {slot}" in git_stdout(repo, "log", "-1", "--format=%B")


def test_generated_path_waits_for_source_conflicts_then_regenerates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SCENARIO: a source file and a generated artifact both conflict.

    MOCK SETUP: `regen_commands` names `gen.txt`; `regenerate_artifact`
    records each call and writes deterministic content.
    EXPECTED BEHAVIOR: start stops (exit 3) without regenerating; `gen.txt`
    stays unmerged. After the source is hand-resolved, resume regenerates
    it and commits the merge. Without the sources-first rule the generator
    would run on a half-merged tree.
    """
    repo, origin = _repo_with_origin(tmp_path)
    (repo / "gen.txt").write_text("base\n")
    commit_all(repo, "add gen")
    git_stdout(repo, "checkout", "-q", "main")
    git_stdout(repo, "merge", "-q", "--ff-only", "feature/x")
    git_stdout(repo, "push", "-q", "origin", "main")
    git_stdout(repo, "checkout", "-q", "feature/x")
    (repo / "gen.txt").write_text("branch\n")
    (repo / "shared.txt").write_text("one\nbranch\nthree\n")
    commit_all(repo, "branch edits both")
    _advance_origin(
        tmp_path, origin, {"gen.txt": "main\n", "shared.txt": "one\nmain\nthree\n"}
    )
    calls: list[str] = []

    def _fake_regenerate(root: Path, path: str, _argv: tuple[str, ...]) -> bool:
        calls.append(path)
        (root / path).write_text("generated\n")
        return True

    monkeypatch.setattr(update_branch, "regen_commands", lambda _r: {"gen.txt": ("g",)})
    monkeypatch.setattr(update_branch, "regenerate_artifact", _fake_regenerate)

    assert (
        update_branch.start(repo, None, push_after=False) == update_branch.EXIT_STOPPED
    )
    assert calls == []
    assert "gen.txt" in unmerged_paths(repo)

    (repo / "shared.txt").write_text("one\nmerged\nthree\n")
    assert update_branch.resume(repo, None, push_after=False) == 0

    assert calls == ["gen.txt"]
    assert (repo / "gen.txt").read_text() == "generated\n"
    assert not _merge_in_progress(repo)
    assert len(_parents(repo)) == 2


def test_start_stops_when_rebump_refuses_the_version_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """SCENARIO: both sides bump the manifest version; the resolver refuses.

    MOCK SETUP: `rebump` replaced with a fake returning a real
    `RebumpOutcome` whose `refusal` is set.
    EXPECTED BEHAVIOR: exit 3, merge in progress, slot path still unmerged,
    and the resolver's refusal reason is relayed so the stop says why.
    """
    slot = update_branch.PLUGIN_PATH
    repo, origin = _repo_with_origin(tmp_path)
    (repo / slot).parent.mkdir(parents=True)
    (repo / slot).write_text('{"version": "1.0.0"}\n')
    commit_all(repo, "add manifest")
    git_stdout(repo, "checkout", "-q", "main")
    git_stdout(repo, "merge", "-q", "--ff-only", "feature/x")
    git_stdout(repo, "push", "-q", "origin", "main")
    git_stdout(repo, "checkout", "-q", "feature/x")
    (repo / slot).write_text('{"version": "1.1.0"}\n')
    commit_all(repo, "branch bump")
    _advance_origin(tmp_path, origin, {slot: '{"version": "1.2.0"}\n'})
    refusal = RebumpOutcome("", "", "", (), refusal="no latest tag")
    monkeypatch.setattr(update_branch, "rebump", lambda _root: refusal)

    with caplog.at_level("WARNING"):
        assert (
            update_branch.start(repo, None, push_after=False)
            == update_branch.EXIT_STOPPED
        )

    assert _merge_in_progress(repo)
    assert unmerged_paths(repo) == [slot]
    assert "no latest tag" in caplog.text


def test_resume_does_not_auto_stage_a_modify_delete_conflict(tmp_path: Path) -> None:
    """SCENARIO: branch modifies `shared.txt`, base deletes it; nobody decides.

    EXPECTED BEHAVIOR: after the stop, resume leaves the (marker-free)
    file unmerged and exits 3. Without the both-sides-hold-it guard the
    file would be staged as a silent keep.
    """
    repo, origin = _repo_with_origin(tmp_path)
    (repo / "shared.txt").write_text("one\nbranch\nthree\n")
    commit_all(repo, "branch edits shared")
    _advance_origin(tmp_path, origin, {"unrelated.txt": "moved\n"})
    other = tmp_path / "other"
    git_stdout(other, "rm", "-q", "shared.txt")
    commit_all(other, "base deletes shared")
    git_stdout(other, "push", "-q", "origin", "main")

    assert (
        update_branch.start(repo, None, push_after=False) == update_branch.EXIT_STOPPED
    )
    assert unmerged_paths(repo) == ["shared.txt"]

    assert (
        update_branch.resume(repo, None, push_after=False) == update_branch.EXIT_STOPPED
    )

    assert unmerged_paths(repo) == ["shared.txt"]
    assert _merge_in_progress(repo)


def test_start_refuses_when_a_merge_is_already_in_progress(tmp_path: Path) -> None:
    """A second `start` mid-merge refuses and points at `--continue`."""
    repo, _ = _conflicted_repo(tmp_path)
    assert update_branch.start(repo, None, push_after=False) == 3

    with pytest.raises(UpdateRefusedError, match=r"--continue"):
        update_branch.start(repo, None, push_after=False)
