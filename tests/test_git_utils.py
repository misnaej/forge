"""Tests for ``forge.git_utils``.

Covers the shared helpers used by every forge CLI: repo root resolution,
modified-file detection, output filtering, and CLI logging setup.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from typing import TYPE_CHECKING

import pytest

from forge import git_utils
from tests.conftest import (
    GIT_ENV as _GIT_ENV,
)
from tests.conftest import (
    _detach_head,
    commit_all,
    make_fake_run,
)
from tests.conftest import (
    init_git_repo as _init_git_repo,
)
from tests.conftest import (
    init_single_track_repo as _init_single_track_repo,
)


if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def _clear_repo_root_cache() -> None:
    """Reset the ``repo_root`` LRU cache between tests."""
    git_utils.repo_root.cache_clear()


# ---------------------------------------------------------------------------
# _parse_files
# ---------------------------------------------------------------------------


def test_parse_files_empty_output_returns_empty() -> None:
    """Empty input produces an empty list."""
    assert git_utils._parse_files("", suffix=".py", prefix=None) == []


def test_parse_files_filters_by_suffix() -> None:
    """Only files ending with the configured suffix survive."""
    output = "a.py\nb.txt\nc.py\n"
    assert git_utils._parse_files(output, suffix=".py", prefix=None) == ["a.py", "c.py"]


def test_parse_files_filters_by_single_prefix() -> None:
    """A string prefix keeps only matching paths."""
    output = "tests/foo.py\nsrc/bar.py\n"
    result = git_utils._parse_files(output, suffix=".py", prefix="tests/")
    assert result == ["tests/foo.py"]


def test_parse_files_filters_by_tuple_of_prefixes() -> None:
    """A tuple of prefixes accepts any matching layout (test/ OR tests/)."""
    output = "test/a.py\ntests/b.py\nsrc/c.py\n"
    result = git_utils._parse_files(output, suffix=".py", prefix=("test/", "tests/"))
    assert result == ["test/a.py", "tests/b.py"]


def test_parse_files_strips_whitespace_and_blank_lines() -> None:
    """Surrounding whitespace and blank lines are dropped."""
    output = "  a.py  \n\n  b.py\n"
    assert git_utils._parse_files(output, suffix=".py", prefix=None) == ["a.py", "b.py"]


# ---------------------------------------------------------------------------
# parse_semver
# ---------------------------------------------------------------------------


def test_parse_semver_bare_triple() -> None:
    """Plain ``X.Y.Z`` returns the integer tuple."""
    assert git_utils.parse_semver("1.2.3") == (1, 2, 3)


def test_parse_semver_v_prefix() -> None:
    """``v``-prefixed git tag is accepted."""
    assert git_utils.parse_semver("v1.2.3") == (1, 2, 3)


def test_parse_semver_tolerates_dev_suffix() -> None:
    """setuptools-scm editable suffixes do not break the leading-triple parse."""
    assert git_utils.parse_semver("1.2.11.dev3+g7e0cdd95b") == (1, 2, 11)


def test_parse_semver_tolerates_prerelease_and_build() -> None:
    """Semver `-rc1` / `+build` suffixes are stripped."""
    assert git_utils.parse_semver("v1.2.3-rc1") == (1, 2, 3)
    assert git_utils.parse_semver("1.2.3+build.42") == (1, 2, 3)


def test_parse_semver_rejects_non_triple() -> None:
    """Strings without a complete ``X.Y.Z`` prefix return ``None``."""
    assert git_utils.parse_semver("1.2") is None
    assert git_utils.parse_semver("v1.x.3") is None
    assert git_utils.parse_semver("") is None
    assert git_utils.parse_semver("not-a-version") is None


# ---------------------------------------------------------------------------
# next_version
# ---------------------------------------------------------------------------


def test_next_version_patch_bump() -> None:
    """A ``"patch"`` bump increments the trailing component."""
    assert git_utils.next_version("v1.2.3", "patch") == "v1.2.4"


def test_next_version_minor_bump_resets_patch() -> None:
    """A ``"minor"`` bump increments minor and resets patch to zero."""
    assert git_utils.next_version("v1.2.3", "minor") == "v1.3.0"


def test_next_version_major_bump_resets_minor_and_patch() -> None:
    """A ``"major"`` bump increments major and resets minor and patch to zero."""
    assert git_utils.next_version("v1.2.3", "major") == "v2.0.0"


def test_next_version_bare_triple_accepted() -> None:
    """A tag without the ``v`` prefix parses the same as a prefixed one."""
    assert git_utils.next_version("1.2.3", "patch") == "v1.2.4"


def test_next_version_none_tag_bases_at_zero() -> None:
    """``None`` (no prior release) is treated as a ``v0.0.0`` base."""
    assert git_utils.next_version(None, "minor") == "v0.1.0"


def test_next_version_unparseable_tag_bases_at_zero() -> None:
    """A tag ``parse_semver`` cannot read is treated as a ``v0.0.0`` base."""
    assert git_utils.next_version("not-a-version", "patch") == "v0.0.1"


def test_next_version_unknown_bump_raises_value_error() -> None:
    """An unrecognized *bump* name raises ``ValueError``."""
    with pytest.raises(ValueError, match="unknown bump"):
        git_utils.next_version("v1.2.3", "revision")


# ---------------------------------------------------------------------------
# Source-dir resolution moved to forge.config (smart-detect + resolver);
# see test_config.py. git_utils no longer owns a source-dir helper.


# ---------------------------------------------------------------------------
# repo_root
# ---------------------------------------------------------------------------


def test_repo_root_returns_toplevel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When git succeeds, repo_root returns the trimmed stdout as a Path."""
    fake_top = tmp_path / "repo"
    fake_top.mkdir()

    def _fake_run(cmd: list[str], **_kwargs: object) -> object:
        assert cmd[:3] == ["git", "rev-parse", "--show-toplevel"]
        return type("P", (), {"returncode": 0, "stdout": f"{fake_top}\n"})()

    monkeypatch.setattr(git_utils.subprocess, "run", _fake_run)
    assert git_utils.repo_root() == fake_top


def test_repo_root_exits_when_not_in_git_repo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Git failure (non-zero exit) raises SystemExit(1)."""
    monkeypatch.setattr(
        git_utils.subprocess,
        "run",
        lambda *_a, **_kw: type("P", (), {"returncode": 128, "stdout": ""})(),
    )
    with pytest.raises(SystemExit) as exc_info:
        git_utils.repo_root()
    assert exc_info.value.code == 1


def test_repo_root_is_cached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Repeated calls hit subprocess only once (lru_cache)."""
    calls = {"count": 0}

    def _fake_run(*_args: object, **_kwargs: object) -> object:
        calls["count"] += 1
        return type("P", (), {"returncode": 0, "stdout": f"{tmp_path}\n"})()

    monkeypatch.setattr(git_utils.subprocess, "run", _fake_run)
    git_utils.repo_root()
    git_utils.repo_root()
    git_utils.repo_root()
    assert calls["count"] == 1


# ---------------------------------------------------------------------------
# _run_git
# ---------------------------------------------------------------------------


def test_run_git_returns_stdout_on_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Success returns trimmed stdout."""

    def _fake_run(cmd: list[str], **_kwargs: object) -> object:
        if cmd[:3] == ["git", "rev-parse", "--show-toplevel"]:
            return type("P", (), {"returncode": 0, "stdout": f"{tmp_path}\n"})()
        return type("P", (), {"returncode": 0, "stdout": "main\n"})()

    monkeypatch.setattr(git_utils.subprocess, "run", _fake_run)
    assert git_utils._run_git("branch", "--show-current") == "main"


def test_run_git_returns_empty_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-zero exit produces an empty string (not raise)."""

    def _fake_run(cmd: list[str], **_kwargs: object) -> object:
        if cmd[:3] == ["git", "rev-parse", "--show-toplevel"]:
            return type("P", (), {"returncode": 0, "stdout": f"{tmp_path}\n"})()
        return type("P", (), {"returncode": 128, "stdout": "x\n", "stderr": "boom"})()

    monkeypatch.setattr(git_utils.subprocess, "run", _fake_run)
    assert git_utils._run_git("nope") == ""


# ---------------------------------------------------------------------------
# get_modified_files
# ---------------------------------------------------------------------------


def _stub_branch_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    current_branch: str,
    diff_outputs: dict[str, str],
    calls: list[tuple[list[str], object]] | None = None,
) -> None:
    r"""Stub subprocess so get_modified_files exercises a branch code path.

    Covers both the feature-branch path (``current_branch`` other than
    ``"main"``, exercising ``rev-parse --verify`` + the three merged diffs)
    and the ``main`` fallback path (``current_branch="main"``, exercising
    only the ``HEAD~1`` diff) with one shared fake.

    Args:
        monkeypatch: pytest fixture.
        tmp_path: Synthetic repo root, returned for an unstubbed
            ``rev-parse --show-toplevel`` (the cached global
            :func:`git_utils.repo_root` path, used when a test calls
            :func:`git_utils.get_modified_files` without ``repo_root=``).
        current_branch: Value returned by ``git branch --show-current``.
        diff_outputs: Maps the trailing arg of `git diff --name-only` to
            stdout (e.g., ``{"main...HEAD": "src/foo.py\\n"}`` on a feature
            branch, or ``{"HEAD~1": "src/x.py\\n"}`` on ``main``).
        calls: When given, every ``(cmd, cwd_kwarg)`` pair is appended —
            lets callers assert on the ``cwd`` each git invocation ran
            with (e.g. to verify an explicit ``repo_root=`` threads through).
    """

    def _fake_run(cmd: list[str], **kwargs: object) -> object:
        if calls is not None:
            calls.append((cmd, kwargs.get("cwd")))
        if cmd[:3] == ["git", "rev-parse", "--show-toplevel"]:
            return type("P", (), {"returncode": 0, "stdout": f"{tmp_path}\n"})()
        if cmd[1:3] == ["branch", "--show-current"]:
            return type("P", (), {"returncode": 0, "stdout": f"{current_branch}\n"})()
        if cmd[1:3] == ["rev-parse", "--verify"]:
            return type("P", (), {"returncode": 0, "stdout": "ok\n"})()
        if cmd[1:3] == ["diff", "--name-only"]:
            tail = cmd[-1] if len(cmd) > 3 else ""
            stdout = diff_outputs.get(tail, "")
            return type("P", (), {"returncode": 0, "stdout": stdout})()
        return type("P", (), {"returncode": 0, "stdout": ""})()

    monkeypatch.setattr(git_utils.subprocess, "run", _fake_run)


