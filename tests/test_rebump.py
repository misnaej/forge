"""Tests for ``forge.rebump`` — the post-merge version-slot + changelog resolver.

Pins the compute-then-write contract (:func:`forge.rebump.rebump`): every
refusal fires before any disk write, so mid-merge and clean-tree scenarios
alike leave the tree untouched on refusal. Real ``git`` subprocess calls
build the merge/conflict states — no mocked git plumbing — mirroring
``tests/test_git_utils.py``'s real-repo conventions.
"""

from __future__ import annotations

import json
import logging
import subprocess
from typing import TYPE_CHECKING

import pytest

from forge import rebump
from tests.conftest import GIT_ENV, commit_all, init_git_repo


if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Local repo-building helpers
# ---------------------------------------------------------------------------


def _write_config(
    repo: Path,
    *,
    base_branch: str = "main",
    fragments: bool = False,
) -> None:
    """Write a ``pyproject.toml`` carrying ``[tool.forge]`` settings.

    Args:
        repo: Repo root to write into.
        base_branch: ``[tool.forge].base_branch`` value.
        fragments: When True, also writes
            ``[tool.forge.changelog] mode = "fragments"``.
    """
    lines = [
        "[tool.forge]",
        f'base_branch = "{base_branch}"',
    ]
    if fragments:
        lines += ["", "[tool.forge.changelog]", 'mode = "fragments"']
    (repo / "pyproject.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_plugin(repo: Path, version: str, *, pretty: bool = False) -> None:
    """Write ``.claude-plugin/plugin.json`` carrying *version*.

    Args:
        repo: Repo root to write into.
        version: Bare semver for the ``"version"`` field.
        pretty: When True, writes an indented multi-key manifest (to prove
            formatting is preserved outside the rewritten field); a compact
            single-line manifest otherwise.
    """
    plugin_dir = repo / ".claude-plugin"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    if pretty:
        text = (
            "{\n"
            '  "name": "forge",\n'
            f'  "version": "{version}",\n'
            '  "description": "test plugin"\n'
            "}\n"
        )
    else:
        text = json.dumps({"name": "forge", "version": version}) + "\n"
    (plugin_dir / "plugin.json").write_text(text, encoding="utf-8")


def _write_changelog(repo: Path, text: str) -> None:
    """Write ``CHANGELOG.md`` with *text*.

    Args:
        repo: Repo root to write into.
        text: Full changelog contents.
    """
    (repo / "CHANGELOG.md").write_text(text, encoding="utf-8")


def _tag(repo: Path, tag: str) -> None:
    """Create a lightweight tag *tag* at ``HEAD``.

    Args:
        repo: Repo root.
        tag: Tag name (e.g. ``"v1.0.0"``).
    """
    subprocess.run(["git", "tag", tag], cwd=repo, env=GIT_ENV, check=True)


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
# rebump() — contract scenarios
# ---------------------------------------------------------------------------


def test_rebump_mid_merge_same_slot_restacks_changelog(tmp_path: Path) -> None:
    """Two branches independently claim the same next slot — restack, don't refuse.

    Both ``feat/x`` and ``other`` bump ``plugin.json`` to the identical next
    version, so git auto-merges that field (no conflict); both also insert a
    changelog bullet at the same position under the shared next heading,
    which DOES conflict. ``rebump`` must resolve the changelog conflict via
    :func:`forge.changelog.restack_changelog` and re-stage the auto-merged
    manifest, without refusing.
    """
    init_git_repo(tmp_path)
    _write_plugin(tmp_path, "1.0.0")
    _write_changelog(tmp_path, "# Changelog\n\n## v1.0.0\n\n- initial\n")
    commit_all(tmp_path, "release v1.0.0")
    _tag(tmp_path, "v1.0.0")

    _checkout_new_branch(tmp_path, "other")
    _write_plugin(tmp_path, "1.1.0")
    _write_changelog(
        tmp_path,
        "# Changelog\n\n## v1.1.0\n\n- other entry\n\n## v1.0.0\n\n- initial\n",
    )
    commit_all(tmp_path, "other: bump + entry")
    _checkout(tmp_path, "main")
    assert _merge_no_commit(tmp_path, "other") == 0
    commit_all(tmp_path, "merge other")

    _checkout(tmp_path, "main")
    _checkout_new_branch(tmp_path, "feat/x")
    subprocess.run(
        ["git", "reset", "-q", "--hard", "v1.0.0"],
        cwd=tmp_path,
        env=GIT_ENV,
        check=True,
    )
    _write_plugin(tmp_path, "1.1.0")
    _write_changelog(
        tmp_path, "# Changelog\n\n## v1.1.0\n\n- feat entry\n\n## v1.0.0\n\n- initial\n"
    )
    commit_all(tmp_path, "feat: bump + entry")

    assert _merge_no_commit(tmp_path, "main") != 0

    outcome = rebump.rebump(tmp_path)

    assert outcome.refusal is None
    assert outcome.version == "1.1.0"
    assert outcome.bump_class == "minor"
    assert outcome.changelog_action == "restacked"
    assert outcome.staged == (rebump.PLUGIN_PATH, rebump.CHANGELOG_PATH)
    changelog_text = (tmp_path / rebump.CHANGELOG_PATH).read_text(encoding="utf-8")
    assert "- feat entry" in changelog_text
    assert "- other entry" in changelog_text
    plugin_version = json.loads(
        (tmp_path / rebump.PLUGIN_PATH).read_text(encoding="utf-8")
    )["version"]
    assert plugin_version == "1.1.0"


def test_rebump_mid_merge_genuine_plugin_conflict_classifies_from_own_delta(
    tmp_path: Path,
) -> None:
    """A real plugin.json conflict is resolved from the branch's OWN fork-point delta.

    ``other`` bumps to a major version (2.0.0); ``feat/x`` independently
    bumps to a minor version (1.1.0) from the same fork point. Merging
    ``main`` (carrying ``other``'s major bump) into ``feat/x`` conflicts the
    manifest textually — the classifier must read ``feat/x``'s own
    stage-1/stage-2 delta (minor), never ``other``'s.
    """
    init_git_repo(tmp_path)
    _write_plugin(tmp_path, "1.0.0")
    commit_all(tmp_path, "release v1.0.0")
    _tag(tmp_path, "v1.0.0")

    _checkout_new_branch(tmp_path, "other")
    _write_plugin(tmp_path, "2.0.0")
    commit_all(tmp_path, "other: major bump")
    _checkout(tmp_path, "main")
    assert _merge_no_commit(tmp_path, "other") == 0
    commit_all(tmp_path, "merge other")

    _checkout(tmp_path, "main")
    _checkout_new_branch(tmp_path, "feat/x")
    subprocess.run(
        ["git", "reset", "-q", "--hard", "v1.0.0"],
        cwd=tmp_path,
        env=GIT_ENV,
        check=True,
    )
    _write_plugin(tmp_path, "1.1.0")
    commit_all(tmp_path, "feat: minor bump")

    assert _merge_no_commit(tmp_path, "main") != 0

    outcome = rebump.rebump(tmp_path)

    assert outcome.refusal is None
    assert outcome.version == "1.1.0"
    assert outcome.bump_class == "minor"
    assert outcome.changelog_action == "no-changelog"
    assert outcome.staged == (rebump.PLUGIN_PATH,)
    plugin_text = (tmp_path / rebump.PLUGIN_PATH).read_text(encoding="utf-8")
    assert "<<<<<<<" not in plugin_text
    assert json.loads(plugin_text)["version"] == "1.1.0"


def test_rebump_clean_tree_post_tag_retitles_stale_heading(tmp_path: Path) -> None:
    """A clean tree behind the latest tag reslots and retitles, without a merge.

    ``feat/x`` forked before ``main`` released its own claim on ``v1.1.0``
    (now tagged); with no merge in progress, ``rebump`` reads the
    fork-point delta from the WORKING TREE (not ``main``'s current state),
    resolves the now-open ``v1.2.0`` slot, and retitles the branch's own
    stale ``## v1.1.0`` heading.
    """
    init_git_repo(tmp_path)
    _write_plugin(tmp_path, "1.0.0")
    _write_changelog(tmp_path, "# Changelog\n\n## v1.0.0\n\n- initial\n")
    commit_all(tmp_path, "release v1.0.0")
    _tag(tmp_path, "v1.0.0")

    _checkout_new_branch(tmp_path, "feat/x")
    _write_plugin(tmp_path, "1.1.0")
    _write_changelog(
        tmp_path, "# Changelog\n\n## v1.1.0\n\n- feat entry\n\n## v1.0.0\n\n- initial\n"
    )
    commit_all(tmp_path, "feat: bump + entry")

    _checkout(tmp_path, "main")
    _write_plugin(tmp_path, "1.1.0")
    _write_changelog(
        tmp_path,
        "# Changelog\n\n## v1.1.0\n\n- other entry\n\n## v1.0.0\n\n- initial\n",
    )
    commit_all(tmp_path, "other: bump + entry, released")
    _tag(tmp_path, "v1.1.0")

    _checkout(tmp_path, "feat/x")

    outcome = rebump.rebump(tmp_path)

    assert outcome.refusal is None
    assert outcome.version == "1.2.0"
    assert outcome.bump_class == "minor"
    assert outcome.changelog_action == "retitled"
    assert outcome.staged == (rebump.PLUGIN_PATH, rebump.CHANGELOG_PATH)
    assert (
        json.loads((tmp_path / rebump.PLUGIN_PATH).read_text(encoding="utf-8"))[
            "version"
        ]
        == "1.2.0"
    )
    changelog_text = (tmp_path / rebump.CHANGELOG_PATH).read_text(encoding="utf-8")
    assert changelog_text.startswith("# Changelog\n\n## v1.2.0\n\n- feat entry\n")
    assert "## v1.0.0\n\n- initial" in changelog_text


def test_rebump_refuses_on_unrelated_merge_conflict(tmp_path: Path) -> None:
    """A conflict outside {plugin.json, CHANGELOG.md} is a real code conflict — refuse.

    ``rebump`` must not attempt to resolve arbitrary source conflicts; it
    names the offending path(s) and leaves the merge for a human.
    """
    init_git_repo(tmp_path)
    (tmp_path / "shared.txt").write_text("base\n")
    commit_all(tmp_path, "add shared.txt")

    _checkout_new_branch(tmp_path, "other")
    (tmp_path / "shared.txt").write_text("other change\n")
    commit_all(tmp_path, "other edit")
    _checkout(tmp_path, "main")

    _checkout_new_branch(tmp_path, "feat/x")
    (tmp_path / "shared.txt").write_text("feat change\n")
    commit_all(tmp_path, "feat edit")

    assert _merge_no_commit(tmp_path, "other") != 0

    outcome = rebump.rebump(tmp_path)

    assert outcome.refusal is not None
    assert "shared.txt" in outcome.refusal
    assert outcome.version == ""
    assert outcome.bump_class == ""
    assert outcome.changelog_action == ""
    assert outcome.staged == ()


def test_rebump_refuses_on_protected_branch(tmp_path: Path) -> None:
    """Rebump refuses on the configured base branch."""
    init_git_repo(tmp_path)
    _write_config(tmp_path, base_branch="main")
    _write_plugin(tmp_path, "1.0.0")
    commit_all(tmp_path, "config + plugin")
    _tag(tmp_path, "v1.0.0")

    outcome = rebump.rebump(tmp_path)

    assert outcome.refusal is not None
    assert "on 'main'" in outcome.refusal
    assert outcome.staged == ()


# ---------------------------------------------------------------------------
# rebump() — edge / error rows
# ---------------------------------------------------------------------------


def test_rebump_refuses_when_no_v_tag_exists(tmp_path: Path) -> None:
    """No ``v*`` tag anywhere in the repo — refuse, nothing to rebump against."""
    init_git_repo(tmp_path)
    _checkout_new_branch(tmp_path, "feat/x")
    _write_plugin(tmp_path, "1.0.0")
    commit_all(tmp_path, "add plugin")

    outcome = rebump.rebump(tmp_path)

    assert outcome.refusal is not None
    assert "no v* tag found" in outcome.refusal


def test_rebump_refuses_on_stray_markers_without_a_merge(tmp_path: Path) -> None:
    """Conflict markers on disk with no MERGE_HEAD — a half-resolved state, refuse."""
    init_git_repo(tmp_path)
    _checkout_new_branch(tmp_path, "feat/x")
    _write_plugin(tmp_path, "1.0.0")
    _write_changelog(
        tmp_path, "<<<<<<< HEAD\n## v1.1.0\n=======\n## v1.2.0\n>>>>>>> other\n"
    )
    commit_all(tmp_path, "half-resolved changelog")
    _tag(tmp_path, "v1.0.0")

    outcome = rebump.rebump(tmp_path)

    assert outcome.refusal is not None
    assert "conflict markers but no merge is in progress" in outcome.refusal


def test_rebump_refuses_when_manifest_version_unreadable(tmp_path: Path) -> None:
    """A missing plugin.json — the branch-side version can't be read, refuse."""
    init_git_repo(tmp_path)
    _checkout_new_branch(tmp_path, "feat/x")
    (tmp_path / "a.txt").write_text("x\n")
    commit_all(tmp_path, "no plugin.json here")
    _tag(tmp_path, "v1.0.0")

    outcome = rebump.rebump(tmp_path)

    assert outcome.refusal is not None
    assert "cannot read the branch-side" in outcome.refusal


def test_rebump_refuses_on_changelog_modify_delete_conflict(tmp_path: Path) -> None:
    """Modify/delete CHANGELOG conflict — no ``theirs`` stage, refuse."""
    init_git_repo(tmp_path)
    _write_plugin(tmp_path, "1.0.0")
    _write_changelog(tmp_path, "# Changelog\n\n## v1.0.0\n\n- initial\n")
    commit_all(tmp_path, "release v1.0.0")
    _tag(tmp_path, "v1.0.0")

    _checkout_new_branch(tmp_path, "other")
    (tmp_path / rebump.CHANGELOG_PATH).unlink()
    commit_all(tmp_path, "other: delete changelog")
    _checkout(tmp_path, "main")

    _checkout_new_branch(tmp_path, "feat/x")
    _write_changelog(tmp_path, "# Changelog\n\n## v1.0.0\n\n- initial\n- feat entry\n")
    commit_all(tmp_path, "feat: edit changelog")

    assert _merge_no_commit(tmp_path, "other") != 0

    outcome = rebump.rebump(tmp_path)

    assert outcome.refusal is not None
    assert "cannot read merge stages for restack" in outcome.refusal


def test_rebump_refuses_when_ours_changelog_has_no_release_heading(
    tmp_path: Path,
) -> None:
    """A conflicted CHANGELOG whose branch side lacks ``## vX.Y.Z`` refuses cleanly.

    Regression pin: this used to escape as a raw ``ValueError`` traceback
    from ``restack_changelog`` instead of the documented refusal/exit-1
    contract.
    """
    init_git_repo(tmp_path)
    _write_plugin(tmp_path, "1.0.0")
    _write_changelog(tmp_path, "# Changelog\n\n## v1.0.0\n\n- initial\n")
    commit_all(tmp_path, "release v1.0.0")
    _tag(tmp_path, "v1.0.0")

    _checkout_new_branch(tmp_path, "other")
    _write_changelog(
        tmp_path,
        "# Changelog\n\n## v1.1.0\n\n- other entry\n\n## v1.0.0\n\n- initial\n",
    )
    commit_all(tmp_path, "other: bump + entry")
    _checkout(tmp_path, "main")

    _checkout_new_branch(tmp_path, "feat/x")
    _write_changelog(tmp_path, "# Changelog\n\n## Unreleased\n\n- feat entry\n")
    commit_all(tmp_path, "feat: headingless changelog edit")

    assert _merge_no_commit(tmp_path, "other") != 0
    plugin_before = (tmp_path / rebump.PLUGIN_PATH).read_bytes()

    outcome = rebump.rebump(tmp_path)

    assert outcome.refusal is not None
    assert "no ## vX.Y.Z heading to restack" in outcome.refusal
    assert (tmp_path / rebump.PLUGIN_PATH).read_bytes() == plugin_before


def test_rebump_fragments_mode_is_a_full_noop_on_clean_tree(tmp_path: Path) -> None:
    """Fragments mode never touches manifest or changelog — assembler-owned versions.

    `forge-changelog release` is the manifest's single writer in fragments
    mode; rebump computes the would-be slot for its report but writes and
    stages nothing.
    """
    init_git_repo(tmp_path)
    _write_config(tmp_path, fragments=True)
    _write_plugin(tmp_path, "1.0.0")
    commit_all(tmp_path, "config + release v1.0.0")
    _tag(tmp_path, "v1.0.0")

    _checkout_new_branch(tmp_path, "feat/x")
    _write_plugin(tmp_path, "1.1.0")
    commit_all(tmp_path, "feat: bump")

    _checkout(tmp_path, "main")
    _write_plugin(tmp_path, "1.1.0")
    commit_all(tmp_path, "other: bump, released")
    _tag(tmp_path, "v1.1.0")
    _checkout(tmp_path, "feat/x")
    plugin_before = (tmp_path / rebump.PLUGIN_PATH).read_bytes()

    outcome = rebump.rebump(tmp_path)

    assert outcome.refusal is None
    assert outcome.version == "1.2.0"
    assert outcome.changelog_action == "fragments-noop"
    assert outcome.staged == ()
    assert (tmp_path / rebump.PLUGIN_PATH).read_bytes() == plugin_before


def test_rebump_fragments_mode_manifest_conflict_refuses_to_release_cli(
    tmp_path: Path,
) -> None:
    """A fragments-mode plugin.json conflict is a release-PR race — refused.

    The refusal points at `forge-changelog release` (the single writer);
    rebump must not adjudicate racing release PRs.
    """
    init_git_repo(tmp_path)
    _write_config(tmp_path, fragments=True)
    _write_plugin(tmp_path, "1.0.0")
    commit_all(tmp_path, "config + release v1.0.0")
    _tag(tmp_path, "v1.0.0")

    _checkout_new_branch(tmp_path, "release-a")
    _write_plugin(tmp_path, "1.1.0")
    commit_all(tmp_path, "release A bump")
    _checkout(tmp_path, "main")
    _write_plugin(tmp_path, "1.2.0")
    commit_all(tmp_path, "release B bump")
    _checkout(tmp_path, "release-a")
    assert _merge_no_commit(tmp_path, "main") != 0

    outcome = rebump.rebump(tmp_path)

    assert outcome.refusal is not None
    assert "forge-changelog release" in outcome.refusal


def test_rebump_fragments_mode_conflicted_changelog_refuses_tree_untouched(
    tmp_path: Path,
) -> None:
    """Fragments mode + a CHANGELOG conflict is unexpected — refuse, tree untouched.

    Assembly is single-writer in fragments mode, so a CHANGELOG conflict
    should never happen; the tool refuses rather than guessing. Pins the
    compute-then-write invariant directly: every field computed
    successfully (plugin version, bump class) before the refusal must
    leave no trace on disk or in the index.
    """
    init_git_repo(tmp_path)
    _write_config(tmp_path, fragments=True)
    _write_plugin(tmp_path, "1.0.0")
    _write_changelog(tmp_path, "# Changelog\n\n## v1.0.0\n\n- initial\n")
    commit_all(tmp_path, "config + release v1.0.0")
    _tag(tmp_path, "v1.0.0")

    _checkout_new_branch(tmp_path, "other")
    _write_plugin(tmp_path, "1.1.0")
    _write_changelog(
        tmp_path,
        "# Changelog\n\n## v1.1.0\n\n- other entry\n\n## v1.0.0\n\n- initial\n",
    )
    commit_all(tmp_path, "other: bump + entry")
    _checkout(tmp_path, "main")
    assert _merge_no_commit(tmp_path, "other") == 0
    commit_all(tmp_path, "merge other")

    _checkout(tmp_path, "main")
    _checkout_new_branch(tmp_path, "feat/x")
    subprocess.run(
        ["git", "reset", "-q", "--hard", "v1.0.0"],
        cwd=tmp_path,
        env=GIT_ENV,
        check=True,
    )
    _write_config(tmp_path, fragments=True)
    _write_plugin(tmp_path, "1.1.0")
    _write_changelog(
        tmp_path, "# Changelog\n\n## v1.1.0\n\n- feat entry\n\n## v1.0.0\n\n- initial\n"
    )
    commit_all(tmp_path, "feat: bump + entry")

    assert _merge_no_commit(tmp_path, "main") != 0

    plugin_bytes_before = (tmp_path / rebump.PLUGIN_PATH).read_bytes()
    staged_before = _staged_paths(tmp_path)

    outcome = rebump.rebump(tmp_path)

    assert outcome.refusal is not None
    assert "assembly is single-writer" in outcome.refusal
    assert (tmp_path / rebump.PLUGIN_PATH).read_bytes() == plugin_bytes_before
    assert _staged_paths(tmp_path) == staged_before


def test_rebump_no_changelog_file_action_on_clean_tree(tmp_path: Path) -> None:
    """Clean tree, shared-heading mode, but no CHANGELOG.md — "no-changelog" action."""
    init_git_repo(tmp_path)
    _write_plugin(tmp_path, "1.0.0")
    commit_all(tmp_path, "release v1.0.0")
    _tag(tmp_path, "v1.0.0")

    _checkout_new_branch(tmp_path, "feat/x")
    _write_plugin(tmp_path, "1.1.0")
    commit_all(tmp_path, "feat: bump")

    _checkout(tmp_path, "main")
    _write_plugin(tmp_path, "1.1.0")
    commit_all(tmp_path, "other: bump, released")
    _tag(tmp_path, "v1.1.0")
    _checkout(tmp_path, "feat/x")

    outcome = rebump.rebump(tmp_path)

    assert outcome.refusal is None
    assert outcome.changelog_action == "no-changelog"
    assert outcome.staged == (rebump.PLUGIN_PATH,)


def test_rebump_already_correct_keeps_own_version_marks_changelog_unchanged(
    tmp_path: Path,
) -> None:
    """Branch already carries the right slot and heading — both halves are no-ops."""
    init_git_repo(tmp_path)
    _write_plugin(tmp_path, "1.0.0")
    _write_changelog(tmp_path, "# Changelog\n\n## v1.0.0\n\n- initial\n")
    commit_all(tmp_path, "release v1.0.0")
    _tag(tmp_path, "v1.0.0")

    _checkout_new_branch(tmp_path, "feat/x")
    _write_plugin(tmp_path, "1.1.0")
    _write_changelog(
        tmp_path, "# Changelog\n\n## v1.1.0\n\n- feat entry\n\n## v1.0.0\n\n- initial\n"
    )
    commit_all(tmp_path, "feat: bump + entry")

    outcome = rebump.rebump(tmp_path)

    assert outcome.refusal is None
    assert outcome.version == "1.1.0"
    assert outcome.changelog_action == "unchanged"
    assert outcome.staged == ()


def test_rebump_full_noop_stages_nothing_in_the_index(tmp_path: Path) -> None:
    """A fully-resolved branch touches the git index not at all, not just returns ()."""
    init_git_repo(tmp_path)
    _write_plugin(tmp_path, "1.0.0")
    commit_all(tmp_path, "release v1.0.0")
    _tag(tmp_path, "v1.0.0")

    _checkout_new_branch(tmp_path, "feat/x")
    _write_plugin(tmp_path, "1.1.0")
    commit_all(tmp_path, "feat: bump")

    outcome = rebump.rebump(tmp_path)

    assert outcome.refusal is None
    assert outcome.staged == ()
    assert _staged_paths(tmp_path) == []


# ---------------------------------------------------------------------------
# _resolve_version — direct calls
# ---------------------------------------------------------------------------


def test_resolve_version_below_latest_takes_next_class_slot(tmp_path: Path) -> None:
    """A branch version behind the latest tag takes the next class-appropriate slot."""
    _write_plugin(tmp_path, "0.9.0")
    version = rebump._resolve_version(tmp_path, "v1.0.0", "minor", mid_merge=False)
    assert version == "1.1.0"


def test_resolve_version_ahead_of_latest_keeps_own_slot(tmp_path: Path) -> None:
    """A branch version already strictly ahead of the latest tag is kept as-is."""
    _write_plugin(tmp_path, "1.2.0")
    version = rebump._resolve_version(tmp_path, "v1.0.0", "minor", mid_merge=False)
    assert version == "1.2.0"


# ---------------------------------------------------------------------------
# _render_plugin_version — direct calls
# ---------------------------------------------------------------------------


def test_render_plugin_version_none_when_already_correct_clean_tree(
    tmp_path: Path,
) -> None:
    """Clean tree, target version already on disk → None (nothing to write)."""
    _write_plugin(tmp_path, "1.0.0")
    result = rebump._render_plugin_version(tmp_path, "1.0.0", mid_merge=False)
    assert result is None


def test_render_plugin_version_preserves_pretty_manifest_formatting(
    tmp_path: Path,
) -> None:
    """A pretty multi-key manifest keeps its indentation; only the version changes."""
    _write_plugin(tmp_path, "1.0.0", pretty=True)
    result = rebump._render_plugin_version(tmp_path, "2.0.0", mid_merge=False)
    assert result == (
        "{\n"
        '  "name": "forge",\n'
        '  "version": "2.0.0",\n'
        '  "description": "test plugin"\n'
        "}\n"
    )


def test_render_plugin_version_raises_when_no_version_field(tmp_path: Path) -> None:
    """A manifest with no ``"version"`` key can't be rewritten → ``_RefusalError``."""
    plugin_dir = tmp_path / ".claude-plugin"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.json").write_text(json.dumps({"name": "forge"}) + "\n")
    with pytest.raises(rebump._RefusalError, match='no "version" field'):
        rebump._render_plugin_version(tmp_path, "1.0.0", mid_merge=False)


def test_render_plugin_version_refuses_json_escape_injection(tmp_path: Path) -> None:
    r"""A version field carrying JSON escapes must refuse, never write broken JSON.

    CWE-116 regression pin: a branch-authored ``"version"`` like
    ``99.0.0\\", \\"pwned\\": \\"x`` defeats the textual rewrite (the regex
    stops at the payload's first embedded quote), so the fail-closed
    post-rewrite validation must raise instead of returning corrupt text.
    """
    plugin_dir = tmp_path / ".claude-plugin"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.json").write_text(
        '{\n  "name": "forge",\n'
        '  "version": "99.0.0\\", \\"pwned\\": \\"x",\n'
        '  "other": "field"\n}\n',
        encoding="utf-8",
    )
    with pytest.raises(rebump._RefusalError, match=r"invalid JSON|did not land"):
        rebump._render_plugin_version(tmp_path, "99.0.0", mid_merge=False)


