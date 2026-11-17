"""Tests for ``forge.install_claude`` — the install-forge-claude CLI.

# MOCKING STRATEGY: no mocks. Every fixture is a real tmp checkout that has
# no ``agents/``, ``skills/`` or ``claude-hooks/`` of its own, so
# ``shipped_path`` falls back to the packaged copies — the consumer case.
# Install, prune and check run for real against ``tmp_path``. Only ``main``
# tests use ``monkeypatch`` (chdir + argv) to drive the CLI entry point.
"""

from __future__ import annotations

import copy
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from forge import install_claude as ic
from forge.agent_harness import PLUGIN_REFERENCE_RE
from forge.hook_registry import script_names
from forge.install_claude import (
    hook_command,
    install_claude,
    is_forge_group,
    merge_settings,
    required_scripts,
)
from forge.instruction_files import generated_name, shipped_path
from forge.plugin_roster import shipped_plugin_names
from tests.conftest import init_git_repo


_SOURCE_LINE = re.compile(r'^source "\$\(dirname "\$0"\)/([\w.-]+\.sh)"', re.MULTILINE)

_CONSUMER_GROUP = {
    "matcher": "Bash",
    "hooks": [{"type": "command", "command": hook_command("my_own_guard.sh")}],
}


def _files(root: Path) -> dict[str, bytes]:
    """Snapshot every file under *root* as relative path to bytes.

    Args:
        root: Directory to walk.

    Returns:
        Mapping of posix relative path to content.
    """
    return {
        str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()
    }


