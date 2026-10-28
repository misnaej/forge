"""Tests for forge.intake — entry validation, grouping, migration and retirement."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from forge import intake


LESSON_FIELDS = {"seen": "2026-01-01", "branch": "feat/a"}
FINDING_FIELDS = {
    "surface": "forge-precommit",
    "release": "forge 1.0.0",
    "seen": "2026-01-01",
    "branch": "feat/a",
}
EVIDENCE = "## Evidence\n\nsrc/forge/x.py:12 quoted output\n"


@pytest.fixture
def intake_dir(tmp_path: Path) -> Path:
    """Initialised intake directory (``docs/intake`` with both kind folders).

    Returns:
        Path to the intake directory.
    """
    path = tmp_path / "docs/intake"
    intake.init(tmp_path, path)
    return path


@pytest.fixture
def fixed_stamp(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the branch and forge version that ``new_entry`` stamps.

    SCENARIO: stamps depend on git and on the installed package version.
    MOCK SETUP: ``resolve_current_branch`` returns ``("feat/x", "git")``;
    ``pip_version`` returns ``"1.2.3"``.
    EXPECTED BEHAVIOR: stamped entries carry those exact values.
    """
    monkeypatch.setattr(
        intake, "resolve_current_branch", lambda _root: ("feat/x", "git")
    )
    monkeypatch.setattr(intake, "pip_version", lambda: "1.2.3")


def write_entry(
    intake_dir: Path,
    kind: str,
    slug: str,
    title: str,
    *,
    body: str = "",
    **fields: str,
) -> Path:
    """Write one entry file and return its path.

    Args:
        intake_dir: The intake directory.
        kind: ``"lesson"`` or ``"finding"``.
        slug: File name stem.
        title: The ``# `` heading text.
        body: Text appended after the fields.
        **fields: ``- key: value`` lines, in order given.

    Returns:
        The written path.
    """
    folder = intake_dir / intake.KIND_DIRS[kind]
    folder.mkdir(parents=True, exist_ok=True)
    lines = [f"# {title}", ""]
    lines.extend(f"- {key.replace('_', ' ')}: {value}" for key, value in fields.items())
    path = folder / f"{slug}.md"
    path.write_text("\n".join(lines) + "\n\n" + body, encoding="utf-8")
    return path


def make_entry(
    slug: str,
    title: str,
    *,
    kind: str = "lesson",
    branch: str = "feat/a",
    **fields: str,
) -> intake.Entry:
    """Build an in-memory entry for grouping tests.

    Args:
        slug: File name stem.
        title: Entry title.
        kind: ``"lesson"`` or ``"finding"``.
        branch: Recording branch.
        **fields: Extra fields (``sibling``, ``occurrences``).

    Returns:
        The entry; its path is never read.
    """
    return intake.Entry(
        path=Path(f"{slug}.md"),
        kind=kind,
        title=title,
        fields={"branch": branch, **fields},
    )


def write_pyproject(root: Path, body: str) -> None:
    """Write ``pyproject.toml`` with *body* into *root*.

    Args:
        root: Directory to write into.
        body: File contents.
    """
    (root / "pyproject.toml").write_text(body, encoding="utf-8")


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------


def test_check_accepts_valid_lesson_and_finding(intake_dir: Path) -> None:
    """A lesson with only ``seen`` and a complete finding raise no problems."""
    write_entry(intake_dir, "lesson", "trap", "Trap", seen="2026-01-01")
    write_entry(intake_dir, "finding", "bug", "Bug", body=EVIDENCE, **FINDING_FIELDS)

    assert intake.check(intake_dir, ()) == []


@pytest.mark.parametrize("missing", ["surface", "release", "seen"])
def test_check_finding_missing_field_is_one_problem(
    intake_dir: Path, missing: str
) -> None:
    """Each required finding field is reported when absent.

    Args:
        missing: The field left out.
    """
    fields = {k: v for k, v in FINDING_FIELDS.items() if k != missing}
    write_entry(intake_dir, "finding", "bug", "Bug", body=EVIDENCE, **fields)

    problems = intake.check(intake_dir, ())

    assert len(problems) == 1
    assert f"missing '- {missing}:' field" in problems[0]


def test_check_evidence_outside_section_is_not_accepted(intake_dir: Path) -> None:
    """A file:line outside ``## Evidence`` does not satisfy the evidence rule."""
    body = "## What it costs\n\nsrc/forge/x.py:12 elsewhere\n\n## Evidence\n\nnone\n"
    write_entry(intake_dir, "finding", "bug", "Bug", body=body, **FINDING_FIELDS)

    problems = intake.check(intake_dir, ())

    assert len(problems) == 1
    assert "'## Evidence' must cite a file:line" in problems[0]


def test_check_evidence_inside_section_is_accepted(intake_dir: Path) -> None:
    """A file:line inside ``## Evidence`` satisfies the evidence rule."""
    body = "## What it costs\n\nnothing\n\n## Evidence\n\nsrc/forge/x.py:12 here\n"
    write_entry(intake_dir, "finding", "bug", "Bug", body=body, **FINDING_FIELDS)

    assert intake.check(intake_dir, ()) == []


