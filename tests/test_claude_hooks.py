"""Tests for the ``claude-hooks/*.sh`` PreToolUse safety hooks.

# MOCKING STRATEGY: each hook is a standalone bash + jq script. A test runs
# it as a subprocess with a synthesized ``{"tool_input":{"command": …}}``
# stdin and asserts the exit code (0 = allowed, 2 = blocked). No forge
# Python is exercised — this is a black-box harness over the shell hooks.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from tests.conftest import GIT_ENV, init_git_repo


_HOOKS_DIR = Path(__file__).resolve().parents[1] / "claude-hooks"


def _run_hook_proc(
    name: str,
    command: str,
    *,
    agent_type: str = "",
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a claude-hook with *command* as the tool_input and return the process.

    Args:
        name: Hook filename under ``claude-hooks/`` (e.g.
            ``"block_claude_attribution.sh"``).
        command: The ``Bash`` tool command the hook inspects.
        agent_type: Optional ``agent_type`` payload field — set to
            ``"forge:git-commit-push"`` to exercise the sanctioned-agent
            bypass path.
        cwd: Directory to run the hook from. Hooks that resolve state via
            ``git rev-parse --show-toplevel`` need a real git repo here.
        env: Optional environment for the hook process — lets a test set a
            standalone env var (e.g. ``FORGE_SKIP_WRAPUP_GATE``) rather than
            embedding it in *command*. ``None`` inherits the caller's
            environment (the default `subprocess.run` behavior).

    Returns:
        The completed subprocess (exit code + captured stdout/stderr).
    """
    tool_input: dict[str, object] = {"tool_input": {"command": command}}
    if agent_type:
        tool_input["agent_type"] = agent_type
    payload = json.dumps(tool_input)
    return subprocess.run(
        ["bash", str(_HOOKS_DIR / name)],
        input=payload,
        capture_output=True,
        text=True,
        check=False,
        cwd=cwd,
        env=env,
    )


