"""Tests for forge.config."""

from __future__ import annotations

from typing import TYPE_CHECKING

from forge.config import (
    DEFAULT_BASE_BRANCH,
    DEFAULT_DEV_BRANCH,
    ForgeConfig,
    detect_source_dirs,
    detect_test_dirs,
    filter_excluded,
    filter_under_roots,
    load_config,
    read_pyproject_raw,
    resolve_tool_roots,
)


if TYPE_CHECKING:
    from pathlib import Path


def test_default_is_single_track() -> None:
    """Default ForgeConfig has base == dev so dual_track is False.

    Back-compat guarantee for consumer repos without ``[tool.forge]``.
    """
    cfg = ForgeConfig()
    assert cfg.base_branch == DEFAULT_BASE_BRANCH
    assert cfg.dev_branch == DEFAULT_DEV_BRANCH
    assert cfg.dual_track is False


def test_dual_track_flag_flips_when_branches_differ() -> None:
    """dual_track flips True the moment dev_branch differs from base_branch."""
    cfg = ForgeConfig(base_branch="main", dev_branch="dev")
    assert cfg.dual_track is True


def test_load_config_missing_pyproject_returns_defaults(tmp_path: Path) -> None:
    """No ``pyproject.toml`` → single-track defaults (with a src-layout repo)."""
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    cfg = load_config(tmp_path)
    assert cfg == ForgeConfig()


def test_load_config_pyproject_without_tool_forge(tmp_path: Path) -> None:
    """``pyproject.toml`` exists but lacks ``[tool.forge]`` → defaults."""
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "foo"\nversion = "0.1.0"\n',
    )
    cfg = load_config(tmp_path)
    assert cfg == ForgeConfig()


def test_load_config_reads_dual_track_block(tmp_path: Path) -> None:
    """``[tool.forge]`` with explicit branch names is parsed correctly."""
    (tmp_path / "pyproject.toml").write_text(
        '[tool.forge]\nbase_branch = "main"\ndev_branch = "dev"\n',
    )
    cfg = load_config(tmp_path)
    assert cfg.base_branch == "main"
    assert cfg.dev_branch == "dev"
    assert cfg.dual_track is True


def test_load_config_custom_branch_names(tmp_path: Path) -> None:
    """Custom branch names (e.g. ``master`` / ``next``) round-trip."""
    (tmp_path / "pyproject.toml").write_text(
        '[tool.forge]\nbase_branch = "master"\ndev_branch = "next"\n',
    )
    cfg = load_config(tmp_path)
    assert cfg.base_branch == "master"
    assert cfg.dev_branch == "next"
    assert cfg.dual_track is True


def test_load_config_malformed_toml_falls_back_to_defaults(tmp_path: Path) -> None:
    """A broken ``pyproject.toml`` degrades to defaults — never raises.

    Config reads happen in hot paths (hooks, agents, pre-commit) so a
    parse failure must not block the workflow.
    """
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "pyproject.toml").write_text("not [ valid toml @@@")
    cfg = load_config(tmp_path)
    assert cfg == ForgeConfig()


def test_load_config_partial_block_uses_defaults(tmp_path: Path) -> None:
    """``[tool.forge]`` with only one of the two keys → other defaults."""
    (tmp_path / "pyproject.toml").write_text(
        '[tool.forge]\ndev_branch = "trunk"\n',
    )
    cfg = load_config(tmp_path)
    assert cfg.base_branch == DEFAULT_BASE_BRANCH
    assert cfg.dev_branch == "trunk"


def test_load_config_default_layout_dirs(tmp_path: Path) -> None:
    """No ``[tool.forge]`` → layout is smart-detected from disk.

    With a src-layout repo, ``source_dirs`` resolves to ``["src"]`` and
    ``test_dirs`` to ``["tests"]`` — the existing dirs, not a fixed guess.
    """
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    cfg = load_config(tmp_path)
    assert cfg.source_dirs == ["src"]
    assert cfg.test_dirs == ["tests"]


def test_load_config_smart_detects_packages_without_src(tmp_path: Path) -> None:
    """Without ``src/``, source_dirs smart-detects top-level packages."""
    pkg = tmp_path / "mypkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (tmp_path / "test").mkdir()  # singular test dir
    cfg = load_config(tmp_path)
    assert cfg.source_dirs == ["mypkg"]
    assert cfg.test_dirs == ["test"]


