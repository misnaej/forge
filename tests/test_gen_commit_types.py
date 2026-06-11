"""Unit tests for forge.gen_commit_types — managed-block parity generator."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from forge import gen_commit_types
from forge.pr_squash_comment import CONVENTIONAL_COMMIT_TYPES


if TYPE_CHECKING:
    from pathlib import Path


_VALID_HOOK = """#!/usr/bin/env bash
set -e
# FORGE_COMMIT_TYPES_BEGIN — managed by forge-gen-commit-types
CONVENTIONAL_TYPES='feat|fix|refactor|test|docs|chore|perf|ci|build|style|revert'
# FORGE_COMMIT_TYPES_END
echo "$CONVENTIONAL_TYPES"
"""


def test_alternation_joins_canonical_tuple_with_pipes() -> None:
    """The rendered alternation is the canonical tuple joined by ``|``."""
    assert gen_commit_types._alternation() == "|".join(CONVENTIONAL_COMMIT_TYPES)


def test_expected_line_matches_shell_variable_format() -> None:
    r"""The expected line is exactly ``CONVENTIONAL_TYPES='<alt>'\\n``."""
    line = gen_commit_types._expected_line()
    assert line.startswith("CONVENTIONAL_TYPES='")
    assert line.endswith("'\n")
    assert "|".join(CONVENTIONAL_COMMIT_TYPES) in line


def test_rewrite_replaces_managed_block_only() -> None:
    """Lines outside the managed block remain byte-identical after rewrite."""
    stale = _VALID_HOOK.replace(
        "feat|fix|refactor|test|docs|chore|perf|ci|build|style|revert",
        "old|stale|types",
    )
    rewritten = gen_commit_types._rewrite(stale)
    assert rewritten == _VALID_HOOK  # canonical tuple restored
    # Everything outside the block (shebang, set -e, echo, trailing nl) survives.
    assert rewritten.startswith("#!/usr/bin/env bash\nset -e\n")
    assert rewritten.endswith('echo "$CONVENTIONAL_TYPES"\n')


def test_rewrite_is_idempotent_when_already_in_sync() -> None:
    """Re-running the rewrite on an in-sync file produces an identical string."""
    once = gen_commit_types._rewrite(_VALID_HOOK)
    twice = gen_commit_types._rewrite(once)
    assert once == twice == _VALID_HOOK


def test_rewrite_raises_when_markers_missing() -> None:
    """A hook file without the managed-block markers is rejected loudly."""
    no_markers = "#!/usr/bin/env bash\necho hello\n"
    with pytest.raises(ValueError, match="FORGE_COMMIT_TYPES_BEGIN"):
        gen_commit_types._rewrite(no_markers)


def _write_hook(tmp_path: Path, content: str) -> Path:
    """Materialise *content* at ``<tmp>/claude-hooks/check_commit_format.sh``.

    Args:
        tmp_path: Pytest tmp dir.
        content: Hook file body to write.

    Returns:
        Absolute path to the hook file (so tests can re-read it).
    """
    hooks_dir = tmp_path / "claude-hooks"
    hooks_dir.mkdir()
    hook = hooks_dir / "check_commit_format.sh"
    hook.write_text(content)
    return hook


def test_main_check_returns_zero_on_in_sync_hook(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``--check`` against an in-sync hook returns 0 and logs OK."""
    _write_hook(tmp_path, _VALID_HOOK)
    monkeypatch.setattr(gen_commit_types, "repo_root", lambda: tmp_path)
    with (
        patch.object(sys, "argv", ["forge-gen-commit-types", "--check"]),
        caplog.at_level("INFO"),
    ):
        rc = gen_commit_types.main()
    assert rc == 0
    assert any("in sync" in r.getMessage() for r in caplog.records)


def test_main_check_returns_one_on_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``--check`` against a diverged hook returns 1 and logs DRIFT."""
    drifted = _VALID_HOOK.replace("feat|fix", "feat|fix|hotfix")
    _write_hook(tmp_path, drifted)
    monkeypatch.setattr(gen_commit_types, "repo_root", lambda: tmp_path)
    with (
        patch.object(sys, "argv", ["forge-gen-commit-types", "--check"]),
        caplog.at_level("ERROR"),
    ):
        rc = gen_commit_types.main()
    assert rc == 1
    assert any("DRIFT" in r.getMessage() for r in caplog.records)


def test_main_apply_rewrites_drifted_hook(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default mode (no ``--check``) rewrites a drifted hook in place."""
    drifted = _VALID_HOOK.replace(
        "feat|fix|refactor|test|docs|chore|perf|ci|build|style|revert",
        "wrong|list",
    )
    hook = _write_hook(tmp_path, drifted)
    monkeypatch.setattr(gen_commit_types, "repo_root", lambda: tmp_path)
    with patch.object(sys, "argv", ["forge-gen-commit-types"]):
        rc = gen_commit_types.main()
    assert rc == 0
    assert hook.read_text() == _VALID_HOOK


def test_main_apply_is_no_op_when_in_sync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Default mode on an in-sync hook leaves the file untouched."""
    hook = _write_hook(tmp_path, _VALID_HOOK)
    monkeypatch.setattr(gen_commit_types, "repo_root", lambda: tmp_path)
    before_mtime = hook.stat().st_mtime
    with (
        patch.object(sys, "argv", ["forge-gen-commit-types"]),
        caplog.at_level("INFO"),
    ):
        rc = gen_commit_types.main()
    assert rc == 0
    assert hook.stat().st_mtime == before_mtime  # no rewrite
    assert any("already in sync" in r.getMessage() for r in caplog.records)


def test_main_returns_one_when_hook_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Missing hook file → exit 1 with a clear log message."""
    monkeypatch.setattr(gen_commit_types, "repo_root", lambda: tmp_path)
    with (
        patch.object(sys, "argv", ["forge-gen-commit-types"]),
        caplog.at_level("ERROR"),
    ):
        rc = gen_commit_types.main()
    assert rc == 1
    assert any("missing" in r.getMessage().lower() for r in caplog.records)