def test_check_reports_bad_file_name_and_missing_title(intake_dir: Path) -> None:
    """A non-slug file name and a first line without ``# `` are both reported."""
    path = intake_dir / "lessons" / "Bad Name.md"
    path.write_text("not a heading\n\n- seen: 2026-01-01\n", encoding="utf-8")

    problems = intake.check(intake_dir, ())

    assert any("file name must match" in p for p in problems)
    assert any("first line must be '# <title>'" in p for p in problems)


@pytest.mark.parametrize(
    "secret",
    [
        "ghp_" + "a" * 24,
        "AKIA" + "A" * 16,
        "sk-" + "a" * 24,
        "xoxb-" + "1" * 12,
        "-----BEGIN RSA " + "PRIVATE KEY-----",
        "ASIA" + "A" * 16,
        "sk_live_" + "a" * 24,
        "https://user:pass@host",
    ],
    ids=[
        "github",
        "aws",
        "api-key",
        "slack",
        "private-key",
        "aws-session",
        "stripe-live",
        "url-credential",
    ],
)
def test_check_flags_secret_in_a_lesson(intake_dir: Path, secret: str) -> None:
    """Secret-shaped strings are refused even in lessons, as ``path:line: ...``.

    Args:
        secret: The secret-shaped text planted in the entry.
    """
    path = write_entry(
        intake_dir,
        "lesson",
        "trap",
        "Trap",
        body=f"token {secret}\n",
        seen="2026-01-01",
    )
    line = next(
        i for i, t in enumerate(path.read_text().splitlines(), 1) if secret in t
    )

    problems = intake.check(intake_dir, ())

    assert len(problems) == 1
    assert problems[0].startswith(f"{path}:{line}: ")
    assert "-shaped string" in problems[0]


def test_check_flags_private_path_and_redact_token_in_finding(
    intake_dir: Path,
) -> None:
    """A machine path and a configured token (any case) fail a finding."""
    body = EVIDENCE + "\nsee /home/someone/proj and ACME internals\n"
    write_entry(intake_dir, "finding", "bug", "Bug", body=body, **FINDING_FIELDS)

    problems = intake.check(intake_dir, ("acme",))

    assert any("machine-specific path" in p for p in problems)
    assert any("redacted token" in p for p in problems)


def test_check_allows_private_path_and_redact_token_in_lesson(
    intake_dir: Path,
) -> None:
    """Lessons stay in the repo, so paths and redact tokens are not checked."""
    write_entry(
        intake_dir,
        "lesson",
        "trap",
        "Trap",
        body="see /home/someone/proj and ACME internals\n",
        seen="2026-01-01",
    )

    assert intake.check(intake_dir, ("acme",)) == []


def test_discover_skips_readme_and_underscore_files(tmp_path: Path) -> None:
    """README.md and ``_``-prefixed files are not entries, so nothing is checked."""
    lessons = tmp_path / "lessons"
    lessons.mkdir()
    for name in ("README.md", "_TEMPLATE.md", "_LEDGER.md"):
        (lessons / name).write_text("junk\n", encoding="utf-8")

    assert intake.discover(tmp_path) == []
    assert intake.check(tmp_path, ()) == []


def test_init_then_check_is_clean(tmp_path: Path) -> None:
    """The files ``init`` writes (README, template) are not themselves entries."""
    path = tmp_path / "docs/intake"
    intake.init(tmp_path, path)

    assert intake.check(path, ()) == []


def test_main_check_fails_on_bad_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``check`` exits 1 and reports the problem count for a bad entry."""
    monkeypatch.setattr(intake, "repo_root", lambda: tmp_path)
    folder = tmp_path / "docs/intake/lessons"
    folder.mkdir(parents=True)
    (folder / "trap.md").write_text("no heading\n", encoding="utf-8")

    code = intake.main(["check"])

    assert code == 1
    assert "problem(s)" in capsys.readouterr().out


def test_main_check_passes_on_clean_intake(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``check`` exits 0 and counts entries when everything passes."""
    monkeypatch.setattr(intake, "repo_root", lambda: tmp_path)
    write_entry(tmp_path / "docs/intake", "lesson", "trap", "Trap", seen="2026-01-01")

    code = intake.main(["check"])

    assert code == 0
    assert "1 entries OK" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# new
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("fixed_stamp")
def test_new_finding_is_stamped(tmp_path: Path) -> None:
    """A finding carries surface, release, UTC date, branch and its sections."""
    path = intake.new_entry(tmp_path / "i", "finding", "bug", "A bug", "forge-x")

    text = path.read_text(encoding="utf-8")
    today = dt.datetime.now(tz=dt.UTC).date().isoformat()
    assert text.startswith("# A bug\n")
    assert "- surface: forge-x" in text
    assert "- release: forge 1.2.3" in text
    assert f"- seen: {today}" in text
    assert "- branch: feat/x" in text
    assert "## Evidence" in text
    assert "## What happened" not in text


