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

from forge import changelog_fragments, git_utils
from forge.changelog_fragments import (
    Fragment,
    assemble_changelog,
    branch_added_fragments,
    check_pending,
    discover_fragments,
    main,
    max_level,
    next_version_from_fragments,
    validate_fragment,
)
from tests.conftest import GIT_ENV, FakeProc, commit_all, init_git_repo


# Captured at import time, before any test monkeypatches `subprocess.run` —
# `changelog_fragments` and `git_utils` both do a bare `import subprocess`,
# so they share this exact module object; a fake installed on it would
# otherwise recurse into itself if it ever needs to fall back to the real
# implementation.
_REAL_SUBPROCESS_RUN = subprocess.run


def _fake_gh_auth_ok(
    cmd: list[str], *args: object, **kwargs: object
) -> subprocess.CompletedProcess[str] | FakeProc:
    """Fake a successful `gh auth status`; forward every other argv untouched.

    Shared by the `release-pr` guard tests: `latest_v_tag` and
    `_stage_release` shell out to real `git` on the sandbox repo, so only
    the `gh` dependency may be faked — a blanket fake would blind
    `latest_v_tag`'s own ``subprocess.run`` call too, since it lives on
    the same shared module object.

    Args:
        cmd: The argv passed to ``subprocess.run``.
        *args: Forwarded positional arguments.
        **kwargs: Forwarded keyword arguments.

    Returns:
        A canned success for `gh auth status`; the real
        ``subprocess.run`` result for anything else.
    """
    if cmd[:2] == ["gh", "auth"]:
        return FakeProc(0)
    return _REAL_SUBPROCESS_RUN(cmd, *args, **kwargs)


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
# next_version_from_fragments
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("levels", "expected"),
    [
        (["patch", "minor"], ("1.3.0", "minor")),
        (["patch", "minor", "major"], ("2.0.0", "major")),
        (["patch"], ("1.2.4", "patch")),
    ],
)
def test_next_version_from_fragments_uses_max_level(
    tmp_path: Path, levels: list[str], expected: tuple[str, str]
) -> None:
    """The strongest pending level drives the bump above the latest tag.

    Args:
        levels: Bump levels declared across the pending fragments.
        expected: Expected ``(bare_version, level)`` above ``v1.2.3``.
    """
    directory = tmp_path / "changelog.d"
    for i, level in enumerate(levels):
        _write_fragment(directory, f"f{i}.added.md", f"bump: {level}\n- x\n")
    assert next_version_from_fragments(tmp_path, "v1.2.3") == expected


def test_next_version_from_fragments_none_when_nothing_pending(
    tmp_path: Path,
) -> None:
    """No pending fragments → None, never a zero-fragment version."""
    assert next_version_from_fragments(tmp_path, "v1.2.3") is None


def test_next_version_from_fragments_raises_listing_every_error(
    tmp_path: Path,
) -> None:
    """Any invalid pending fragment raises, with every error in the message."""
    directory = tmp_path / "changelog.d"
    _write_fragment(directory, "ok.added.md", "bump: minor\n- fine\n")
    _write_fragment(directory, "bad-type.bogus.md", "bump: minor\n- x\n")
    _write_fragment(directory, "bad-level.added.md", "bump: superduper\n- x\n")
    with pytest.raises(ValueError, match="unknown type 'bogus'") as excinfo:
        next_version_from_fragments(tmp_path, "v1.2.3")
    assert "unknown level 'superduper'" in str(excinfo.value)


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
# main() — next-version
# ---------------------------------------------------------------------------


def _init_tagged_repo(repo: Path, tag: str = "v1.0.0") -> None:
    """Init a git repo with one seed commit tagged *tag*.

    Args:
        repo: Directory to initialize.
        tag: Release tag to cut at the seed commit.
    """
    init_git_repo(repo)
    (repo / "seed.txt").write_text("seed\n")
    commit_all(repo, "seed")
    subprocess.run(["git", "tag", tag], cwd=repo, env=GIT_ENV, check=True)


