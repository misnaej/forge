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


def _run_hook(name: str, command: str, *, agent_type: str = "") -> int:
    """Run a claude-hook with *command* as the tool_input and return its exit code.

    Args:
        name: Hook filename under ``claude-hooks/`` (e.g.
            ``"block_claude_attribution.sh"``).
        command: The ``Bash`` tool command the hook inspects.
        agent_type: Optional ``agent_type`` payload field — set to
            ``"forge:git-commit-push"`` to exercise the sanctioned-agent
            bypass path.

    Returns:
        The hook's process exit code — ``0`` (allow) or ``2`` (block).
    """
    tool_input: dict[str, object] = {"tool_input": {"command": command}}
    if agent_type:
        tool_input["agent_type"] = agent_type
    payload = json.dumps(tool_input)
    proc = subprocess.run(
        ["bash", str(_HOOKS_DIR / name)],
        input=payload,
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode


_PROTECTED = "block_protected_branches.sh"


def test_protected_blocks_push_head_to_dev() -> None:
    """`git push origin HEAD:dev` is blocked by the refspec-destination guard (#74).

    The current branch is unprotected, but the refspec DESTINATION is the
    protected `dev` — the gap that let a direct push to dev slip through.
    """
    assert _run_hook(_PROTECTED, "git push origin HEAD:dev") == 2


def test_protected_blocks_push_feature_to_dev() -> None:
    """`git push origin feature:dev` (explicit src:dst to protected) is blocked."""
    assert _run_hook(_PROTECTED, "git push origin my-feat:dev") == 2


def test_protected_blocks_push_fully_qualified_dev_ref() -> None:
    """`feature:refs/heads/dev` is blocked (refs/heads/ prefix normalized)."""
    assert _run_hook(_PROTECTED, "git push -u origin my-feat:refs/heads/dev") == 2


def test_protected_destination_guard_has_no_agent_bypass() -> None:
    """Even forge:git-commit-push cannot push to a protected destination (#74).

    This is the exact incident: the sanctioned agent bypasses the
    current-branch check, but the refspec-destination guard must still block
    a push whose destination is a protected branch.
    """
    assert (
        _run_hook(
            _PROTECTED,
            "git push origin HEAD:dev",
            agent_type="forge:git-commit-push",
        )
        == 2
    )


def test_protected_allows_feature_push() -> None:
    """A normal feature-branch push (unprotected destination) is allowed."""
    assert (
        _run_hook(
            _PROTECTED,
            "git push -u origin my-feat:refs/heads/my-feat",
            agent_type="forge:git-commit-push",
        )
        == 0
    )


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


def test_install_deps_blocks_conda_env_update() -> None:
    """`conda env update` (refresh-from-spec, installs packages) is blocked."""
    assert _run_hook(_INSTALL_DEPS, "conda env update -f environment.yml") == 2


def test_install_deps_blocks_conda_run_conda_env_update() -> None:
    """The wrapped `conda run conda env update` form is blocked too."""
    assert _run_hook(_INSTALL_DEPS, "conda run conda env update -f env.yml") == 2


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


_REBASE = "block_git_rebase.sh"


def test_rebase_blocks_plain_rebase() -> None:
    """`git rebase <upstream>` is blocked — rewriting history is forbidden."""
    assert _run_hook(_REBASE, "git rebase origin/dev") == 2


def test_rebase_blocks_interactive_rebase() -> None:
    """`git rebase -i` is blocked like any other rebase invocation."""
    assert _run_hook(_REBASE, "git rebase -i HEAD~3") == 2


def test_rebase_blocks_pull_rebase_long_flag() -> None:
    """`git pull --rebase` replays local commits onto upstream — also blocked."""
    assert _run_hook(_REBASE, "git pull --rebase origin dev") == 2


def test_rebase_blocks_pull_rebase_short_flag() -> None:
    """`git pull -r` (the short `--rebase`) is blocked too."""
    assert _run_hook(_REBASE, "git pull -r") == 2


def test_rebase_has_no_agent_bypass() -> None:
    """Even forge:git-commit-push cannot rebase — the block has no bypass."""
    assert (
        _run_hook(_REBASE, "git rebase origin/dev", agent_type="forge:git-commit-push")
        == 2
    )


def test_rebase_allows_plain_pull() -> None:
    """A non-rebase `git pull` is allowed (merge is the sanctioned sync)."""
    assert _run_hook(_REBASE, "git pull origin dev") == 0


def test_rebase_allows_merge() -> None:
    """`git merge` — the sanctioned way to sync a branch — is allowed."""
    assert _run_hook(_REBASE, "git merge origin/dev") == 0


def test_rebase_ignores_word_in_commit_message() -> None:
    """The word "rebase" inside a quoted commit body does not false-positive."""
    assert _run_hook(_REBASE, 'git commit -m "explain why we avoid rebase here"') == 0


_FORCE_PUSH = "block_force_push.sh"


def test_force_push_blocks_long_force_flag() -> None:
    """`git push --force` is blocked."""
    assert _run_hook(_FORCE_PUSH, "git push --force origin main") == 2


def test_force_push_blocks_force_with_lease() -> None:
    """`git push --force-with-lease` (and its =value form) is blocked."""
    assert _run_hook(_FORCE_PUSH, "git push --force-with-lease=origin/main") == 2


def test_force_push_blocks_short_f_flag() -> None:
    """`git push -f` (short --force) is blocked — the pre-hardening gap."""
    assert _run_hook(_FORCE_PUSH, "git push -f origin main") == 2


def test_force_push_blocks_combined_short_flag_cluster() -> None:
    """A short-flag cluster containing `f` (`-uf`) is blocked."""
    assert _run_hook(_FORCE_PUSH, "git push -uf origin feat") == 2


def test_force_push_blocks_plus_refspec() -> None:
    """A `+`-prefixed force refspec (`origin +main`) is blocked."""
    assert _run_hook(_FORCE_PUSH, "git push origin +main") == 2


def test_force_push_allows_plain_push() -> None:
    """A normal `git push origin main` is allowed."""
    assert _run_hook(_FORCE_PUSH, "git push origin main") == 0


def test_force_push_allows_set_upstream() -> None:
    """`git push -u origin feat` (no `f`) is not mistaken for a force push."""
    assert _run_hook(_FORCE_PUSH, "git push -u origin feat") == 0


def test_force_push_allows_follow_tags() -> None:
    """`--follow-tags` contains no short `-f` cluster and is allowed."""
    assert _run_hook(_FORCE_PUSH, "git push --follow-tags origin main") == 0