def test_load_config_reads_layout_dirs(tmp_path: Path) -> None:
    """``source_dirs`` / ``test_dirs`` override the repo-layout defaults."""
    (tmp_path / "pyproject.toml").write_text(
        '[tool.forge]\nsource_dirs = ["src", "projects"]\ntest_dirs = ["t"]\n'
    )
    cfg = load_config(tmp_path)
    assert cfg.source_dirs == ["src", "projects"]
    assert cfg.test_dirs == ["t"]


def test_read_pyproject_raw_returns_full_dict(tmp_path: Path) -> None:
    """The shared raw reader returns the whole parsed TOML tree."""
    (tmp_path / "pyproject.toml").write_text(
        '[tool.forge]\nbase_branch = "main"\n\n[tool.interrogate]\nfail-under = 90\n'
    )
    data = read_pyproject_raw(tmp_path)
    assert data["tool"]["forge"]["base_branch"] == "main"
    assert data["tool"]["interrogate"]["fail-under"] == 90


def test_read_pyproject_raw_empty_on_missing_or_malformed(tmp_path: Path) -> None:
    """Missing file and malformed TOML both degrade to ``{}`` (never raise)."""
    assert read_pyproject_raw(tmp_path) == {}
    (tmp_path / "pyproject.toml").write_text("not [ valid toml @@@")
    assert read_pyproject_raw(tmp_path) == {}


# ---------------------------------------------------------------------------
# Smart-detect + resolve_tool_roots (the shared source-dir resolution)
# ---------------------------------------------------------------------------


def _forge_toml(tmp_path: Path, body: str) -> None:
    """Write a ``[tool.forge]`` block to tmp_path's pyproject.toml.

    Args:
        tmp_path: Repo root to write into.
        body: TOML lines under ``[tool.forge]`` (subtables allowed).
    """
    (tmp_path / "pyproject.toml").write_text(
        f"[tool.forge]\n{body}\n", encoding="utf-8"
    )


def test_detect_source_dirs_prefers_src(tmp_path: Path) -> None:
    """src/ wins when present (src-layout)."""
    (tmp_path / "src").mkdir()
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("")
    assert detect_source_dirs(tmp_path) == ["src"]


def test_detect_source_dirs_falls_back_to_packages(tmp_path: Path) -> None:
    """Without src/, top-level importable packages are detected."""
    for name in ("alpha", "beta"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "__init__.py").write_text("")
    (tmp_path / "notapkg").mkdir()
    assert detect_source_dirs(tmp_path) == ["alpha", "beta"]


def test_detect_source_dirs_empty_when_nothing(tmp_path: Path) -> None:
    """No src/ and no packages → empty (caller decides to skip)."""
    assert detect_source_dirs(tmp_path) == []