# ---------------------------------------------------------------------------
# _read_index_stage
# ---------------------------------------------------------------------------


def test_read_index_stage_reads_base_ours_and_theirs(tmp_path: Path) -> None:
    """A real conflicted file's stages 1/2/3 read the base/ours/theirs blobs."""
    init_git_repo(tmp_path)
    (tmp_path / "shared.txt").write_text("base\n")
    commit_all(tmp_path, "add shared.txt")

    _checkout_new_branch(tmp_path, "other")
    (tmp_path / "shared.txt").write_text("theirs\n")
    commit_all(tmp_path, "other edit")
    _checkout(tmp_path, "main")

    _checkout_new_branch(tmp_path, "feat/x")
    (tmp_path / "shared.txt").write_text("ours\n")
    commit_all(tmp_path, "feat edit")

    assert _merge_no_commit(tmp_path, "other") != 0

    assert rebump._read_index_stage(tmp_path, 1, "shared.txt") == "base\n"
    assert rebump._read_index_stage(tmp_path, 2, "shared.txt") == "ours\n"
    assert rebump._read_index_stage(tmp_path, 3, "shared.txt") == "theirs\n"


def test_read_index_stage_missing_stage_returns_none(tmp_path: Path) -> None:
    """A path with no conflict (never staged as unmerged) has no stage entries."""
    init_git_repo(tmp_path)
    (tmp_path / "a.txt").write_text("x\n")
    commit_all(tmp_path, "add a.txt")
    assert rebump._read_index_stage(tmp_path, 2, "a.txt") is None