@pytest.fixture(scope="module")
def installed_repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A tmp checkout with the integration installed once (read-only for tests).

    Returns:
        The repo root.
    """
    root = tmp_path_factory.mktemp("installed")
    changes, error = install_claude(root)
    assert error is None
    assert changes
    return root


@pytest.fixture
def bare_repo(tmp_path: Path) -> Path:
    """An empty tmp checkout with nothing installed.

    Returns:
        The repo root.
    """
    root = tmp_path / "bare"
    root.mkdir()
    return root


@pytest.fixture
def consumer_repo_with_own_files(tmp_path: Path) -> Path:
    """A checkout whose `.claude/` already holds the consumer's own files.

    Seeds one agent, one skill, one hook script and a settings file with a
    consumer Bash hook group, permissions and another plugin's entry.

    Returns:
        The repo root.
    """
    root = tmp_path / "consumer"
    claude = root / ".claude"
    (claude / "agents").mkdir(parents=True)
    (claude / "agents" / "mine.md").write_text("# mine\n", encoding="utf-8")
    (claude / "skills" / "mine").mkdir(parents=True)
    (claude / "skills" / "mine" / "SKILL.md").write_text("# mine\n", encoding="utf-8")
    (claude / "hooks").mkdir()
    (claude / "hooks" / "my_own_guard.sh").write_text(
        "#!/bin/sh\nexit 0\n", encoding="utf-8"
    )
    settings = {
        "permissions": {"allow": ["Bash(ls:*)"]},
        "enabledPlugins": {"other@market": True},
        "hooks": {"PreToolUse": [_CONSUMER_GROUP]},
    }
    (claude / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    return root


# --- 4. is_forge_group ------------------------------------------------------

_SCRIPTS = frozenset({"a.sh", "b.sh"})


def _group(*commands: object) -> dict[str, object]:
    """Build one hooks group holding a command entry per given command.

    Args:
        *commands: Command strings, one command entry each.
    """
    return {"hooks": [{"type": "command", "command": c} for c in commands]}


@pytest.mark.parametrize(
    ("group", "expected"),
    [
        pytest.param(_group(hook_command("a.sh")), True, id="forge-script"),
        pytest.param(
            _group(hook_command("a.sh"), hook_command("b.sh")), True, id="all-forge"
        ),
        pytest.param(_group("/elsewhere/a.sh"), True, id="forge-name-any-directory"),
        pytest.param(
            _group(hook_command("my_own_guard.sh")),
            False,
            id="same-dir-consumer-script",
        ),
        pytest.param(
            _group(hook_command("a.sh"), hook_command("mine.sh")), False, id="mixed"
        ),
        pytest.param(_group(), False, id="empty-hooks"),
        pytest.param({"matcher": "Bash"}, False, id="no-hooks-key"),
        pytest.param({"hooks": "a.sh"}, False, id="hooks-not-a-list"),
        pytest.param({"hooks": ["a.sh"]}, False, id="hook-not-a-dict"),
        pytest.param({"hooks": [{"type": "command"}]}, False, id="no-command"),
        pytest.param({"hooks": [{"command": 3}]}, False, id="command-not-a-string"),
        pytest.param("a.sh", False, id="group-not-a-dict"),
        pytest.param(None, False, id="group-none"),
    ],
)
def test_is_forge_group_truth_table(group: object, *, expected: bool) -> None:
    """Ownership follows script names, never the directory a command points at.

    Args:
        group: A settings hook group candidate.
        expected: Whether forge owns it.

    SCENARIO: forge and the consumer share `.claude/hooks/`. The
    same-directory consumer script is the case a directory-prefix test
    would wrongly claim and then drop on the next install.
    EXPECTED BEHAVIOR: `True` only for a non-empty group whose every
    command names a wired script.
    """
    assert is_forge_group(group, _SCRIPTS) is expected


# --- 5. consumer files survive ---------------------------------------------


def test_install_twice_keeps_consumer_files_and_registration(
    consumer_repo_with_own_files: Path,
) -> None:
    """Consumer agent, skill, hook bytes and hook registration survive reinstall."""
    root = consumer_repo_with_own_files
    mine = {
        rel: (root / rel).read_bytes()
        for rel in (
            ".claude/agents/mine.md",
            ".claude/skills/mine/SKILL.md",
            ".claude/hooks/my_own_guard.sh",
        )
    }
    for _ in range(2):
        _, error = install_claude(root)
        assert error is None
    for rel, data in mine.items():
        assert (root / rel).read_bytes() == data
    settings = json.loads((root / ".claude/settings.json").read_text(encoding="utf-8"))
    assert _CONSUMER_GROUP in settings["hooks"]["PreToolUse"]
    forge_groups = [
        g for g in settings["hooks"]["PreToolUse"] if is_forge_group(g, script_names())
    ]
    assert forge_groups
    assert settings["permissions"] == {"allow": ["Bash(ls:*)"]}
    assert settings["enabledPlugins"] == {"other@market": True}


def test_install_replaces_a_stale_agent_matcher_group_not_adds_one(
    bare_repo: Path,
) -> None:
    """Behavior: an upgrade swaps a leftover agent matcher for the current one.

    SCENARIO: settings from before the rename hold a forge-owned
    SubagentStop group whose matcher is a retired agent spelling.
    EXPECTED BEHAVIOR: after install exactly one group runs the verdict
    guard, its matcher is the freshly derived one, and no stale group stays.
    """
    stale_matcher = "^(forge:)?precommit-fixer$"
    settings_path = bare_repo / ".claude/settings.json"
    settings_path.parent.mkdir(parents=True)
    stale = {
        "matcher": stale_matcher,
        "hooks": [
            {"type": "command", "command": hook_command("require_fixer_verdict.sh")}
        ],
    }
    settings_path.write_text(
        json.dumps({"hooks": {"SubagentStop": [stale]}}), encoding="utf-8"
    )

    _, error = install_claude(bare_repo)
    assert error is None

    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    verdict_groups = [
        g
        for g in settings["hooks"]["SubagentStop"]
        if any("require_fixer_verdict.sh" in h["command"] for h in g["hooks"])
    ]
    assert len(verdict_groups) == 1
    assert verdict_groups[0]["matcher"] != stale_matcher
    assert re.fullmatch(verdict_groups[0]["matcher"], generated_name("precommit-fixer"))
    assert stale not in settings["hooks"]["SubagentStop"]


# --- 6. required_scripts ----------------------------------------------------


def test_required_scripts_follows_sourced_helpers_transitively(tmp_path: Path) -> None:
    """A sources b sources c are all required; an unreferenced d is not."""
    hooks = tmp_path / "claude-hooks"
    hooks.mkdir()
    (hooks / "a.sh").write_text('source "$(dirname "$0")/b.sh"\n', encoding="utf-8")
    (hooks / "b.sh").write_text('source "$(dirname "$0")/c.sh"\n', encoding="utf-8")
    (hooks / "c.sh").write_text("true\n", encoding="utf-8")
    (hooks / "d.sh").write_text("true\n", encoding="utf-8")
    source = tmp_path / "claude-hooks"
    assert required_scripts(source, frozenset({"a.sh"})) == {"a.sh", "b.sh", "c.sh"}


# --- 7. lockout detector ----------------------------------------------------


def test_every_sourced_helper_is_installed_and_executable(installed_repo: Path) -> None:
    """A guard that cannot source its helper refuses every call it protects.

    SCENARIO: scan the installed guard scripts for top-level `source` lines.
    EXPECTED BEHAVIOR: each sourced helper is present and executable beside
    the guards. A precondition requires at least one such line, so the test
    cannot pass vacuously if the pattern stops matching.
    """
    hooks_dir = installed_repo / ".claude" / "hooks"
    sourced = {
        helper
        for script in hooks_dir.glob("*.sh")
        for helper in _SOURCE_LINE.findall(script.read_text(encoding="utf-8"))
    }
    assert sourced, 'no `source "$(dirname "$0")/x.sh"` line found; pattern stale'
    for helper in sorted(sourced):
        path = hooks_dir / helper
        assert path.is_file(), helper
        assert os.access(path, os.X_OK), helper


# --- 8. idempotence ---------------------------------------------------------


def test_second_install_and_check_report_nothing(bare_repo: Path) -> None:
    """After one install, a second install and a dry run both find no change."""
    first, error = install_claude(bare_repo)
    assert error is None
    assert first
    assert (bare_repo / ".claude/hooks/git_anchor.sh").is_file()
    assert install_claude(bare_repo) == ([], None)
    assert install_claude(bare_repo, check=True) == ([], None)


# --- 9. layout --------------------------------------------------------------


def test_installed_layout_matches_the_packaged_sources(installed_repo: Path) -> None:
    """Agents, skills and hooks installed mirror the packaged glob."""
    agents_src = shipped_path("agents")
    skills_src = shipped_path("skills")
    claude = installed_repo / ".claude"
    expected_agents = {
        f"{generated_name(p.stem)}.md"
        for p in agents_src.glob("*.md")
        if p.name != "_TEMPLATE.md"
    }
    assert expected_agents
    assert {p.name for p in (claude / "agents").glob("*.md")} == expected_agents
    assert not (claude / "agents" / "_TEMPLATE.md").exists()
    expected_skills = {
        generated_name(p.parent.name) for p in skills_src.glob("*/SKILL.md")
    }
    assert expected_skills
    assert {
        p.parent.name for p in (claude / "skills").glob("*/SKILL.md")
    } == expected_skills
    hooks = list((claude / "hooks").glob("*.sh"))
    assert hooks
    assert {h.name for h in hooks} >= script_names()
    assert all(os.access(h, os.X_OK) for h in hooks)


# --- 10. namespace rewrite wired into both publishers -----------------------


def test_no_unresolvable_forge_reference_survives(installed_repo: Path) -> None:
    """Generated agents and skills carry no `forge:<shipped name>` reference.

    SCENARIO: the shipped sources name each other with the plugin prefix.
    EXPECTED BEHAVIOR: after install none remain, proving the rewrite is
    wired into both the agent and the skill publisher. Preconditions check
    that the sources really carry such references in both kinds.
    """
    shipped = shipped_plugin_names()

    def refs(paths: list[Path]) -> list[str]:
        return [
            m.group(1)
            for p in paths
            for m in PLUGIN_REFERENCE_RE.finditer(p.read_text(encoding="utf-8"))
            if m.group(1) in shipped
        ]

    agents_src = [
        p for p in shipped_path("agents").glob("*.md") if p.name != "_TEMPLATE.md"
    ]
    skills_src = list(shipped_path("skills").glob("*/SKILL.md"))
    assert refs(agents_src)
    assert refs(skills_src)
    claude = installed_repo / ".claude"
    assert refs(list((claude / "agents").glob("*.md"))) == []
    assert refs(list((claude / "skills").glob("*/SKILL.md"))) == []


# --- 11, 12. merge_settings -------------------------------------------------

_FORGE_GROUPS = {
    "PreToolUse": [
        {
            "matcher": "Bash",
            "hooks": [{"type": "command", "command": hook_command("a.sh")}],
        }
    ]
}
_FORGE_OLD = {"hooks": [{"type": "command", "command": hook_command("a.sh")}]}


def test_merge_settings_replaces_not_duplicates_and_keeps_consumer() -> None:
    """Merging twice equals once; a dropped event loses forge's groups only."""
    existing = {
        "hooks": {
            "PreToolUse": [_FORGE_OLD, _CONSUMER_GROUP],
            "Gone": [_FORGE_OLD, _CONSUMER_GROUP],
            "OnlyForge": [_FORGE_OLD],
        }
    }
    frozen = copy.deepcopy(existing)
    once = merge_settings(existing, _FORGE_GROUPS, _SCRIPTS)
    assert existing == frozen
    assert merge_settings(once, _FORGE_GROUPS, _SCRIPTS) == once
    assert once["hooks"]["PreToolUse"] == [
        *_FORGE_GROUPS["PreToolUse"],
        _CONSUMER_GROUP,
    ]
    assert once["hooks"]["Gone"] == [_CONSUMER_GROUP]
    assert "OnlyForge" not in once["hooks"]