@pytest.mark.usefixtures("fixed_stamp")
def test_new_lesson_omits_surface_and_uses_lesson_sections(tmp_path: Path) -> None:
    """A lesson with no surface has no surface line and the lesson sections."""
    path = intake.new_entry(tmp_path / "i", "lesson", "trap", "A trap", "")

    text = path.read_text(encoding="utf-8")
    assert "- surface:" not in text
    assert "## What happened" in text
    assert "## Fix, if known" in text
    assert "## Evidence" not in text


@pytest.mark.parametrize("slug", ["../x", "A b", "", "a/b"])
def test_new_rejects_bad_slug(tmp_path: Path, slug: str) -> None:
    """A slug that is not a plain slug raises before anything is written.

    Args:
        slug: The rejected slug.
    """
    with pytest.raises(ValueError, match="slug"):
        intake.new_entry(tmp_path / "i", "lesson", slug, "T", "")

    assert not (tmp_path / "i").exists()


def test_new_finding_without_surface_raises(tmp_path: Path) -> None:
    """A finding must name the forge surface involved."""
    with pytest.raises(ValueError, match="--surface"):
        intake.new_entry(tmp_path / "i", "finding", "bug", "T", "")


@pytest.mark.usefixtures("fixed_stamp")
def test_new_same_slug_gets_numbered_suffixes(tmp_path: Path) -> None:
    """A repeated slug is suffixed ``-2``, ``-3``; the first file is untouched."""
    first = intake.new_entry(tmp_path / "i", "lesson", "trap", "First", "")
    second = intake.new_entry(tmp_path / "i", "lesson", "trap", "Second", "")
    third = intake.new_entry(tmp_path / "i", "lesson", "trap", "Third", "")

    assert [p.name for p in (first, second, third)] == [
        "trap.md",
        "trap-2.md",
        "trap-3.md",
    ]
    assert first.read_text(encoding="utf-8").startswith("# First\n")
    assert second.read_text(encoding="utf-8").startswith("# Second\n")


# ---------------------------------------------------------------------------
# init and migration
# ---------------------------------------------------------------------------


def test_init_creates_layout_and_is_idempotent(tmp_path: Path) -> None:
    """``init`` writes dirs, README and template; a second run changes nothing."""
    path = tmp_path / "docs/intake"

    first = intake.init(tmp_path, path)
    (path / "README.md").write_text("my edits\n", encoding="utf-8")
    second = intake.init(tmp_path, path)

    assert first
    assert (path / "lessons").is_dir()
    assert (path / "findings" / "_TEMPLATE.md").is_file()
    assert (path / "lessons" / "_TEMPLATE.md").is_file()
    assert second == []
    assert (path / "README.md").read_text(encoding="utf-8") == "my edits\n"


LEGACY = """\
# Lessons

## A trap here
- occurrences: 3
- first seen: 2026-01-02
- where it applies: tests

Prose about the trap.

## Another one
- occurrences: 2

Second prose.
"""


def test_migration_writes_one_file_per_lesson_and_deletes_legacy(
    tmp_path: Path,
) -> None:
    """Each legacy ``## `` entry becomes a migrated lesson with prose kept."""
    legacy = tmp_path / "docs/lessons.md"
    legacy.parent.mkdir()
    legacy.write_text(LEGACY, encoding="utf-8")
    path = tmp_path / "docs/intake"

    intake.init(tmp_path, path)

    files = sorted((path / "lessons").glob("[!_]*.md"))
    assert [f.name for f in files] == ["a-trap-here.md", "another-one.md"]
    entry = intake.parse_entry(files[0], "lesson")
    assert entry.branch == "migrated"
    assert entry.occurrences == 3
    assert entry.fields["seen"] == "2026-01-02"
    assert entry.fields["where it applies"] == "tests"
    assert "Prose about the trap." in files[0].read_text(encoding="utf-8")
    assert not legacy.exists()
    assert intake.check(path, ()) == []


def test_migration_resumes_after_partial_run_without_duplicates(
    tmp_path: Path,
) -> None:
    """An already-written target is kept, the rest written, reruns add nothing."""
    legacy = tmp_path / "docs/lessons.md"
    legacy.parent.mkdir()
    legacy.write_text(LEGACY, encoding="utf-8")
    path = tmp_path / "docs/intake"
    intake.init(tmp_path, path)
    folder = path / "lessons"
    kept = (folder / "a-trap-here.md").read_text(encoding="utf-8")
    (folder / "another-one.md").unlink()

    legacy.write_text(LEGACY, encoding="utf-8")
    intake.migrate_legacy_lessons(tmp_path, path)
    legacy.write_text(LEGACY, encoding="utf-8")
    intake.migrate_legacy_lessons(tmp_path, path)

    assert sorted(f.name for f in folder.glob("[!_]*.md")) == [
        "a-trap-here.md",
        "another-one.md",
    ]
    assert (folder / "a-trap-here.md").read_text(encoding="utf-8") == kept
    assert not legacy.exists()