# ---------------------------------------------------------------------------


def test_main_exit_0_logs_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A successful rebump exits 0 and logs the resolved version + staged paths."""
    init_git_repo(tmp_path)
    _write_plugin(tmp_path, "1.0.0")
    commit_all(tmp_path, "release v1.0.0")
    _tag(tmp_path, "v1.0.0")
    _checkout_new_branch(tmp_path, "feat/x")
    _write_plugin(tmp_path, "1.1.0")
    commit_all(tmp_path, "feat: bump")

    _checkout(tmp_path, "main")
    _write_plugin(tmp_path, "1.1.0")
    commit_all(tmp_path, "other: bump, released")
    _tag(tmp_path, "v1.1.0")
    _checkout(tmp_path, "feat/x")

    monkeypatch.setattr(rebump.Path, "cwd", staticmethod(lambda: tmp_path))
    monkeypatch.setattr("sys.argv", ["forge-rebump"])
    with caplog.at_level(logging.INFO, logger="forge.rebump"):
        exit_code = rebump.main()

    assert exit_code == 0
    assert any("Resolved" in record.message for record in caplog.records)
    assert any("Staged:" in record.message for record in caplog.records)


def test_main_exit_1_logs_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A refusal exits 1 and logs the refusal reason."""
    init_git_repo(tmp_path)
    _checkout_new_branch(tmp_path, "feat/x")

    monkeypatch.setattr(rebump.Path, "cwd", staticmethod(lambda: tmp_path))
    monkeypatch.setattr("sys.argv", ["forge-rebump"])
    with caplog.at_level(logging.INFO, logger="forge.rebump"):
        exit_code = rebump.main()

    assert exit_code == 1
    assert any("Refusing to rebump" in record.message for record in caplog.records)


def test_main_nothing_to_stage_logs_that_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A fully-resolved branch exits 0 and logs the nothing-to-stage message."""
    init_git_repo(tmp_path)
    _write_plugin(tmp_path, "1.0.0")
    commit_all(tmp_path, "release v1.0.0")
    _tag(tmp_path, "v1.0.0")
    _checkout_new_branch(tmp_path, "feat/x")
    _write_plugin(tmp_path, "1.1.0")
    commit_all(tmp_path, "feat: bump")

    monkeypatch.setattr(rebump.Path, "cwd", staticmethod(lambda: tmp_path))
    monkeypatch.setattr("sys.argv", ["forge-rebump"])
    with caplog.at_level(logging.INFO, logger="forge.rebump"):
        exit_code = rebump.main()

    assert exit_code == 0
    assert any(
        "Nothing to stage — tree already resolved." in record.message
        for record in caplog.records
    )