def test_merge_settings_strips_only_the_retired_plugin_keys() -> None:
    """Forge's plugin entries go; another plugin, emptied blocks, permissions stay."""
    existing = {
        "permissions": {"allow": ["x"]},
        "enabledPlugins": {"forge@forge": True, "other@market": True},
        "extraKnownMarketplaces": {"forge": {"source": {}}},
    }
    merged = merge_settings(existing, {}, _SCRIPTS)
    assert merged["enabledPlugins"] == {"other@market": True}
    assert "extraKnownMarketplaces" not in merged
    assert merged["permissions"] == {"allow": ["x"]}


# --- 13. unparseable settings ----------------------------------------------


@pytest.mark.parametrize(
    "content",
    [
        pytest.param("{ not json", id="invalid-json"),
        pytest.param("[1, 2]", id="json-array"),
    ],
)
def test_unparseable_settings_refused_with_nothing_published(
    bare_repo: Path, content: str
) -> None:
    """A bad settings file yields an error and leaves the repo untouched.

    Args:
        bare_repo: Empty checkout.
        content: Settings bytes to plant.

    SCENARIO: settings is invalid JSON or a non-object.
    EXPECTED BEHAVIOR: an error naming the file, settings bytes unchanged,
    and no generated file anywhere — the file is validated before any
    publishing, so no half-converted repo is left behind.
    """
    settings = bare_repo / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text(content, encoding="utf-8")
    before = _files(bare_repo)
    changes, error = install_claude(bare_repo)
    assert changes == []
    assert error is not None
    assert "settings.json" in error
    assert _files(bare_repo) == before