def test_get_modified_files_feature_branch_aggregates_three_diffs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On a feature branch, branch-commits + staged + unstaged are merged."""
    _stub_branch_path(
        monkeypatch,
        tmp_path,
        current_branch="feat/x",
        diff_outputs={
            "origin/main...HEAD": "src/a.py\n",
            "--cached": "src/b.py\n",
            # plain `git diff --name-only` (no trailing arg) → key is ""
            "": "src/c.py\nsrc/a.py\n",
        },
    )
    files = git_utils.get_modified_files()
    assert files == ["src/a.py", "src/b.py", "src/c.py"]


def test_get_modified_files_applies_prefix_tuple(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`prefix=("test/", "tests/")` accepts either layout in the diff output."""
    _stub_branch_path(
        monkeypatch,
        tmp_path,
        current_branch="feat/x",
        diff_outputs={
            "origin/main...HEAD": "test/old.py\ntests/new.py\nsrc/foo.py\n",
            "--cached": "",
            "": "",
        },
    )
    files = git_utils.get_modified_files(prefix=("test/", "tests/"))
    assert files == ["test/old.py", "tests/new.py"]


def test_get_modified_files_main_falls_back_to_head_prev(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On main, the previous-commit diff is used."""
    _stub_branch_path(
        monkeypatch,
        tmp_path,
        current_branch="main",
        diff_outputs={"HEAD~1": "src/x.py\n"},
    )
    assert git_utils.get_modified_files() == ["src/x.py"]


def test_get_modified_files_threads_repo_root_into_feature_branch_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit `repo_root=` threads through every feature-branch `_run_git` call.

    MOCK SETUP: shares `_stub_branch_path`'s feature-branch fake — `rev-parse
    --verify main` succeeds, so every diff variant (`base...HEAD`,
    `--cached`, plain) is exercised. `calls` captures each `(cmd, cwd)`
    pair to confirm every git invocation ran with the explicit `repo_root`
    rather than the cached process-wide :func:`git_utils.repo_root`.
    """
    other_root = tmp_path / "other"
    other_root.mkdir()
    calls: list[tuple[list[str], object]] = []
    _stub_branch_path(
        monkeypatch,
        tmp_path,
        current_branch="feat/x",
        diff_outputs={"origin/main...HEAD": "src/a.py\n", "--cached": "", "": ""},
        calls=calls,
    )
    files = git_utils.get_modified_files(repo_root=other_root)
    assert files == ["src/a.py"]
    assert calls
    assert all(cwd == other_root for _cmd, cwd in calls)
    assert not any(cmd[1:3] == ["rev-parse", "--show-toplevel"] for cmd, _cwd in calls)


def test_get_modified_files_threads_repo_root_into_main_fallback_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit `repo_root=` threads through the main-branch `HEAD~1` fallback call.

    MOCK SETUP: shares `_stub_branch_path`'s main-fallback fake; `calls`
    captures each `(cmd, cwd)` pair to confirm the `HEAD~1` diff ran with
    the explicit `repo_root`.
    """
    other_root = tmp_path / "other"
    other_root.mkdir()
    calls: list[tuple[list[str], object]] = []
    _stub_branch_path(
        monkeypatch,
        tmp_path,
        current_branch="main",
        diff_outputs={"HEAD~1": "src/x.py\n"},
        calls=calls,
    )
    files = git_utils.get_modified_files(repo_root=other_root)
    assert files == ["src/x.py"]
    head_prev_cwds = [
        cwd
        for cmd, cwd in calls
        if cmd[1:3] == ["diff", "--name-only"] and cmd[-1] == "HEAD~1"
    ]
    assert head_prev_cwds == [other_root]


def test_get_tracked_files_filters_suffix_and_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """get_tracked_files lists `git ls-files` filtered by suffix/prefix."""

    def _fake_run(cmd: list[str], **_kwargs: object) -> object:
        if cmd[:3] == ["git", "rev-parse", "--show-toplevel"]:
            return type("P", (), {"returncode": 0, "stdout": f"{tmp_path}\n"})()
        if cmd[1:2] == ["ls-files"]:
            stdout = "src/a.py\ntests/b.py\nREADME.md\nsrc/c.txt\n"
            return type("P", (), {"returncode": 0, "stdout": stdout})()
        return type("P", (), {"returncode": 0, "stdout": ""})()

    monkeypatch.setattr(git_utils.subprocess, "run", _fake_run)
    assert git_utils.get_tracked_files() == ["src/a.py", "tests/b.py"]
    assert git_utils.get_tracked_files(prefix=("tests/",)) == ["tests/b.py"]


def test_get_tracked_files_repo_root_kwarg_targets_explicit_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `repo_root` kwarg runs `git ls-files` in that dir, not cwd."""
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    _init_git_repo(repo)
    (repo / "src" / "a.py").write_text("")
    # `git ls-files` reads the index — staging is enough, no commit needed.
    subprocess.run(["git", "add", "-A"], cwd=repo, env=_GIT_ENV, check=True)

    monkeypatch.chdir(tmp_path)
    assert git_utils.get_tracked_files(repo_root=repo) == ["src/a.py"]


# ---------------------------------------------------------------------------
# get_untracked_files
# ---------------------------------------------------------------------------


def test_get_untracked_files_lists_untracked_non_ignored_file(tmp_path: Path) -> None:
    """An untracked, non-ignored file is listed."""
    _init_git_repo(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("")
    assert git_utils.get_untracked_files(repo_root=tmp_path) == ["src/a.py"]


def test_get_untracked_files_excludes_tracked_file(tmp_path: Path) -> None:
    """A file already staged in the index is not untracked."""
    _init_git_repo(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, env=_GIT_ENV, check=True)
    assert git_utils.get_untracked_files(repo_root=tmp_path) == []


def test_get_untracked_files_excludes_gitignored_file(tmp_path: Path) -> None:
    """A gitignored file is never listed, even though it is untracked."""
    _init_git_repo(tmp_path)
    (tmp_path / ".gitignore").write_text("ignored/\n")
    (tmp_path / "ignored").mkdir()
    (tmp_path / "ignored" / "c.py").write_text("")
    assert git_utils.get_untracked_files(repo_root=tmp_path) == []


def test_get_untracked_files_filters_suffix_and_prefix(tmp_path: Path) -> None:
    """get_untracked_files lists `git ls-files --others` filtered by suffix/prefix."""
    _init_git_repo(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "note.txt").write_text("")
    assert git_utils.get_untracked_files(repo_root=tmp_path) == ["src/a.py"]
    assert git_utils.get_untracked_files(repo_root=tmp_path, suffix=".txt") == [
        "docs/note.txt"
    ]
    assert git_utils.get_untracked_files(repo_root=tmp_path, prefix="src/") == [
        "src/a.py"
    ]


def test_get_untracked_files_repo_root_kwarg_targets_explicit_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `repo_root` kwarg runs `git ls-files --others` in that dir, not cwd."""
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    _init_git_repo(repo)
    (repo / "src" / "a.py").write_text("")

    monkeypatch.chdir(tmp_path)
    assert git_utils.get_untracked_files(repo_root=repo) == ["src/a.py"]


# ---------------------------------------------------------------------------
# emit + configure_cli_logging
# ---------------------------------------------------------------------------


def test_emit_writes_line_to_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    """Emit writes its arg plus a trailing newline to stdout."""
    git_utils.emit("hello world")
    assert capsys.readouterr().out == "hello world\n"


def test_configure_cli_logging_sets_info_level() -> None:
    """configure_cli_logging configures the root logger at INFO.

    basicConfig is a no-op when handlers already exist, so the test
    detaches existing handlers first and restores them after.
    """
    root = logging.getLogger()
    prior_level = root.level
    prior_handlers = root.handlers[:]
    root.handlers = []
    try:
        git_utils.configure_cli_logging()
        assert root.level == logging.INFO
    finally:
        root.setLevel(prior_level)
        root.handlers = prior_handlers


def test_configure_cli_logging_is_idempotent() -> None:
    """Calling configure_cli_logging twice is safe (handlers already attached)."""
    git_utils.configure_cli_logging()
    git_utils.configure_cli_logging()  # second call must not raise


def test_latest_v_tag_returns_highest_sorted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first line of the ``--sort=-v:refname`` output (highest) is returned."""
    monkeypatch.setattr(
        git_utils.subprocess,
        "run",
        make_fake_run(stdout="v1.21.0\nv1.20.2\nv1.20.0\n"),
    )
    assert git_utils.latest_v_tag(tmp_path) == "v1.21.0"


def test_latest_v_tag_none_when_no_tags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No ``v*`` tags → ``None``."""
    monkeypatch.setattr(git_utils.subprocess, "run", make_fake_run(stdout=""))
    assert git_utils.latest_v_tag(tmp_path) is None


# ---------------------------------------------------------------------------
# fetch_tags_best_effort (moved from test_precommit.py)
# ---------------------------------------------------------------------------


def test_fetch_tags_best_effort_success_returns_no_notes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A successful `git fetch --tags` refresh returns no degradation notes.

    MOCK SETUP: `subprocess.run` faked to capture its call args and report
    `returncode=0`, so the call contract (cwd, stdin, timeout, check) is
    asserted alongside the empty-notes return.
    """
    calls: list[tuple[list[str], dict[str, object]]] = []

    def _fake_subprocess_run(cmd: list[str], **kw: object) -> object:
        calls.append((list(cmd), kw))
        return type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(git_utils.subprocess, "run", _fake_subprocess_run)
    assert git_utils.fetch_tags_best_effort(tmp_path) == []
    assert len(calls) == 1
    cmd, kw = calls[0]
    assert cmd == ["git", "fetch", "--tags", "--quiet", "origin"]
    assert kw["cwd"] == tmp_path
    assert kw["stdin"] is subprocess.DEVNULL
    assert kw["timeout"] == 10
    assert kw["check"] is False


def test_fetch_tags_best_effort_failure_returns_stale_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-zero `git fetch --tags` exit degrades with a visible note."""

    def _fake_subprocess_run(*_a: object, **_kw: object) -> object:
        return type("P", (), {"returncode": 1, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(git_utils.subprocess, "run", _fake_subprocess_run)
    notes = git_utils.fetch_tags_best_effort(tmp_path)
    assert notes == [
        (
            "Note: `git fetch --tags` failed — validating against local "
            "tags, which may be stale."
        )
    ]


def test_fetch_tags_best_effort_timeout_returns_stale_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hung `git fetch --tags` (timeout) degrades the same as a failed exit."""

    def _fake_subprocess_run(*_a: object, **_kw: object) -> object:
        raise subprocess.TimeoutExpired(
            cmd=["git", "fetch", "--tags", "--quiet", "origin"], timeout=10
        )

    monkeypatch.setattr(git_utils.subprocess, "run", _fake_subprocess_run)
    notes = git_utils.fetch_tags_best_effort(tmp_path)
    assert notes == [
        (
            "Note: `git fetch --tags` failed — validating against local "
            "tags, which may be stale."
        )
    ]


def test_fetch_tags_best_effort_custom_timeout_flows_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A caller-supplied `timeout` overrides the 10s default in the call."""
    calls: list[dict[str, object]] = []

    def _fake_subprocess_run(_cmd: list[str], **kw: object) -> object:
        calls.append(kw)
        return type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(git_utils.subprocess, "run", _fake_subprocess_run)
    assert git_utils.fetch_tags_best_effort(tmp_path, timeout=3) == []
    assert calls[0]["timeout"] == 3


# ---------------------------------------------------------------------------
# read_local_plugin_version (moved from test_next_prep.py)
# ---------------------------------------------------------------------------


def test_read_plugin_version_returns_semver_string(tmp_path: Path) -> None:
    """Valid plugin.json with a semver version returns the string."""
    (tmp_path / ".claude-plugin").mkdir()
    (tmp_path / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "x", "version": "1.2.10"})
    )
    assert git_utils.read_local_plugin_version(tmp_path) == "1.2.10"


def test_read_plugin_version_returns_none_when_file_missing(tmp_path: Path) -> None:
    """No plugin.json → None."""
    assert git_utils.read_local_plugin_version(tmp_path) is None


def test_read_plugin_version_returns_none_on_non_semver(tmp_path: Path) -> None:
    """Non-semver version field → None (defence against tag injection)."""
    (tmp_path / ".claude-plugin").mkdir()
    (tmp_path / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "x", "version": "1.2"})
    )
    assert git_utils.read_local_plugin_version(tmp_path) is None


def test_read_plugin_version_returns_none_on_malformed_json(tmp_path: Path) -> None:
    """Malformed JSON → None (not raise)."""
    (tmp_path / ".claude-plugin").mkdir()
    (tmp_path / ".claude-plugin" / "plugin.json").write_text("{not valid")
    assert git_utils.read_local_plugin_version(tmp_path) is None


# ---------------------------------------------------------------------------
# render_plugin_version / write_plugin_version
# ---------------------------------------------------------------------------


def test_render_plugin_version_rewrites_only_the_version_value() -> None:
    """The version value changes; every other byte of the manifest survives."""
    source = '{\n  "name": "forge",\n  "version": "1.0.0",\n  "extra": "kept"\n}\n'
    result = git_utils.render_plugin_version(source, "2.0.0")
    assert result == (
        '{\n  "name": "forge",\n  "version": "2.0.0",\n  "extra": "kept"\n}\n'
    )


def test_render_plugin_version_raises_without_version_field() -> None:
    """A manifest with no version field cannot be rewritten → ValueError."""
    with pytest.raises(ValueError, match='no "version" field'):
        git_utils.render_plugin_version('{"name": "forge"}', "1.0.0")


def test_render_plugin_version_refuses_json_escape_injection() -> None:
    r"""A version field carrying JSON escapes must refuse, never return broken JSON.

    CWE-116 pin (mirrors the ``forge-rebump`` regression): a
    branch-authored ``"version"`` like ``99.0.0\\", \\"pwned\\": \\"x``
    defeats the textual rewrite (the regex stops at the payload's first
    embedded quote), so the fail-closed post-rewrite validation must
    raise instead of returning corrupt text.
    """
    source = (
        '{\n  "name": "forge",\n'
        '  "version": "99.0.0\\", \\"pwned\\": \\"x",\n'
        '  "other": "field"\n}\n'
    )
    with pytest.raises(ValueError, match=r"invalid JSON|did not land"):
        git_utils.render_plugin_version(source, "99.0.0")


def test_write_plugin_version_rewrites_manifest_on_disk(tmp_path: Path) -> None:
    """The on-disk manifest carries the target version, formatting preserved."""
    plugin_dir = tmp_path / ".claude-plugin"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.json").write_text(
        '{\n  "name": "forge",\n  "version": "1.0.0"\n}\n', encoding="utf-8"
    )
    git_utils.write_plugin_version(tmp_path, "1.1.0")
    assert (plugin_dir / "plugin.json").read_text(encoding="utf-8") == (
        '{\n  "name": "forge",\n  "version": "1.1.0"\n}\n'
    )


def test_write_plugin_version_fail_closed_leaves_file_untouched(
    tmp_path: Path,
) -> None:
    """A manifest that fails validation is never (partially) written."""
    plugin_dir = tmp_path / ".claude-plugin"
    plugin_dir.mkdir()
    original = '{"name": "forge"}\n'
    (plugin_dir / "plugin.json").write_text(original, encoding="utf-8")
    with pytest.raises(ValueError, match='no "version" field'):
        git_utils.write_plugin_version(tmp_path, "1.1.0")
    assert (plugin_dir / "plugin.json").read_text(encoding="utf-8") == original


# ---------------------------------------------------------------------------
# Real-git helpers for run_git / get_tree_sha / read_plugin_version_at_ref
# (_GIT_ENV + _init_git_repo are imported from tests.conftest — #85)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# run_git
# ---------------------------------------------------------------------------


def test_run_git_check_true_success_returns_trimmed_stdout(tmp_path: Path) -> None:
    """check=True on a valid command returns the trimmed 40-char commit SHA."""
    _init_git_repo(tmp_path)
    sha = git_utils.run_git("rev-parse", "HEAD", cwd=tmp_path)
    assert len(sha) == 40
    assert all(c in "0123456789abcdef" for c in sha)


def test_run_git_check_true_failure_raises(tmp_path: Path) -> None:
    """check=True and a failing git command raises CalledProcessError.

    ``--verify`` is used to prevent git's passthrough mode (which echoes
    bare unknown names to stdout with exit 0 instead of failing).
    """
    _init_git_repo(tmp_path)
    with pytest.raises(subprocess.CalledProcessError):
        git_utils.run_git(
            "rev-parse", "--verify", "nonexistent_branch_xyz", cwd=tmp_path
        )


def test_run_git_check_false_failure_returns_empty(tmp_path: Path) -> None:
    """check=False and a failing command returns '' without raising.

    ``--verify`` is used to prevent git's passthrough mode (which echoes
    bare unknown names to stdout with exit 0 instead of failing).
    """
    _init_git_repo(tmp_path)
    result = git_utils.run_git(
        "rev-parse", "--verify", "nonexistent_branch_xyz", cwd=tmp_path, check=False
    )
    assert result == ""


def test_run_git_check_true_failure_logs_git_stderr(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A failing git command logs git's captured stderr before re-raising.

    Without this, CI logs show only a bare non-zero exit code — the
    investigation behind #242/#243 needed the actual git message
    ("fatal: ...") to diagnose an identity-less runner, and had only the
    exit code to go on.

    ``--verify`` is used to prevent git's passthrough mode (which echoes
    bare unknown names to stdout with exit 0 instead of failing).
    """
    _init_git_repo(tmp_path)
    with (
        caplog.at_level(logging.ERROR, logger="forge.git_utils"),
        pytest.raises(subprocess.CalledProcessError),
    ):
        git_utils.run_git(
            "rev-parse", "--verify", "nonexistent_branch_xyz", cwd=tmp_path
        )
    assert any("Needed a single revision" in r.getMessage() for r in caplog.records)