def test_migration_without_headings_leaves_file_in_place(tmp_path: Path) -> None:
    """A legacy file with no ``## `` entry is reported and not deleted."""
    legacy = tmp_path / "docs/lessons.md"
    legacy.parent.mkdir()
    legacy.write_text("just prose\n", encoding="utf-8")

    report = intake.migrate_legacy_lessons(tmp_path, tmp_path / "docs/intake")

    assert report is not None
    assert "left in place" in report
    assert legacy.exists()


def test_migration_ignores_symlinked_legacy_file(tmp_path: Path) -> None:
    """A symlink at the legacy path is never followed; its target is untouched."""
    target = tmp_path / "outside.md"
    target.write_text(LEGACY, encoding="utf-8")
    legacy = tmp_path / "docs/lessons.md"
    legacy.parent.mkdir()
    legacy.symlink_to(target)
    path = tmp_path / "docs/intake"

    intake.init(tmp_path, path)

    assert target.read_text(encoding="utf-8") == LEGACY
    assert legacy.is_symlink()
    assert list((path / "lessons").glob("[!_]*.md")) == []


def test_migration_keeps_both_bodies_when_slugs_collide(tmp_path: Path) -> None:
    """Titles slugifying alike get ``-2``; punctuation-only titles get an index."""
    legacy_text = "## Foo\nfirst body\n\n## foo!\nsecond body\n\n## !!!\nthird body\n"
    legacy = tmp_path / "docs/lessons.md"
    legacy.parent.mkdir()
    legacy.write_text(legacy_text, encoding="utf-8")
    path = tmp_path / "docs/intake"

    intake.init(tmp_path, path)

    folder = path / "lessons"
    assert sorted(f.name for f in folder.glob("[!_]*.md")) == [
        "foo-2.md",
        "foo.md",
        "lesson-3.md",
    ]
    assert "first body" in (folder / "foo.md").read_text(encoding="utf-8")
    assert "second body" in (folder / "foo-2.md").read_text(encoding="utf-8")

    legacy.write_text(legacy_text, encoding="utf-8")
    intake.migrate_legacy_lessons(tmp_path, path)

    assert len(list(folder.glob("[!_]*.md"))) == 3


# ---------------------------------------------------------------------------
# grouping
# ---------------------------------------------------------------------------


def test_group_joins_plural_variants() -> None:
    """Plural variants, "stale bullets" and "stale bullet", are one problem."""
    a = make_entry("stale-bullets", "Stale bullets", branch="feat/a")
    b = make_entry("stale-bullet", "Stale bullet", branch="feat/b")

    assert intake.group_entries([a, b]) == [[a, b]]


def test_group_keeps_unrelated_titles_apart() -> None:
    """Entries with no word overlap stay in separate groups."""
    a = make_entry("stale-bullets", "Stale bullets")
    b = make_entry("cache-invalidation", "Cache invalidation")

    assert len(intake.group_entries([a, b])) == 2


@pytest.mark.parametrize(
    ("a_sibling", "b_sibling"),
    [("second-slug, third-slug", ""), ("", "first-slug")],
    ids=["comma-list-from-a", "named-by-b"],
)
def test_group_follows_sibling_link_in_either_direction(
    a_sibling: str, b_sibling: str
) -> None:
    """A ``sibling`` field groups entries whose words do not overlap.

    Args:
        a_sibling: The first entry's ``sibling`` field value.
        b_sibling: The second entry's ``sibling`` field value.
    """
    fields_a = {"sibling": a_sibling} if a_sibling else {}
    fields_b = {"sibling": b_sibling} if b_sibling else {}
    a = make_entry("first-slug", "Quick red fox", **fields_a)
    b = make_entry("second-slug", "Lazy dog sleeps", **fields_b)

    assert intake.group_entries([a, b]) == [[a, b]]


def test_group_never_mixes_lessons_and_findings() -> None:
    """Identical wording in a lesson and a finding does not group them."""
    lesson = make_entry("same-words", "Same words", kind="lesson")
    finding = make_entry("same-words", "Same words", kind="finding")

    assert len(intake.group_entries([lesson, finding])) == 2


def test_occurrence_count_counts_a_branch_once() -> None:
    """Two files from one branch are one sighting; two branches are two."""
    same = [
        make_entry("x-1", "X", branch="feat/a"),
        make_entry("x-2", "X", branch="feat/a"),
    ]
    different = [
        make_entry("x-1", "X", branch="feat/a"),
        make_entry("x-2", "X", branch="feat/b"),
    ]

    assert intake.occurrence_count(same) == 1
    assert intake.occurrence_count(different) == 2


def test_occurrence_count_adds_migrated_counts() -> None:
    """A migrated lesson contributes its carried count on top of branch files."""
    group = [
        make_entry("old", "Old", branch="migrated", occurrences="3"),
        make_entry("new", "Old", branch="feat/a"),
    ]

    assert intake.occurrence_count(group) == 4