def _run_hook(
    name: str,
    command: str,
    *,
    agent_type: str = "",
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> int:
    """Run a claude-hook with *command* as the tool_input and return its exit code.

    Args:
        name: Hook filename under ``claude-hooks/`` (e.g.
            ``"block_claude_attribution.sh"``).
        command: The ``Bash`` tool command the hook inspects.
        agent_type: Optional ``agent_type`` payload field — set to
            ``"forge:git-commit-push"`` to exercise the sanctioned-agent
            bypass path.
        cwd: Directory to run the hook from — see `_run_hook_proc`.
        env: Optional environment for the hook process — see `_run_hook_proc`.

    Returns:
        The hook's process exit code — ``0`` (allow) or ``2`` (block).
    """
    return _run_hook_proc(
        name, command, agent_type=agent_type, cwd=cwd, env=env
    ).returncode


@pytest.fixture
def git_repo_with_commit(tmp_path: Path) -> tuple[Path, str]:
    """A tmp git repo with one commit, for hooks that shell out to `git rev-parse`.

    Returns:
        The repo root path and its HEAD commit's full sha.
    """
    init_git_repo(tmp_path)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=tmp_path,
        env=GIT_ENV,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return tmp_path, sha


def _write_wrapup(repo: Path, sha: str) -> None:
    """Write a minimal `code_health/pr_wrapup.md` naming *sha* in `verified-at:`.

    Args:
        repo: Repo root to write under — matches the hook's
            `--show-toplevel` resolution.
        sha: Commit sha (full or short) to embed in the `verified-at:` line.
    """
    code_health = repo / "code_health"
    code_health.mkdir(parents=True, exist_ok=True)
    (code_health / "pr_wrapup.md").write_text(f"# PR Wrap-up\n\nverified-at: {sha}\n")


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
    """`generated with the claude model` blocks via the `.{0,4}` separator bound."""
    assert (
        _run_hook(_ATTRIBUTION, 'git commit -m "x\n\ngenerated with the claude model"')
        == 2
    )


def test_attribution_allows_benign_generated_prose() -> None:
    """`generated with care` passes — no vendor term near the verb."""
    assert _run_hook(_ATTRIBUTION, 'git commit -m "x\n\ngenerated with care"') == 0


def test_attribution_blocks_newer_credit_phrasings() -> None:
    """`ai-generated`, `assisted by ai`, and vendor-credit phrases block.

    These alternatives mirror the newer entries in
    `forge.pr_squash_comment.AI_ATTRIBUTION_PATTERNS` (added by hand —
    this hook stays deliberately narrower than the Python list).
    """
    for body in (
        "ai-generated fix",
        "assisted by AI throughout",
        "built with claude code",
        "authored by claude",
    ):
        assert _run_hook(_ATTRIBUTION, f'git commit -m "x\n\n{body}"') == 2


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


def test_force_push_blocks_chained_after_separator() -> None:
    """A force push chained after a separator (`foo; git push -f`) is blocked.

    Regression: the outer gate must anchor after a shell separator, not only
    at string-start, or a chained command bypasses the block entirely.
    """
    assert _run_hook(_FORCE_PUSH, "true; git push --force origin main") == 2


def test_force_push_blocks_doubled_space() -> None:
    """`git  push -f` (a doubled space) is blocked — the gate allows any ws."""
    assert _run_hook(_FORCE_PUSH, "git  push -f origin main") == 2


def test_force_push_allows_non_push_git() -> None:
    """A non-push git command (`git status`) is not inspected."""
    assert _run_hook(_FORCE_PUSH, "git status") == 0


# --- shared-anchor prefix bypasses (env-var / leading-ws / wrapper) --------
# The anchor in block_force_push / block_raw_git / block_git_rebase must
# recognize `git` even behind an inline env assignment, leading whitespace, or
# a `command`/`env` wrapper — common shell idioms, not adversarial tricks.

_RAW_GIT = "block_raw_git.sh"


def test_force_push_blocks_env_var_prefix() -> None:
    """`GIT_DIR=/tmp/x git push -f` (inline env assignment) is blocked."""
    assert _run_hook(_FORCE_PUSH, "GIT_DIR=/tmp/x git push -f origin main") == 2


def test_force_push_blocks_leading_whitespace() -> None:
    """A force push with leading whitespace (`   git push -f`) is blocked."""
    assert _run_hook(_FORCE_PUSH, "   git push -f origin main") == 2


def test_force_push_blocks_command_wrapper() -> None:
    """`command git push -f` (builtin wrapper) is blocked."""
    assert _run_hook(_FORCE_PUSH, "command git push -f origin main") == 2


def test_force_push_allows_env_prefix_non_force() -> None:
    """An env-prefixed non-force push is still allowed (no over-block)."""
    assert _run_hook(_FORCE_PUSH, "GIT_DIR=/tmp/x git push origin main") == 0


def test_force_push_blocks_subshell_and_flagged_wrapper() -> None:
    """Subshell parens and flag tokens between wrapper and git still block."""
    assert _run_hook(_FORCE_PUSH, "(git push --force origin main)") == 2
    assert _run_hook(_FORCE_PUSH, "sudo -n git push -f origin main") == 2


def test_rebase_blocks_subshell_and_flagged_wrapper() -> None:
    """Subshell parens and flag tokens between wrapper and git still block."""
    assert _run_hook(_REBASE, "(git rebase origin/dev)") == 2
    assert _run_hook(_REBASE, "sudo -n git rebase origin/dev") == 2


def test_raw_git_blocks_subshell_and_flagged_wrapper() -> None:
    """Subshell parens and flag tokens between wrapper and git still block."""
    assert _run_hook(_RAW_GIT, "(git commit -m x)") == 2
    assert _run_hook(_RAW_GIT, "sudo -n git push origin feat") == 2


def test_raw_git_blocks_env_var_prefix_push() -> None:
    """`GIT_DIR=x git push` bypassed the raw-git gate before the anchor fix."""
    assert _run_hook(_RAW_GIT, "GIT_DIR=/tmp/x git push origin main") == 2


def test_raw_git_env_prefix_still_bypassable_slips_agent_bypass() -> None:
    """The git-commit-push bypass still applies under an env prefix."""
    assert (
        _run_hook(
            _RAW_GIT,
            "GIT_DIR=/tmp/x git push origin main",
            agent_type="forge:git-commit-push",
        )
        == 0
    )


def test_rebase_blocks_env_var_prefix() -> None:
    """`GIT_DIR=x git rebase main` (inline env assignment) is blocked."""
    assert _run_hook(_REBASE, "GIT_DIR=/tmp/x git rebase main") == 2


def test_rebase_blocks_leading_whitespace() -> None:
    """A rebase with leading whitespace (`   git rebase main`) is blocked."""
    assert _run_hook(_REBASE, "   git rebase main") == 2


_DESTRUCTIVE = "block_git_destructive.sh"


# --- git reset: blanket ban, all forms (#363 — user directive: "git reset
# absolutely all mode forbidden"). Agents unstage via `git restore --staged
# <path>` instead; that command stays allowed (tested below). -------------


def test_destructive_blocks_bare_reset() -> None:
    """A bare `git reset` (mixed reset) is blocked under the blanket ban."""
    assert _run_hook(_DESTRUCTIVE, "git reset") == 2


def test_destructive_blocks_soft_reset() -> None:
    """`git reset --soft HEAD~1` is blocked — the ban covers every reset mode."""
    assert _run_hook(_DESTRUCTIVE, "git reset --soft HEAD~1") == 2


def test_destructive_blocks_ref_only_reset() -> None:
    """`git reset HEAD~1` (no explicit mode flag) is blocked too."""
    assert _run_hook(_DESTRUCTIVE, "git reset HEAD~1") == 2


def test_destructive_blocks_path_reset() -> None:
    """`git reset -- some_file.py` (path-scoped unstage) is blocked.

    Use `git restore --staged some_file.py` instead — see
    `test_destructive_allows_restore_staged_path`.
    """
    assert _run_hook(_DESTRUCTIVE, "git reset -- some_file.py") == 2


def test_destructive_blocks_hard_with_ref() -> None:
    """`git reset --hard HEAD~1` (targeted hard reset) is blocked."""
    assert _run_hook(_DESTRUCTIVE, "git reset --hard HEAD~1") == 2


def test_destructive_blocks_merge_reset() -> None:
    """`git reset --merge` is blocked — same destruction class as `--hard`."""
    assert _run_hook(_DESTRUCTIVE, "git reset --merge") == 2


def test_destructive_blocks_reset_flag_glued_by_substitution() -> None:
    """A flag glued to an empty substitution (`$(echo)--hard`) still blocks.

    Bash concatenates an adjacent substitution with literal text into one
    argv word, so no whitespace precedes the flag in the raw command — but
    the blanket ban fires on the bare `reset` verb regardless.
    """
    assert _run_hook(_DESTRUCTIVE, "git reset $(echo)--hard") == 2


def test_destructive_blocks_reset_leading_whitespace() -> None:
    """A reset with leading whitespace (`   git reset HEAD~1`) is blocked."""
    assert _run_hook(_DESTRUCTIVE, "   git reset HEAD~1") == 2


def test_destructive_blocks_reset_env_var_prefix() -> None:
    """`GIT_DIR=/tmp/x git reset --soft HEAD~1` (inline env assignment) is blocked."""
    assert _run_hook(_DESTRUCTIVE, "GIT_DIR=/tmp/x git reset --soft HEAD~1") == 2


def test_destructive_blocks_reset_subshell_and_sudo_wrapper() -> None:
    """Subshell parens and flag tokens between wrapper and git still block."""
    assert _run_hook(_DESTRUCTIVE, "(git reset HEAD~1)") == 2
    assert _run_hook(_DESTRUCTIVE, "sudo -n git reset HEAD~1") == 2


def test_destructive_allows_restore_staged_path() -> None:
    """`git restore --staged some_file.py` — the sanctioned unstage — is allowed."""
    assert _run_hook(_DESTRUCTIVE, "git restore --staged some_file.py") == 0


def test_destructive_reset_has_no_agent_bypass() -> None:
    """Even forge:git-commit-push cannot reset — the block has no bypass."""
    assert _run_hook(_DESTRUCTIVE, "git reset", agent_type="forge:git-commit-push") == 2


def test_destructive_blocks_reset_compound_command() -> None:
    """A reset chained after `cd` (`cd /tmp/x && git reset --hard`) is blocked."""
    assert _run_hook(_DESTRUCTIVE, "cd /tmp/x && git reset --hard") == 2


def test_destructive_allows_reset_commit_message_mention() -> None:
    """The words "reset --hard" inside a quoted commit message do not false-positive."""
    assert _run_hook(_DESTRUCTIVE, 'git commit -m "never reset --hard here"') == 0


def test_destructive_allows_reset_non_git_tool() -> None:
    """A non-git command merely containing the words is not inspected."""
    assert _run_hook(_DESTRUCTIVE, "some-other-tool --hard reset") == 0


def test_destructive_reset_flag_in_other_compound_segment_stays_scoped() -> None:
    """A quoted `--hard` mention in one segment never taints another segment.

    The reset ban still only fires on an actual `git reset` invocation in
    the compound command, not on unrelated text elsewhere in the line.
    """
    assert _run_hook(_DESTRUCTIVE, "echo done --hard; echo unrelated") == 0


def test_destructive_allows_reset_stash_push() -> None:
    """`git stash -u` — the sanctioned dirty-tree sync step — is allowed."""
    assert _run_hook(_DESTRUCTIVE, "git stash -u") == 0


# --- git clean: -f/-d/-x/-X/--force block; dry-run (-n/--dry-run) allowed --


def test_destructive_blocks_clean_force_flag() -> None:
    """`git clean -f` is blocked."""
    assert _run_hook(_DESTRUCTIVE, "git clean -f") == 2


def test_destructive_blocks_clean_clustered_fdx() -> None:
    """`git clean -fdx` (clustered short flags) is blocked."""
    assert _run_hook(_DESTRUCTIVE, "git clean -fdx") == 2


def test_destructive_blocks_clean_separate_flags() -> None:
    """`git clean -f -d -x` (separate short flags) is blocked."""
    assert _run_hook(_DESTRUCTIVE, "git clean -f -d -x") == 2


def test_destructive_blocks_clean_long_force_flag() -> None:
    """`git clean --force` is blocked."""
    assert _run_hook(_DESTRUCTIVE, "git clean --force") == 2


def test_destructive_allows_clean_dry_run_short_flag() -> None:
    """`git clean -n` (dry run) is allowed — it only lists candidates."""
    assert _run_hook(_DESTRUCTIVE, "git clean -n") == 0


def test_destructive_allows_clean_dry_run_long_flag() -> None:
    """`git clean --dry-run` is allowed."""
    assert _run_hook(_DESTRUCTIVE, "git clean --dry-run") == 0


def test_destructive_allows_clean_dry_run_clustered_with_force() -> None:
    """`-nf` (dry-run clustered with force in the same token) short-circuits allowed.

    The dry-run check runs first, so a dry-run flag anywhere in the
    invocation stands down the force check even when `-f` sits in the
    same cluster.
    """
    assert _run_hook(_DESTRUCTIVE, "git clean -nf") == 0


def test_destructive_allows_clean_dry_run_and_force_as_separate_flags() -> None:
    """`--dry-run --force` (both present) is allowed — dry-run wins."""
    assert _run_hook(_DESTRUCTIVE, "git clean --dry-run --force") == 0


def test_destructive_allows_bare_clean() -> None:
    """A bare `git clean` (no flags at all) is allowed — nothing to delete yet."""
    assert _run_hook(_DESTRUCTIVE, "git clean") == 0


def test_destructive_blocks_clean_bounded_per_invocation() -> None:
    """`git clean -n; git clean -f` — the second invocation still blocks.

    The dry-run short-circuit is bounded to the same invocation
    (`[^;&|]*`), so an earlier dry-run in a chained command must not
    launder a later forceful `git clean`.
    """
    assert _run_hook(_DESTRUCTIVE, "git clean -n; git clean -f") == 2


# --- git checkout / restore: literal discard-everything only --------------


def test_destructive_blocks_checkout_dot() -> None:
    """`git checkout .` discards every uncommitted modification — blocked."""
    assert _run_hook(_DESTRUCTIVE, "git checkout .") == 2


def test_destructive_blocks_checkout_dash_dash_dot() -> None:
    """`git checkout -- .` (explicit pathspec separator) is blocked too."""
    assert _run_hook(_DESTRUCTIVE, "git checkout -- .") == 2


def test_destructive_blocks_restore_dot() -> None:
    """`git restore .` discards every uncommitted modification — blocked."""
    assert _run_hook(_DESTRUCTIVE, "git restore .") == 2


def test_destructive_blocks_checkout_dot_after_separator() -> None:
    """`git status; git checkout .` — chained after a separator — is blocked."""
    assert _run_hook(_DESTRUCTIVE, "git status; git checkout .") == 2


def test_destructive_allows_checkout_branch() -> None:
    """`git checkout main` (branch switch) is allowed."""
    assert _run_hook(_DESTRUCTIVE, "git checkout main") == 0


def test_destructive_allows_checkout_subdir_path() -> None:
    """`git checkout ./subdir` (a path, not the whole-tree `.`) is allowed."""
    assert _run_hook(_DESTRUCTIVE, "git checkout ./subdir") == 0


def test_destructive_allows_checkout_ours_path() -> None:
    """`git checkout --ours -- some_file.py` (path-scoped) is allowed."""
    assert _run_hook(_DESTRUCTIVE, "git checkout --ours -- some_file.py") == 0


def test_destructive_allows_checkout_ref_path() -> None:
    """`git checkout main -- some_file.py` (ref + path) is allowed."""
    assert _run_hook(_DESTRUCTIVE, "git checkout main -- some_file.py") == 0


def test_destructive_allows_restore_path() -> None:
    """`git restore some_file.py` (path-scoped restore) is allowed."""
    assert _run_hook(_DESTRUCTIVE, "git restore some_file.py") == 0


def test_destructive_allows_restore_staged_dot() -> None:
    """`git restore --staged .` unstages everything but keeps the worktree — allowed.

    Distinct from `git restore .` (no `--staged`): this only touches the
    index, never the working tree, so it isn't in the discard-everything
    class the bare form is.
    """
    assert _run_hook(_DESTRUCTIVE, "git restore --staged .") == 0


# --- git stash: drop/clear block; pop/list/push allowed --------------------


def test_destructive_blocks_stash_drop() -> None:
    """`git stash drop` is blocked — a dropped stash is unreferenced."""
    assert _run_hook(_DESTRUCTIVE, "git stash drop") == 2


def test_destructive_blocks_stash_clear() -> None:
    """`git stash clear` is blocked — same unrecoverable-deletion class."""
    assert _run_hook(_DESTRUCTIVE, "git stash clear") == 2


def test_destructive_allows_stash_pop() -> None:
    """`git stash pop` is allowed — part of FOUNDATION §2's sanctioned dance."""
    assert _run_hook(_DESTRUCTIVE, "git stash pop") == 0


def test_destructive_allows_stash_list() -> None:
    """`git stash list` (read-only) is allowed."""
    assert _run_hook(_DESTRUCTIVE, "git stash list") == 0


def test_destructive_allows_stash_push_with_message() -> None:
    """`git stash push -u -m "wip"` (the sanctioned dirty-tree sync) is allowed."""
    assert _run_hook(_DESTRUCTIVE, 'git stash push -u -m "wip"') == 0


# --- shared anchor idiom, one representative case per family ---------------


def test_destructive_blocks_clean_env_var_prefix() -> None:
    """`GIT_DIR=/tmp/x git clean -fd` (inline env assignment) is blocked."""
    assert _run_hook(_DESTRUCTIVE, "GIT_DIR=/tmp/x git clean -fd") == 2


def test_destructive_blocks_clean_subshell() -> None:
    """`(git clean -f)` — subshell-wrapped — is blocked."""
    assert _run_hook(_DESTRUCTIVE, "(git clean -f)") == 2


def test_destructive_blocks_clean_sudo_wrapper() -> None:
    """`sudo -n git clean -f` (flagged wrapper) is blocked."""
    assert _run_hook(_DESTRUCTIVE, "sudo -n git clean -f") == 2


def test_destructive_blocks_checkout_dot_env_var_prefix() -> None:
    """`GIT_DIR=/tmp/x git checkout .` (inline env assignment) is blocked."""
    assert _run_hook(_DESTRUCTIVE, "GIT_DIR=/tmp/x git checkout .") == 2


def test_destructive_blocks_stash_drop_command_wrapper() -> None:
    """`command git stash drop` (builtin wrapper) is blocked."""
    assert _run_hook(_DESTRUCTIVE, "command git stash drop") == 2


# --- quoted mentions per family stay allowed --------------------------------


def test_destructive_allows_clean_mention_in_commit_message() -> None:
    """`git clean -fd` mentioned inside a quoted commit body is allowed."""
    assert _run_hook(_DESTRUCTIVE, 'git commit -m "run git clean -fd later"') == 0


def test_destructive_allows_checkout_dot_mention_in_commit_message() -> None:
    """`git checkout .` mentioned inside a quoted commit body is allowed."""
    assert _run_hook(_DESTRUCTIVE, 'git commit -m "just git checkout . for now"') == 0


def test_destructive_allows_stash_drop_mention_in_commit_message() -> None:
    """`git stash drop` mentioned inside a quoted commit body is allowed."""
    assert _run_hook(_DESTRUCTIVE, 'git commit -m "don\'t git stash drop"') == 0


# --- registration / retirement -----------------------------------------


def test_destructive_registered_and_reset_hard_retired() -> None:
    """plugin.json wires block_git_destructive.sh in; block_git_reset_hard.sh is gone.

    `block_git_destructive.sh` subsumes and retires the older, narrower
    `block_git_reset_hard.sh` (#363's blanket-ban widening) — the manifest
    must reference only the new hook.
    """
    manifest = json.loads(
        (_HOOKS_DIR.parent / ".claude-plugin" / "plugin.json").read_text()
    )
    pre_tool_use = manifest["hooks"]["PreToolUse"]
    commands = [hook["command"] for group in pre_tool_use for hook in group["hooks"]]
    assert any(_DESTRUCTIVE in cmd for cmd in commands)
    assert not any("block_git_reset_hard.sh" in cmd for cmd in commands)


def test_destructive_old_hook_file_removed() -> None:
    """The retired `block_git_reset_hard.sh` file no longer exists on disk."""
    assert not (_HOOKS_DIR / "block_git_reset_hard.sh").exists()


_CONTINUATION_DELETE = "block_continuation_delete.sh"


def test_continuation_delete_allows_sibling_file() -> None:
    """Deleting a `.plan/` sibling file (not CONTINUATION.md) is allowed (#241).

    The issue's reported false positive: a weekly-summary file living
    alongside CONTINUATION.md must stay deletable.
    """
    assert _run_hook(_CONTINUATION_DELETE, "rm .plan/weekly_summary_2026-07-10.md") == 0


def test_continuation_delete_allows_interpreter_one_liner() -> None:
    """A `python -c` one-liner unlink slips through — documented, accepted gap."""
    assert (
        _run_hook(
            _CONTINUATION_DELETE,
            "python -c \"import pathlib; pathlib.Path('.plan/w.md').unlink()\"",
        )
        == 0
    )


def test_continuation_delete_allows_quoted_prose_mention() -> None:
    """`rm`/`unlink` wording quoted inside prose (e.g. an issue body) is allowed."""
    assert (
        _run_hook(
            _CONTINUATION_DELETE,
            'gh issue create --body "blocked rm .plan/weekly.md and unlink"',
        )
        == 0
    )


def test_continuation_delete_allows_non_plan_path() -> None:
    """`rm foo.plan` (a file merely ending in `.plan`) is not the `.plan/` dir."""
    assert _run_hook(_CONTINUATION_DELETE, "rm foo.plan") == 0


def test_continuation_delete_allows_mention_without_delete() -> None:
    """Mentioning CONTINUATION.md without a delete verb is allowed."""
    assert _run_hook(_CONTINUATION_DELETE, "echo CONTINUATION.md") == 0


def test_continuation_delete_allows_non_delete_action() -> None:
    """A non-delete action on `.plan` (`ls`) is allowed."""
    assert _run_hook(_CONTINUATION_DELETE, "ls .plan") == 0


def test_continuation_delete_blocks_continuation_md_direct() -> None:
    """`rm .plan/CONTINUATION.md` — the direct target — is blocked."""
    assert _run_hook(_CONTINUATION_DELETE, "rm .plan/CONTINUATION.md") == 2


def test_continuation_delete_blocks_plan_directory() -> None:
    """`rm -rf .plan` and its trailing-slash form both take CONTINUATION.md down."""
    assert _run_hook(_CONTINUATION_DELETE, "rm -rf .plan") == 2
    assert _run_hook(_CONTINUATION_DELETE, "rm -rf .plan/") == 2


def test_continuation_delete_blocks_glob_forms() -> None:
    """Glob shapes expanding to the directory's contents stay blocked.

    Security review of the anchor rewrite: the shell expands these to the
    directory's files (taking CONTINUATION.md down) even though the path
    text does not literally end at `.plan` — the terminator classes must
    treat glob metacharacters and `/.`-style suffixes as whole-directory
    deletion, not as named siblings.
    """
    assert _run_hook(_CONTINUATION_DELETE, "rm -rf .plan/*") == 2
    assert _run_hook(_CONTINUATION_DELETE, "rm -rf .plan*") == 2
    assert _run_hook(_CONTINUATION_DELETE, "rm .plan?") == 2
    assert _run_hook(_CONTINUATION_DELETE, "rm -rf .plan/{a,b}") == 2
    assert _run_hook(_CONTINUATION_DELETE, "rm -rf .plan/.") == 2


def test_continuation_delete_blocks_dotfile_sibling_by_design() -> None:
    """A dotfile sibling matches the `/.` form — accepted safe-erring bias."""
    assert _run_hook(_CONTINUATION_DELETE, "rm .plan/.gitkeep") == 2


def test_continuation_delete_blocks_flagged_wrapper_forms() -> None:
    """Flag tokens between wrapper and verb must not break the anchor chain.

    Design review of the anchor rewrite: without a flag-tolerant wrapper
    group, `xargs -0 rm` / `xargs -I{} rm {}` / `sudo -n rm` — all
    blocked by the pre-rewrite pattern — would silently slip through,
    and the `-0` form is the realistic space-safe idiom.
    """
    assert _run_hook(_CONTINUATION_DELETE, "find .plan -print0 | xargs -0 rm") == 2
    assert _run_hook(_CONTINUATION_DELETE, "find .plan | xargs -I{} rm {}") == 2
    assert _run_hook(_CONTINUATION_DELETE, "sudo -n rm -rf .plan") == 2


def test_continuation_delete_allows_flag_lookalike_without_delete() -> None:
    """Flag tolerance must not over-block commands with no delete verb."""
    assert (
        _run_hook(_CONTINUATION_DELETE, "git commit -m 'refactor plan handling'") == 0
    )


def test_continuation_delete_blocks_relative_dot_slash_path() -> None:
    """`rm ./.plan` (leading `./`) is still recognized as the `.plan` directory."""
    assert _run_hook(_CONTINUATION_DELETE, "rm ./.plan") == 2


def test_continuation_delete_blocks_chained_commands() -> None:
    """A chained delete is blocked regardless of separator spacing.

    Covers `&&` with a space, `&&` glued directly to the prior token (no
    space), and `;` — the anchor must not assume whitespace around the
    separator.
    """
    assert _run_hook(_CONTINUATION_DELETE, "echo x && rm .plan") == 2
    assert _run_hook(_CONTINUATION_DELETE, "rm -rf .plan&&git status") == 2
    assert _run_hook(_CONTINUATION_DELETE, "rm .plan;echo done") == 2


def test_continuation_delete_blocks_subshell() -> None:
    """`(rm .plan)` — a subshell-wrapped delete — is blocked."""
    assert _run_hook(_CONTINUATION_DELETE, "(rm .plan)") == 2


def test_continuation_delete_blocks_env_var_prefix() -> None:
    """An inline env assignment before `rm` (`FOO=1 rm ...`) does not bypass."""
    assert _run_hook(_CONTINUATION_DELETE, "FOO=1 rm .plan/CONTINUATION.md") == 2


def test_continuation_delete_blocks_sudo_prefix() -> None:
    """`sudo rm -rf .plan` is blocked like the unprivileged form."""
    assert _run_hook(_CONTINUATION_DELETE, "sudo rm -rf .plan") == 2


def test_continuation_delete_blocks_unlink_verb() -> None:
    """`unlink` (not just `rm`) targeting CONTINUATION.md is blocked."""
    assert _run_hook(_CONTINUATION_DELETE, "unlink .plan/CONTINUATION.md") == 2


def test_continuation_delete_blocks_xargs_pipe_regression() -> None:
    """`find .plan | xargs rm` is blocked — the deliberate anchor addition.

    Unlike the git-hook family idiom (which accepts `xargs` slip-through per
    block_install_deps.sh's stance), this hook keeps `xargs` in the wrapper
    list because deletion is irreversible. A future anchor-alignment cleanup
    that copies the git-hook idiom verbatim would silently regress this case.
    """
    assert _run_hook(_CONTINUATION_DELETE, "find .plan -name '*.md' | xargs rm") == 2


def test_continuation_delete_blocks_multiline_command_body() -> None:
    """A multiline command body with `rm .plan/CONTINUATION.md` on its own line blocks.

    Documents current behavior shared with the whole `RM_ANCHOR` family: the
    `^` anchor matches per-line in grep, not just at string-start, so a
    delete buried in a multiline body is still caught. Blocked today by
    design — the safe-erring direction.
    """
    assert (
        _run_hook(_CONTINUATION_DELETE, "line one\nrm .plan/CONTINUATION.md\nline") == 2
    )


_UNVERIFIED_PR_CREATE = "block_unverified_pr_create.sh"


def test_unverified_pr_create_ignores_non_pr_create_command() -> None:
    """A command that isn't `gh pr create` is allowed — no git state is touched."""
    assert _run_hook(_UNVERIFIED_PR_CREATE, "git status") == 0


def test_unverified_pr_create_blocks_missing_wrapup(
    git_repo_with_commit: tuple[Path, str],
) -> None:
    """`gh pr create` with no authored `code_health/pr_wrapup.md` is blocked (§6)."""
    repo, _sha = git_repo_with_commit
    proc = _run_hook_proc(_UNVERIFIED_PR_CREATE, "gh pr create --title x", cwd=repo)
    assert proc.returncode == 2
    assert "authored wrap-up" in proc.stderr


def test_unverified_pr_create_allows_full_sha_match(
    git_repo_with_commit: tuple[Path, str],
) -> None:
    """A wrap-up whose `verified-at:` line names the full current HEAD sha passes."""
    repo, sha = git_repo_with_commit
    _write_wrapup(repo, sha)
    assert _run_hook(_UNVERIFIED_PR_CREATE, "gh pr create --title x", cwd=repo) == 0


def test_unverified_pr_create_allows_short_sha_match(
    git_repo_with_commit: tuple[Path, str],
) -> None:
    """The hook also accepts the 7-char short-sha form in `verified-at:`."""
    repo, sha = git_repo_with_commit
    _write_wrapup(repo, sha[:7])
    assert _run_hook(_UNVERIFIED_PR_CREATE, "gh pr create --title x", cwd=repo) == 0


def test_unverified_pr_create_blocks_stale_sha(
    git_repo_with_commit: tuple[Path, str],
) -> None:
    """A wrap-up naming a different commit than current HEAD is blocked."""
    repo, _sha = git_repo_with_commit
    _write_wrapup(repo, "0" * 40)
    proc = _run_hook_proc(_UNVERIFIED_PR_CREATE, "gh pr create --title x", cwd=repo)
    assert proc.returncode == 2
    assert "does not name current HEAD" in proc.stderr


def test_unverified_pr_create_blocks_after_separator(
    git_repo_with_commit: tuple[Path, str],
) -> None:
    """`git status && gh pr create` — the separator-chained form — is still blocked.

    No wrap-up is authored here, so the assertion documents the outer
    separator-anchored match, not the sha-comparison branch.
    """
    repo, _sha = git_repo_with_commit
    assert (
        _run_hook(
            _UNVERIFIED_PR_CREATE, "git status && gh pr create --title x", cwd=repo
        )
        == 2
    )


def test_unverified_pr_create_allows_text_mention() -> None:
    """`echo gh pr create` (plain space, no separator) is a text mention, not a call.

    Mirrors `block_pr_merge.sh`'s convention: a bare space ahead of `gh` is
    not a shell separator, so quoting/echoing the command text is harmless.
    """
    assert _run_hook(_UNVERIFIED_PR_CREATE, "echo gh pr create") == 0


def test_unverified_pr_create_allows_skip_gate_env_prefix(
    git_repo_with_commit: tuple[Path, str],
) -> None:
    """`FORGE_SKIP_WRAPUP_GATE=1 gh pr create` bypasses the gate — even with no wrap-up.

    The skip check runs after the create-match but before the missing-file
    check, so this must pass with `code_health/pr_wrapup.md` absent — the
    sanctioned user-requested bypass documented in the hook's header comment.
    """
    repo, _sha = git_repo_with_commit
    assert (
        _run_hook(
            _UNVERIFIED_PR_CREATE,
            "FORGE_SKIP_WRAPUP_GATE=1 gh pr create --title x",
            cwd=repo,
        )
        == 0
    )


def test_unverified_pr_create_allows_skip_gate_standalone_env_var(
    git_repo_with_commit: tuple[Path, str],
) -> None:
    """The gate also honors `FORGE_SKIP_WRAPUP_GATE=1` set as a real env var.

    Not embedded in the command string this time — passed to the hook's own
    process environment, the other sanctioned form the hook checks
    (`[ "${FORGE_SKIP_WRAPUP_GATE:-}" = "1" ]`).
    """
    repo, _sha = git_repo_with_commit
    assert (
        _run_hook(
            _UNVERIFIED_PR_CREATE,
            "gh pr create --title x",
            cwd=repo,
            env={**os.environ, "FORGE_SKIP_WRAPUP_GATE": "1"},
        )
        == 0
    )


def test_unverified_pr_create_allows_non_git_cwd_with_wrapup(tmp_path: Path) -> None:
    """Outside any git repo, the hook falls back to `.` and still needs a wrap-up.

    The file check runs first (`REPO_ROOT` falls back to `.` when `git
    rev-parse --show-toplevel` fails outside a repo); once the wrap-up file
    exists, `HEAD_SHA` then comes back empty too, and the hook exits 0
    before ever comparing `verified-at:` — the git-less fallback the hook's
    tail comment documents.
    """
    (tmp_path / "code_health").mkdir()
    (tmp_path / "code_health" / "pr_wrapup.md").write_text("verified-at: deadbeef\n")
    assert _run_hook(_UNVERIFIED_PR_CREATE, "gh pr create --title x", cwd=tmp_path) == 0


def test_unverified_pr_create_blocks_token_mention_in_argument(
    git_repo_with_commit: tuple[Path, str],
) -> None:
    """A --title merely MENTIONING the skip token does not bypass the gate.

    Regression: the embedded skip form was an unanchored substring grep, so
    PR prose discussing `FORGE_SKIP_WRAPUP_GATE=1` (plausible in this repo)
    silently disabled the gate. The token must sit at command position,
    directly prefixing the create invocation.
    """
    repo, _sha = git_repo_with_commit
    assert (
        _run_hook(
            _UNVERIFIED_PR_CREATE,
            'gh pr create --title "docs mention FORGE_SKIP_WRAPUP_GATE=1"',
            cwd=repo,
        )
        == 2
    )


def test_unverified_pr_create_allows_release_branch_without_wrapup(
    git_repo_with_commit: tuple[Path, str],
) -> None:
    """A `release/vX.Y.Z` branch whose tree reproduces tag `vX.Y.Z` self-exempts.

    The exemption demands provenance, not just naming (per the hook's header
    comment): the `vX.Y.Z` tag must exist AND HEAD's tree must reproduce it.
    Tagging HEAD before branching satisfies both, so no wrap-up is needed —
    the `/promote` flow has no Step 3.92; its verification is the
    release-fingerprint check instead.
    """
    repo, sha = git_repo_with_commit
    subprocess.run(["git", "tag", "v1.2.3", sha], cwd=repo, env=GIT_ENV, check=True)
    subprocess.run(
        ["git", "checkout", "-q", "-b", "release/v1.2.3"],
        cwd=repo,
        env=GIT_ENV,
        check=True,
    )
    assert _run_hook(_UNVERIFIED_PR_CREATE, "gh pr create --title x", cwd=repo) == 0


def test_unverified_pr_create_blocks_release_branch_suffix(
    git_repo_with_commit: tuple[Path, str],
) -> None:
    """`release/v1.2.3-rc1` does not match the anchored `release/vX.Y.Z` regex.

    Regression guard for the exemption's anchoring: a suffixed branch name
    (e.g. a release-candidate) must fall through to the normal wrap-up gate
    rather than silently exempting itself.
    """
    repo, _sha = git_repo_with_commit
    subprocess.run(
        ["git", "checkout", "-q", "-b", "release/v1.2.3-rc1"],
        cwd=repo,
        env=GIT_ENV,
        check=True,
    )
    proc = _run_hook_proc(_UNVERIFIED_PR_CREATE, "gh pr create --title x", cwd=repo)
    assert proc.returncode == 2
    assert "authored wrap-up" in proc.stderr


def test_unverified_pr_create_blocks_spoofed_release_branch(
    git_repo_with_commit: tuple[Path, str],
) -> None:
    """A branch merely NAMED `release/vX.Y.Z` with no matching tag is not exempt.

    Regression for the naming-only exemption this replaced: without a
    `v9.9.9` tag to reproduce, the hook withholds the exemption and falls
    through to the normal (missing-wrap-up) gate.
    """
    repo, _sha = git_repo_with_commit
    subprocess.run(
        ["git", "checkout", "-q", "-b", "release/v9.9.9"],
        cwd=repo,
        env=GIT_ENV,
        check=True,
    )
    proc = _run_hook_proc(_UNVERIFIED_PR_CREATE, "gh pr create --title x", cwd=repo)
    assert proc.returncode == 2
    assert "promotion exemption withheld" in proc.stderr


def test_unverified_pr_create_allows_release_branch_changelog_only_divergence(
    git_repo_with_commit: tuple[Path, str],
) -> None:
    """A tree diverging from its tag ONLY in `CHANGELOG.md` stays exempt.

    `CHANGELOG.md` is the one tolerated divergence (the curated release-notes
    entry written after tagging) — the hook diffs `tag..HEAD` excluding it.
    """
    repo, sha = git_repo_with_commit
    subprocess.run(["git", "tag", "v1.2.3", sha], cwd=repo, env=GIT_ENV, check=True)
    subprocess.run(
        ["git", "checkout", "-q", "-b", "release/v1.2.3"],
        cwd=repo,
        env=GIT_ENV,
        check=True,
    )
    (repo / "CHANGELOG.md").write_text("## v1.2.3\n\n- release notes\n")
    subprocess.run(["git", "add", "CHANGELOG.md"], cwd=repo, env=GIT_ENV, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "changelog"], cwd=repo, env=GIT_ENV, check=True
    )
    assert _run_hook(_UNVERIFIED_PR_CREATE, "gh pr create --title x", cwd=repo) == 0