# --- 14. check mode ---------------------------------------------------------


def test_check_on_fresh_repo_lists_changes_and_writes_nothing(bare_repo: Path) -> None:
    """A dry run on an empty repo reports work and creates no `.claude/`."""
    changes, error = install_claude(bare_repo, check=True)
    assert error is None
    assert changes
    assert not (bare_repo / ".claude").exists()


def test_check_reports_an_edited_hook_and_leaves_it_alone(bare_repo: Path) -> None:
    """A hand-edited published hook shows as drift and is not overwritten by check."""
    install_claude(bare_repo)
    hook = bare_repo / ".claude/hooks/block_force_push.sh"
    hook.write_text("# tampered\n", encoding="utf-8")
    changes, error = install_claude(bare_repo, check=True)
    assert error is None
    assert ".claude/hooks/block_force_push.sh" in changes
    assert hook.read_text(encoding="utf-8") == "# tampered\n"


# --- 15. prune --------------------------------------------------------------


def test_prune_removes_retired_agent_keeps_a_same_named_consumer_file(
    bare_repo: Path,
) -> None:
    """A retired agent goes by the ledger; a file forge never wrote stays.

    SCENARIO: a previous run's ledger records an agent forge no longer
    ships, beside two files forge never wrote — one with a name of its
    own, and one named exactly like a shipped skill.
    EXPECTED BEHAVIOR: only the ledger-recorded file is removed.
    Ownership is recorded, never inferred: pruning by shipped or roster
    name would delete `commit.md`, since `commit` is a shipped skill
    name and an entirely plausible name for a repo's own agent. That is a
    data-loss path this test exists to keep shut.
    """
    install_claude(bare_repo)

    agents = bare_repo / ".claude" / "agents"
    retired = agents / f"{generated_name('retired-thing')}.md"
    retired.write_text("old\n", encoding="utf-8")
    (agents / "mine.md").write_text("mine\n", encoding="utf-8")
    (agents / "commit.md").write_text("mine too\n", encoding="utf-8")

    ignore = bare_repo / ".claude" / ".gitignore"
    ignore.write_text(
        ignore.read_text(encoding="utf-8")
        + f"agents/{generated_name('retired-thing')}.md\n",
        encoding="utf-8",
    )

    _, error = install_claude(bare_repo)

    assert error is None
    assert not retired.exists()
    assert (agents / "mine.md").is_file()
    assert (agents / "commit.md").is_file()


