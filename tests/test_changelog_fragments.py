"""Tests for ``forge.changelog_fragments``.

Fragment mode's validation, discovery, assembly, and the
``forge-changelog`` CLI (``check`` / ``assemble``) — the gate shared with
the ``changelog_version`` / ``changelog_updated`` pre-commit steps
(``tests/test_precommit.py``).
"""

from __future__ import annotations

import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from forge import changelog_fragments
from forge.changelog_fragments import (
    Fragment,
    assemble_changelog,
    check_pending,
    discover_fragments,
    main,
    max_level,
    validate_fragment,
)
from tests.conftest import GIT_ENV, commit_all, init_git_repo


def _write_fragment(directory: Path, name: str, body: str) -> Path:
    """Write a fragment file *name* with *body* under *directory*.

    Args:
        directory: Target ``changelog.d``-shaped directory (created if absent).
        name: Fragment filename.
        body: Full file contents, written verbatim.

    Returns:
        The written fragment path.
    """
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(body, encoding="utf-8")
    return path


def _make_fragment(
    *, ftype: str = "added", level: str = "minor", slug: str = "x", body: str = "- x"
) -> Fragment:
    """Build a :class:`Fragment` in memory, without touching the filesystem.

    ``assemble_changelog`` and ``max_level`` only read a fragment's
    fields — they never touch ``path`` on disk — so callers of this
    helper don't need a real file underneath.

    Args:
        ftype: Fragment type token.
        level: Declared bump level.
        slug: Filename stem.
        body: Entry markdown body.

    Returns:
        A `Fragment` with a placeholder (non-existent) path.
    """
    return Fragment(
        path=Path(f"{slug}.{ftype}.md"), slug=slug, type=ftype, level=level, body=body
    )


class _FixedDatetime(datetime):
    """A ``datetime`` subclass whose ``now()`` always returns a fixed instant."""

    @classmethod
    def now(cls, tz: object = None) -> _FixedDatetime:
        """Return the fixed instant, tagged with *tz*.

        Args:
            tz: Timezone to attach (matches ``datetime.now``'s signature).

        Returns:
            The fixed 2026-01-02 instant.
        """
        return cls(2026, 1, 2, tzinfo=tz)


# ---------------------------------------------------------------------------
# validate_fragment
# ---------------------------------------------------------------------------


def test_validate_fragment_happy_path(tmp_path: Path) -> None:
    """A well-formed fragment parses cleanly with no errors."""
    path = _write_fragment(
        tmp_path, "issue-229.added.md", "bump: minor\n- did a thing\n"
    )
    fragment, errors = validate_fragment(path)
    assert errors == []
    assert fragment == Fragment(
        path=path, slug="issue-229", type="added", level="minor", body="- did a thing"
    )


@pytest.mark.parametrize(
    "name",
    [
        "issue229.md",  # missing the <type> segment
        "issue 229.added.md",  # space in slug
        "issue-229.added",  # missing .md extension
    ],
)
def test_validate_fragment_malformed_filename(tmp_path: Path, name: str) -> None:
    """Filenames missing a segment, containing a space, or lacking `.md` fail.

    Args:
        name: The malformed filename under test.
    """
    path = _write_fragment(tmp_path, name, "bump: minor\n- x\n")
    fragment, errors = validate_fragment(path)
    assert fragment is None
    assert any("filename must be <slug>.<type>.md" in e for e in errors)


def test_validate_fragment_unknown_type(tmp_path: Path) -> None:
    """A type token outside `FRAGMENT_TYPES` is rejected by name."""
    path = _write_fragment(tmp_path, "x.bogus.md", "bump: minor\n- x\n")
    _fragment, errors = validate_fragment(path)
    assert any("unknown type 'bogus'" in e for e in errors)


def test_validate_fragment_version_shaped_filename(tmp_path: Path) -> None:
    """A concrete version number embedded in the filename is rejected."""
    path = _write_fragment(tmp_path, "note-1.2.3.added.md", "bump: minor\n- x\n")
    _fragment, errors = validate_fragment(path)
    assert any("version-shaped string in filename" in e for e in errors)