def test_unverified_pr_create_blocks_release_branch_non_changelog_divergence(
    git_repo_with_commit: tuple[Path, str],
) -> None:
    """A tree diverging from its tag in a file OTHER than `CHANGELOG.md` is not exempt.

    The excluded-path diff (`':(exclude)CHANGELOG.md'`) is scoped to that one
    file — any other post-tag edit means the tree no longer reproduces the
    release, so the promotion exemption is withheld.
    """
    repo, sha = git_repo_with_commit
    subprocess.run(["git", "tag", "v1.2.3", sha], cwd=repo, env=GIT_ENV, check=True)
    subprocess.run(
        ["git", "checkout", "-q", "-b", "release/v1.2.3"],
        cwd=repo,
        env=GIT_ENV,
        check=True,
    )
    (repo / "other.txt").write_text("post-tag change\n")
    subprocess.run(["git", "add", "other.txt"], cwd=repo, env=GIT_ENV, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "unrelated change"],
        cwd=repo,
        env=GIT_ENV,
        check=True,
    )
    proc = _run_hook_proc(_UNVERIFIED_PR_CREATE, "gh pr create --title x", cwd=repo)
    assert proc.returncode == 2
    assert "promotion exemption withheld" in proc.stderr


def test_unverified_pr_create_provenance_check_is_cwd_independent(
    git_repo_with_commit: tuple[Path, str],
) -> None:
    """A non-CHANGELOG divergence is detected even from a subdirectory cwd.

    Regression: the divergence diff used a cwd-relative `.` pathspec, so a
    hook invocation from a subdirectory silently narrowed the check —
    files diverging elsewhere in the tree became invisible and a
    tag-diverged branch wrongly earned the exemption. The top-anchored
    pathspecs make the check cwd-independent.
    """
    repo, sha = git_repo_with_commit
    subprocess.run(["git", "tag", "v1.2.3", sha], cwd=repo, env=GIT_ENV, check=True)
    subprocess.run(
        ["git", "checkout", "-q", "-b", "release/v1.2.3"],
        cwd=repo,
        env=GIT_ENV,
        check=True,
    )
    (repo / "outside.txt").write_text("diverges\n", encoding="utf-8")
    subprocess.run(["git", "add", "outside.txt"], cwd=repo, env=GIT_ENV, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "diverge"], cwd=repo, env=GIT_ENV, check=True
    )
    subdir = repo / "sub"
    subdir.mkdir()
    proc = _run_hook_proc(_UNVERIFIED_PR_CREATE, "gh pr create --title x", cwd=subdir)
    assert proc.returncode == 2
    assert "promotion exemption withheld" in proc.stderr