def test_prune_removes_retired_hook_recorded_in_ledger(bare_repo: Path) -> None:
    """A hook forge stopped shipping goes via `.claude/.gitignore`; others stay."""
    hooks = bare_repo / ".claude" / "hooks"
    hooks.mkdir(parents=True)
    (hooks / "retired_guard.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (hooks / "my_own_guard.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (bare_repo / ".claude" / ".gitignore").write_text(
        ".gitignore\nhooks/retired_guard.sh\n", encoding="utf-8"
    )
    _, error = install_claude(bare_repo)
    assert error is None
    assert not (hooks / "retired_guard.sh").exists()
    assert (hooks / "my_own_guard.sh").is_file()


# --- 16. gitignore semantics ------------------------------------------------


def test_gitignore_hides_generated_files_and_not_consumer_ones(
    consumer_repo_with_own_files: Path,
) -> None:
    """Forge's files are git-ignored; the consumer's and settings.json are not."""
    root = consumer_repo_with_own_files
    init_git_repo(root)
    _, error = install_claude(root)
    assert error is None
    agent = next((root / ".claude/agents").glob("*.md"))
    skill = next((root / ".claude/skills").glob("*/SKILL.md"))
    paths = [
        agent.relative_to(root),
        skill.relative_to(root),
        Path(".claude/hooks/block_force_push.sh"),
        Path(".claude/agents/mine.md"),
        Path(".claude/skills/mine/SKILL.md"),
        Path(".claude/hooks/my_own_guard.sh"),
        Path(".claude/settings.json"),
    ]
    result = subprocess.run(
        ["git", "check-ignore", "--stdin"],
        cwd=root,
        input="\n".join(str(p) for p in paths) + "\n",
        capture_output=True,
        text=True,
        check=False,
    )
    ignored = set(result.stdout.split())
    assert ignored == {str(p) for p in paths[:3]}


# --- 17. main exit codes ----------------------------------------------------


def _bad_settings(root: Path) -> None:
    """Write an unparseable settings.json under root/.claude.

    Args:
        root: Directory that receives the .claude folder.
    """
    (root / ".claude").mkdir()
    (root / ".claude" / "settings.json").write_text("{ nope", encoding="utf-8")


@pytest.mark.parametrize(
    "case",
    [
        pytest.param(("--check", None, 1, False), id="check-fresh"),
        pytest.param(("--verify", None, 1, False), id="verify-fresh"),
        pytest.param(("--check", install_claude, 0, False), id="check-installed"),
        pytest.param(("--verify", install_claude, 0, False), id="verify-installed"),
        pytest.param(("--check", _bad_settings, 1, False), id="check-bad-settings"),
        pytest.param(("--verify", _bad_settings, 1, False), id="verify-bad-settings"),
        pytest.param((None, None, 0, True), id="plain-install"),
        pytest.param((None, _bad_settings, 1, False), id="plain-bad-settings"),
    ],
)
def test_main_exit_codes(
    bare_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: tuple[str | None, object, int, bool],
) -> None:
    """`main` exits 1 on drift or error, 0 when current, and --verify aliases --check.

    Args:
        bare_repo: Empty checkout used as the working directory.
        monkeypatch: Sets cwd and argv.
        case: ``(flag, setup, code, writes)`` — the CLI flag or ``None``
            for a plain install; a callable preparing the repo; the
            expected exit code; and whether the run should create
            ``.claude/``. Passed as one tuple so the signature stays
            within the argument-count limit.
    """
    flag, setup, code, writes = case
    if setup is not None:
        setup(bare_repo)
    monkeypatch.chdir(bare_repo)
    monkeypatch.setattr(
        sys, "argv", ["install-forge-claude", *([flag] if flag else [])]
    )
    before = _files(bare_repo)
    assert ic.main() == code
    if not writes:
        assert _files(bare_repo) == before
    else:
        assert (bare_repo / ".claude").is_dir()
