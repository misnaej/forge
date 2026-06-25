"""Tests for ``forge.install_readme_badges``."""

from __future__ import annotations

from typing import TYPE_CHECKING

from forge import install_readme_badges as rb


if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_python_badge_from_requires_python() -> None:
    """The Python badge takes its floor from ``requires-python``."""
    badge = rb._python_badge({"project": {"requires-python": ">=3.11"}})
    assert badge is not None
    assert "python-3.11%2B-blue" in badge


def test_license_badge_table_and_string_forms() -> None:
    """Both ``license = "MIT"`` and ``{ text = "MIT" }`` yield a badge."""
    assert "License-MIT-green" in (
        rb._license_badge({"project": {"license": "MIT"}}) or ""
    )
    table = {"project": {"license": {"text": "Apache-2.0"}}}
    assert "License-Apache--2.0-green" in (rb._license_badge(table) or "")


def test_license_badge_none_when_absent() -> None:
    """No declared license → no badge."""
    assert rb._license_badge({"project": {}}) is None


def test_coverage_badge_only_when_svg_present(tmp_path: Path) -> None:
    """The local docstring-coverage SVG is referenced only when it exists."""
    assert rb._coverage_badge(tmp_path) is None
    badges = tmp_path / ".badges"
    badges.mkdir()
    (badges / "docstring-coverage.svg").write_text("<svg/>")
    assert (
        rb._coverage_badge(tmp_path)
        == "![Docstring coverage](.badges/docstring-coverage.svg)"
    )


def test_git_remote_slug_parses_ssh_and_https(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The owner/repo slug is parsed from SSH and HTTPS origin URLs."""
    monkeypatch.setattr(
        rb, "run_git", lambda *_a, **_k: "git@github.com:acme/widget.git"
    )
    assert rb._git_remote_slug(tmp_path) == "acme/widget"
    monkeypatch.setattr(
        rb, "run_git", lambda *_a, **_k: "https://github.com/acme/widget"
    )
    assert rb._git_remote_slug(tmp_path) == "acme/widget"


def test_inject_creates_block_after_h1() -> None:
    """With no existing markers, the block is inserted after the first H1."""
    out = rb.inject("# Title\n\nbody\n", rb.render_block(["![x](y)"]))
    lines = out.splitlines()
    assert lines[0] == "# Title"
    assert rb._START in out
    assert rb._END in out
    assert "body" in out
    assert out.index("# Title") < out.index(rb._START) < out.index("body")


def test_inject_replaces_existing_block_preserving_prose() -> None:
    """Re-running replaces only the managed block; outside prose is preserved."""
    original = f"# T\n\n{rb._START}\nOLD\n{rb._END}\n\nkeep me\n"
    out = rb.inject(original, rb.render_block(["![new](u)"]))
    assert "OLD" not in out
    assert "![new](u)" in out
    assert "keep me" in out
    assert out.count(rb._START) == 1


def test_main_skips_when_not_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without ``[tool.forge.badges] enabled = true`` the CLI is a no-op (exit 0)."""
    (tmp_path / "README.md").write_text("# T\n")
    monkeypatch.setattr(rb, "get_repo_root", lambda: tmp_path)
    monkeypatch.setattr("sys.argv", ["install-forge-readme-badges"])
    assert rb.main() == 0
    assert rb._START not in (tmp_path / "README.md").read_text()


def test_main_writes_block_when_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When enabled, the badge block is written into the README."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nrequires-python = ">=3.11"\nlicense = "MIT"\n'
        "[tool.forge.badges]\nenabled = true\n"
    )
    (tmp_path / "README.md").write_text("# Demo\n\nIntro.\n")
    monkeypatch.setattr(rb, "get_repo_root", lambda: tmp_path)
    monkeypatch.setattr(rb, "_git_remote_slug", lambda _root: None)  # no CI badge
    monkeypatch.setattr("sys.argv", ["install-forge-readme-badges"])
    assert rb.main() == 0
    text = (tmp_path / "README.md").read_text()
    assert rb._START in text
    assert rb._END in text
    assert "License-MIT-green" in text
    assert "Intro." in text


def test_main_check_reports_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--check returns 1 when the block is missing/stale, writing nothing."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nlicense = "MIT"\n[tool.forge.badges]\nenabled = true\n'
    )
    (tmp_path / "README.md").write_text("# Demo\n")
    monkeypatch.setattr(rb, "get_repo_root", lambda: tmp_path)
    monkeypatch.setattr(rb, "_git_remote_slug", lambda _root: None)
    monkeypatch.setattr("sys.argv", ["install-forge-readme-badges", "--check"])
    assert rb.main() == 1
    assert rb._START not in (tmp_path / "README.md").read_text()


def test_ci_badge_rejects_non_bare_workflow(tmp_path: Path) -> None:
    """A workflow override with a path separator / traversal is ignored.

    Prevents the `is_file` probe from becoming a filesystem-existence oracle
    and a raw path from reaching the badge URL.
    """
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "ci.yml").write_text("on: push\n")
    # A bare, existing filename works.
    assert rb._ci_badge(tmp_path, "a/b", "ci.yml") is not None
    # Path-separator / traversal forms are rejected (None), even if a file exists.
    assert rb._ci_badge(tmp_path, "a/b", "../workflows/ci.yml") is None
    assert rb._ci_badge(tmp_path, "a/b", "/etc/passwd") is None


def test_main_refuses_readme_escaping_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `[tool.forge.badges] readme` that escapes the repo is refused (exit 1)."""
    (tmp_path / "pyproject.toml").write_text(
        '[tool.forge.badges]\nenabled = true\nreadme = "../outside.md"\n'
    )
    (tmp_path.parent / "outside.md").write_text("# Outside\n")
    monkeypatch.setattr(rb, "get_repo_root", lambda: tmp_path)
    monkeypatch.setattr("sys.argv", ["install-forge-readme-badges"])
    assert rb.main() == 1
    assert rb._START not in (tmp_path.parent / "outside.md").read_text()


def test_inject_single_blank_when_h1_has_no_blank(tmp_path: Path) -> None:
    """Inserting after an H1 with no following blank line doesn't double-blank."""
    out = rb.inject("# Title\nbody\n", rb.render_block(["![x](y)"]))
    assert out == f"# Title\n\n{rb._START}\n![x](y)\n{rb._END}\n\nbody\n"