# --- block_fixer_recon.sh: agent-scoped Bash allowlist ---------------------

_FIXER_RECON = "block_fixer_recon.sh"


def test_fixer_recon_blocks_git_status() -> None:
    """`git status` is reconnaissance, outside the precommit-fixer's allowlist."""
    assert (
        _run_hook(_FIXER_RECON, "git status", agent_type="forge:precommit-fixer") == 2
    )


def test_fixer_recon_blocks_find_and_checksum_pipe() -> None:
    """A `find | xargs md5sum` pipe is blocked — neither segment is allowlisted."""
    assert (
        _run_hook(
            _FIXER_RECON,
            "find . -name '*.py' | xargs md5sum",
            agent_type="forge:precommit-fixer",
        )
        == 2
    )


def test_fixer_recon_blocks_bare_pytest() -> None:
    """A bare `pytest` (no node-id selector) is blocked — too broad to be targeted."""
    assert _run_hook(_FIXER_RECON, "pytest", agent_type="forge:precommit-fixer") == 2


def test_fixer_recon_blocks_pytest_directory() -> None:
    """`pytest tests/` (directory, no `::` selector) is blocked."""
    assert (
        _run_hook(_FIXER_RECON, "pytest tests/", agent_type="forge:precommit-fixer")
        == 2
    )