def test_validate_fragment_version_shaped_body(tmp_path: Path) -> None:
    """A concrete version number embedded in the body is rejected."""
    path = _write_fragment(
        tmp_path, "note.added.md", "bump: minor\nSee v1.2.3 for context.\n"
    )
    fragment, errors = validate_fragment(path)
    assert fragment is None
    assert errors == [
        (
            "note.added.md: version-shaped string in body — the assembler is the "
            "only writer of version numbers"
        )
    ]


@pytest.mark.parametrize(
    "first_line",
    [
        "no bump line at all",
        "bump minor",  # missing colon
    ],
)
def test_validate_fragment_malformed_bump_first_line(
    tmp_path: Path, first_line: str
) -> None:
    """A first line that doesn't match `bump: <level>` is rejected.

    Args:
        first_line: The malformed first line under test.
    """
    path = _write_fragment(tmp_path, "note.added.md", f"{first_line}\n- entry\n")
    _fragment, errors = validate_fragment(path)
    assert any("first line must be 'bump: patch|minor|major'" in e for e in errors)


def test_validate_fragment_unknown_level(tmp_path: Path) -> None:
    """A bump level outside `FRAGMENT_LEVELS` is rejected by name."""
    path = _write_fragment(tmp_path, "note.added.md", "bump: superduper\n- x\n")
    _fragment, errors = validate_fragment(path)
    assert any("unknown level 'superduper'" in e for e in errors)


def test_validate_fragment_empty_body(tmp_path: Path) -> None:
    """A fragment with no entry content past the bump line is rejected."""
    path = _write_fragment(tmp_path, "note.added.md", "bump: minor\n")
    fragment, errors = validate_fragment(path)
    assert fragment is None
    assert errors == ["note.added.md: empty entry body"]


def test_validate_fragment_embedded_heading(tmp_path: Path) -> None:
    """A `## ` heading in the body would splice fake structure — rejected."""
    path = _write_fragment(
        tmp_path, "note.added.md", "bump: minor\n## Sneaky heading\n- x\n"
    )
    _fragment, errors = validate_fragment(path)
    assert any("embedded '## ' heading in body" in e for e in errors)


def test_validate_fragment_unreadable_path_returns_only_unreadable_error(
    tmp_path: Path,
) -> None:
    """An unreadable path pre-empts filename errors — the early return discards them.

    A directory masquerading as a fragment "file" fails BOTH the filename
    pattern (no `<type>` segment) and the read (`IsADirectoryError`); the
    validator returns as soon as the read fails, so only the unreadable
    error surfaces — the filename error is discarded, never accumulated.
    """
    bad_path = tmp_path / "badname.md"
    bad_path.mkdir()
    fragment, errors = validate_fragment(bad_path)
    assert fragment is None
    assert len(errors) == 1
    assert "unreadable" in errors[0]


def test_validate_fragment_accumulates_all_simultaneous_violations(
    tmp_path: Path,
) -> None:
    """Independent violations (type, level, empty body) all accumulate together."""
    path = _write_fragment(tmp_path, "thing.bogus.md", "bump: superduper\n")
    fragment, errors = validate_fragment(path)
    assert fragment is None
    assert len(errors) == 3
    assert any("unknown type 'bogus'" in e for e in errors)
    assert any("unknown level 'superduper'" in e for e in errors)
    assert any("empty entry body" in e for e in errors)


# ---------------------------------------------------------------------------
# discover_fragments
# ---------------------------------------------------------------------------


def test_discover_fragments_returns_sorted_regardless_of_write_order(
    tmp_path: Path,
) -> None:
    """Fragments come back filename-sorted, independent of write order."""
    directory = tmp_path / "changelog.d"
    _write_fragment(directory, "c.added.md", "bump: minor\n- c\n")
    _write_fragment(directory, "a.added.md", "bump: minor\n- a\n")
    _write_fragment(directory, "b.added.md", "bump: minor\n- b\n")
    result = discover_fragments(tmp_path)
    assert [p.name for p in result] == ["a.added.md", "b.added.md", "c.added.md"]


def test_discover_fragments_absent_dir_returns_empty_list(tmp_path: Path) -> None:
    """No `changelog.d/` directory at all → empty list, not an error."""
    assert discover_fragments(tmp_path) == []


