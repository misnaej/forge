"""Tests for the ``claude-hooks/*.sh`` PreToolUse safety hooks.

# MOCKING STRATEGY: each hook is a standalone bash + jq script. A test runs
# it as a subprocess with a synthesized ``{"tool_input":{"command": …}}``
# stdin and asserts the exit code (0 = allowed, 2 = blocked). No forge
# Python is exercised — this is a black-box harness over the shell hooks.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path


_HOOKS_DIR = Path(__file__).resolve().parents[1] / "claude-hooks"


def _run_hook(name: str, command: str) -> int:
    """Run a claude-hook with *command* as the tool_input and return its exit code.

    Args:
        name: Hook filename under ``claude-hooks/`` (e.g.
            ``"block_claude_attribution.sh"``).
        command: The ``Bash`` tool command the hook inspects.

    Returns:
        The hook's process exit code — ``0`` (allow) or ``2`` (block).
    """
    payload = json.dumps({"tool_input": {"command": command}})
    proc = subprocess.run(
        ["bash", str(_HOOKS_DIR / name)],
        input=payload,
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode


_INSTALL_DEPS = "block_install_deps.sh"


def test_install_deps_blocks_conda_run_conda_install() -> None:
    """`conda run conda install` is blocked — the wrapper-of-a-manager gap (#62).

    The bare-conda rule anchors the install verb to a command start/separator,
    so the inner `conda install` (preceded only by whitespace after `run`)
    would slip; the wrapper rule must catch it.
    """
    assert _run_hook(_INSTALL_DEPS, "conda run conda install numpy") == 2


def test_install_deps_blocks_conda_run_pip_install() -> None:
    """The pre-existing `conda run pip install` wrapper form still blocks."""
    assert _run_hook(_INSTALL_DEPS, "conda run pip install numpy") == 2


def test_install_deps_allows_conda_run_readonly() -> None:
    """`conda run conda info` (read-only, no install verb) is not blocked."""
    assert _run_hook(_INSTALL_DEPS, "conda run conda info") == 0


_ATTRIBUTION = "block_claude_attribution.sh"


def test_attribution_blocks_markdown_link_footer() -> None:
    """The canonical Claude Code footer (markdown link) is blocked.

    Regression: `Generated with [Claude Code](…)` slipped through because
    the old regex needed "generated with claude" as adjacent words and the
    `[` sits between them — the exact footer the harness emits by default.
    """
    body = "fix the thing\n\n🤖 Generated with [Claude Code](https://claude.com/claude-code)"
    assert _run_hook(_ATTRIBUTION, f'gh pr create --title x --body "{body}"') == 2


def test_attribution_blocks_emoji_signature() -> None:
    """The robot-emoji signature alone is blocked."""
    assert (
        _run_hook(_ATTRIBUTION, 'git commit -m "x\n\n🤖 Generated with Claude Code"')
        == 2
    )


def test_attribution_blocks_co_authored_by() -> None:
    """The Co-Authored-By trailer (Claude or Anthropic) still blocks."""
    trailer = "Co-Authored-By: Claude <noreply@anthropic.com>"
    assert _run_hook(_ATTRIBUTION, f'git commit -m "x\n\n{trailer}"') == 2


def test_attribution_blocks_plain_generated_with_claude() -> None:
    """The pre-existing plain `generated with claude` form still blocks."""
    assert (
        _run_hook(_ATTRIBUTION, 'gh issue create --body "Generated with Claude"') == 2
    )


def test_attribution_blocks_short_separator_before_claude() -> None:
    """A short separator (`the `, 4 chars) between the verb and claude blocks.

    Documents the `.{0,4}` bound: `generated with the claude model` matches
    because ` the ` is exactly four characters; this is intended.
    """
    assert (
        _run_hook(_ATTRIBUTION, 'git commit -m "x\n\ngenerated with the claude model"')
        == 2
    )


def test_attribution_allows_benign_generated_prose() -> None:
    """Benign prose ("generated with care") does not false-positive.

    `claude` is not within the `.{0,4}` window after the verb, so the
    deliberately tiny bound keeps ordinary commit prose unblocked.
    """
    assert (
        _run_hook(_ATTRIBUTION, 'git commit -m "this code was generated with care"')
        == 0
    )


def test_attribution_ignores_non_history_commands() -> None:
    """Commands that don't write to git/GitHub history aren't inspected."""
    assert _run_hook(_ATTRIBUTION, 'echo "generated with claude"') == 0