@pytest.mark.parametrize("extra", [{}, {"occurrences": "0"}], ids=["missing", "zero"])
def test_migrated_without_valid_count_counts_once(extra: dict[str, str]) -> None:
    """A migrated entry with no positive count stands for one sighting.

    Args:
        extra: Extra fields on the migrated entry.
    """
    assert (
        intake.occurrence_count([make_entry("old", "Old", branch="migrated", **extra)])
        == 1
    )


def test_group_orders_largest_first() -> None:
    """The group with more sightings is listed before a smaller earlier one."""
    solo = make_entry("solo-thing", "Solo thing")
    pair_a = make_entry("pair-snag", "Pair snag", branch="feat/a")
    pair_b = make_entry("pair-snag-2", "Pair snag", branch="feat/b")

    groups = intake.group_entries([solo, pair_a, pair_b])

    assert groups[0] == [pair_a, pair_b]
    assert groups[1] == [solo]


@pytest.mark.parametrize(
    ("members", "expected"),
    [
        ([("feat/a", "")], "Occurrences: 1, on 1 branch"),
        (
            [("feat/a", ""), ("feat/b", "")],
            "Occurrences: 2, on 2 branches",
        ),
        ([("migrated", "3")], "Occurrences: 3, 3 recorded before the intake"),
        (
            [("feat/a", ""), ("migrated", "3")],
            "Occurrences: 4, on 1 branch, 3 recorded before the intake",
        ),
    ],
    ids=["one", "two", "migrated", "mixed"],
)
def test_occurrence_header_strings(
    members: list[tuple[str, str]], expected: str
) -> None:
    """The header carries counts only, in the documented wording.

    Args:
        members: ``(branch, occurrences)`` per entry in the group.
        expected: The exact header.
    """
    group = [
        make_entry(
            f"m-{i}", "Title", branch=branch, **({"occurrences": n} if n else {})
        )
        for i, (branch, n) in enumerate(members)
    ]

    assert intake.occurrence_header(group) == expected