def test_discover_fragments_skips_directory_matching_glob(tmp_path: Path) -> None:
    """A directory whose name matches the `*.md` glob is not a fragment.

    Pins the ``p.is_file()`` filter: `Path.glob` alone would yield a
    directory entry named ``x.added.md``, which is not a readable
    fragment — only the real file must come back.
    """
    directory = tmp_path / "changelog.d"
    _write_fragment(directory, "a.added.md", "bump: minor\n- a\n")
    (directory / "x.added.md").mkdir()
    result = discover_fragments(tmp_path)
    assert [p.name for p in result] == ["a.added.md"]


# ---------------------------------------------------------------------------
# max_level
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("levels", "expected"),
    [
        (["patch"], "patch"),
        (["patch", "minor"], "minor"),
        (["major", "patch"], "major"),
    ],
)
def test_max_level_returns_the_strongest_level(
    levels: list[str], expected: str
) -> None:
    """The strongest declared level wins, regardless of list order.

    Args:
        levels: Bump levels declared across the fragment set.
        expected: The strongest level among *levels*.
    """
    fragments = [
        _make_fragment(level=level, slug=f"f{i}") for i, level in enumerate(levels)
    ]
    assert max_level(fragments) == expected


# ---------------------------------------------------------------------------
# assemble_changelog
# ---------------------------------------------------------------------------


def test_assemble_changelog_groups_by_type_in_fixed_order() -> None:
    """Groups render in `FRAGMENT_TYPES` order, regardless of caller list order."""
    fragments = [
        _make_fragment(ftype="docs", slug="d1", body="- doc entry"),
        _make_fragment(ftype="added", slug="a1", body="- add entry"),
    ]
    result = assemble_changelog("", fragments, "v1.0.0")
    assert result.index("### Features") < result.index("### Docs")


def test_assemble_changelog_preserves_within_group_caller_order() -> None:
    """Fragments sharing a type keep the caller's list order, not re-sorted."""
    fragments = [
        _make_fragment(ftype="added", slug="z", body="- second call, printed first"),
        _make_fragment(ftype="added", slug="a", body="- first call, printed second"),
    ]
    result = assemble_changelog("", fragments, "v1.0.0")
    assert result.index("second call, printed first") < result.index(
        "first call, printed second"
    )


def test_assemble_changelog_inserts_above_existing_heading_preserving_old_content() -> (
    None
):
    """A new heading is inserted above the top one; everything below is untouched."""
    text = "# Changelog\n\n## v1.0.0\n\n- old entry\n"
    result = assemble_changelog(text, [_make_fragment(body="- new entry")], "v1.1.0")
    assert result.startswith("# Changelog\n\n## v1.1.0")
    assert result.index("## v1.1.0") < result.index("## v1.0.0")
    assert result.endswith("## v1.0.0\n\n- old entry\n")


def test_assemble_changelog_appends_after_preamble_when_no_heading() -> None:
    """With no existing release heading, the new entry is appended after the prose."""
    text = "# Changelog\n\nIntro prose.\n"
    result = assemble_changelog(text, [_make_fragment(body="- entry")], "v1.0.0")
    assert result.startswith("# Changelog\n\nIntro prose.\n\n## v1.0.0")


def test_assemble_changelog_raises_valueerror_on_existing_target_heading() -> None:
    """A heading for *version* already present in *text* is a hard error."""
    text = "## v1.0.0\n\n- x\n"
    with pytest.raises(ValueError, match="already exists"):
        assemble_changelog(text, [_make_fragment()], "v1.0.0")


def test_assemble_changelog_raises_valueerror_on_empty_fragments() -> None:
    """Assembling with no fragments at all is a hard error, not a no-op."""
    with pytest.raises(ValueError, match="no fragments to assemble"):
        assemble_changelog("", [], "v1.0.0")


