"""Tests for the verify-forge-repo-structure CLI public API.

# MOCKING STRATEGY: no mocks. The disk side of the check comes from git, so
# every test runs against a real throwaway git repo. One module-scoped
# template repo is built once and copied per test (cheaper than one
# ``git init`` per test); only ``main`` tests patch ``repo_root``.
"""

from __future__ import annotations

import logging
import re
import shutil
import sys
from typing import TYPE_CHECKING

import pytest

from forge.verify_repo_structure import (
    CANONICAL_OPENING,
    EXEMPT_NAMES,
    IGNORE_PATTERNS,
    SUMMARY_MARKER,
    main,
    parse_map,
    verify_structure,
)
from tests.conftest import commit_all, init_git_repo


if TYPE_CHECKING:
    from pathlib import Path


SECTION_FORGE = """\
## Forge Package (`src/forge/`)

- precommit.py: pre-commit dispatcher
"""

SECTION_CONFIG = """\
## Configuration Files

- README.md: main documentation
- REPO_STRUCTURE.md: this file
"""


def _build_map(*sections: str, opening: str = CANONICAL_OPENING) -> str:
    """Assemble a REPO_STRUCTURE.md body.

    Args:
        *sections: Markdown sections placed after the opening.
        opening: Text between the title and the first section.

    Returns:
        The markdown text.
    """
    parts = ["# Repo Structure", opening, *sections]
    return "\n\n".join(p for p in parts if p) + "\n"


IN_SYNC_MARKDOWN = _build_map(SECTION_FORGE, SECTION_CONFIG)


