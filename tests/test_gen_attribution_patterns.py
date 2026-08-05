"""Unit tests for forge.gen_attribution_patterns — managed-block parity generator."""

from __future__ import annotations

import re
import sys
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from forge import gen_attribution_patterns
from forge.pr_squash_comment import AI_ATTRIBUTION_PATTERNS


if TYPE_CHECKING:
    from pathlib import Path


# The managed-block regex allows continuation comment lines between the
# BEGIN marker and the ATTRIBUTION_PATTERNS line (`(?:#[^\n]*\n)*`) — mirror
# that shape here so the fixture matches the real hook file structure.
_VALID_HOOK = """#!/usr/bin/env bash
set -e
INPUT=$(cat)
# FORGE_ATTRIBUTION_PATTERNS_BEGIN — managed by `forge-gen-attribution-patterns`.
# Mirror of forge.pr_squash_comment.AI_ATTRIBUTION_PATTERNS.
ATTRIBUTION_PATTERNS='co\\-authored\\-by:|generated\\ with'
# FORGE_ATTRIBUTION_PATTERNS_END
if echo "$COMMAND" | grep -qiE "$ATTRIBUTION_PATTERNS"; then
    exit 2
fi
""".replace(
    "co\\-authored\\-by:|generated\\ with",
    "|".join(re.escape(p) for p in AI_ATTRIBUTION_PATTERNS),
)


def test_alternation_joins_escaped_patterns_with_pipes() -> None:
    """The rendered alternation is the canonical tuple, each phrase re.escape'd.

    Locks the escaping contract: a future phrase containing a regex
    metacharacter must not silently widen the hook's match.
    """
    assert gen_attribution_patterns._alternation() == "|".join(
        re.escape(p) for p in AI_ATTRIBUTION_PATTERNS
    )


def test_expected_line_matches_shell_variable_format() -> None:
    r"""The expected line is exactly ``ATTRIBUTION_PATTERNS='<alt>'\\n``."""
    line = gen_attribution_patterns._expected_line()
    assert line.startswith("ATTRIBUTION_PATTERNS='")
    assert line.endswith("'\n")
    assert "|".join(re.escape(p) for p in AI_ATTRIBUTION_PATTERNS) in line


def test_rewrite_replaces_managed_block_only() -> None:
    """Lines outside the managed block remain byte-identical after rewrite."""
    stale = _VALID_HOOK.replace(
        "|".join(re.escape(p) for p in AI_ATTRIBUTION_PATTERNS),
        "old|stale|patterns",
    )
    rewritten = gen_attribution_patterns._rewrite(stale)
    assert rewritten == _VALID_HOOK  # canonical tuple restored
    # Everything outside the block (shebang, set -e, if-block) survives.
    assert rewritten.startswith("#!/usr/bin/env bash\nset -e\n")
    assert rewritten.endswith("    exit 2\nfi\n")


def test_rewrite_is_idempotent_when_already_in_sync() -> None:
    """Re-running the rewrite on an in-sync file produces an identical string."""
    once = gen_attribution_patterns._rewrite(_VALID_HOOK)
    twice = gen_attribution_patterns._rewrite(once)
    assert once == twice == _VALID_HOOK


def test_rewrite_raises_when_markers_missing() -> None:
    """A hook file without the managed-block markers is rejected loudly."""
    no_markers = "#!/usr/bin/env bash\necho hello\n"
    with pytest.raises(ValueError, match="FORGE_ATTRIBUTION_PATTERNS_BEGIN"):
        gen_attribution_patterns._rewrite(no_markers)


def _write_hook(tmp_path: Path, content: str) -> Path:
    """Materialise *content* at ``<tmp>/claude-hooks/block_claude_attribution.sh``.

    Args:
        tmp_path: Pytest tmp dir.
        content: Hook file body to write.

    Returns:
        Absolute path to the hook file (so tests can re-read it).
    """
    hooks_dir = tmp_path / "claude-hooks"
    hooks_dir.mkdir()
    hook = hooks_dir / "block_claude_attribution.sh"
    hook.write_text(content)
    return hook


def test_main_check_returns_zero_on_in_sync_hook(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``--check`` against an in-sync hook returns 0 and logs OK."""
    _write_hook(tmp_path, _VALID_HOOK)
    monkeypatch.setattr(gen_attribution_patterns, "repo_root", lambda: tmp_path)
    with (
        patch.object(sys, "argv", ["forge-gen-attribution-patterns", "--check"]),
        caplog.at_level("INFO"),
    ):
        rc = gen_attribution_patterns.main()
    assert rc == 0
    assert any("in sync" in r.getMessage() for r in caplog.records)


def test_main_check_returns_one_on_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``--check`` against a diverged hook returns 1 and logs DRIFT."""
    drifted = _VALID_HOOK.replace(
        "|".join(re.escape(p) for p in AI_ATTRIBUTION_PATTERNS),
        "co\\-authored\\-by:|hotfix",
    )
    _write_hook(tmp_path, drifted)
    monkeypatch.setattr(gen_attribution_patterns, "repo_root", lambda: tmp_path)
    with (
        patch.object(sys, "argv", ["forge-gen-attribution-patterns", "--check"]),
        caplog.at_level("ERROR"),
    ):
        rc = gen_attribution_patterns.main()
    assert rc == 1
    assert any("DRIFT" in r.getMessage() for r in caplog.records)


def test_main_apply_rewrites_drifted_hook(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default mode (no ``--check``) rewrites a drifted hook in place."""
    drifted = _VALID_HOOK.replace(
        "|".join(re.escape(p) for p in AI_ATTRIBUTION_PATTERNS),
        "wrong|list",
    )
    hook = _write_hook(tmp_path, drifted)
    monkeypatch.setattr(gen_attribution_patterns, "repo_root", lambda: tmp_path)
    with patch.object(sys, "argv", ["forge-gen-attribution-patterns"]):
        rc = gen_attribution_patterns.main()
    assert rc == 0
    assert hook.read_text() == _VALID_HOOK


def test_main_apply_is_no_op_when_in_sync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Default mode on an in-sync hook leaves the file untouched."""
    hook = _write_hook(tmp_path, _VALID_HOOK)
    monkeypatch.setattr(gen_attribution_patterns, "repo_root", lambda: tmp_path)
    before_mtime = hook.stat().st_mtime
    with (
        patch.object(sys, "argv", ["forge-gen-attribution-patterns"]),
        caplog.at_level("INFO"),
    ):
        rc = gen_attribution_patterns.main()
    assert rc == 0
    assert hook.stat().st_mtime == before_mtime  # no rewrite
    assert any("already in sync" in r.getMessage() for r in caplog.records)


def test_main_returns_one_when_hook_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Missing hook file → exit 1 with a clear log message."""
    monkeypatch.setattr(gen_attribution_patterns, "repo_root", lambda: tmp_path)
    with (
        patch.object(sys, "argv", ["forge-gen-attribution-patterns"]),
        caplog.at_level("ERROR"),
    ):
        rc = gen_attribution_patterns.main()
    assert rc == 1
    assert any("missing" in r.getMessage().lower() for r in caplog.records)