def test_assemble_changelog_date_default_and_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The heading date defaults to "today" (UTC) but an explicit `date=` wins."""
    monkeypatch.setattr(changelog_fragments, "datetime", _FixedDatetime)
    default_result = assemble_changelog("", [_make_fragment()], "v1.0.0")
    assert "## v1.0.0 — 2026-01-02" in default_result

    override_result = assemble_changelog(
        "", [_make_fragment()], "v1.0.0", date="2020-12-31"
    )
    assert "## v1.0.0 — 2020-12-31" in override_result


# ---------------------------------------------------------------------------
# check_pending
# ---------------------------------------------------------------------------


def test_check_pending_empty_when_no_fragments(tmp_path: Path) -> None:
    """No `changelog.d/` directory → no errors."""
    assert check_pending(tmp_path) == []


def test_check_pending_aggregates_errors_across_fragments(tmp_path: Path) -> None:
    """One valid plus two differently-invalid fragments — errors from both."""
    directory = tmp_path / "changelog.d"
    _write_fragment(directory, "ok.added.md", "bump: minor\n- fine\n")
    _write_fragment(directory, "bad-type.bogus.md", "bump: minor\n- x\n")
    _write_fragment(directory, "bad-level.added.md", "bump: superduper\n- x\n")
    errors = check_pending(tmp_path)
    assert any("unknown type 'bogus'" in e for e in errors)
    assert any("unknown level 'superduper'" in e for e in errors)
    assert len(errors) == 2


# ---------------------------------------------------------------------------
# main() — CLI
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("has_fragment", "expected_snippet"),
    [
        (False, "no pending"),
        (True, "all valid"),
    ],
)
def test_main_check_exit_zero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *,
    has_fragment: bool,
    expected_snippet: str,
) -> None:
    """`check` exits 0 whether nothing is pending or everything pending is valid.

    Args:
        has_fragment: Whether a valid fragment is written before checking.
        expected_snippet: Substring expected in the reported message.
    """
    monkeypatch.setattr(changelog_fragments, "repo_root", lambda: tmp_path)
    if has_fragment:
        _write_fragment(tmp_path / "changelog.d", "a.added.md", "bump: minor\n- x\n")
    assert main(["check"]) == 0
    assert expected_snippet in capsys.readouterr().out


def test_main_check_exit_two_on_invalid_fragment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`check` exits 2 and reports `INVALID` when a pending fragment fails."""
    monkeypatch.setattr(changelog_fragments, "repo_root", lambda: tmp_path)
    _write_fragment(tmp_path / "changelog.d", "bad.bogus.md", "bump: minor\n- x\n")
    assert main(["check"]) == 2
    assert "INVALID" in capsys.readouterr().out