@pytest.fixture(scope="module")
def minimal_git_repo_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A committed git repo holding the files IN_SYNC_MARKDOWN describes.

    Returns:
        Path of the template repo; tests copy it, never modify it.
    """
    root = tmp_path_factory.mktemp("repo_template")
    init_git_repo(root)
    (root / "src" / "forge").mkdir(parents=True)
    (root / "src" / "forge" / "__init__.py").write_text("")
    (root / "src" / "forge" / "precommit.py").write_text("")
    (root / "README.md").write_text("")
    commit_all(root, "template")
    return root


@pytest.fixture
def repo(minimal_git_repo_template: Path, tmp_path: Path) -> Path:
    """A private copy of the template repo with IN_SYNC_MARKDOWN written.

    Returns:
        Root of the copied repo.
    """
    root = tmp_path / "repo"
    shutil.copytree(minimal_git_repo_template, root)
    (root / "REPO_STRUCTURE.md").write_text(IN_SYNC_MARKDOWN)
    return root


def _write_map(root: Path, content: str) -> None:
    """Replace the repo's REPO_STRUCTURE.md.

    Args:
        root: Repo root.
        content: New markdown content.
    """
    (root / "REPO_STRUCTURE.md").write_text(content)


def _add_file(root: Path, rel: str) -> None:
    """Create an empty file, making parent folders as needed.

    Args:
        root: Repo root.
        rel: Repo-relative path.
    """
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")


def _run_main(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> int:
    """Run the CLI ``main`` against *root* with INFO logs captured.

    Args:
        root: Repo root to verify.
        monkeypatch: Pytest monkeypatch fixture.
        caplog: Pytest log capture fixture.

    Returns:
        The exit code.
    """
    monkeypatch.setattr("forge.verify_repo_structure.repo_root", lambda: root)
    monkeypatch.setattr(sys, "argv", ["verify-forge-repo-structure"])
    with caplog.at_level(logging.INFO):
        return main()


def _messages(caplog: pytest.LogCaptureFixture) -> str:
    """Join every captured log message.

    Returns:
        All messages, newline-separated.
    """
    return "\n".join(record.getMessage() for record in caplog.records)


# --- in sync / missing ------------------------------------------------------


def test_matching_map_is_in_sync(repo: Path) -> None:
    """A map matching the tree yields no findings."""
    findings = verify_structure(repo)
    assert findings.in_sync
    assert findings.strict_folders == ("src/forge",)


def test_listed_file_absent_from_disk_is_missing(repo: Path) -> None:
    """A listed file that does not exist is reported as missing."""
    _write_map(
        repo,
        _build_map(SECTION_FORGE + "- ghost.py: not on disk\n", SECTION_CONFIG),
    )
    findings = verify_structure(repo)
    assert any("src/forge/ghost.py" in m for m in findings.missing)


def test_deleted_listed_markdown_file_is_missing(repo: Path) -> None:
    """A listed ``.md`` file removed from the tree is reported missing."""
    _add_file(repo, "agents/design-checker.md")
    section = "## Agents (`agents/`)\n\n- design-checker.md: reviewer\n"
    _write_map(repo, _build_map(SECTION_FORGE, section, SECTION_CONFIG))
    assert verify_structure(repo).in_sync
    (repo / "agents" / "design-checker.md").unlink()
    findings = verify_structure(repo)
    assert any("agents/design-checker.md" in m for m in findings.missing)


def test_sibling_folder_bullets_do_not_nest() -> None:
    """``- a/:`` then ``- b/:`` at one indent resolve as siblings."""
    content = _build_map("## Skills (`skills/`)\n\n- a/: first\n- b/: second\n")
    paths = [b.path for b in parse_map(content).bullets]
    assert paths == ["skills/a", "skills/b"]


def test_indented_bullet_nests_under_folder_bullet() -> None:
    """A deeper-indented bullet resolves under the folder bullet above it."""
    content = _build_map("## Skills (`skills/`)\n\n- a/: first\n  - x.md: inner\n")
    paths = [b.path for b in parse_map(content).bullets]
    assert paths == ["skills/a", "skills/a/x.md"]


# --- unlisted entries -------------------------------------------------------


def test_unlisted_file_in_headed_folder_fails(repo: Path) -> None:
    """A file in a headed folder that the map does not name is unlisted."""
    _add_file(repo, "src/forge/extra.py")
    findings = verify_structure(repo)
    assert not findings.in_sync
    assert "src/forge/extra.py" in findings.unlisted


def test_summary_marked_section_is_not_strict(repo: Path) -> None:
    """``<!-- summary -->`` opts a headed folder out of the full listing."""
    _add_file(repo, "src/forge/extra.py")
    section = SECTION_FORGE.replace("`)", "`) <!-- summary -->", 1)
    _write_map(repo, _build_map(section, SECTION_CONFIG))
    findings = verify_structure(repo)
    assert findings.in_sync
    assert findings.strict_folders == ()


def test_bullet_only_subfolder_contents_not_checked(repo: Path) -> None:
    """A subfolder named only as a bullet counts as one entry."""
    _add_file(repo, "src/forge/sub/a.py")
    _add_file(repo, "src/forge/sub/b.py")
    section = SECTION_FORGE + "- sub/: a subpackage\n"
    _write_map(repo, _build_map(section, SECTION_CONFIG))
    assert verify_structure(repo).in_sync


def test_unlisted_subfolder_in_headed_folder_fails(repo: Path) -> None:
    """A subfolder the map never names is unlisted."""
    _add_file(repo, "src/forge/sub/a.py")
    assert "src/forge/sub" in verify_structure(repo).unlisted


def test_exempt_entries_need_no_listing(repo: Path) -> None:
    """Ignored, hidden, ``__init__.py`` and ``conftest.py`` are never required."""
    (repo / ".gitignore").write_text("ignored.py\n")
    _add_file(repo, "src/forge/ignored.py")
    _add_file(repo, "src/forge/.hidden")
    _add_file(repo, "src/forge/conftest.py")
    _add_file(repo, "src/forge/__pycache__/x.pyc")
    assert verify_structure(repo).in_sync


def test_must_document_top_level_item_is_required(repo: Path) -> None:
    """A MUST_DOCUMENT top-level folder the map never mentions is unlisted."""
    _add_file(repo, "docs/guide.md")
    assert "docs" in verify_structure(repo).unlisted


def test_leftover_exhaustive_marker_is_harmless(repo: Path) -> None:
    """A retired ``<!-- exhaustive -->`` marker is just a comment."""
    section = SECTION_FORGE.replace("`)", "`) <!-- exhaustive -->", 1)
    _write_map(repo, _build_map(section, SECTION_CONFIG))
    assert verify_structure(repo).in_sync


# --- opening and prose ------------------------------------------------------


def test_extra_prose_before_first_section_fails(repo: Path) -> None:
    """Anything beyond the canonical sentence in the opening is a violation."""
    _write_map(
        repo,
        _build_map(
            SECTION_FORGE,
            SECTION_CONFIG,
            opening=CANONICAL_OPENING + "\n\nThis repo is great.",
        ),
    )
    findings = verify_structure(repo)
    assert any("opening" in v for v in findings.violations)


def test_missing_opening_fails(repo: Path) -> None:
    """A map with no opening sentence at all is a violation."""
    _write_map(repo, _build_map(SECTION_FORGE, SECTION_CONFIG, opening=""))
    assert not verify_structure(repo).in_sync


def test_canonical_opening_alone_passes(repo: Path) -> None:
    """The canonical sentence, wrapped across lines, passes."""
    wrapped = CANONICAL_OPENING.replace(", ", ",\n", 1)
    _write_map(repo, _build_map(SECTION_FORGE, SECTION_CONFIG, opening=wrapped))
    assert verify_structure(repo).in_sync


def test_prose_in_section_without_folder_path_fails(repo: Path) -> None:
    """A prose paragraph under a heading with no folder path is a violation."""
    section = SECTION_CONFIG + "\nThese files configure the repo.\n"
    _write_map(repo, _build_map(SECTION_FORGE, section))
    findings = verify_structure(repo)
    assert any("prose" in v for v in findings.violations)


# --- stale names ------------------------------------------------------------


def test_stale_backticked_name_in_description_fails(repo: Path) -> None:
    """A backticked file that does not exist is a stale name."""
    section = SECTION_FORGE + "- other.py: wraps `ghost.py` for callers\n"
    _add_file(repo, "src/forge/other.py")
    _write_map(repo, _build_map(section, SECTION_CONFIG))
    findings = verify_structure(repo)
    assert any("ghost.py" in s for s in findings.stale_names)


def test_existing_backticked_name_passes(repo: Path) -> None:
    """A backticked name resolving under the section folder is fine."""
    section = SECTION_FORGE.replace(
        "pre-commit dispatcher",
        "see `__init__.py` and `README.md`",
    )
    _write_map(repo, _build_map(section, SECTION_CONFIG))
    assert verify_structure(repo).in_sync


def test_cli_names_and_config_keys_are_not_paths(repo: Path) -> None:
    """CLI names, dotted config keys and globs are never checked as paths."""
    section = SECTION_FORGE.replace(
        "pre-commit dispatcher",
        "runs `forge-precommit`, reads `tool.forge.step` and `*.toml`, "
        "`--only` and `a=b`",
    )
    _write_map(repo, _build_map(section, SECTION_CONFIG))
    assert verify_structure(repo).in_sync


def test_names_under_ignored_roots_are_skipped(repo: Path) -> None:
    """Paths under ``code_health/`` and ``.plan/`` are not checked."""
    section = SECTION_FORGE.replace(
        "pre-commit dispatcher",
        "writes `code_health/x.log` and `.plan/CONTINUATION.md`",
    )
    _write_map(repo, _build_map(section, SECTION_CONFIG))
    assert verify_structure(repo).in_sync


# --- containment ------------------------------------------------------------


def test_traversal_heading_cannot_escape_repo(
    minimal_git_repo_template: Path,
    tmp_path: Path,
) -> None:
    """A ``../`` heading is a violation and leaks nothing from outside.

    The outside folder genuinely exists next to the repo, so only the
    containment guard can be what keeps its files out of the findings.
    """
    root = tmp_path / "a" / "b"
    shutil.copytree(minimal_git_repo_template, root)
    _add_file(tmp_path / "a", "outside/leaked.sh")
    escape = "## Escape (`../outside/`)\n\n- one.sh: whatever\n"
    _write_map(root, _build_map(SECTION_FORGE, escape, SECTION_CONFIG))
    findings = verify_structure(root)
    assert any("escapes" in v for v in findings.violations)
    assert not any("leaked" in u for u in findings.unlisted)
    assert "../outside" not in findings.strict_folders


# --- CLI --------------------------------------------------------------------


def test_main_returns_zero_and_prints_both_counts_when_in_sync(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A pass exits 0 and still prints both drift counts."""
    assert _run_main(repo, monkeypatch, caplog) == 0
    text = _messages(caplog)
    assert "Listed but missing: 0" in text
    assert "On disk but unlisted: 0" in text
    assert "Paths checked" not in text