def test_main_next_version_prints_computed_version_and_level(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`next-version` prints `vX.Y.Z (level)` from latest tag + max pending level."""
    _init_tagged_repo(tmp_path)
    _write_fragment(tmp_path / "changelog.d", "a.added.md", "bump: minor\n- x\n")
    _write_fragment(tmp_path / "changelog.d", "b.fixed.md", "bump: patch\n- y\n")
    monkeypatch.setattr(changelog_fragments, "repo_root", lambda: tmp_path)
    assert main(["next-version"]) == 0
    assert capsys.readouterr().out.strip() == "v1.1.0 (minor)"


def test_main_next_version_exit_two_without_tag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No v* tag → exit 2, message names the missing baseline."""
    init_git_repo(tmp_path)
    _write_fragment(tmp_path / "changelog.d", "a.added.md", "bump: minor\n- x\n")
    monkeypatch.setattr(changelog_fragments, "repo_root", lambda: tmp_path)
    assert main(["next-version"]) == 2
    assert "no v* tag" in capsys.readouterr().out


def test_main_next_version_exit_two_without_fragments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No pending fragments → exit 2, message says nothing is pending."""
    _init_tagged_repo(tmp_path)
    monkeypatch.setattr(changelog_fragments, "repo_root", lambda: tmp_path)
    assert main(["next-version"]) == 2
    assert "no pending fragments" in capsys.readouterr().out


def test_main_next_version_exit_two_on_invalid_fragment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An invalid pending fragment → exit 2, its error reported."""
    _init_tagged_repo(tmp_path)
    _write_fragment(tmp_path / "changelog.d", "bad.bogus.md", "bump: minor\n- x\n")
    monkeypatch.setattr(changelog_fragments, "repo_root", lambda: tmp_path)
    assert main(["next-version"]) == 2
    assert "unknown type 'bogus'" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# main() — release
# ---------------------------------------------------------------------------


def test_main_release_with_manifest_stages_everything_commits_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`release` assembles, rewrites the manifest, stages all — never commits.

    SCENARIO: a plugin repo at tag ``v1.0.0`` (manifest parked at
    ``1.0.0``) with a minor and a patch fragment pending.
    EXPECTED BEHAVIOR: CHANGELOG.md gains ``## v1.1.0`` with both
    entries; both fragments are deleted and their deletions staged; the
    manifest reads ``1.1.0`` and is staged; the version is printed
    prominently; ``HEAD`` still points at the seed commit (no commit
    made).
    """
    init_git_repo(tmp_path)
    (tmp_path / ".claude-plugin").mkdir()
    (tmp_path / ".claude-plugin" / "plugin.json").write_text(
        '{\n  "name": "x",\n  "version": "1.0.0"\n}\n'
    )
    (tmp_path / "CHANGELOG.md").write_text("# Changelog\n")
    _write_fragment(tmp_path / "changelog.d", "a.added.md", "bump: minor\n- new\n")
    _write_fragment(tmp_path / "changelog.d", "b.fixed.md", "bump: patch\n- fix\n")
    commit_all(tmp_path, "seed")
    subprocess.run(["git", "tag", "v1.0.0"], cwd=tmp_path, env=GIT_ENV, check=True)
    head_before = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=tmp_path,
        env=GIT_ENV,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    monkeypatch.setattr(changelog_fragments, "repo_root", lambda: tmp_path)

    assert main(["release"]) == 0

    changelog_text = (tmp_path / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## v1.1.0" in changelog_text
    assert "- new" in changelog_text
    assert "- fix" in changelog_text
    manifest_text = (tmp_path / ".claude-plugin" / "plugin.json").read_text(
        encoding="utf-8"
    )
    assert manifest_text == '{\n  "name": "x",\n  "version": "1.1.0"\n}\n'
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
    assert "D\tchangelog.d/b.fixed.md" in status
    assert "M\t.claude-plugin/plugin.json" in status
    head_after = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=tmp_path,
        env=GIT_ENV,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert head_after == head_before
    assert "Release v1.1.0 prepared" in capsys.readouterr().out


def test_main_release_without_manifest_prints_version_for_tag_flow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A manifest-less (tag-versioned) repo assembles and prints the version."""
    init_git_repo(tmp_path)
    (tmp_path / "CHANGELOG.md").write_text("# Changelog\n")
    _write_fragment(tmp_path / "changelog.d", "a.added.md", "bump: patch\n- x\n")
    commit_all(tmp_path, "seed")
    subprocess.run(["git", "tag", "v1.0.0"], cwd=tmp_path, env=GIT_ENV, check=True)
    monkeypatch.setattr(changelog_fragments, "repo_root", lambda: tmp_path)

    assert main(["release"]) == 0

    assert not (tmp_path / ".claude-plugin").exists()
    assert "## v1.0.1" in (tmp_path / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "Release v1.0.1 prepared" in capsys.readouterr().out


def test_main_release_manifest_refusal_leaves_tree_untouched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A manifest render refusal fires before ANY write — no half-release.

    Compute-then-write pin (mirrors rebump's invariant): a manifest with
    no ``"version"`` field refuses at render time, so the changelog is
    not assembled, no fragment is deleted, and nothing is staged.
    """
    _init_tagged_repo(tmp_path)
    original = "# Changelog\n"
    (tmp_path / "CHANGELOG.md").write_text(original)
    _write_fragment(tmp_path / "changelog.d", "ok.added.md", "bump: minor\n- x\n")
    plugin_dir = tmp_path / ".claude-plugin"
    plugin_dir.mkdir(exist_ok=True)
    (plugin_dir / "plugin.json").write_text('{"name": "forge"}\n')
    monkeypatch.setattr(changelog_fragments, "repo_root", lambda: tmp_path)

    assert main(["release"]) == 2

    assert 'no "version" field' in capsys.readouterr().out
    assert (tmp_path / "CHANGELOG.md").read_text(encoding="utf-8") == original
    assert (tmp_path / "changelog.d" / "ok.added.md").exists()


def test_main_release_exit_two_on_invalid_fragment_touches_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An invalid pending fragment blocks the release before any write."""
    _init_tagged_repo(tmp_path)
    original = "# Changelog\n"
    (tmp_path / "CHANGELOG.md").write_text(original)
    _write_fragment(tmp_path / "changelog.d", "bad.bogus.md", "bump: minor\n- x\n")
    monkeypatch.setattr(changelog_fragments, "repo_root", lambda: tmp_path)
    assert main(["release"]) == 2
    assert "unknown type 'bogus'" in capsys.readouterr().out
    assert (tmp_path / "CHANGELOG.md").read_text(encoding="utf-8") == original


def test_main_release_exit_two_without_fragments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Nothing pending → exit 2; a release needs at least one fragment."""
    _init_tagged_repo(tmp_path)
    monkeypatch.setattr(changelog_fragments, "repo_root", lambda: tmp_path)
    assert main(["release"]) == 2
    assert "no pending fragments" in capsys.readouterr().out


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


# ---------------------------------------------------------------------------
# main() — auto-tag (tag-per-merge)
# ---------------------------------------------------------------------------


def _init_autotag_repo(repo: Path, origin: Path, *, auto: str | None = "merge") -> None:
    """Init a fragments-mode repo with a bare origin, seeded and pushed.

    Args:
        repo: Working repo directory.
        origin: Bare repository path to use as ``origin``.
        auto: ``[tool.forge.release].auto`` value; ``None`` omits the table.
    """
    subprocess.run(
        ["git", "init", "-q", "--bare", str(origin)], env=GIT_ENV, check=True
    )
    init_git_repo(repo)
    subprocess.run(
        ["git", "remote", "add", "origin", str(origin)],
        cwd=repo,
        env=GIT_ENV,
        check=True,
    )
    release = f'\n[tool.forge.release]\nauto = "{auto}"\n' if auto else "\n"
    (repo / "pyproject.toml").write_text(
        '[tool.forge]\nbase_branch = "main"\n\n'
        '[tool.forge.changelog]\nmode = "fragments"\n' + release
    )
    (repo / "changelog.d").mkdir()
    (repo / "changelog.d" / "first.added.md").write_text("bump: minor\n- first\n")
    commit_all(repo, "seed")
    subprocess.run(
        ["git", "push", "-q", "origin", "main"], cwd=repo, env=GIT_ENV, check=True
    )


def _remote_tags(origin: Path) -> list[str]:
    """Return tag names present on the bare *origin*.

    Args:
        origin: Bare repository path.

    Returns:
        Tag names (dereference suffixes dropped), sorted.
    """
    out = subprocess.run(
        ["git", "ls-remote", "--tags", str(origin)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return sorted(
        line.split("refs/tags/")[-1] for line in out.splitlines() if "^{}" not in line
    )


def test_main_auto_tag_cuts_and_pushes_tag_from_merged_fragment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Merge with a new fragment → tag computed, cut, and pushed to origin.

    Spec scenario 1 (verify-by-execution): the tag actually appears on the
    remote; bootstrap case (no prior tag → next_version from v0.0.0).
    """
    origin = tmp_path / "origin.git"
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_autotag_repo(repo, origin)
    monkeypatch.setattr(changelog_fragments, "repo_root", lambda: repo)

    assert main(["auto-tag"]) == 0

    assert "cut v0.1.0 (minor)" in capsys.readouterr().out
    assert _remote_tags(origin) == ["v0.1.0"]


def test_main_auto_tag_no_new_fragments_is_a_visible_noop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No fragments newer than the last tag → exit 0 with a stated reason.

    Spec scenario 2: never a silent no-op — and the fragment already
    counted by the previous tag (tag-tree membership) must not re-bump.
    """
    origin = tmp_path / "origin.git"
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_autotag_repo(repo, origin)
    monkeypatch.setattr(changelog_fragments, "repo_root", lambda: repo)
    assert main(["auto-tag"]) == 0
    capsys.readouterr()

    assert main(["auto-tag"]) == 0

    assert "no new fragments since v0.1.0" in capsys.readouterr().out
    assert _remote_tags(origin) == ["v0.1.0"]


def test_main_auto_tag_takes_strongest_new_level(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A major fragment merged after a minor tag drives a major bump.

    Spec scenario 3's level-fidelity half: the bump comes from the
    fragments present at the tagged commit, never a stale computation.
    """
    origin = tmp_path / "origin.git"
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_autotag_repo(repo, origin)
    monkeypatch.setattr(changelog_fragments, "repo_root", lambda: repo)
    assert main(["auto-tag"]) == 0
    (repo / "changelog.d" / "big.added.md").write_text("bump: major\n- breaking\n")
    commit_all(repo, "feat: breaking")
    capsys.readouterr()

    assert main(["auto-tag"]) == 0

    assert "cut v1.0.0 (major)" in capsys.readouterr().out
    assert _remote_tags(origin) == ["v0.1.0", "v1.0.0"]


def test_main_auto_tag_invalid_new_fragment_blocks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An invalid newly merged fragment blocks the tag with each error listed."""
    origin = tmp_path / "origin.git"
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_autotag_repo(repo, origin)
    (repo / "changelog.d" / "bad.bogus.md").write_text("bump: minor\n- x\n")
    commit_all(repo, "bad fragment")
    monkeypatch.setattr(changelog_fragments, "repo_root", lambda: repo)

    assert main(["auto-tag"]) == 2

    assert "unknown type 'bogus'" in capsys.readouterr().out
    assert _remote_tags(origin) == []


def test_main_auto_tag_existing_tag_defers_to_winner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The computed tag already existing → another-runner-won no-op, exit 0.

    Spec scenario: idempotent and race-tolerant — a re-triggered workflow
    or concurrent runner never double-tags.
    """
    origin = tmp_path / "origin.git"
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_autotag_repo(repo, origin)
    subprocess.run(["git", "tag", "v0.1.0"], cwd=repo, env=GIT_ENV, check=True)
    monkeypatch.setattr(changelog_fragments, "repo_root", lambda: repo)

    assert main(["auto-tag"]) == 0

    out = capsys.readouterr().out
    assert "no new fragments since v0.1.0" in out or "another runner won" in out


def test_main_auto_tag_warn_floor_when_not_opted_in(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    r"""Fragments pending without `auto = \"merge\"` → exit 3 loud warning.

    Spec requirement: no configuration may leave merges accumulating
    fragments silently — the warn floor is the minimum.
    """
    origin = tmp_path / "origin.git"
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_autotag_repo(repo, origin, auto=None)
    monkeypatch.setattr(changelog_fragments, "repo_root", lambda: repo)

    assert main(["auto-tag"]) == 3

    out = capsys.readouterr().out
    assert "no tag will be cut" in out
    assert _remote_tags(origin) == []


def test_main_auto_tag_not_fragments_mode_noop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Shared-heading repos: auto-tag states it does not apply, exit 0."""
    init_git_repo(tmp_path)
    (tmp_path / "pyproject.toml").write_text("[tool.forge]\n")
    commit_all(tmp_path, "seed")
    monkeypatch.setattr(changelog_fragments, "repo_root", lambda: tmp_path)

    assert main(["auto-tag"]) == 0
    assert "not a fragments-mode repo" in capsys.readouterr().out


def test_main_auto_tag_push_race_defers_to_remote_winner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Push failure with a genuinely competing remote tag defers to the winner.

    Two runners race to cut ``v0.1.0`` from the same seed fragment. A
    ``winner`` clone commits a divergent change, tags it ``v0.1.0``, and
    pushes the tag to origin first; the main ``repo`` then loses its own
    push. The losing runner must exit 0 with "appeared remotely", and the
    remote must still carry only the winner's tag at the winner's commit —
    the losing runner's local tag never reaches origin.
    """
    origin = tmp_path / "origin.git"
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_autotag_repo(repo, origin)

    winner = tmp_path / "winner"
    subprocess.run(
        ["git", "clone", "-q", str(origin), str(winner)], env=GIT_ENV, check=True
    )
    (winner / "winner.txt").write_text("winner change\n")
    commit_all(winner, "winner: divergent change")
    subprocess.run(
        ["git", "tag", "-a", "v0.1.0", "-m", "winner tag"],
        cwd=winner,
        env=GIT_ENV,
        check=True,
    )
    subprocess.run(
        ["git", "push", "-q", "origin", "v0.1.0"], cwd=winner, env=GIT_ENV, check=True
    )
    winner_commit = subprocess.run(
        ["git", "rev-parse", "v0.1.0^{commit}"],
        cwd=winner,
        env=GIT_ENV,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    monkeypatch.setattr(changelog_fragments, "repo_root", lambda: repo)

    assert main(["auto-tag"]) == 0

    assert "appeared remotely" in capsys.readouterr().out
    assert _remote_tags(origin) == ["v0.1.0"]
    remote_commit = subprocess.run(
        ["git", "ls-remote", str(origin), "v0.1.0^{}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()[0]
    assert remote_commit == winner_commit


def test_main_auto_tag_push_failure_without_winner_reports_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A push failure with no reachable remote reports failure, not a false winner.

    Origin is repointed at a nonexistent path after seeding, so both the
    tag push and the remote-winner check fail to reach anything — there is
    no competing tag to explain the failure. The push failure must surface
    as a real failure (exit 2, "no concurrent winner") instead of being
    misread as a lost race.
    """
    origin = tmp_path / "origin.git"
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_autotag_repo(repo, origin)
    subprocess.run(
        ["git", "remote", "set-url", "origin", "/nonexistent/xyz.git"],
        cwd=repo,
        env=GIT_ENV,
        check=True,
    )
    monkeypatch.setattr(changelog_fragments, "repo_root", lambda: repo)

    assert main(["auto-tag"]) == 2

    assert "no concurrent winner" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# _assembly_pr_body
# ---------------------------------------------------------------------------


def test_assembly_pr_body_manifest_less_names_post_merge_tagging(
    tmp_path: Path,
) -> None:
    """A repo without a plugin manifest gets the post-merge tagging sentence.

    Regression for the PR #456 review finding: the body must not claim
    the tag-release workflow tags the merge (`forge-next-prep --tag`) when
    there is no manifest for `plugin_version` to race ahead of.
    """
    body = changelog_fragments._assembly_pr_body(tmp_path, "v1.1.0")

    assert "forge-release --from-changelog" in body
    assert "forge-next-prep --tag" not in body


def test_assembly_pr_body_with_manifest_says_workflow_tags_merge(
    tmp_path: Path,
) -> None:
    """A repo with a plugin manifest gets the auto-tag-on-merge sentence."""
    plugin_dir = tmp_path / ".claude-plugin"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.json").write_text('{"name": "forge"}\n')

    body = changelog_fragments._assembly_pr_body(tmp_path, "v1.1.0")

    assert "forge-next-prep --tag" in body
    assert "forge-release --from-changelog" not in body


# ---------------------------------------------------------------------------
# main() — release-pr (scheduled assembly PR)
# ---------------------------------------------------------------------------


def test_main_release_pr_not_fragments_mode_noop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A shared-heading repo self-skips before any `gh` dependency is touched.

    The self-skip must precede `require_cli` — the guard cannot assume
    `gh` is even installed on a repo that isn't in fragments mode.
    """
    monkeypatch.setattr(changelog_fragments, "repo_root", lambda: tmp_path)
    require_calls: list[str] = []
    monkeypatch.setattr(
        changelog_fragments,
        "require_cli",
        lambda name, **_kw: require_calls.append(name),
    )

    assert main(["release-pr"]) == 0

    assert "not a fragments-mode repo" in capsys.readouterr().out
    assert require_calls == []


def test_main_release_pr_gh_unauthenticated_exits_two(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An unauthenticated `gh` aborts before the tag/fragment checks run.

    Pins the pre-flight ordering (`require_cli` -> auth -> version
    computation): `latest_v_tag` must never be reached once `gh auth
    status` fails.
    """
    (tmp_path / "pyproject.toml").write_text(
        '[tool.forge.changelog]\nmode = "fragments"\n'
    )
    monkeypatch.setattr(changelog_fragments, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(changelog_fragments, "require_cli", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        changelog_fragments.subprocess,
        "run",
        lambda *_a, **_kw: FakeProc(1, stderr="not logged in"),
    )
    tag_calls: list[str] = []
    monkeypatch.setattr(
        changelog_fragments,
        "latest_v_tag",
        lambda *_a, **_kw: tag_calls.append("x") or "v1.0.0",
    )

    assert main(["release-pr"]) == 2

    out = capsys.readouterr().out
    assert "gh is not authenticated" in out
    assert "not logged in" in out
    assert tag_calls == []


def test_main_release_pr_exit_two_without_tag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No `v*` tag at all → exit 2, no baseline to bump from."""
    init_git_repo(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        '[tool.forge.changelog]\nmode = "fragments"\n'
    )
    monkeypatch.setattr(changelog_fragments, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(changelog_fragments, "require_cli", lambda *_a, **_kw: None)
    monkeypatch.setattr(changelog_fragments.subprocess, "run", _fake_gh_auth_ok)

    assert main(["release-pr"]) == 2
    assert "no v* tag" in capsys.readouterr().out


def test_main_release_pr_exit_two_on_invalid_fragment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An invalid pending fragment blocks before any branch/push is attempted."""
    _init_tagged_repo(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        '[tool.forge.changelog]\nmode = "fragments"\n'
    )
    _write_fragment(tmp_path / "changelog.d", "bad.bogus.md", "bump: minor\n- x\n")
    monkeypatch.setattr(changelog_fragments, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(changelog_fragments, "require_cli", lambda *_a, **_kw: None)
    monkeypatch.setattr(changelog_fragments.subprocess, "run", _fake_gh_auth_ok)
    publish_calls: list[str] = []
    monkeypatch.setattr(
        changelog_fragments,
        "_publish_assembly_pr",
        lambda *_a, **_kw: publish_calls.append("x") or 2,
    )

    assert main(["release-pr"]) == 2

    assert "unknown type 'bogus'" in capsys.readouterr().out
    assert publish_calls == []


def test_main_release_pr_nothing_pending_is_quiet_noop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Zero pending fragments → quiet exit 0, before the dedup check or publish.

    "Quiet" means the dedup lookup and the publish step are never
    reached at all, not just that nothing visible happens — the claim
    cited by docs/release-process.md.
    """
    _init_tagged_repo(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        '[tool.forge.changelog]\nmode = "fragments"\n'
    )
    monkeypatch.setattr(changelog_fragments, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(changelog_fragments, "require_cli", lambda *_a, **_kw: None)
    monkeypatch.setattr(changelog_fragments.subprocess, "run", _fake_gh_auth_ok)
    dedup_calls: list[str] = []
    monkeypatch.setattr(
        changelog_fragments,
        "find_open_pr_by_head_prefix",
        lambda *_a, **_kw: dedup_calls.append("x") or None,
    )
    publish_calls: list[str] = []
    monkeypatch.setattr(
        changelog_fragments,
        "_publish_assembly_pr",
        lambda *_a, **_kw: publish_calls.append("x") or 0,
    )

    assert main(["release-pr"]) == 0

    assert "no pending fragments" in capsys.readouterr().out
    assert dedup_calls == []
    assert publish_calls == []


def test_main_release_pr_defers_to_open_assembly_pr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An already-open assembly PR defers with exit 0, before any publish step.

    Proves the defer happens before any branch/push work — the
    idempotent-and-race-tolerant contract docs/release-process.md cites.
    """
    _init_tagged_repo(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        '[tool.forge.changelog]\nmode = "fragments"\n'
    )
    _write_fragment(tmp_path / "changelog.d", "a.added.md", "bump: minor\n- x\n")
    monkeypatch.setattr(changelog_fragments, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(changelog_fragments, "require_cli", lambda *_a, **_kw: None)
    monkeypatch.setattr(changelog_fragments.subprocess, "run", _fake_gh_auth_ok)
    url = "https://github.com/x/y/pull/7"
    monkeypatch.setattr(
        changelog_fragments, "find_open_pr_by_head_prefix", lambda *_a, **_kw: url
    )
    publish_calls: list[str] = []
    monkeypatch.setattr(
        changelog_fragments,
        "_publish_assembly_pr",
        lambda *_a, **_kw: publish_calls.append("x") or 0,
    )

    assert main(["release-pr"]) == 0

    out = capsys.readouterr().out.strip()
    assert out == f"release-pr: assembly PR already open — {url}"
    assert publish_calls == []


def test_main_release_pr_happy_path_opens_pr_with_assembled_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A pending fragment drives a real branch, commit, push, and PR open.

    The end-to-end wiring check: proves `release-pr` reaches a real
    `_stage_release` + `create_commit` against a bare origin, without
    re-deriving assembly correctness already pinned by the `release`
    tests above (manifest handling, group ordering, …).
    """
    origin = tmp_path / "origin.git"
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-q", "--bare", str(origin)], env=GIT_ENV, check=True
    )
    init_git_repo(repo)
    subprocess.run(
        ["git", "remote", "add", "origin", str(origin)],
        cwd=repo,
        env=GIT_ENV,
        check=True,
    )
    (repo / "pyproject.toml").write_text('[tool.forge.changelog]\nmode = "fragments"\n')
    (repo / "CHANGELOG.md").write_text("# Changelog\n")
    _write_fragment(
        repo / "changelog.d", "note.added.md", "bump: minor\n- new feature\n"
    )
    commit_all(repo, "seed")
    subprocess.run(["git", "tag", "v1.0.0"], cwd=repo, env=GIT_ENV, check=True)
    monkeypatch.setattr(changelog_fragments, "repo_root", lambda: repo)
    monkeypatch.setattr(changelog_fragments, "require_cli", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        changelog_fragments, "_gate_evidence", lambda _root: (True, "<evidence>")
    )
    monkeypatch.setattr(
        changelog_fragments, "find_open_pr_by_head_prefix", lambda *_a, **_kw: None
    )

    real_run = subprocess.run  # captured BEFORE patching, for the git argv below
    gh_pr_create_calls: list[list[str]] = []

    # MOCKING: selective dispatcher — `git` argv is routed to the real
    # `subprocess.run` (so the branch really gets pushed to the bare
    # origin above), while `gh` argv returns a canned success, since no
    # real `gh` binary can authenticate or open a PR in a test sandbox.
    # `gh pr create` argv is also recorded, to assert the `--base` wiring
    # below.
    def _dispatch(
        cmd: list[str], *_a: object, **kwargs: object
    ) -> subprocess.CompletedProcess[str] | FakeProc:
        if cmd[0] == "git":
            return real_run(cmd, *_a, **kwargs)
        if cmd[:2] == ["gh", "auth"]:
            return FakeProc(0)
        if cmd[:2] == ["gh", "pr"]:
            gh_pr_create_calls.append(cmd)
            return FakeProc(0, stdout="https://github.com/x/y/pull/42\n")
        msg = f"unexpected subprocess.run call: {cmd}"
        raise AssertionError(msg)

    monkeypatch.setattr(changelog_fragments.subprocess, "run", _dispatch)

    assert main(["release-pr"]) == 0

    out = capsys.readouterr().out
    assert "release-pr: opened https://github.com/x/y/pull/42" in out
    assert len(gh_pr_create_calls) == 1
    create_argv = gh_pr_create_calls[0]
    assert "--base" in create_argv
    assert create_argv[create_argv.index("--base") + 1] == "main"
    # No plugin.json in this fixture repo (bare pyproject setup) — the real
    # publish path must emit the manifest-less tagging sentence, not the
    # auto-tag-on-merge one.
    assert (
        "forge-release --from-changelog" in create_argv[create_argv.index("--body") + 1]
    )

    remote_branches = subprocess.run(
        ["git", "ls-remote", "--heads", str(origin), "chore/assemble-v1.1.0"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert remote_branches.strip() != ""

    changelog_at_branch = subprocess.run(
        ["git", "show", "chore/assemble-v1.1.0:CHANGELOG.md"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "## v1.1.0" in changelog_at_branch
    assert "- new feature" in changelog_at_branch

    fragment_show = subprocess.run(
        ["git", "show", "chore/assemble-v1.1.0:changelog.d/note.added.md"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert fragment_show.returncode != 0

    commit_message = subprocess.run(
        ["git", "log", "-1", "--format=%s", "chore/assemble-v1.1.0"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert commit_message == "chore(release): assemble v1.1.0"

    current_branch = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert current_branch == "main"


# ---------------------------------------------------------------------------
# _gate_evidence
# ---------------------------------------------------------------------------


def test_gate_evidence_pass_formats_success_headline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A zero exit formats a ✅ headline with the gate output fenced below."""
    monkeypatch.setattr(
        git_utils.subprocess,
        "run",
        lambda *_a, **_kw: FakeProc(0, stdout="ok\n"),
    )

    passed, evidence = changelog_fragments._gate_evidence(tmp_path)

    assert passed is True
    assert "✅" in evidence
    assert "Versioning gates pass" in evidence
    assert "ok" in evidence


def test_gate_evidence_fail_formats_warning_headline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-zero exit formats a ⚠️ headline — a gate failure never blocks the PR."""
    monkeypatch.setattr(
        git_utils.subprocess,
        "run",
        lambda *_a, **_kw: FakeProc(1, stderr="boom"),
    )

    passed, evidence = changelog_fragments._gate_evidence(tmp_path)

    assert passed is False
    assert "⚠️" in evidence
    assert "FAILED" in evidence
    assert "boom" in evidence


def test_gate_evidence_truncates_long_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Output beyond the cap is truncated with a trailing marker, not balloon the PR."""
    oversized = "x" * 5000
    monkeypatch.setattr(
        git_utils.subprocess,
        "run",
        lambda *_a, **_kw: FakeProc(0, stdout=oversized),
    )

    _passed, evidence = changelog_fragments._gate_evidence(tmp_path)

    assert evidence.count("x") == git_utils.EVIDENCE_OUTPUT_CAP
    assert evidence.endswith("… (truncated)\n````\n")


def test_gate_evidence_invokes_precommit_with_exact_argv_and_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate subprocess is `forge-precommit --only <gates>`, run at *root*."""
    calls: list[tuple[list[str], object]] = []

    def _fake_run(cmd: list[str], **kwargs: object) -> FakeProc:
        calls.append((cmd, kwargs.get("cwd")))
        return FakeProc(0)

    monkeypatch.setattr(git_utils.subprocess, "run", _fake_run)

    changelog_fragments._gate_evidence(tmp_path)

    assert calls == [
        (["forge-precommit", "--only", "changelog_version,plugin_version"], tmp_path)
    ]


# ---------------------------------------------------------------------------
# _push_and_open_pr
# ---------------------------------------------------------------------------


def test_push_and_open_pr_push_race_defers_to_open_pr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A `git push` rejection explained by a racing PR defers with exit 0."""
    monkeypatch.setattr(
        changelog_fragments, "_gate_evidence", lambda _root: (True, "e")
    )
    monkeypatch.setattr(
        changelog_fragments.subprocess,
        "run",
        lambda *_a, **_kw: FakeProc(1, stderr="rejected"),
    )
    url = "https://github.com/x/y/pull/9"
    monkeypatch.setattr(
        changelog_fragments, "find_open_pr_by_head_prefix", lambda *_a, **_kw: url
    )

    rc = changelog_fragments._push_and_open_pr(
        tmp_path, "chore/assemble-v1.1.0", "v1.1.0", "main", draft=False
    )

    assert rc == 0
    assert "lost the race — assembly PR open at" in capsys.readouterr().out


def test_push_and_open_pr_push_failure_without_race_exits_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A `git push` failure with no racing PR to explain it exits 2."""
    monkeypatch.setattr(
        changelog_fragments, "_gate_evidence", lambda _root: (True, "e")
    )
    monkeypatch.setattr(
        changelog_fragments.subprocess,
        "run",
        lambda *_a, **_kw: FakeProc(1, stderr="rejected"),
    )
    monkeypatch.setattr(
        changelog_fragments, "find_open_pr_by_head_prefix", lambda *_a, **_kw: None
    )

    rc = changelog_fragments._push_and_open_pr(
        tmp_path, "chore/assemble-v1.1.0", "v1.1.0", "main", draft=False
    )

    assert rc == 2
    assert "push FAILED" in capsys.readouterr().out


def test_push_and_open_pr_create_race_defers_to_open_pr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A `gh pr create` failure (e.g. 422 duplicate) explained by a racing PR defers."""
    monkeypatch.setattr(
        changelog_fragments, "_gate_evidence", lambda _root: (True, "e")
    )

    def _dispatch(cmd: list[str], *_a: object, **_kw: object) -> FakeProc:
        if cmd[0] == "git":
            return FakeProc(0)
        return FakeProc(1, stderr="422")

    monkeypatch.setattr(changelog_fragments.subprocess, "run", _dispatch)
    url = "https://github.com/x/y/pull/9"
    monkeypatch.setattr(
        changelog_fragments, "find_open_pr_by_head_prefix", lambda *_a, **_kw: url
    )

    rc = changelog_fragments._push_and_open_pr(
        tmp_path, "chore/assemble-v1.1.0", "v1.1.0", "main", draft=False
    )

    assert rc == 0
    assert "assembly PR already open —" in capsys.readouterr().out


def test_push_and_open_pr_create_failure_without_race_exits_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A `gh pr create` failure with no racing PR to explain it exits 2."""
    monkeypatch.setattr(
        changelog_fragments, "_gate_evidence", lambda _root: (True, "e")
    )

    def _dispatch(cmd: list[str], *_a: object, **_kw: object) -> FakeProc:
        if cmd[0] == "git":
            return FakeProc(0)
        return FakeProc(1, stderr="422")

    monkeypatch.setattr(changelog_fragments.subprocess, "run", _dispatch)
    monkeypatch.setattr(
        changelog_fragments, "find_open_pr_by_head_prefix", lambda *_a, **_kw: None
    )

    rc = changelog_fragments._push_and_open_pr(
        tmp_path, "chore/assemble-v1.1.0", "v1.1.0", "main", draft=False
    )

    assert rc == 2
    out = capsys.readouterr().out
    assert "gh pr create FAILED" in out
    assert "open the PR manually" in out


# ---------------------------------------------------------------------------
# _publish_assembly_pr
# ---------------------------------------------------------------------------


def test_publish_assembly_pr_mid_step_exception_still_restores_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mid-step exception still restores the starting branch via `finally`."""

    class BoomError(Exception):
        """Local sentinel exception for the `_stage_release` failure."""

    def _raise_boom(*_a: object, **_kw: object) -> int:
        raise BoomError

    _init_tagged_repo(tmp_path)
    monkeypatch.setattr(changelog_fragments, "_stage_release", _raise_boom)

    with pytest.raises(BoomError):
        changelog_fragments._publish_assembly_pr(
            tmp_path, "v1.1.0", "1.1.0", date="", draft=False
        )

    current_branch = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert current_branch == "main"


def test_fragments_new_since_tag_excludes_tag_tree_members(
    tmp_path: Path,
) -> None:
    """Fragments already in the tag's tree are consumed; only later ones count."""
    init_git_repo(tmp_path)
    (tmp_path / "changelog.d").mkdir()
    (tmp_path / "changelog.d" / "old.added.md").write_text("bump: minor\n- old\n")
    commit_all(tmp_path, "old fragment")
    subprocess.run(["git", "tag", "v1.0.0"], cwd=tmp_path, env=GIT_ENV, check=True)
    (tmp_path / "changelog.d" / "new.added.md").write_text("bump: patch\n- new\n")
    commit_all(tmp_path, "new fragment")

    new = changelog_fragments.fragments_new_since_tag(tmp_path, "v1.0.0")

    assert [p.name for p in new] == ["new.added.md"]
    assert [
        p.name for p in changelog_fragments.fragments_new_since_tag(tmp_path, None)
    ] == ["new.added.md", "old.added.md"]


# ---------------------------------------------------------------------------
# branch_added_fragments — one-fragment-per-PR enforcement seam
# ---------------------------------------------------------------------------


def test_branch_added_fragments_excludes_fork_point_and_base_side_deletions(
    tmp_path: Path,
) -> None:
    """Only fragments the branch itself adds since its fork point count.

    Guards against a regression that diffs the live base tip instead of
    the merge-base: the seeded fragment here is both present at the fork
    and later deleted on the base — a tip-based diff would resurrect it
    as "added"; the merge-base diff never sees it.
    """
    origin = tmp_path / "origin.git"
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_autotag_repo(repo, origin)

    subprocess.run(
        ["git", "checkout", "-q", "-b", "feat/x"], cwd=repo, env=GIT_ENV, check=True
    )
    (repo / "changelog.d" / "second.added.md").write_text("bump: patch\n- second\n")
    commit_all(repo, "feat: second fragment")

    subprocess.run(["git", "checkout", "-q", "main"], cwd=repo, env=GIT_ENV, check=True)
    subprocess.run(
        ["git", "rm", "-q", "changelog.d/first.added.md"],
        cwd=repo,
        env=GIT_ENV,
        check=True,
    )
    commit_all(repo, "chore: assemble release")
    subprocess.run(
        ["git", "push", "-q", "origin", "main"], cwd=repo, env=GIT_ENV, check=True
    )

    subprocess.run(
        ["git", "checkout", "-q", "feat/x"], cwd=repo, env=GIT_ENV, check=True
    )

    assert branch_added_fragments(repo) == ["changelog.d/second.added.md"]


def test_branch_added_fragments_unresolvable_base_returns_empty(
    tmp_path: Path,
) -> None:
    """An unresolvable configured base branch degrades to an empty list."""
    init_git_repo(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        '[tool.forge]\nbase_branch = "no-such-branch"\n'
    )
    (tmp_path / "changelog.d").mkdir()
    (tmp_path / "changelog.d" / "a.added.md").write_text("bump: minor\n- a\n")
    commit_all(tmp_path, "feat: fragment")

    assert branch_added_fragments(tmp_path) == []