def test_fixer_recon_blocks_pipe_into_non_allowlisted() -> None:
    """`forge-precommit | tee out.log` is blocked — the piped-into segment isn't."""
    assert (
        _run_hook(
            _FIXER_RECON,
            "forge-precommit | tee out.log",
            agent_type="forge:precommit-fixer",
        )
        == 2
    )


def test_fixer_recon_blocks_chained_recon() -> None:
    """`forge-precommit && git diff` is blocked — every chained segment must pass."""
    assert (
        _run_hook(
            _FIXER_RECON,
            "forge-precommit && git diff",
            agent_type="forge:precommit-fixer",
        )
        == 2
    )


def test_fixer_recon_allows_forge_precommit() -> None:
    """Bare `forge-precommit` call is allowed — the fixer's primary evidence source."""
    assert (
        _run_hook(_FIXER_RECON, "forge-precommit", agent_type="forge:precommit-fixer")
        == 0
    )


def test_fixer_recon_allows_forge_precommit_with_flags() -> None:
    """`forge-precommit --only ruff,typecheck` (scoped refresh) is allowed."""
    assert (
        _run_hook(
            _FIXER_RECON,
            "forge-precommit --only ruff,typecheck",
            agent_type="forge:precommit-fixer",
        )
        == 0
    )