def test_main_group_prints_only_groups_reaching_minimum(
    intake_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``group --min 2`` lists the repeated problem with member paths only."""
    monkeypatch.setattr(intake, "repo_root", lambda: tmp_path)
    one = write_entry(
        intake_dir, "lesson", "stale-cache-a", "Stale cache", seen="x", branch="feat/a"
    )
    two = write_entry(
        intake_dir, "lesson", "stale-cache-b", "Stale cache", seen="x", branch="feat/b"
    )
    lone = write_entry(
        intake_dir, "lesson", "lonely-trap", "Lonely trap", seen="x", branch="feat/a"
    )

    code = intake.main(["group", "--min", "2"])

    out = capsys.readouterr().out
    assert code == 0
    assert "Occurrences: 2, on 2 branches" in out
    assert str(one) in out
    assert str(two) in out
    assert str(lone) not in out


def test_main_list_lists_every_entry(
    intake_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``list`` prints one line per entry, lessons and findings."""
    monkeypatch.setattr(intake, "repo_root", lambda: tmp_path)
    lesson = write_entry(intake_dir, "lesson", "trap", "Trap", seen="x")
    finding = write_entry(intake_dir, "finding", "bug", "Bug", seen="x")

    code = intake.main(["list"])

    out = capsys.readouterr().out
    assert code == 0
    assert str(lesson) in out
    assert str(finding) in out


# ---------------------------------------------------------------------------
# retire
# ---------------------------------------------------------------------------

ISSUE_URL = "https://example.com/org/repo/issues/1"


def _two_findings(intake_dir: Path, title: str = "Step misfires") -> list[Path]:
    """Write two findings of one problem from different branches.

    Args:
        intake_dir: The intake directory.
        title: Shared title.

    Returns:
        The two paths.
    """
    return [
        write_entry(
            intake_dir,
            "finding",
            f"misfire-{n}",
            title,
            body=EVIDENCE,
            **{**FINDING_FIELDS, "branch": f"feat/{n}"},
        )
        for n in ("a", "b")
    ]


def test_retire_writes_ledger_row_and_deletes_files(intake_dir: Path) -> None:
    """Retiring records one escaped row under a header and removes the files."""
    paths = _two_findings(intake_dir, title="Step | misfires")

    row = intake.retire(intake_dir, paths, ISSUE_URL)

    today = dt.datetime.now(tz=dt.UTC).date().isoformat()
    ledger = (intake_dir / "_LEDGER.md").read_text(encoding="utf-8")
    expected = (
        f"| {today} | `Step \\| misfires` | {ISSUE_URL} | "
        "Occurrences: 2, on 2 branches | 2 |"
    )
    assert row == expected
    assert ledger.startswith("# Intake ledger")
    assert ledger.count("| Retired |") == 1
    assert expected in ledger
    assert not any(p.exists() for p in paths)


def test_retire_twice_appends_without_rewriting_header(intake_dir: Path) -> None:
    """A second retirement adds a row; the ledger header is written once."""
    intake.retire(intake_dir, _two_findings(intake_dir), ISSUE_URL)
    more = [
        write_entry(
            intake_dir, "finding", "other", "Other", body=EVIDENCE, **FINDING_FIELDS
        )
    ]

    intake.retire(intake_dir, more, ISSUE_URL)

    ledger = (intake_dir / "_LEDGER.md").read_text(encoding="utf-8")
    assert ledger.count("| Retired |") == 1
    assert ledger.count(ISSUE_URL) == 2


@pytest.mark.parametrize(
    "url",
    ["http://x", "javascript:x", "https://a\n| x", "", "https://a/b\n", "https://a|b"],
)
def test_retire_refuses_bad_url(intake_dir: Path, url: str) -> None:
    """Only a single-line https URL is accepted; nothing is deleted.

    Args:
        url: The rejected ``--issue`` value.
    """
    paths = _two_findings(intake_dir)

    with pytest.raises(ValueError, match="https URL"):
        intake.retire(intake_dir, paths, url)

    assert all(p.exists() for p in paths)
    assert not (intake_dir / "_LEDGER.md").exists()


@pytest.mark.parametrize("bad", ["outside", "root", "missing", "symlink"])
def test_retire_refuses_non_entry_paths_and_deletes_nothing(
    intake_dir: Path, tmp_path: Path, bad: str
) -> None:
    """Any path that is not a real entry aborts the whole retirement.

    Args:
        bad: Which kind of bad path is mixed in with a valid one.
    """
    good = _two_findings(intake_dir)[0]
    outside = tmp_path / "outside.md"
    outside.write_text("# X\n", encoding="utf-8")
    root_file = intake_dir / "stray.md"
    root_file.write_text("# X\n", encoding="utf-8")
    link = intake_dir / "findings" / "link.md"
    link.symlink_to(good)
    bad_path = {
        "outside": outside,
        "root": root_file,
        "missing": intake_dir / "findings" / "ghost.md",
        "symlink": link,
    }[bad]

    with pytest.raises(ValueError, match="not an entry"):
        intake.retire(intake_dir, [good, bad_path], ISSUE_URL)

    assert good.exists()
    assert not (intake_dir / "_LEDGER.md").exists()


def test_main_retire_bad_url_exits_one_without_traceback(
    intake_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A refused input prints ``forge-intake: ...`` and returns 1."""
    monkeypatch.setattr(intake, "repo_root", lambda: tmp_path)
    paths = _two_findings(intake_dir)

    code = intake.main(["retire", str(paths[0]), "--issue", "http://x"])

    err = capsys.readouterr().err
    assert code == 1
    assert err.startswith("forge-intake: ")
    assert "Traceback" not in err


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


def test_configured_dir_defaults_without_config(tmp_path: Path) -> None:
    """No config means ``docs/intake``."""
    assert intake.configured_dir(tmp_path) == tmp_path / "docs/intake"


def test_configured_dir_honours_relative_path(tmp_path: Path) -> None:
    """A relative in-repo ``dir`` is used as configured."""
    write_pyproject(tmp_path, '[tool.forge.intake]\ndir = "notes/in"\n')

    assert intake.configured_dir(tmp_path) == tmp_path / "notes/in"


@pytest.mark.parametrize("value", ['"/etc/in"', '"../x"'], ids=["absolute", "escape"])
def test_configured_dir_rejects_unsafe_path_with_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, value: str
) -> None:
    """An absolute or escaping ``dir`` falls back to the default and warns.

    Args:
        value: TOML literal for the rejected setting.
    """
    write_pyproject(tmp_path, f"[tool.forge.intake]\ndir = {value}\n")

    with caplog.at_level("WARNING"):
        result = intake.configured_dir(tmp_path)

    assert result == tmp_path / "docs/intake"
    assert "relative path" in caplog.text


@pytest.mark.parametrize("value", ['""', "5"], ids=["empty", "non-string"])
def test_configured_dir_ignores_empty_or_non_string(tmp_path: Path, value: str) -> None:
    """An empty or non-string ``dir`` falls back to the default.

    Args:
        value: TOML literal for the ignored setting.
    """
    write_pyproject(tmp_path, f"[tool.forge.intake]\ndir = {value}\n")

    assert intake.configured_dir(tmp_path) == tmp_path / "docs/intake"


def test_configured_redact_keeps_only_non_blank_strings(tmp_path: Path) -> None:
    """Non-string and blank tokens are dropped; real ones are kept as written."""
    write_pyproject(
        tmp_path, '[tool.forge.intake]\nredact = ["Acme", " ", 3, "corp"]\n'
    )

    assert intake.configured_redact(tmp_path) == ("Acme", "corp")


def test_configured_redact_non_list_is_empty_with_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A ``redact`` that is not a list yields no tokens and warns."""
    write_pyproject(tmp_path, '[tool.forge.intake]\nredact = "acme"\n')

    with caplog.at_level("WARNING"):
        result = intake.configured_redact(tmp_path)

    assert result == ()
    assert "redact must be a list" in caplog.text


# ---------------------------------------------------------------------------
# hardening: counting, parsing, symlinks, migration, retire, check
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("branch", ["main", "unknown", ""])
def test_shared_branch_files_each_count_as_a_sighting(branch: str) -> None:
    """Unrelated sightings on the base (or an unset) branch are not merged into one.

    Args:
        branch: The default base branch, or no branch recorded.
    """
    group = [
        make_entry("x-1", "X", branch=branch),
        make_entry("x-2", "X", branch=branch),
    ]

    assert intake.occurrence_count(group) == 2


def test_configured_base_branch_is_the_shared_one() -> None:
    """With ``base_branch = "develop"``, develop sightings count; main's do not."""

    def pair(branch: str) -> list[intake.Entry]:
        entries = [make_entry(f"x-{n}", "X", branch=branch) for n in (1, 2)]
        for entry in entries:
            entry.base_branch = "develop"
        return entries

    assert intake.occurrence_count(pair("develop")) == 2
    assert intake.occurrence_count(pair("main")) == 1


def test_identical_legacy_sections_merge_and_add_counts(tmp_path: Path) -> None:
    """Two identical legacy sections become one file whose count is their sum."""
    legacy = tmp_path / "docs" / "lessons.md"
    legacy.parent.mkdir()
    section = "## Same trap\n- occurrences: 2\n\nBody.\n\n"
    legacy.write_text(section + section, encoding="utf-8")

    intake.migrate_legacy_lessons(tmp_path, tmp_path / "docs" / "intake")

    files = sorted((tmp_path / "docs" / "intake" / "lessons").glob("[!_]*.md"))
    assert [f.name for f in files] == ["same-trap.md"]
    assert "- occurrences: 4" in files[0].read_text(encoding="utf-8")


def test_feature_branch_files_count_as_one_sighting() -> None:
    """Two files on one feature branch are one piece of work."""
    group = [make_entry("x-1", "X"), make_entry("x-2", "X")]

    assert intake.occurrence_count(group) == 1


def test_non_ascii_digit_occurrences_counts_once() -> None:
    """``occurrences: ²`` passes ``str.isdigit`` but not ``int``; it counts 1."""
    entry = make_entry("old", "Old", branch="migrated", occurrences="²")

    assert entry.occurrences == 1


def test_field_value_trailing_spaces_are_stripped(tmp_path: Path) -> None:
    """A field value padded with spaces parses to the bare value."""
    path = tmp_path / "trap.md"
    path.write_text("# T\n\n- seen: 2026-01-01   \n", encoding="utf-8")

    assert intake.parse_entry(path, "lesson").fields["seen"] == "2026-01-01"


@pytest.mark.parametrize(
    ("title", "surface"), [("a\nb", ""), ("ok", "x\ny")], ids=["title", "surface"]
)
def test_new_rejects_line_break_in_title_or_surface(
    tmp_path: Path, title: str, surface: str
) -> None:
    """A line break would forge a field, so it is refused before any write.

    Args:
        title: The ``--title`` value.
        surface: The ``--surface`` value.
    """
    with pytest.raises(ValueError, match="one line"):
        intake.new_entry(tmp_path / "i", "lesson", "trap", title, surface)

    assert not (tmp_path / "i").exists()


@pytest.mark.parametrize("kind", ["lesson", "finding"])
def test_new_refuses_symlinked_kind_dir(tmp_path: Path, kind: str) -> None:
    """A committed symlink cannot aim ``new`` at a directory outside the intake.

    Args:
        kind: The entry kind whose directory is the symlink.
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    base = tmp_path / "i"
    base.mkdir()
    (base / intake.KIND_DIRS[kind]).symlink_to(outside)

    with pytest.raises(ValueError, match="symlink"):
        intake.new_entry(base, kind, "trap", "T", "forge-x")

    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("relative", ["README.md", "lessons/_TEMPLATE.md"])
def test_init_does_not_follow_dangling_symlink(tmp_path: Path, relative: str) -> None:
    """A dangling symlink at a file ``init`` writes is left alone, target uncreated.

    Args:
        relative: Path of the planted symlink under the intake directory.
    """
    base = tmp_path / "docs/intake"
    (base / "lessons").mkdir(parents=True)
    target = tmp_path / "nowhere.md"
    (base / relative).symlink_to(target)

    intake.init(tmp_path, base)

    assert not target.exists()


def test_init_raises_on_symlinked_kind_dir(tmp_path: Path) -> None:
    """``init`` refuses a symlinked kind directory and writes nothing through it."""
    outside = tmp_path / "outside"
    outside.mkdir()
    base = tmp_path / "docs/intake"
    base.mkdir(parents=True)
    (base / "lessons").symlink_to(outside)

    with pytest.raises(ValueError, match="symlink"):
        intake.init(tmp_path, base)

    assert list(outside.iterdir()) == []


@pytest.mark.parametrize(
    "legacy_text",
    [
        "Intro prose.\n\n## A\nbody\n",
        "## \nbody\n\n## Real\nx\n",
    ],
    ids=["prose-before-first-heading", "empty-heading"],
)
def test_migration_leaves_file_untouched_when_text_would_be_lost(
    tmp_path: Path, legacy_text: str
) -> None:
    """Text no entry would carry blocks migration: file kept, nothing written.

    Args:
        legacy_text: Legacy file content with unmigratable text.
    """
    legacy = tmp_path / "docs/lessons.md"
    legacy.parent.mkdir()
    legacy.write_text(legacy_text, encoding="utf-8")
    base = tmp_path / "docs/intake"

    report = intake.migrate_legacy_lessons(tmp_path, base)

    assert report is not None
    assert "left in place" in report
    assert legacy.read_text(encoding="utf-8") == legacy_text
    assert not base.exists()


def test_migration_keeps_heading_inside_code_fence_as_body(tmp_path: Path) -> None:
    """A ``## `` line inside a fenced block is body text, not a new lesson."""
    legacy = tmp_path / "docs/lessons.md"
    legacy.parent.mkdir()
    legacy.write_text("## A\n```\n## not a heading\n```\nafter\n", encoding="utf-8")
    base = tmp_path / "docs/intake"

    intake.migrate_legacy_lessons(tmp_path, base)

    files = sorted((base / "lessons").glob("*.md"))
    assert [f.name for f in files] == ["a.md"]
    assert "## not a heading" in files[0].read_text(encoding="utf-8")


def test_migration_ignores_legacy_file_under_symlinked_docs(tmp_path: Path) -> None:
    """A symlinked ``docs/`` is not followed; the real file stays untouched."""
    real = tmp_path / "real_docs"
    real.mkdir()
    (real / "lessons.md").write_text(LEGACY, encoding="utf-8")
    (tmp_path / "docs").symlink_to(real)
    base = tmp_path / "intake"

    report = intake.migrate_legacy_lessons(tmp_path, base)

    assert report is None
    assert (real / "lessons.md").read_text(encoding="utf-8") == LEGACY
    assert not base.exists()


@pytest.mark.parametrize(
    ("folder", "name"),
    [
        ("lessons", "_TEMPLATE.md"),
        ("findings", "_TEMPLATE.md"),
        ("findings", "README.md"),
        ("findings", "notes.txt"),
    ],
)
def test_retire_refuses_template_readme_and_non_markdown(
    intake_dir: Path, folder: str, name: str
) -> None:
    """Only real entry files can be retired; the rest of the folder is protected.

    Args:
        folder: Kind directory holding the refused file.
        name: The refused file name.
    """
    good = _two_findings(intake_dir)[0]
    bad = intake_dir / folder / name
    bad.write_text("# X\n", encoding="utf-8")

    with pytest.raises(ValueError, match="not an entry"):
        intake.retire(intake_dir, [good, bad], ISSUE_URL)

    assert good.exists()
    assert bad.exists()
    assert not (intake_dir / "_LEDGER.md").exists()


def test_retire_refuses_symlinked_ledger(intake_dir: Path, tmp_path: Path) -> None:
    """A symlinked ledger is not written through and no entry is deleted."""
    paths = _two_findings(intake_dir)
    target = tmp_path / "elsewhere.md"
    target.write_text("keep\n", encoding="utf-8")
    (intake_dir / "_LEDGER.md").symlink_to(target)

    with pytest.raises(ValueError, match="symlink"):
        intake.retire(intake_dir, paths, ISSUE_URL)

    assert target.read_text(encoding="utf-8") == "keep\n"
    assert all(p.exists() for p in paths)


def test_retire_renders_title_as_sanitised_code_span(intake_dir: Path) -> None:
    """Backticks and control characters in a title cannot break out of the span."""
    paths = [
        write_entry(
            intake_dir,
            "lesson",
            "trap",
            "a `b`\x01c",
            seen="2026-01-01",
            branch="feat/a",
        )
    ]

    row = intake.retire(intake_dir, paths, ISSUE_URL)

    assert "| `a 'b' c` |" in row


@pytest.mark.parametrize(
    "line", ["see /home/bob", "see C:\\Users\\bob"], ids=["posix-at-eol", "windows"]
)
def test_check_flags_machine_path_variants_in_finding(
    intake_dir: Path, line: str
) -> None:
    """A path with no trailing slash, or a Windows one, still fails a finding.

    Args:
        line: Evidence line carrying the machine path.
    """
    write_entry(
        intake_dir,
        "finding",
        "bug",
        "Bug",
        body=EVIDENCE + f"\n{line}\n",
        **FINDING_FIELDS,
    )

    problems = intake.check(intake_dir, ())

    assert len(problems) == 1
    assert "machine-specific path" in problems[0]