def test_main_returns_one_and_logs_drift(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Drift exits 1, names the unlisted file, and prints the fix text."""
    _add_file(repo, "src/forge/extra.py")
    assert _run_main(repo, monkeypatch, caplog) == 1
    text = _messages(caplog)
    assert "DRIFT DETECTED" in text
    assert "src/forge/extra.py" in text
    assert "<!-- summary -->" in text


def test_main_prints_canonical_sentence_on_opening_violation(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An opening violation prints the exact sentence to paste."""
    _write_map(repo, _build_map(SECTION_FORGE, SECTION_CONFIG, opening="Hello."))
    assert _run_main(repo, monkeypatch, caplog) == 1
    assert CANONICAL_OPENING in _messages(caplog)


def test_main_log_carries_no_raw_control_character_from_map_names(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A stale backticked name holding an ANSI escape is logged inert.

    This covers the map's own backticked tokens; the filename channel is
    covered by the unlisted-file test below.
    """
    section = SECTION_FORGE.replace(
        "pre-commit dispatcher",
        "wraps `ghost\x1b\x07pwned.py`",
    )
    _write_map(repo, _build_map(section, SECTION_CONFIG))
    assert _run_main(repo, monkeypatch, caplog) == 1
    text = _messages(caplog)
    assert "ghost" in text
    assert text.count("RESULT:") == 1
    assert not [ch for ch in text if ch != "\n" and (ord(ch) < 0x20 or ord(ch) == 0x7F)]


def test_main_reports_unlisted_files_git_would_quote_sanitized(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Tracked control-character and non-ASCII names are still seen as unlisted.

    SCENARIO: a strict folder gains two tracked, unlisted files whose names
    plain `git ls-files` would C-quote, hiding their folder prefix.
    EXPECTED BEHAVIOR: both are reported under "on disk but unlisted", the
    log holds no raw control character, and RESULT appears once.
    """
    _add_file(repo, "src/forge/ctl\x1bname.py")
    _add_file(repo, "src/forge/café.py")
    commit_all(repo, "add quoted names")
    assert _run_main(repo, monkeypatch, caplog) == 1
    text = _messages(caplog)
    assert "café.py" in text
    assert "ctl" in text
    assert "On disk but unlisted: 2" in text
    assert text.count("RESULT:") == 1
    assert not [ch for ch in text if ch != "\n" and (ord(ch) < 0x20 or ord(ch) == 0x7F)]


def test_canonical_opening_names_every_exemption() -> None:
    """The map's opening names each exemption the check actually applies."""
    literal = re.compile(r"\^([A-Za-z0-9_]+)\$")
    folder_names = {
        m.group(1)
        for pattern in IGNORE_PATTERNS
        if (m := literal.fullmatch(pattern)) and not m.group(1).startswith("__")
    }
    assert {"build", "dist", "tmp", "code_health"} <= folder_names
    required = folder_names | EXEMPT_NAMES | {SUMMARY_MARKER}
    missing = {name for name in required if name not in CANONICAL_OPENING}
    assert not missing


def test_main_returns_one_when_repo_structure_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Exit code is 1 and an error is logged when REPO_STRUCTURE.md is absent."""
    assert _run_main(tmp_path, monkeypatch, caplog) == 1
    assert "REPO_STRUCTURE.md not found" in _messages(caplog)