def test_fixer_recon_allows_env_prefix() -> None:
    """`CI=1 forge-precommit` (inline env assignment) is allowed."""
    assert (
        _run_hook(
            _FIXER_RECON, "CI=1 forge-precommit", agent_type="forge:precommit-fixer"
        )
        == 0
    )


def test_fixer_recon_allows_cd_and_chain() -> None:
    """`cd /tmp && forge-precommit` — chained into allowed CLI."""
    assert (
        _run_hook(
            _FIXER_RECON,
            "cd /tmp && forge-precommit",
            agent_type="forge:precommit-fixer",
        )
        == 0
    )


def test_fixer_recon_allows_step_cli() -> None:
    """A standalone step CLI (`verify-forge-docstrings`) is allowed."""
    assert (
        _run_hook(
            _FIXER_RECON, "verify-forge-docstrings", agent_type="forge:precommit-fixer"
        )
        == 0
    )


def test_fixer_recon_allows_targeted_pytest_single_nodeid() -> None:
    """`pytest tests/foo.py::test_bar` (one explicit node-id) is allowed."""
    assert (
        _run_hook(
            _FIXER_RECON,
            "pytest tests/foo.py::test_bar",
            agent_type="forge:precommit-fixer",
        )
        == 0
    )


def test_fixer_recon_allows_targeted_pytest_multiple_nodeids() -> None:
    """`pytest` with several explicit node-ids is allowed."""
    assert (
        _run_hook(
            _FIXER_RECON,
            "pytest tests/foo.py::test_bar tests/foo.py::test_baz",
            agent_type="forge:precommit-fixer",
        )
        == 0
    )