def test_detect_test_dirs_prefers_tests_then_test(tmp_path: Path) -> None:
    """tests/ preferred; test/ accepted; both → tests first."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "test").mkdir()
    assert detect_test_dirs(tmp_path) == ["tests", "test"]


def test_resolve_tool_roots_granular_wins(tmp_path: Path) -> None:
    """[tool.forge.<tool>].paths overrides source_dirs and auto-detect."""
    (tmp_path / "src").mkdir()
    (tmp_path / "only").mkdir()
    _forge_toml(tmp_path, 'source_dirs = ["src"]\n[tool.forge.ruff]\npaths = ["only"]')
    assert resolve_tool_roots(tmp_path, "ruff", include_tests=True) == ["only"]


def test_resolve_tool_roots_honors_source_dirs(tmp_path: Path) -> None:
    """With no granular key, [tool.forge].source_dirs (+test_dirs) is used."""
    for name in ("lib", "extra", "t"):
        (tmp_path / name).mkdir()
    _forge_toml(tmp_path, 'source_dirs = ["lib", "extra"]\ntest_dirs = ["t"]')
    assert resolve_tool_roots(tmp_path, "ruff", include_tests=True) == [
        "lib",
        "extra",
        "t",
    ]
    # Source-only tools drop the test dir.
    assert resolve_tool_roots(tmp_path, "api_digest") == ["lib", "extra"]


def test_resolve_tool_roots_smart_detect_default(tmp_path: Path) -> None:
    """Neither granular nor source_dirs set → smart auto-detect."""
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    assert resolve_tool_roots(tmp_path, "ruff", include_tests=True) == ["src", "tests"]
    assert resolve_tool_roots(tmp_path, "api_digest") == ["src"]


def test_resolve_tool_roots_drops_repo_escaping_paths(tmp_path: Path) -> None:
    """A configured path escaping the repo is dropped (traversal guard)."""
    (tmp_path / "src").mkdir()
    _forge_toml(tmp_path, '[tool.forge.ruff]\npaths = ["../evil", "/etc", "src"]')
    assert resolve_tool_roots(tmp_path, "ruff", include_tests=True) == ["src"]


def test_resolve_tool_roots_drops_option_like_and_blank_paths(tmp_path: Path) -> None:
    """Option-like / blank configured paths are dropped (flag-injection guard).

    A value like ``--output=x`` would be parsed as a flag by the consuming
    tool (ruff/pytest/pyrefly) and an empty string scans the cwd; both must
    never reach the subprocess argv.
    """
    (tmp_path / "src").mkdir()
    _forge_toml(tmp_path, '[tool.forge.ruff]\npaths = ["--output=x", "-rf", "", "src"]')
    assert resolve_tool_roots(tmp_path, "ruff", include_tests=True) == ["src"]


# ---------------------------------------------------------------------------
# filter_under_roots (issue #83 — source-tree scoping for whole-tree steps)
# ---------------------------------------------------------------------------


def test_filter_under_roots_keeps_file_inside_root() -> None:
    """A file living under a declared root is kept."""
    assert filter_under_roots(["src/foo.py"], ["src"]) == ["src/foo.py"]


def test_filter_under_roots_drops_file_outside_all_roots() -> None:
    """A file outside every declared root is dropped."""
    assert filter_under_roots(["vendor/x.py"], ["src"]) == []


def test_filter_under_roots_no_partial_root_name_match() -> None:
    """``src`` must NOT admit ``src_extra/x.py`` — partial prefix is rejected."""
    assert filter_under_roots(["src_extra/x.py"], ["src"]) == []


def test_filter_under_roots_empty_roots_keeps_nothing() -> None:
    """An empty roots list keeps nothing, whatever the files."""
    assert filter_under_roots(["src/foo.py", "tests/test_foo.py"], []) == []


def test_filter_under_roots_exact_root_name_included() -> None:
    """A file whose path equals a root (no trailing slash) is kept."""
    assert filter_under_roots(["src"], ["src"]) == ["src"]


def test_filter_under_roots_trailing_slash_root_normalized() -> None:
    """A root supplied with a trailing slash behaves identically to one without."""
    assert filter_under_roots(["src/foo.py"], ["src/"]) == ["src/foo.py"]


def test_filter_under_roots_multiple_roots_any_match() -> None:
    """A file under ANY declared root is kept; one outside all is dropped."""
    result = filter_under_roots(
        ["src/a.py", "tests/test_a.py", "docs/conf.py"],
        ["src", "tests"],
    )
    assert result == ["src/a.py", "tests/test_a.py"]


def test_filter_under_roots_preserves_input_order() -> None:
    """Output order follows the input order, not the roots order."""
    files = ["tests/t.py", "src/a.py", "tests/u.py"]
    assert filter_under_roots(files, ["src", "tests"]) == files


def test_filter_under_roots_empty_files_empty_result() -> None:
    """Empty file list → empty output (no error)."""
    assert filter_under_roots([], ["src"]) == []


def test_filter_under_roots_nested_subdir_included() -> None:
    """A deeply nested file under a declared root is included."""
    assert filter_under_roots(["src/a/b/c/deep.py"], ["src"]) == ["src/a/b/c/deep.py"]


# ---------------------------------------------------------------------------
# filter_excluded (issue #83 — repo-wide glob exclude for whole-tree steps)
# ---------------------------------------------------------------------------


def test_filter_excluded_empty_globs_identity() -> None:
    """Empty globs → the exact input list object is returned (no-op / no copy)."""
    files = ["src/foo.py", "tests/test_foo.py"]
    assert filter_excluded(files, []) is files


def test_filter_excluded_bare_dir_excludes_subtree() -> None:
    """A bare directory name (``vendor``) excludes its whole subtree."""
    result = filter_excluded(["vendor/lib/x.py", "src/foo.py"], ["vendor"])
    assert result == ["src/foo.py"]


def test_filter_excluded_trailing_slash_glob_normalized() -> None:
    """A glob with a trailing slash (``vendor/``) behaves the same as ``vendor``."""
    result = filter_excluded(["vendor/x.py", "src/foo.py"], ["vendor/"])
    assert result == ["src/foo.py"]


def test_filter_excluded_fnmatch_extension_glob() -> None:
    """Fnmatch glob ``*.gen.py`` excludes matching files anywhere in the tree."""
    result = filter_excluded(
        ["src/foo.py", "src/auto.gen.py", "src/other.gen.py"],
        ["*.gen.py"],
    )
    assert result == ["src/foo.py"]


def test_filter_excluded_nested_double_star_glob() -> None:
    """``vendor/**`` glob excludes deeply nested paths via fnmatch."""
    result = filter_excluded(["vendor/pkg/a.py", "src/foo.py"], ["vendor/**"])
    assert result == ["src/foo.py"]


def test_filter_excluded_non_matching_files_kept() -> None:
    """Files that match no glob are kept unchanged."""
    result = filter_excluded(["src/a.py", "tests/t.py"], ["vendor"])
    assert result == ["src/a.py", "tests/t.py"]


def test_filter_excluded_no_partial_dir_name_match() -> None:
    """``vendor`` must NOT exclude ``vendor_extra/x.py`` — symmetric boundary."""
    result = filter_excluded(["vendor_extra/x.py"], ["vendor"])
    assert result == ["vendor_extra/x.py"]


def test_filter_excluded_file_named_same_as_dir_excluded() -> None:
    """A path that exactly equals the bare dir name is excluded."""
    result = filter_excluded(["vendor", "src/foo.py"], ["vendor"])
    assert result == ["src/foo.py"]


def test_filter_excluded_multiple_globs_any_match() -> None:
    """A file matching ANY single exclude glob is dropped."""
    result = filter_excluded(
        ["src/a.py", "src/b.gen.py", "vendor/x.py"],
        ["*.gen.py", "vendor"],
    )
    assert result == ["src/a.py"]


def test_filter_excluded_preserves_order_for_survivors() -> None:
    """Surviving files appear in the same order as the input list."""
    files = ["src/z.py", "src/a.py", "tests/t.py"]
    assert filter_excluded(files, ["nothing"]) == files


def test_filter_excluded_empty_files_nonempty_globs_empty() -> None:
    """Empty file list + nonempty globs → empty output (no error)."""
    assert filter_excluded([], ["vendor", "*.gen.py"]) == []


# ---------------------------------------------------------------------------
# ForgeConfig.exclude field + load_config exclude loading
# ---------------------------------------------------------------------------


def test_forge_config_default_exclude_is_empty() -> None:
    """Bare ForgeConfig() has an empty exclude list — no patterns by default."""
    assert ForgeConfig().exclude == []


def test_load_config_reads_exclude_list(tmp_path: Path) -> None:
    """load_config reads [tool.forge].exclude as a list of glob strings."""
    (tmp_path / "pyproject.toml").write_text(
        '[tool.forge]\nexclude = ["vendor", "*.gen.py"]\n'
    )
    cfg = load_config(tmp_path)
    assert cfg.exclude == ["vendor", "*.gen.py"]


def test_load_config_exclude_absent_key_returns_empty(tmp_path: Path) -> None:
    """When exclude is absent from [tool.forge], cfg.exclude is []."""
    (tmp_path / "pyproject.toml").write_text("[tool.forge]\n")
    cfg = load_config(tmp_path)
    assert cfg.exclude == []


def test_load_config_exclude_non_list_value_behavior(tmp_path: Path) -> None:
    """Exclude = "vendor" (bare TOML string) is guarded and returns [] not char list.

    The scalar ``exclude = "vendor"`` (brackets forgotten) would otherwise
    iterate character-by-character into ``["v", "e", "n", ...]``. The guard
    in load_config rejects non-list values and degrades to no excludes.
    """
    (tmp_path / "pyproject.toml").write_text('[tool.forge]\nexclude = "vendor"\n')
    cfg = load_config(tmp_path)
    assert cfg.exclude == []