def test_main_assemble_without_delete_writes_changelog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`assemble` without `--delete` writes CHANGELOG.md — no git repo required."""
    monkeypatch.setattr(changelog_fragments, "repo_root", lambda: tmp_path)
    _write_fragment(
        tmp_path / "changelog.d", "a.added.md", "bump: minor\n- new thing\n"
    )
    assert main(["assemble", "--version", "v1.0.0"]) == 0
    changelog_text = (tmp_path / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## v1.0.0" in changelog_text
    assert "- new thing" in changelog_text


def test_main_assemble_with_delete_stages_changelog_and_fragment_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`assemble --delete` on a real repo stages CHANGELOG + removes the fragment."""
    init_git_repo(tmp_path)
    (tmp_path / "CHANGELOG.md").write_text("# Changelog\n")
    _write_fragment(
        tmp_path / "changelog.d", "a.added.md", "bump: minor\n- new thing\n"
    )
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, env=GIT_ENV, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "seed"], cwd=tmp_path, env=GIT_ENV, check=True
    )
    monkeypatch.setattr(changelog_fragments, "repo_root", lambda: tmp_path)

    assert main(["assemble", "--version", "v1.0.0", "--delete"]) == 0

    status = subprocess.run(
        ["git", "diff", "--cached", "--name-status"],
        cwd=tmp_path,
        env=GIT_ENV,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "M\tCHANGELOG.md" in status
    assert "D\tchangelog.d/a.added.md" in status


def test_main_assemble_invalid_fragment_exits_two_and_leaves_changelog_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An invalid pending fragment blocks assembly and never touches CHANGELOG.md."""
    monkeypatch.setattr(changelog_fragments, "repo_root", lambda: tmp_path)
    original = "# Changelog\n\n## v0.9.0\n"
    (tmp_path / "CHANGELOG.md").write_text(original)
    _write_fragment(tmp_path / "changelog.d", "bad.bogus.md", "bump: minor\n- x\n")
    assert main(["assemble", "--version", "v1.0.0"]) == 2
    assert (tmp_path / "CHANGELOG.md").read_text(encoding="utf-8") == original


def test_main_assemble_nothing_pending_exits_two(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No pending fragments at all → exit 2 with a "nothing to assemble" message."""
    monkeypatch.setattr(changelog_fragments, "repo_root", lambda: tmp_path)
    assert main(["assemble", "--version", "v1.0.0"]) == 2
    assert "nothing to assemble" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# main() — restrand
# ---------------------------------------------------------------------------


def test_main_restrand_skips_in_fragments_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Fragments mode self-skips — nothing can strand under a shared heading."""
    (tmp_path / "pyproject.toml").write_text(
        '[tool.forge.changelog]\nmode = "fragments"\n'
    )
    monkeypatch.setattr(changelog_fragments, "repo_root", lambda: tmp_path)
    assert main(["restrand"]) == 0
    assert "fragments mode" in capsys.readouterr().out


def test_main_restrand_skips_without_changelog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No CHANGELOG.md at all → exit 0, nothing to do."""
    monkeypatch.setattr(changelog_fragments, "repo_root", lambda: tmp_path)
    assert main(["restrand"]) == 0
    assert "no CHANGELOG.md" in capsys.readouterr().out


def test_main_restrand_skips_without_tag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No v* tag yet → nothing can be stranded, exit 0."""
    init_git_repo(tmp_path)
    (tmp_path / "CHANGELOG.md").write_text("# Changelog\n\n## v0.1.0\n\n- x\n")
    commit_all(tmp_path, "seed changelog")
    monkeypatch.setattr(changelog_fragments, "repo_root", lambda: tmp_path)
    assert main(["restrand"]) == 0
    assert "no v* tag" in capsys.readouterr().out


def test_main_restrand_exit_two_when_no_comparison_point(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A tag cut before CHANGELOG.md existed, plus an unresolvable base → exit 2.

    SCENARIO: ``v1.0.0`` is tagged at the initial commit, before
    CHANGELOG.md is ever added; ``base_branch`` is configured to a branch
    that does not exist (no remote either), so
    ``merge_base_with_head`` resolves nothing. Neither reference
    ``_restrand_old_text`` tries (base, then the tagged copy) yields a
    CHANGELOG.md — the tagged commit predates the file's existence.
    EXPECTED BEHAVIOR: exit 2, "no comparison point" reported.
    """
    init_git_repo(tmp_path)
    subprocess.run(["git", "tag", "v1.0.0"], cwd=tmp_path, env=GIT_ENV, check=True)
    (tmp_path / "pyproject.toml").write_text(
        '[tool.forge]\nbase_branch = "nonexistent-branch"\n'
    )
    (tmp_path / "CHANGELOG.md").write_text("# Changelog\n\n## v1.0.0\n\n- x\n")
    commit_all(tmp_path, "add changelog after the tag")
    monkeypatch.setattr(changelog_fragments, "repo_root", lambda: tmp_path)
    assert main(["restrand"]) == 2
    assert "no comparison point" in capsys.readouterr().out


def test_main_restrand_end_to_end_moves_stranded_bullet_and_stages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A stranded bullet on a feature branch moves to a fresh slot and stages.

    SCENARIO: CHANGELOG.md carries ``## v1.0.0`` with one bullet at the
    shared merge-base commit. A ``feature`` branch diverges and adds a
    second bullet under that heading; ``v1.0.0`` is then tagged on a
    LATER ``main`` commit whose CHANGELOG.md gained a released-only
    bullet — so the merge-base copy and the tagged copy differ, and only
    the merge-base copy classifies the released-only bullet as
    pre-existing. The stranded-entries race, with a discriminating
    fixture: preferring the tag over the merge base would make the
    branch look like it DELETED the released-only bullet, tripping the
    released-deleted verifier and exiting 2 instead of 0.
    EXPECTED BEHAVIOR: ``restrand`` (default ``--bump patch``) exits 0,
    opens ``## v1.0.1`` above the released section with the second
    bullet — and only it — moved under it, and stages CHANGELOG.md
    (``git diff --cached`` reports a modification, never a commit).
    """
    init_git_repo(tmp_path)
    (tmp_path / "CHANGELOG.md").write_text("# Changelog\n\n## v1.0.0\n\n- one bullet\n")
    commit_all(tmp_path, "seed changelog")
    subprocess.run(
        ["git", "checkout", "-q", "-b", "feature"],
        cwd=tmp_path,
        env=GIT_ENV,
        check=True,
    )
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n## v1.0.0\n\n- one bullet\n- second bullet\n"
    )
    commit_all(tmp_path, "add second bullet")
    subprocess.run(
        ["git", "checkout", "-q", "main"], cwd=tmp_path, env=GIT_ENV, check=True
    )
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n## v1.0.0\n\n- one bullet\n- released-only bullet\n"
    )
    commit_all(tmp_path, "release adds its own bullet")
    subprocess.run(["git", "tag", "v1.0.0"], cwd=tmp_path, env=GIT_ENV, check=True)
    subprocess.run(
        ["git", "checkout", "-q", "feature"], cwd=tmp_path, env=GIT_ENV, check=True
    )
    monkeypatch.setattr(changelog_fragments, "repo_root", lambda: tmp_path)

    assert main(["restrand"]) == 0

    changelog_text = (tmp_path / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## v1.0.1" in changelog_text
    assert changelog_text.index("- second bullet") < changelog_text.index("## v1.0.0")
    assert "- released-only bullet" not in changelog_text.split("## v1.0.0")[0]
    status = subprocess.run(
        ["git", "diff", "--cached", "--name-status"],
        cwd=tmp_path,
        env=GIT_ENV,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "M\tCHANGELOG.md" in status


def test_main_restrand_falls_back_to_tagged_changelog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """With no resolvable base branch, the tagged CHANGELOG.md is the old text.

    SCENARIO: ``base_branch`` points at a branch that does not exist, so
    the merge-base reference fails — but ``v1.0.0`` is tagged at a
    commit whose CHANGELOG.md exists, so ``_restrand_old_text``'s tag
    fallback supplies the comparison point. A later commit strands one
    bullet under the released heading.
    EXPECTED BEHAVIOR: exit 0; ``## v1.0.1`` opens with the stranded
    bullet moved under it.
    """
    init_git_repo(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        '[tool.forge]\nbase_branch = "nonexistent-branch"\n'
    )
    (tmp_path / "CHANGELOG.md").write_text("# Changelog\n\n## v1.0.0\n\n- one bullet\n")
    commit_all(tmp_path, "seed changelog")
    subprocess.run(["git", "tag", "v1.0.0"], cwd=tmp_path, env=GIT_ENV, check=True)
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n## v1.0.0\n\n- one bullet\n- stranded bullet\n"
    )
    commit_all(tmp_path, "strand a bullet")
    monkeypatch.setattr(changelog_fragments, "repo_root", lambda: tmp_path)

    assert main(["restrand"]) == 0
    assert "Restranded" in capsys.readouterr().out
    changelog_text = (tmp_path / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## v1.0.1" in changelog_text
    assert changelog_text.index("- stranded bullet") < changelog_text.index("## v1.0.0")


def test_main_restrand_bump_minor_opens_minor_slot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--bump minor` opens `## v1.1.0`, not the default patch `## v1.0.1`."""
    init_git_repo(tmp_path)
    (tmp_path / "CHANGELOG.md").write_text("# Changelog\n\n## v1.0.0\n\n- one bullet\n")
    commit_all(tmp_path, "seed changelog")
    subprocess.run(["git", "tag", "v1.0.0"], cwd=tmp_path, env=GIT_ENV, check=True)
    subprocess.run(
        ["git", "checkout", "-q", "-b", "feature"],
        cwd=tmp_path,
        env=GIT_ENV,
        check=True,
    )
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n## v1.0.0\n\n- one bullet\n- second bullet\n"
    )
    commit_all(tmp_path, "add second bullet")
    monkeypatch.setattr(changelog_fragments, "repo_root", lambda: tmp_path)

    assert main(["restrand", "--bump", "minor"]) == 0

    changelog_text = (tmp_path / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## v1.1.0" in changelog_text
    assert "## v1.0.1" not in changelog_text