def test_fixer_recon_allows_python_m_pytest_nodeid() -> None:
    """`python -m pytest` with an explicit node-id is allowed."""
    assert (
        _run_hook(
            _FIXER_RECON,
            "python -m pytest tests/foo.py::test_bar",
            agent_type="forge:precommit-fixer",
        )
        == 0
    )


def test_fixer_recon_blocks_backtick_substitution() -> None:
    """A backticked command inside an allowed segment blocks outright."""
    assert (
        _run_hook(
            _FIXER_RECON,
            "forge-precommit `git status`",
            agent_type="forge:precommit-fixer",
        )
        == 2
    )


def test_fixer_recon_blocks_backtick_glued_to_flag() -> None:
    """A backtick glued to a flag value (`--only=`...``) blocks too."""
    assert (
        _run_hook(
            _FIXER_RECON,
            "forge-precommit --only=`git branch --show-current`",
            agent_type="forge:precommit-fixer",
        )
        == 2
    )


def test_fixer_recon_ignores_other_agent() -> None:
    """The hook only restricts precommit-fixer — another agent's `git status` passes."""
    assert (
        _run_hook(_FIXER_RECON, "git status", agent_type="forge:git-commit-push") == 0
    )


def test_fixer_recon_ignores_missing_agent_type() -> None:
    """No `agent_type` payload field at all — the hook is a no-op."""
    assert _run_hook(_FIXER_RECON, "git status") == 0