def test_run_git_log_errors_false_suppresses_failure_log(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """``log_errors=False`` raises without an ERROR line (tolerated failures)."""
    _init_git_repo(tmp_path)
    with (
        caplog.at_level(logging.ERROR, logger="forge.git_utils"),
        pytest.raises(subprocess.CalledProcessError),
    ):
        git_utils.run_git(
            "rev-parse",
            "--verify",
            "nonexistent_branch_xyz",
            cwd=tmp_path,
            log_errors=False,
        )
    assert not caplog.records


# ---------------------------------------------------------------------------
# _fallback_identity_args
# ---------------------------------------------------------------------------


def test_fallback_identity_args_empty_when_identity_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A usable committer identity (non-empty probe) → no fallback flags."""
    monkeypatch.setattr(git_utils, "run_git", lambda *_a, **_kw: "t <t@t> 0 +0000")
    assert git_utils._fallback_identity_args(tmp_path) == []


def test_fallback_identity_args_returns_forge_release_flags_when_probe_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty ``GIT_COMMITTER_IDENT`` probe → the forge-release ``-c`` flags."""
    monkeypatch.setattr(git_utils, "run_git", lambda *_a, **_kw: "")
    assert git_utils._fallback_identity_args(tmp_path) == [
        "-c",
        "user.name=forge-release",
        "-c",
        "user.email=forge-release@users.noreply.github.com",
    ]


# ---------------------------------------------------------------------------
# create_annotated_tag
# ---------------------------------------------------------------------------


def test_create_annotated_tag_default_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default call (``commit="HEAD"``, ``force=False``) builds the plain tag argv."""
    captured: list[tuple[str, ...]] = []

    def _fake_run_git(*args: str, **_kw: object) -> str:
        captured.append(args)
        return "t <t@t> 0 +0000" if args[:2] == ("var", "GIT_COMMITTER_IDENT") else ""

    monkeypatch.setattr(git_utils, "run_git", _fake_run_git)
    git_utils.create_annotated_tag(tmp_path, "v1.0.0")
    assert captured[-1] == ("tag", "-a", "-m", "v1.0.0", "--", "v1.0.0", "HEAD")


def test_create_annotated_tag_commit_and_force_prepend_dash_f(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``force=True`` + an explicit ``commit`` prepend ``-f``, use that commit-ish."""
    captured: list[tuple[str, ...]] = []

    def _fake_run_git(*args: str, **_kw: object) -> str:
        captured.append(args)
        return "t <t@t> 0 +0000" if args[:2] == ("var", "GIT_COMMITTER_IDENT") else ""

    monkeypatch.setattr(git_utils, "run_git", _fake_run_git)
    git_utils.create_annotated_tag(tmp_path, "v1.0.0", commit="abc123", force=True)
    assert captured[-1] == (
        "tag",
        "-f",
        "-a",
        "-m",
        "v1.0.0",
        "--",
        "v1.0.0",
        "abc123",
    )


def test_create_annotated_tag_prepends_fallback_identity_when_probe_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty identity probe → the ``-c`` flags land ahead of ``tag`` in argv."""
    captured: list[tuple[str, ...]] = []

    def _fake_run_git(*args: str, **_kw: object) -> str:
        captured.append(args)
        return ""

    monkeypatch.setattr(git_utils, "run_git", _fake_run_git)
    git_utils.create_annotated_tag(tmp_path, "v1.0.0")
    assert captured[-1] == (
        "-c",
        "user.name=forge-release",
        "-c",
        "user.email=forge-release@users.noreply.github.com",
        "tag",
        "-a",
        "-m",
        "v1.0.0",
        "--",
        "v1.0.0",
        "HEAD",
    )


def test_create_annotated_tag_succeeds_with_no_git_identity_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SCENARIO: a fresh CI runner with no git identity anywhere (#242 repro).

    MOCK SETUP: ``_fallback_identity_args`` is pinned to return the
        production ``_FALLBACK_IDENTITY`` flags unconditionally — the
        identity-less *probe* result cannot be forced portably (a dev
        machine auto-detects an identity from ``getpwuid``/hostname, and
        empty-string identity env vars would defeat ``-c`` flags too, so
        even the fix could not tag). Everything downstream is real git:
        config sources scrubbed (``HOME``, ``GIT_CONFIG_*``), identity
        env vars unset, repo history created with inline ``-c`` identity.
    EXPECTED BEHAVIOR: tag creation does not raise, the ref is a real
        annotated ``tag`` object, and its tagger is the injected
        ``forge-release`` identity — proving the production fallback
        flags satisfy git wherever auto-detection would have failed
        (``-c`` outranks config and auto-detection, so the assertion is
        machine-independent).
    """
    subprocess.run(
        ["git", "init", "-q", "-b", "main"],
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", "")},
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "initial",
        ],
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", "")},
        check=True,
    )

    monkeypatch.setenv("HOME", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for var in (
        "GIT_AUTHOR_NAME",
        "GIT_AUTHOR_EMAIL",
        "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL",
        "EMAIL",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(
        git_utils,
        "_fallback_identity_args",
        lambda _root: list(git_utils._FALLBACK_IDENTITY),
    )

    git_utils.create_annotated_tag(tmp_path, "v1.0.0")

    tag_type = subprocess.run(
        ["git", "cat-file", "-t", "v1.0.0"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert tag_type == "tag"
    tagger = subprocess.run(
        ["git", "for-each-ref", "--format=%(taggername)", "refs/tags/v1.0.0"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert tagger == "forge-release"


# ---------------------------------------------------------------------------
# create_commit
# ---------------------------------------------------------------------------


def test_create_commit_default_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A usable identity (non-empty probe) → plain ``commit -m`` argv, no ``-c``."""
    captured: list[tuple[str, ...]] = []

    def _fake_run_git(*args: str, **_kw: object) -> str:
        captured.append(args)
        return "t <t@t> 0 +0000" if args[:2] == ("var", "GIT_COMMITTER_IDENT") else ""

    monkeypatch.setattr(git_utils, "run_git", _fake_run_git)
    git_utils.create_commit(tmp_path, "chore: resync")
    assert captured[-1] == ("commit", "-m", "chore: resync")


def test_create_commit_prepends_fallback_identity_when_probe_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty identity probe → the ``-c`` flags land ahead of ``commit`` in argv."""
    captured: list[tuple[str, ...]] = []

    def _fake_run_git(*args: str, **_kw: object) -> str:
        captured.append(args)
        return ""

    monkeypatch.setattr(git_utils, "run_git", _fake_run_git)
    git_utils.create_commit(tmp_path, "chore: resync")
    assert captured[-1] == (
        "-c",
        "user.name=forge-release",
        "-c",
        "user.email=forge-release@users.noreply.github.com",
        "commit",
        "-m",
        "chore: resync",
    )


def test_create_commit_passes_message_through_unmodified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A message with spaces and brackets stays one untouched argv element."""
    captured: list[tuple[str, ...]] = []
    message = "chore: resync (1.2.3) [no-version]"

    def _fake_run_git(*args: str, **_kw: object) -> str:
        captured.append(args)
        return "t <t@t> 0 +0000" if args[:2] == ("var", "GIT_COMMITTER_IDENT") else ""

    monkeypatch.setattr(git_utils, "run_git", _fake_run_git)
    git_utils.create_commit(tmp_path, message)
    assert captured[-1] == ("commit", "-m", message)


def test_create_commit_succeeds_with_no_git_identity_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SCENARIO: a fresh CI runner with no git identity anywhere commits via forge.

    MOCK SETUP: ``_fallback_identity_args`` is pinned to return the
        production ``_FALLBACK_IDENTITY`` flags unconditionally — the
        identity-less *probe* result cannot be forced portably (a dev
        machine auto-detects an identity from ``getpwuid``/hostname, and
        empty-string identity env vars would defeat ``-c`` flags too, so
        even the fix could not commit). Everything downstream is real
        git: config sources scrubbed (``HOME``, ``GIT_CONFIG_*``),
        identity env vars unset, repo history created with inline ``-c``
        identity, a file staged.
    EXPECTED BEHAVIOR: ``create_commit`` does not raise, and the new
        commit's committer identity is the injected ``forge-release``
        identity with the exact message passed through — proving the
        production fallback flags satisfy git wherever auto-detection
        would have failed (``-c`` outranks config and auto-detection, so
        the assertion is machine-independent).
    """
    subprocess.run(
        ["git", "init", "-q", "-b", "main"],
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", "")},
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "initial",
        ],
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", "")},
        check=True,
    )
    (tmp_path / "file.txt").write_text("content\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "file.txt"],
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", "")},
        check=True,
    )

    monkeypatch.setenv("HOME", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for var in (
        "GIT_AUTHOR_NAME",
        "GIT_AUTHOR_EMAIL",
        "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL",
        "EMAIL",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(
        git_utils,
        "_fallback_identity_args",
        lambda _root: list(git_utils._FALLBACK_IDENTITY),
    )

    message = "chore: resync forge-managed artifacts (1.2.3) [no-version]"
    git_utils.create_commit(tmp_path, message)

    committer_name = subprocess.run(
        ["git", "show", "-s", "--format=%cn"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    committer_email = subprocess.run(
        ["git", "show", "-s", "--format=%ce"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    subject = subprocess.run(
        ["git", "show", "-s", "--format=%s"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert committer_name == "forge-release"
    assert committer_email == "forge-release@users.noreply.github.com"
    assert subject == message


def test_create_commit_raises_when_nothing_staged(tmp_path: Path) -> None:
    """Nothing staged → git exits non-zero and ``create_commit`` propagates it."""
    subprocess.run(
        ["git", "init", "-q", "-b", "main"],
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", "")},
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "initial",
        ],
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", "")},
        check=True,
    )

    with pytest.raises(subprocess.CalledProcessError):
        git_utils.create_commit(tmp_path, "chore: nothing to commit")


# ---------------------------------------------------------------------------
# ref_exists
# ---------------------------------------------------------------------------


def test_ref_exists_true_for_existing_branch(tmp_path: Path) -> None:
    """A ref naming an existing branch resolves to ``True``."""
    _init_git_repo(tmp_path)
    assert git_utils.ref_exists(tmp_path, "HEAD") is True


def test_ref_exists_false_for_missing_ref(tmp_path: Path) -> None:
    """A ref with no matching commit resolves to ``False``."""
    _init_git_repo(tmp_path)
    assert git_utils.ref_exists(tmp_path, "nonexistent_branch_xyz") is False


# ---------------------------------------------------------------------------
# resolve_base_branch_ref
# ---------------------------------------------------------------------------


def test_resolve_base_branch_ref_rejects_empty_base_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty ``base_branch`` is rejected without touching git."""
    calls: list[list[str]] = []

    def _fake_run(cmd: list[str], **_kwargs: object) -> object:
        calls.append(cmd)
        return type("P", (), {"returncode": 0, "stdout": "ok\n"})()

    monkeypatch.setattr(git_utils.subprocess, "run", _fake_run)
    assert git_utils.resolve_base_branch_ref(tmp_path, "") is None
    assert calls == []


def test_resolve_base_branch_ref_rejects_flag_shaped_base_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `-`-prefixed ``base_branch`` (option injection) is rejected, no git call."""
    calls: list[list[str]] = []

    def _fake_run(cmd: list[str], **_kwargs: object) -> object:
        calls.append(cmd)
        return type("P", (), {"returncode": 0, "stdout": "ok\n"})()

    monkeypatch.setattr(git_utils.subprocess, "run", _fake_run)
    assert git_utils.resolve_base_branch_ref(tmp_path, "--output=pwned") is None
    assert calls == []


def test_resolve_base_branch_ref_prefers_origin_over_local(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When both `origin/<base>` and local `<base>` resolve, origin wins."""

    def _fake_run(cmd: list[str], **_kwargs: object) -> object:
        return type("P", (), {"returncode": 0, "stdout": "ok\n"})()

    monkeypatch.setattr(git_utils.subprocess, "run", _fake_run)
    assert git_utils.resolve_base_branch_ref(tmp_path, "main") == "origin/main"


def test_resolve_base_branch_ref_falls_back_to_local_when_origin_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`origin/<base>` fails to resolve, local `<base>` does → local is used."""

    def _fake_run(cmd: list[str], **_kwargs: object) -> object:
        if cmd[-1] == "origin/main^{commit}":
            return type("P", (), {"returncode": 1, "stdout": ""})()
        return type("P", (), {"returncode": 0, "stdout": "ok\n"})()

    monkeypatch.setattr(git_utils.subprocess, "run", _fake_run)
    assert git_utils.resolve_base_branch_ref(tmp_path, "main") == "main"


def test_resolve_base_branch_ref_none_when_neither_resolves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Neither `origin/<base>` nor local `<base>` resolves → `None`."""

    def _fake_run(cmd: list[str], **_kwargs: object) -> object:
        return type("P", (), {"returncode": 1, "stdout": ""})()

    monkeypatch.setattr(git_utils.subprocess, "run", _fake_run)
    assert git_utils.resolve_base_branch_ref(tmp_path, "main") is None


def test_resolve_base_branch_ref_root_none_uses_cached_repo_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`root=None` resolves against the cached process-wide `repo_root()`."""
    _init_git_repo(tmp_path)
    monkeypatch.setattr(git_utils, "repo_root", lambda: tmp_path)
    assert git_utils.resolve_base_branch_ref(None, "main") == "main"


def test_resolve_base_branch_ref_ci_shape_origin_only(tmp_path: Path) -> None:
    """CI-shaped checkout: local `main` deleted, only `origin/main` resolves.

    Mirrors a CI checkout of a detached ``refs/pull/N/merge``: no local
    ``main`` branch exists, but the fetch that created the checkout left
    ``origin/main`` behind.
    """
    work, _bare = _init_single_track_repo(tmp_path)
    head_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=work,
        env=_GIT_ENV,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    subprocess.run(["git", "fetch", "-q", "origin"], cwd=work, env=_GIT_ENV, check=True)
    subprocess.run(
        ["git", "checkout", "-q", "--detach", head_sha],
        cwd=work,
        env=_GIT_ENV,
        check=True,
    )
    subprocess.run(["git", "branch", "-D", "main"], cwd=work, env=_GIT_ENV, check=True)
    assert git_utils.resolve_base_branch_ref(work, "main") == "origin/main"


def test_resolve_base_branch_ref_offline_local_only(tmp_path: Path) -> None:
    """No remote configured → falls back to the local base branch."""
    _init_git_repo(tmp_path)
    assert git_utils.resolve_base_branch_ref(tmp_path, "main") == "main"


def test_resolve_base_branch_ref_honors_non_main_base_branch(tmp_path: Path) -> None:
    """A repo whose branch is `develop`, with an `origin` remote, resolves it."""
    work = tmp_path / "work"
    bare = tmp_path / "origin.git"
    work.mkdir()
    bare.mkdir()
    for cmd in (
        ["git", "init", "-q", "-b", "develop"],
        ["git", "commit", "-q", "--allow-empty", "-m", "initial"],
    ):
        subprocess.run(cmd, cwd=work, env=_GIT_ENV, check=True)
    subprocess.run(["git", "init", "--bare", "-q"], cwd=bare, env=_GIT_ENV, check=True)
    subprocess.run(
        ["git", "remote", "add", "origin", str(bare)],
        cwd=work,
        env=_GIT_ENV,
        check=True,
    )
    subprocess.run(
        ["git", "push", "-q", "origin", "develop"], cwd=work, env=_GIT_ENV, check=True
    )
    assert git_utils.resolve_base_branch_ref(work, "develop") == "origin/develop"


# ---------------------------------------------------------------------------
# added_or_moved_files
# ---------------------------------------------------------------------------


def test_added_or_moved_files_routes_through_resolve_base_branch_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The diff base is resolved via `resolve_base_branch_ref`, not inlined.

    MOCK SETUP: `git_utils.resolve_base_branch_ref` is replaced by a spy
    returning a sentinel ref; `subprocess.run` is stubbed for the resulting
    `git diff ... sentinel/ref` call.
    EXPECTED BEHAVIOR: the spy is called with `(repo_root, base_branch)`
    and its sentinel return value is the ref diffed against.
    """
    calls: list[tuple[object, object]] = []

    def _spy(root: object, base_branch: object) -> str:
        calls.append((root, base_branch))
        return "sentinel/ref"

    monkeypatch.setattr(git_utils, "resolve_base_branch_ref", _spy)

    def _fake_run(cmd: list[str], **_kwargs: object) -> object:
        if cmd[-1] == "sentinel/ref":
            return type("P", (), {"returncode": 0, "stdout": "src/a.py\n"})()
        return type("P", (), {"returncode": 0, "stdout": ""})()

    monkeypatch.setattr(git_utils.subprocess, "run", _fake_run)
    files = git_utils.added_or_moved_files(repo_root=tmp_path, base_branch="main")
    assert files == ["src/a.py"]
    assert calls == [(tmp_path, "main")]


def test_added_or_moved_files_unresolvable_base_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unresolvable base branch short-circuits to `[]` before any git diff.

    No `subprocess.run` stub is installed — a call past the early return
    would hit real git and fail (or hang), so this proves the short-circuit.
    """
    monkeypatch.setattr(git_utils, "resolve_base_branch_ref", lambda *_a, **_kw: None)
    assert git_utils.added_or_moved_files(base_branch="main") == []


@pytest.mark.parametrize(
    ("suffix", "expected"),
    [
        (".py", ["src/a.py"]),
        (".toml", ["pyproject.toml"]),
    ],
)
def test_added_or_moved_files_filters_by_suffix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    suffix: str,
    expected: list[str],
) -> None:
    """Only files matching `suffix` survive a mixed-extension diff.

    Args:
        suffix: Extension filter passed to `added_or_moved_files`.
        expected: Filtered paths expected back.
    """
    monkeypatch.setattr(
        git_utils, "resolve_base_branch_ref", lambda *_a, **_kw: "origin/main"
    )
    monkeypatch.setattr(
        git_utils.subprocess,
        "run",
        lambda *_a, **_kw: type(
            "P",
            (),
            {"returncode": 0, "stdout": "src/a.py\npyproject.toml\nREADME.md\n"},
        )(),
    )
    files = git_utils.added_or_moved_files(repo_root=tmp_path, suffix=suffix)
    assert files == expected


def test_added_or_moved_files_empty_output_returns_empty_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty diff output yields `[]`, not `[""]`."""
    monkeypatch.setattr(
        git_utils, "resolve_base_branch_ref", lambda *_a, **_kw: "origin/main"
    )
    monkeypatch.setattr(
        git_utils.subprocess,
        "run",
        lambda *_a, **_kw: type("P", (), {"returncode": 0, "stdout": ""})(),
    )
    assert git_utils.added_or_moved_files(repo_root=tmp_path) == []


def test_added_or_moved_files_uses_diff_filter_ar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The git invocation carries the exact `--diff-filter=AR` flag."""
    monkeypatch.setattr(
        git_utils, "resolve_base_branch_ref", lambda *_a, **_kw: "origin/main"
    )
    captured: dict[str, list[str]] = {}

    def _fake_run(cmd: list[str], **_kwargs: object) -> object:
        captured["cmd"] = cmd
        return type("P", (), {"returncode": 0, "stdout": ""})()

    monkeypatch.setattr(git_utils.subprocess, "run", _fake_run)
    git_utils.added_or_moved_files(repo_root=tmp_path)
    assert captured["cmd"] == [
        "git",
        "diff",
        "--name-only",
        "--diff-filter=AR",
        "origin/main",
    ]


def test_added_or_moved_files_repo_root_threads_to_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The explicit `repo_root` is passed as `cwd` to the git subprocess call."""
    monkeypatch.setattr(
        git_utils, "resolve_base_branch_ref", lambda *_a, **_kw: "origin/main"
    )
    calls: list[tuple[list[str], object]] = []

    def _fake_run(cmd: list[str], **kwargs: object) -> object:
        calls.append((cmd, kwargs.get("cwd")))
        return type("P", (), {"returncode": 0, "stdout": ""})()

    monkeypatch.setattr(git_utils.subprocess, "run", _fake_run)
    git_utils.added_or_moved_files(repo_root=tmp_path)
    assert calls[0][1] == tmp_path


# ---------------------------------------------------------------------------
# resolve_current_branch
# ---------------------------------------------------------------------------


def test_resolve_current_branch_prefers_local_show_current(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A checked-out branch resolves via `--show-current`, source `"local"`."""
    monkeypatch.delenv("GITHUB_HEAD_REF", raising=False)
    _init_git_repo(tmp_path)
    subprocess.run(
        ["git", "checkout", "-q", "-b", "feat/x"],
        cwd=tmp_path,
        env=_GIT_ENV,
        check=True,
    )
    assert git_utils.resolve_current_branch(tmp_path) == ("feat/x", "local")


def test_resolve_current_branch_falls_back_to_github_head_ref_when_detached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A detached HEAD falls back to `GITHUB_HEAD_REF`, source `"GITHUB_HEAD_REF"`."""
    monkeypatch.delenv("GITHUB_HEAD_REF", raising=False)
    _init_git_repo(tmp_path)
    _detach_head(tmp_path)
    monkeypatch.setenv("GITHUB_HEAD_REF", "chore/y")
    assert git_utils.resolve_current_branch(tmp_path) == ("chore/y", "GITHUB_HEAD_REF")


def test_resolve_current_branch_none_when_neither_resolves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A detached HEAD with no `GITHUB_HEAD_REF` resolves to `None`."""
    monkeypatch.delenv("GITHUB_HEAD_REF", raising=False)
    _init_git_repo(tmp_path)
    _detach_head(tmp_path)
    assert git_utils.resolve_current_branch(tmp_path) is None


def test_resolve_current_branch_local_wins_over_github_head_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A checked-out branch wins even when `GITHUB_HEAD_REF` names another."""
    monkeypatch.delenv("GITHUB_HEAD_REF", raising=False)
    _init_git_repo(tmp_path)
    subprocess.run(
        ["git", "checkout", "-q", "-b", "feat/x"],
        cwd=tmp_path,
        env=_GIT_ENV,
        check=True,
    )
    monkeypatch.setenv("GITHUB_HEAD_REF", "chore/y")
    assert git_utils.resolve_current_branch(tmp_path) == ("feat/x", "local")


# ---------------------------------------------------------------------------
# merge_base_with_head
# ---------------------------------------------------------------------------


def test_merge_base_with_head_happy_path_returns_sha(tmp_path: Path) -> None:
    """A feature branch ahead of main returns the shared merge-base SHA."""
    _init_git_repo(tmp_path)
    subprocess.run(
        ["git", "checkout", "-q", "-b", "feat/x"],
        cwd=tmp_path,
        env=_GIT_ENV,
        check=True,
    )
    subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", "feature work"],
        cwd=tmp_path,
        env=_GIT_ENV,
        check=True,
    )
    expected = subprocess.run(
        ["git", "merge-base", "main", "HEAD"],
        cwd=tmp_path,
        env=_GIT_ENV,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    result = git_utils.merge_base_with_head(tmp_path, "main")
    assert result == expected
    assert len(result) == 40
    assert all(c in "0123456789abcdef" for c in result)


def test_merge_base_with_head_empty_when_unresolvable(tmp_path: Path) -> None:
    """An empty `base_branch` never resolves → empty string, not an exception."""
    _init_git_repo(tmp_path)
    assert git_utils.merge_base_with_head(tmp_path, "") == ""


def test_merge_base_with_head_empty_when_merge_base_fails(tmp_path: Path) -> None:
    """The base ref resolves, but `git merge-base` itself fails → empty string.

    An orphan branch shares no history with `main`, so `git merge-base`
    exits non-zero even though `resolve_base_branch_ref` happily resolves
    `"main"` — the two failure modes (unresolvable ref vs. no common
    ancestor) must both collapse to `""`, matching the documented
    Returns contract.
    """
    _init_git_repo(tmp_path)
    subprocess.run(
        ["git", "checkout", "-q", "--orphan", "feat/x"],
        cwd=tmp_path,
        env=_GIT_ENV,
        check=True,
    )
    subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", "unrelated history"],
        cwd=tmp_path,
        env=_GIT_ENV,
        check=True,
    )
    assert git_utils.merge_base_with_head(tmp_path, "main") == ""


# ---------------------------------------------------------------------------
# merge_in_progress
# ---------------------------------------------------------------------------


def _create_diverged_branches(repo: Path) -> None:
    """Create ``other`` and ``feat/x`` off ``main``, each touching a distinct file.

    Leaves ``feat/x`` checked out.

    Args:
        repo: Repository path.
    """
    subprocess.run(
        ["git", "checkout", "-q", "-b", "other"], cwd=repo, env=_GIT_ENV, check=True
    )
    (repo / "other.txt").write_text("other\n")
    subprocess.run(["git", "add", "other.txt"], cwd=repo, env=_GIT_ENV, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "other work"],
        cwd=repo,
        env=_GIT_ENV,
        check=True,
    )
    subprocess.run(
        ["git", "checkout", "-q", "main"], cwd=repo, env=_GIT_ENV, check=True
    )
    subprocess.run(
        ["git", "checkout", "-q", "-b", "feat/x"], cwd=repo, env=_GIT_ENV, check=True
    )
    (repo / "feat.txt").write_text("feat\n")
    subprocess.run(["git", "add", "feat.txt"], cwd=repo, env=_GIT_ENV, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "feat work"], cwd=repo, env=_GIT_ENV, check=True
    )


def test_merge_in_progress_false_on_clean_repo(tmp_path: Path) -> None:
    """A freshly initialized repo with no merge underway reports False."""
    _init_git_repo(tmp_path)
    assert git_utils.merge_in_progress(tmp_path) is False


def test_merge_in_progress_true_mid_merge(tmp_path: Path) -> None:
    """``MERGE_HEAD`` exists mid ``git merge --no-ff --no-commit`` -> True.

    ``--no-ff`` is required: a fast-forward merge never writes
    ``MERGE_HEAD``, so this reproduces the git state
    ``step_changelog_version`` actually guards against.
    """
    _init_git_repo(tmp_path)
    _create_diverged_branches(tmp_path)
    subprocess.run(
        ["git", "merge", "--no-ff", "--no-commit", "other"],
        cwd=tmp_path,
        env=_GIT_ENV,
        check=True,
    )
    assert git_utils.merge_in_progress(tmp_path) is True


def test_merge_in_progress_false_after_merge_commit(tmp_path: Path) -> None:
    """Completing the merge with a commit clears ``MERGE_HEAD`` -> False."""
    _init_git_repo(tmp_path)
    _create_diverged_branches(tmp_path)
    subprocess.run(
        ["git", "merge", "--no-ff", "--no-commit", "other"],
        cwd=tmp_path,
        env=_GIT_ENV,
        check=True,
    )
    subprocess.run(
        ["git", "commit", "-q", "--no-edit"], cwd=tmp_path, env=_GIT_ENV, check=True
    )
    assert git_utils.merge_in_progress(tmp_path) is False


def test_merge_in_progress_worktree_safe(tmp_path: Path) -> None:
    """A linked worktree mid-merge resolves via its own git-path.

    Not a hardcoded ``.git/MERGE_HEAD``: a linked worktree's ``.git`` is
    a gitlink *file* pointing at the main repo's ``worktrees/<name>``
    directory, not a ``.git`` directory — so ``MERGE_HEAD`` never lives
    at ``<worktree>/.git/MERGE_HEAD``. This proves ``merge_in_progress``
    resolves the real path via ``--git-path`` rather than assuming that
    layout.
    """
    main_repo = tmp_path / "main_repo"
    main_repo.mkdir()
    _init_git_repo(main_repo)
    subprocess.run(
        ["git", "checkout", "-q", "-b", "other"],
        cwd=main_repo,
        env=_GIT_ENV,
        check=True,
    )
    (main_repo / "other.txt").write_text("other\n")
    subprocess.run(["git", "add", "other.txt"], cwd=main_repo, env=_GIT_ENV, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "other work"],
        cwd=main_repo,
        env=_GIT_ENV,
        check=True,
    )
    subprocess.run(
        ["git", "checkout", "-q", "main"], cwd=main_repo, env=_GIT_ENV, check=True
    )

    wt = tmp_path / "wt"
    subprocess.run(
        ["git", "worktree", "add", "-q", "-b", "feat/wt", str(wt), "main"],
        cwd=main_repo,
        env=_GIT_ENV,
        check=True,
    )
    subprocess.run(
        ["git", "merge", "--no-ff", "--no-commit", "other"],
        cwd=wt,
        env=_GIT_ENV,
        check=True,
    )

    assert git_utils.merge_in_progress(wt) is True
    assert not (wt / ".git" / "MERGE_HEAD").exists()


def test_merge_in_progress_false_when_not_a_git_repo(tmp_path: Path) -> None:
    """A directory that is not a git repo returns False without raising."""
    assert git_utils.merge_in_progress(tmp_path) is False


# ---------------------------------------------------------------------------
# get_tree_sha
# ---------------------------------------------------------------------------


def test_get_tree_sha_valid_ref_returns_40_hex(tmp_path: Path) -> None:
    """A valid ref resolves to a 40-character hex tree SHA."""
    _init_git_repo(tmp_path)
    tree_sha = git_utils.get_tree_sha(tmp_path, "HEAD")
    assert tree_sha is not None
    assert len(tree_sha) == 40
    assert all(c in "0123456789abcdef" for c in tree_sha)


def test_get_tree_sha_unresolvable_ref_not_a_valid_tree_sha(tmp_path: Path) -> None:
    """An unresolvable ref never produces a 40-char hex tree SHA.

    Some git builds use passthrough mode: a failed ``rev-parse`` still
    echoes the argument to stdout (rc=128, non-empty stdout). The
    function does not check the return code, so it may return the
    passthrough string rather than ``None``.  Either way — ``None`` or a
    passthrough string containing ``^{tree}`` — the result is not a valid
    40-char hex SHA, so ``index.get(result)`` in callers correctly
    returns ``None`` for all unresolvable refs.
    """
    _init_git_repo(tmp_path)
    result = git_utils.get_tree_sha(tmp_path, "HEAD~999999")
    is_valid_tree_sha = (
        result is not None
        and len(result) == 40
        and all(c in "0123456789abcdef" for c in result)
    )
    assert not is_valid_tree_sha


# ---------------------------------------------------------------------------
# release_tree_fingerprint
# ---------------------------------------------------------------------------


def test_release_fingerprint_equal_when_only_changelog_differs(tmp_path: Path) -> None:
    """Two commits differing ONLY in CHANGELOG.md share a fingerprint."""
    _init_git_repo(tmp_path)
    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / "CHANGELOG.md").write_text("## v1.0.0\n")
    commit_all(tmp_path, "base")
    base_fp = git_utils.release_tree_fingerprint(tmp_path, "HEAD")
    # Change ONLY the CHANGELOG.
    (tmp_path / "CHANGELOG.md").write_text("## v1.0.0\n## v1.1.0 — curated\n")
    commit_all(tmp_path, "changelog only")
    assert git_utils.release_tree_fingerprint(tmp_path, "HEAD") == base_fp


def test_release_fingerprint_differs_when_other_file_changes(tmp_path: Path) -> None:
    """A change to any non-CHANGELOG file changes the fingerprint."""
    _init_git_repo(tmp_path)
    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / "CHANGELOG.md").write_text("## v1.0.0\n")
    commit_all(tmp_path, "base")
    base_fp = git_utils.release_tree_fingerprint(tmp_path, "HEAD")
    (tmp_path / "a.py").write_text("x = 2\n")
    commit_all(tmp_path, "code change")
    assert git_utils.release_tree_fingerprint(tmp_path, "HEAD") != base_fp


def test_release_fingerprint_none_for_unresolvable_ref(tmp_path: Path) -> None:
    """An unresolvable ref yields ``None`` (empty tree listing)."""
    _init_git_repo(tmp_path)
    assert git_utils.release_tree_fingerprint(tmp_path, "HEAD~999999") is None


def test_release_fingerprint_none_when_tree_is_only_changelog(tmp_path: Path) -> None:
    """A tree whose only file is CHANGELOG.md yields ``None``, not sha256("").

    Excluding the sole file leaves nothing to fingerprint; returning a hash
    of the empty string would make every such tree falsely release-equal.
    """
    _init_git_repo(tmp_path)
    (tmp_path / "CHANGELOG.md").write_text("## v1.0.0\n")
    commit_all(tmp_path, "changelog only")
    assert git_utils.release_tree_fingerprint(tmp_path, "HEAD") is None


def test_release_fingerprint_equal_when_only_changelog_fragments_differ(
    tmp_path: Path,
) -> None:
    """Two commits differing ONLY under `changelog.d/*.md` share a fingerprint.

    Fragment mode's pending entries are excluded the same way
    `CHANGELOG.md` is — they're deleted at assembly, so their presence or
    content must never affect the release-equal comparison.
    """
    _init_git_repo(tmp_path)
    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / "changelog.d").mkdir()
    (tmp_path / "changelog.d" / "note.added.md").write_text("bump: minor\n- x\n")
    commit_all(tmp_path, "base")
    base_fp = git_utils.release_tree_fingerprint(tmp_path, "HEAD")
    # Change ONLY the pending fragment.
    (tmp_path / "changelog.d" / "note.added.md").write_text("bump: minor\n- y\n")
    commit_all(tmp_path, "fragment only")
    assert git_utils.release_tree_fingerprint(tmp_path, "HEAD") == base_fp


def test_release_fingerprint_differs_for_changelog_md_prefix_similar_path(
    tmp_path: Path,
) -> None:
    """A `CHANGELOG.md.orig` file is NOT excluded — guards the exact-match boundary.

    `CHANGELOG.md.orig` shares its first eleven characters with
    `CHANGELOG.md`; a naive prefix check (rather than the exact-equality
    match `_release_ignored` uses for non-directory entries) would
    wrongly swallow it too. Symmetric with
    ``test_release_fingerprint_differs_for_prefix_similar_non_fragment_path``,
    which guards the same boundary for the ``changelog.d/`` member.
    """
    _init_git_repo(tmp_path)
    (tmp_path / "CHANGELOG.md.orig").write_text("x\n")
    commit_all(tmp_path, "base")
    base_fp = git_utils.release_tree_fingerprint(tmp_path, "HEAD")
    (tmp_path / "CHANGELOG.md.orig").write_text("y\n")
    commit_all(tmp_path, "prefix-similar path changed")
    assert git_utils.release_tree_fingerprint(tmp_path, "HEAD") != base_fp


def test_release_fingerprint_differs_for_prefix_similar_non_fragment_path(
    tmp_path: Path,
) -> None:
    """A `changelog.dev/` path is NOT excluded — guards `startswith` over-matching.

    `changelog.dev/` shares its first ten characters with `changelog.d/`;
    a naive prefix check that forgot the trailing slash would wrongly
    exclude it too. The trailing slash in `_RELEASE_EQUAL_IGNORE` makes
    this an ordinary tracked file, so a change to it still moves the
    fingerprint.
    """
    _init_git_repo(tmp_path)
    (tmp_path / "changelog.dev").mkdir()
    (tmp_path / "changelog.dev" / "note.md").write_text("x\n")
    commit_all(tmp_path, "base")
    base_fp = git_utils.release_tree_fingerprint(tmp_path, "HEAD")
    (tmp_path / "changelog.dev" / "note.md").write_text("y\n")
    commit_all(tmp_path, "prefix-similar path changed")
    assert git_utils.release_tree_fingerprint(tmp_path, "HEAD") != base_fp


# ---------------------------------------------------------------------------
# write_tree
# ---------------------------------------------------------------------------


def _create_conflicting_branches(repo: Path) -> None:
    """Create ``other`` and ``feat/x`` off ``main``, each editing the same line.

    Unlike :func:`_create_diverged_branches` (each branch touches a
    distinct file, so a merge between them is clean), both branches here
    edit the same line of ``shared.txt`` — a genuine content conflict, so
    a subsequent ``git merge`` exits non-zero and leaves the conflict
    staged-unresolved. Leaves ``feat/x`` checked out.

    Args:
        repo: Repository path.
    """
    (repo / "shared.txt").write_text("base\n")
    subprocess.run(["git", "add", "shared.txt"], cwd=repo, env=_GIT_ENV, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "add shared.txt"],
        cwd=repo,
        env=_GIT_ENV,
        check=True,
    )
    subprocess.run(
        ["git", "checkout", "-q", "-b", "other"], cwd=repo, env=_GIT_ENV, check=True
    )
    (repo / "shared.txt").write_text("other change\n")
    subprocess.run(["git", "add", "shared.txt"], cwd=repo, env=_GIT_ENV, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "other edit"], cwd=repo, env=_GIT_ENV, check=True
    )
    subprocess.run(
        ["git", "checkout", "-q", "main"], cwd=repo, env=_GIT_ENV, check=True
    )
    subprocess.run(
        ["git", "checkout", "-q", "-b", "feat/x"], cwd=repo, env=_GIT_ENV, check=True
    )
    (repo / "shared.txt").write_text("feat change\n")
    subprocess.run(["git", "add", "shared.txt"], cwd=repo, env=_GIT_ENV, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "feat edit"], cwd=repo, env=_GIT_ENV, check=True
    )


def test_write_tree_clean_index_matches_head_tree(tmp_path: Path) -> None:
    """A freshly committed repo's staged tree equals its ``HEAD`` tree."""
    _init_git_repo(tmp_path)
    assert git_utils.write_tree(tmp_path) == git_utils.get_tree_sha(tmp_path, "HEAD")


def test_write_tree_reflects_staged_uncommitted_change(tmp_path: Path) -> None:
    """Staging a new file changes the written tree vs ``HEAD``'s tree."""
    _init_git_repo(tmp_path)
    head_tree = git_utils.get_tree_sha(tmp_path, "HEAD")
    (tmp_path / "new.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "new.py"], cwd=tmp_path, env=_GIT_ENV, check=True)
    assert git_utils.write_tree(tmp_path) != head_tree


def test_write_tree_returns_none_on_unresolved_conflict(tmp_path: Path) -> None:
    """An unresolved merge conflict left staged → ``None`` (``write-tree`` refuses)."""
    _init_git_repo(tmp_path)
    _create_conflicting_branches(tmp_path)
    result = subprocess.run(
        ["git", "merge", "--no-ff", "--no-commit", "other"],
        cwd=tmp_path,
        env=_GIT_ENV,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert git_utils.write_tree(tmp_path) is None


def test_write_tree_none_when_not_a_git_repo(tmp_path: Path) -> None:
    """A plain, non-git directory → ``None`` without raising."""
    assert git_utils.write_tree(tmp_path) is None


# ---------------------------------------------------------------------------
# read_plugin_version_at_ref
# ---------------------------------------------------------------------------


def test_read_plugin_version_at_ref_returns_version_at_commit(
    tmp_path: Path,
) -> None:
    """Committed plugin.json at a ref returns the version string."""
    _init_git_repo(tmp_path)
    plugin_dir = tmp_path / ".claude-plugin"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.json").write_text(
        json.dumps({"name": "x", "version": "1.2.3"})
    )
    subprocess.run(["git", "add", "."], cwd=tmp_path, env=_GIT_ENV, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "add plugin"],
        cwd=tmp_path,
        env=_GIT_ENV,
        check=True,
    )
    assert git_utils.read_plugin_version_at_ref(tmp_path, "HEAD") == "1.2.3"


def test_read_plugin_version_at_ref_absent_file_returns_none(
    tmp_path: Path,
) -> None:
    """No plugin.json committed at ref → None."""
    _init_git_repo(tmp_path)
    assert git_utils.read_plugin_version_at_ref(tmp_path, "HEAD") is None


def test_read_plugin_version_at_ref_malformed_json_returns_none(
    tmp_path: Path,
) -> None:
    """Malformed JSON committed at ref → None (not raise)."""
    _init_git_repo(tmp_path)
    plugin_dir = tmp_path / ".claude-plugin"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.json").write_text("{not valid json")
    subprocess.run(["git", "add", "."], cwd=tmp_path, env=_GIT_ENV, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "bad plugin"],
        cwd=tmp_path,
        env=_GIT_ENV,
        check=True,
    )
    assert git_utils.read_plugin_version_at_ref(tmp_path, "HEAD") is None


def test_read_plugin_version_at_ref_missing_version_key_returns_none(
    tmp_path: Path,
) -> None:
    """plugin.json without a ``"version"`` key → None."""
    _init_git_repo(tmp_path)
    plugin_dir = tmp_path / ".claude-plugin"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.json").write_text(
        json.dumps({"name": "x", "description": "no version key"})
    )
    subprocess.run(["git", "add", "."], cwd=tmp_path, env=_GIT_ENV, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "no version"],
        cwd=tmp_path,
        env=_GIT_ENV,
        check=True,
    )
    assert git_utils.read_plugin_version_at_ref(tmp_path, "HEAD") is None


# ---------------------------------------------------------------------------
# stage_modified_paths
# ---------------------------------------------------------------------------


def test_stage_modified_paths_returns_empty_without_git_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No .git directory → returns [] without firing subprocess.

    The early-exit guard avoids git calls on paths that aren't repos —
    important when this helper is called from a generated-doc step that may
    run in a checkout without a .git dir.
    """
    fired: list[bool] = []

    def _spy(cmd: list[str], **_kw: object) -> object:
        fired.append(True)
        return type("P", (), {"returncode": 0, "stdout": ""})()

    monkeypatch.setattr(git_utils.subprocess, "run", _spy)
    result = git_utils.stage_modified_paths(tmp_path, ["docs/api-digest.md"])
    assert result == []
    assert fired == []


def test_stage_modified_paths_returns_empty_on_diff_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Git diff returns non-zero (e.g. bad repo state) → [] without calling git add."""
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(
        git_utils.subprocess,
        "run",
        lambda *_a, **_kw: type("P", (), {"returncode": 1, "stdout": ""})(),
    )
    result = git_utils.stage_modified_paths(tmp_path, ["docs/api-digest.md"])
    assert result == []


def test_stage_modified_paths_returns_empty_when_nothing_changed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Git diff rc=0 empty stdout → [] and git add is NOT called.

    The pathspec scoping means only tracked modifications under the given paths
    are staged; zero-output diff means nothing to stage.
    """
    (tmp_path / ".git").mkdir()
    cmds: list[list[str]] = []

    def _fake(cmd: list[str], **_kw: object) -> object:
        cmds.append(cmd)
        return type("P", (), {"returncode": 0, "stdout": ""})()

    monkeypatch.setattr(git_utils.subprocess, "run", _fake)
    result = git_utils.stage_modified_paths(tmp_path, ["docs/api-digest.md"])
    assert result == []
    add_calls = [c for c in cmds if len(c) >= 2 and c[1] == "add"]
    assert add_calls == []


def test_stage_modified_paths_stages_changed_files_and_returns_list(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Diff returns one changed file → that file is passed to git add and returned.

    SCENARIO: git diff outputs "docs/api-digest.md" under the pathspec; git add
        succeeds.
    MOCK SETUP: subprocess.run dispatches on cmd[1]: "diff" returns rc=0 with
        one changed path; "add" returns rc=0.
    EXPECTED BEHAVIOR: returns ["docs/api-digest.md"]; diff argv contains "--"
        and the pathspec; add argv contains "--" and the changed path.
    """
    (tmp_path / ".git").mkdir()
    cmds: list[list[str]] = []

    def _fake(cmd: list[str], **_kw: object) -> object:
        cmds.append(cmd)
        if cmd[1] == "diff":
            return type("P", (), {"returncode": 0, "stdout": "docs/api-digest.md\n"})()
        return type("P", (), {"returncode": 0, "stdout": ""})()

    monkeypatch.setattr(git_utils.subprocess, "run", _fake)
    result = git_utils.stage_modified_paths(tmp_path, ["docs/api-digest.md"])
    assert result == ["docs/api-digest.md"]
    diff_cmd = next(c for c in cmds if c[1] == "diff")
    assert "--" in diff_cmd
    assert "docs/api-digest.md" in diff_cmd
    add_cmd = next(c for c in cmds if c[1] == "add")
    assert "--" in add_cmd
    assert "docs/api-digest.md" in add_cmd


def test_stage_modified_paths_passes_multiple_pathspecs_to_diff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    r"""Two pathspecs both appear after \"--\" in the diff subprocess argv."""
    (tmp_path / ".git").mkdir()
    captured: list[list[str]] = []

    def _fake(cmd: list[str], **_kw: object) -> object:
        captured.append(cmd)
        return type("P", (), {"returncode": 0, "stdout": ""})()

    monkeypatch.setattr(git_utils.subprocess, "run", _fake)
    git_utils.stage_modified_paths(
        tmp_path, ["docs/api-digest.md", "docs/cli-reference.md"]
    )
    diff_cmd = next(c for c in captured if c[1] == "diff")
    sep_idx = diff_cmd.index("--")
    specs_after = diff_cmd[sep_idx + 1 :]
    assert "docs/api-digest.md" in specs_after
    assert "docs/cli-reference.md" in specs_after


def test_stage_modified_paths_real_git_stages_modified_tracked_file(
    tmp_path: Path,
) -> None:
    """Integration: a committed-then-modified file is staged and returned.

    Commits ``docs/api-digest.md`` ("v1"), modifies it to "v2", calls the
    helper, then verifies the path is returned and appears in
    ``git diff --cached --name-only``.
    """
    _init_git_repo(tmp_path)
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    target = docs_dir / "api-digest.md"
    target.write_text("v1\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, env=_GIT_ENV, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "add doc"],
        cwd=tmp_path,
        env=_GIT_ENV,
        check=True,
    )
    target.write_text("v2\n")
    result = git_utils.stage_modified_paths(tmp_path, ["docs/api-digest.md"])
    assert result == ["docs/api-digest.md"]
    proc = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=tmp_path,
        env=_GIT_ENV,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "docs/api-digest.md" in proc.stdout


# ---------------------------------------------------------------------------
# path_escapes_repo
# ---------------------------------------------------------------------------


def test_path_escapes_repo_in_repo_file_false(tmp_path: Path) -> None:
    """A plain in-repo relative path is not an escape."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "ok.py").write_text("")
    assert git_utils.path_escapes_repo(tmp_path, "src/ok.py") is False


def test_path_escapes_repo_dotdot_traversal_true(tmp_path: Path) -> None:
    """A ``..`` segment that climbs above repo_root is an escape."""
    assert git_utils.path_escapes_repo(tmp_path, "../evil.py") is True


def test_path_escapes_repo_absolute_outside_true(tmp_path: Path) -> None:
    """An absolute path replaces repo_root in the join and escapes."""
    assert git_utils.path_escapes_repo(tmp_path, "/etc/passwd") is True


def test_path_escapes_repo_nested_in_repo_false(tmp_path: Path) -> None:
    """A deeply nested in-repo path is not an escape."""
    assert git_utils.path_escapes_repo(tmp_path, "a/b/c/deep.py") is False


def test_path_escapes_repo_symlink_escape_true(tmp_path: Path) -> None:
    """A symlink inside repo_root resolving outside it is an escape.

    Creates ``tmp_path/link`` as a symlink to a sibling directory outside
    ``tmp_path`` (the repo root), then resolves a path through it —
    ``Path.resolve()`` follows the symlink out of the repo.
    """
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation not permitted on this platform")
    assert git_utils.path_escapes_repo(tmp_path, "link/x.py") is True


def test_get_modified_files_honors_custom_base_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """base_branch="develop" diffs against develop, not a hardcoded main."""
    _stub_branch_path(
        monkeypatch,
        tmp_path,
        current_branch="feat/x",
        diff_outputs={"origin/develop...HEAD": "src/a.py\n"},
    )
    files = git_utils.get_modified_files(repo_root=tmp_path, base_branch="develop")
    assert files == ["src/a.py"]


def test_get_modified_files_on_custom_base_falls_back_to_prev_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sitting on the configured base branch compares against HEAD~1."""
    _stub_branch_path(
        monkeypatch,
        tmp_path,
        current_branch="develop",
        diff_outputs={"HEAD~1": "src/x.py\n"},
    )
    files = git_utils.get_modified_files(repo_root=tmp_path, base_branch="develop")
    assert files == ["src/x.py"]


def test_get_modified_files_routes_through_resolve_base_branch_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`get_modified_files` resolves its diff base via `resolve_base_branch_ref`.

    MOCK SETUP: `git_utils.resolve_base_branch_ref` is replaced by a spy
    returning a sentinel ref; `subprocess.run` is stubbed for `branch
    --show-current` and the resulting `sentinel/ref...HEAD` diff.
    EXPECTED BEHAVIOR: the spy is called with `(repo_root, base_branch)`
    and its sentinel return value is the ref diffed against.
    """
    calls: list[tuple[object, object]] = []

    def _spy(root: object, base_branch: object) -> str:
        calls.append((root, base_branch))
        return "sentinel/ref"

    monkeypatch.setattr(git_utils, "resolve_base_branch_ref", _spy)

    def _fake_run(cmd: list[str], **_kwargs: object) -> object:
        if cmd[1:3] == ["branch", "--show-current"]:
            return type("P", (), {"returncode": 0, "stdout": "feat/x\n"})()
        if cmd[1:3] == ["diff", "--name-only"]:
            tail = cmd[-1] if len(cmd) > 3 else ""
            stdout = "src/a.py\n" if tail == "sentinel/ref...HEAD" else ""
            return type("P", (), {"returncode": 0, "stdout": stdout})()
        return type("P", (), {"returncode": 0, "stdout": ""})()

    monkeypatch.setattr(git_utils.subprocess, "run", _fake_run)
    files = git_utils.get_modified_files(repo_root=tmp_path, base_branch="main")
    assert files == ["src/a.py"]
    assert calls == [(tmp_path, "main")]


# ---------------------------------------------------------------------------
# forge_install_command
# ---------------------------------------------------------------------------


def test_forge_install_command_no_extra_returns_bare_pip_install() -> None:
    """No extra names the bare core-install command."""
    assert git_utils.forge_install_command() == "pip install forge-scripts"


def test_forge_install_command_with_extra_quotes_bracket() -> None:
    """An extra names the quoted bracketed extras-group form."""
    assert (
        git_utils.forge_install_command("typecheck")
        == 'pip install "forge-scripts[typecheck]"'
    )


# ---------------------------------------------------------------------------
# missing_dependency_hint
# ---------------------------------------------------------------------------


def test_missing_dependency_hint_no_extra_names_core_install() -> None:
    """No ``extra`` names the package and the bare consumer-safe pip command."""
    hint = git_utils.missing_dependency_hint("vulture")
    assert "`vulture`" in hint
    assert "pip install forge-scripts" in hint
    assert "[" not in hint
    assert '-e ".[' not in hint


def test_missing_dependency_hint_custom_extra_substitutes_bracket() -> None:
    """A custom ``extra`` substitutes into the bracket, not just the default."""
    hint = git_utils.missing_dependency_hint("jsonschema", extra="docs")
    assert "`jsonschema`" in hint
    assert 'forge-scripts[docs]"' in hint
    assert '-e ".[' not in hint


# ---------------------------------------------------------------------------
# require_cli
# ---------------------------------------------------------------------------


def test_require_cli_noop_when_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """A CLI found on PATH returns None without raising."""
    monkeypatch.setattr(git_utils.shutil, "which", lambda _name: "/fake/bin")
    assert git_utils.require_cli("ruff") is None


def test_require_cli_default_hint_has_no_extra(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A missing CLI with no ``extra``/``hint`` names the bare install command."""
    monkeypatch.setattr(git_utils.shutil, "which", lambda _name: None)
    with pytest.raises(SystemExit) as exc_info:
        git_utils.require_cli("ruff")
    err = capsys.readouterr().err
    assert "pip install forge-scripts" in err
    assert "or your repo's equivalent" in err
    assert "[" not in err
    assert exc_info.value.code == 2


def test_require_cli_extra_threads_into_default_hint(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``extra`` substitutes into the default hint's bracketed extras group."""
    monkeypatch.setattr(git_utils.shutil, "which", lambda _name: None)
    with pytest.raises(SystemExit):
        git_utils.require_cli("pyrefly", extra="typecheck")
    err = capsys.readouterr().err
    assert 'pip install "forge-scripts[typecheck]"' in err


def test_require_cli_hint_overrides_extra(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An explicit ``hint`` replaces the default line even when ``extra`` is set."""
    monkeypatch.setattr(git_utils.shutil, "which", lambda _name: None)
    with pytest.raises(SystemExit):
        git_utils.require_cli("gh", extra="typecheck", hint="Custom line.")
    err = capsys.readouterr().err
    assert "Custom line." in err
    assert "forge-scripts[typecheck]" not in err


def test_require_cli_caller_prefixes_message(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``caller`` prefixes the error; the default prefix is ``"forge"``."""
    monkeypatch.setattr(git_utils.shutil, "which", lambda _name: None)
    with pytest.raises(SystemExit):
        git_utils.require_cli("ruff", caller="forge-precommit")
    err = capsys.readouterr().err
    assert err.startswith("forge-precommit: required CLI")

    with pytest.raises(SystemExit):
        git_utils.require_cli("ruff")
    err = capsys.readouterr().err
    assert err.startswith("forge: required CLI")


# ---------------------------------------------------------------------------
# is_ancestor
# ---------------------------------------------------------------------------


def test_is_ancestor_returncode_zero_is_true(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exit 0 from ``git merge-base --is-ancestor`` means ancestor → ``True``.

    Pins the exact argv and ``cwd`` threading: the shared probe every
    reachability caller routes through must invoke git with the ancestor
    and descendant refs in that order, and run it in the given ``root``.
    """
    captured: list[tuple[list[str], object]] = []

    def _fake_run(cmd: list[str], **kwargs: object) -> object:
        captured.append((cmd, kwargs.get("cwd")))
        return type("P", (), {"returncode": 0})()

    monkeypatch.setattr(git_utils.subprocess, "run", _fake_run)
    assert git_utils.is_ancestor(tmp_path, "v1.0.0", "main") is True
    assert captured == [
        (["git", "merge-base", "--is-ancestor", "v1.0.0", "main"], tmp_path)
    ]


def test_is_ancestor_returncode_nonzero_is_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-zero exit (unreachable or unresolvable ref) → ``False``."""
    monkeypatch.setattr(
        git_utils.subprocess,
        "run",
        lambda *_a, **_kw: type("P", (), {"returncode": 1})(),
    )
    assert git_utils.is_ancestor(tmp_path, "v1.0.0", "main") is False


def test_is_ancestor_real_repo_true_for_reachable_commit(tmp_path: Path) -> None:
    """A real repo: HEAD's parent is an ancestor of HEAD."""
    _init_git_repo(tmp_path)
    subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", "second"],
        cwd=tmp_path,
        env=_GIT_ENV,
        check=True,
    )
    assert git_utils.is_ancestor(tmp_path, "HEAD~1", "HEAD") is True


def test_is_ancestor_real_repo_false_for_unreachable_ref(tmp_path: Path) -> None:
    """A real repo: an unresolvable ref is treated as not-an-ancestor."""
    _init_git_repo(tmp_path)
    assert git_utils.is_ancestor(tmp_path, "nonexistent_branch_xyz", "HEAD") is False


def test_is_ancestor_rejects_flag_shaped_refs() -> None:
    """Dash-prefixed refs return False without any git invocation.

    A leading `-` would parse as a git option (option injection); the
    guard lives at the shared primitive so every caller inherits it,
    mirroring `resolve_base_branch_ref`.
    """
    assert git_utils.is_ancestor(None, "-v1.0.0", "HEAD") is False
    assert git_utils.is_ancestor(None, "v1.0.0", "--all") is False


# ---------------------------------------------------------------------------
# classify_bump
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        ((1, 2, 3), (2, 0, 0), "major"),
        ((1, 2, 3), (1, 3, 0), "minor"),
        ((1, 2, 3), (1, 2, 4), "patch"),
        ((1, 2, 3), (1, 2, 3), None),
        (None, (1, 2, 3), None),
        ((1, 2, 3), None, None),
        (None, None, None),
    ],
)
def test_classify_bump_reads_highest_order_differing_component(
    old: tuple[int, int, int] | None,
    new: tuple[int, int, int] | None,
    expected: str | None,
) -> None:
    """The highest-order differing component wins; equal or missing sides → None.

    Shared by ``forge-next-prep``'s promotion advisory and ``forge-rebump``'s
    intent classifier — one table pins the mapping both depend on.

    Args:
        old: Baseline semver tuple fed to the classifier.
        new: Candidate semver tuple fed to the classifier.
        expected: The class the delta must map to.
    """
    assert git_utils.classify_bump(old, new) == expected


# ---------------------------------------------------------------------------
# has_conflict_markers
# ---------------------------------------------------------------------------


def test_has_conflict_markers_true_on_open_marker() -> None:
    """A ``<<<<<<< `` opener at line start is detected."""
    text = "<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> branch\n"
    assert git_utils.has_conflict_markers(text) is True


def test_has_conflict_markers_false_on_plain_text() -> None:
    """Ordinary text with no marker-shaped lines → False."""
    assert git_utils.has_conflict_markers("just some prose\n") is False


def test_has_conflict_markers_false_on_bare_equals_line() -> None:
    """A bare ``=======`` line (e.g. a markdown setext underline) is not enough.

    Only the open marker signals an unresolved conflict — a closer or
    separator line alone (as legitimately appears in markdown / quoted
    diffs) must not false-positive.
    """
    assert git_utils.has_conflict_markers("Title\n=======\n") is False


def test_has_conflict_markers_false_without_open_marker() -> None:
    """A closer line with no matching opener → False."""
    assert git_utils.has_conflict_markers(">>>>>>> branch\n") is False


def test_has_conflict_markers_false_on_eight_angle_brackets() -> None:
    """Eight ``<`` at line start does not match the exact 7-``<`` opener shape.

    ``^<{7}( |$)`` requires the 8th character to be a space or end-of-line;
    an 8th ``<`` fails that immediately after the line-anchored match
    attempt, so this stays unmatched rather than a false positive.
    """
    assert git_utils.has_conflict_markers("<<<<<<<<\n") is False


# ---------------------------------------------------------------------------
# file_has_conflict_markers
# ---------------------------------------------------------------------------


def test_file_has_conflict_markers_true_for_real_conflicted_file(
    tmp_path: Path,
) -> None:
    """A file on disk holding an open marker → True."""
    path = tmp_path / "CHANGELOG.md"
    path.write_text("<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> branch\n")
    assert git_utils.file_has_conflict_markers(path) is True


def test_file_has_conflict_markers_false_for_missing_file(tmp_path: Path) -> None:
    """A path that does not exist is simply not mid-conflict → False."""
    assert git_utils.file_has_conflict_markers(tmp_path / "absent.md") is False