def test_fixer_recon_matches_unprefixed_agent_form() -> None:
    """The unprefixed `precommit-fixer` agent-type form is scoped too."""
    assert _run_hook(_FIXER_RECON, "git status", agent_type="precommit-fixer") == 2


# --- block_forge_docs_edits.sh: forge-docs/ mirror is agent-write-protected -

_FORGE_DOCS_EDITS = "block_forge_docs_edits.sh"


def _run_docs_hook(
    file_path: str, *, cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    """Run block_forge_docs_edits.sh with *file_path* as the tool_input.

    Args:
        file_path: The ``file_path`` a Write/Edit tool call would carry.
            Empty string exercises the "no file_path in payload" case.
        cwd: Directory to run the hook from. Only matters for a bare
            relative ``forge-docs/*`` path, where ``ROOT`` is empty and
            the ``src/forge/data/docs`` self-skip check resolves against
            the hook's cwd rather than an extracted path prefix.

    Returns:
        The completed subprocess (exit code + captured stdout/stderr).
    """
    payload = json.dumps({"tool_input": {"file_path": file_path}})
    return subprocess.run(
        ["bash", str(_HOOKS_DIR / _FORGE_DOCS_EDITS)],
        input=payload,
        capture_output=True,
        text=True,
        check=False,
        cwd=cwd,
    )


def test_forge_docs_edits_blocks_consumer_repo_edit(tmp_path: Path) -> None:
    """Blocks edits under forge-docs/ in a consumer repo (no src/forge/data/docs)."""
    file_path = str(tmp_path / "forge-docs" / "configuration.md")
    proc = _run_docs_hook(file_path)
    assert proc.returncode == 2
    assert "install-forge-claude-md" in proc.stderr


def test_forge_docs_edits_allows_forge_own_repo(tmp_path: Path) -> None:
    """Forge's own repo (src/forge/data/docs present) is the self-skip escape."""
    (tmp_path / "src" / "forge" / "data" / "docs").mkdir(parents=True)
    file_path = str(tmp_path / "forge-docs" / "configuration.md")
    assert _run_docs_hook(file_path).returncode == 0


def test_forge_docs_edits_allows_substring_lookalike_dir() -> None:
    """A dir name merely containing "forge-docs" as a substring stays editable.

    Locks the segment-anchored `ROOT` fix: `case "$FILE_PATH" in
    */forge-docs/*)` only matches the literal path component, so
    `my-forge-docs-notes/` (a plausible personal-notes dir name) must not
    be swept up by a naive substring match.
    """
    assert _run_docs_hook("/r/my-forge-docs-notes/file.md").returncode == 0


def test_forge_docs_edits_blocks_nested_lookalike_dir() -> None:
    """A real `forge-docs/` nested inside a lookalike dir still blocks.

    Companion to the substring-lookalike allow case above: the
    segment-anchored fix must not over-correct into ignoring a genuine
    `forge-docs/` segment just because an ancestor directory's name also
    contains the substring.
    """
    proc = _run_docs_hook("/r/my-forge-docs-notes/forge-docs/x.md")
    assert proc.returncode == 2
    assert "install-forge-claude-md" in proc.stderr


def test_forge_docs_edits_blocks_bare_relative_path_in_consumer_repo(
    tmp_path: Path,
) -> None:
    """A bare relative `forge-docs/*` path (ROOT="") blocks in a consumer repo.

    Exercises the first `case` arm directly — no leading directory
    segment, so `ROOT` stays empty and the self-skip check resolves
    `src/forge/data/docs` against the hook's cwd. No such directory here.
    """
    proc = _run_docs_hook("forge-docs/x.md", cwd=tmp_path)
    assert proc.returncode == 2
    assert "install-forge-claude-md" in proc.stderr


def test_forge_docs_edits_allows_bare_relative_path_in_forge_own_repo(
    tmp_path: Path,
) -> None:
    """A bare relative `forge-docs/*` path is allowed when cwd IS forge's own repo.

    Companion to the blocking case above: with `src/forge/data/docs`
    present under the hook's cwd, the empty-`ROOT` self-skip check must
    still find it and stand down.
    """
    (tmp_path / "src" / "forge" / "data" / "docs").mkdir(parents=True)
    assert _run_docs_hook("forge-docs/x.md", cwd=tmp_path).returncode == 0


def test_forge_docs_edits_allows_unrelated_path(tmp_path: Path) -> None:
    """A path outside forge-docs/ is not inspected at all."""
    assert _run_docs_hook(str(tmp_path / "README.md")).returncode == 0


def test_forge_docs_edits_allows_empty_file_path() -> None:
    """An empty/absent file_path (e.g. a non-Write/Edit payload) is a no-op."""
    assert _run_docs_hook("").returncode == 0


def test_forge_docs_edits_registered_for_edit_and_write() -> None:
    """plugin.json wires block_forge_docs_edits.sh into both Edit and Write."""
    manifest = json.loads(
        (_HOOKS_DIR.parent / ".claude-plugin" / "plugin.json").read_text()
    )
    pre_tool_use = manifest["hooks"]["PreToolUse"]
    for matcher in ("Edit", "Write"):
        group = next(g for g in pre_tool_use if g["matcher"] == matcher)
        commands = [h["command"] for h in group["hooks"]]
        assert any(_FORGE_DOCS_EDITS in cmd for cmd in commands), (
            f"{_FORGE_DOCS_EDITS} not registered under {matcher}"
        )
