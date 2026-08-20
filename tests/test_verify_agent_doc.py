"""Tests for ``forge.verify_agent_doc``.

# MOCKING STRATEGY: each test builds a throwaway repo tree under tmp_path
# (pyproject.toml, agents/*.md, skills/*/SKILL.md, claude-hooks/*.sh) and runs
# the real ``_config_doc_path`` / ``_roster`` / ``_check_doc`` functions
# against it. ``main`` tests pin ``repo_root`` to tmp_path and patch
# ``sys.argv`` so argparse does not consume pytest's own arguments.
"""

from __future__ import annotations

import subprocess
import sys
from typing import TYPE_CHECKING

from forge import verify_agent_doc as vad


if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _write(path: Path, text: str = "") -> None:
    """Write *text* to *path*, creating parent directories as needed.

    Args:
        path: Destination file path.
        text: Contents to write.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _build_repo(root: Path) -> None:
    """Create a repo tree with one real agent, skill, hook, and CLI.

    Args:
        root: Directory to populate as the repo root.
    """
    _write(root / "agents" / "reviewer.md", "# reviewer\n")
    _write(root / "agents" / "_TEMPLATE.md", "# template\n")
    _write(root / "skills" / "ship" / "SKILL.md", "# ship\n")
    _write(root / "claude-hooks" / "block_push.sh", "#!/bin/sh\n")
    _write(
        root / "pyproject.toml",
        '[project.scripts]\nforge-ship = "x:main"\n'
        '[tool.forge.agent_doc]\npath = "docs/agent-architecture.md"\n',
    )


def _git_commit_all(root: Path, message: str) -> None:
    """Stage and commit every file under *root* to its git repo.

    Args:
        root: Git repository root (already ``git init``-ed on first call).
        message: Commit message.

    Invariant: `_diff_report` shells out to real `git`, so exercising it
    end-to-end needs an actual repository rather than a mocked subprocess —
    a fake diff string risks drifting from git's real output format.
    """
    subprocess.run(["git", "-C", str(root), "init", "-q"], check=True)
    subprocess.run(
        ["git", "-C", str(root), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(root), "commit", "-q", "-m", message],
        check=True,
    )


# --- _config_doc_path ---------------------------------------------------


def test_config_doc_path_returns_configured_path(tmp_path: Path) -> None:
    """The configured path string is returned when present."""
    _write(
        tmp_path / "pyproject.toml",
        '[tool.forge.agent_doc]\npath = "docs/agent-architecture.md"\n',
    )
    assert vad._config_doc_path(tmp_path) == "docs/agent-architecture.md"


def test_config_doc_path_none_when_table_absent(tmp_path: Path) -> None:
    """None is returned when [tool.forge.agent_doc] is absent."""
    _write(tmp_path / "pyproject.toml", '[project.scripts]\nfoo = "x:main"\n')
    assert vad._config_doc_path(tmp_path) is None


def test_config_doc_path_none_when_pyproject_missing(tmp_path: Path) -> None:
    """None is returned when pyproject.toml does not exist at all."""
    assert vad._config_doc_path(tmp_path) is None


# --- _roster --------------------------------------------------------------


def test_roster_discovers_agents_skills_hooks_clis(tmp_path: Path) -> None:
    """Agents, skills, hooks, and CLIs are all discovered; `_`-prefixed excluded."""
    _build_repo(tmp_path)
    roster = vad._roster(tmp_path)
    assert roster["agents"] == {"reviewer"}
    assert roster["skills"] == {"ship"}
    assert roster["hooks"] == {"block_push"}
    assert roster["clis"] == {"forge-ship"}


# --- _check_doc -------------------------------------------------------------


def test_check_doc_in_sync_returns_empty(tmp_path: Path) -> None:
    """A doc mentioning every agent and skill, with no dangling refs, is clean."""
    _build_repo(tmp_path)
    roster = vad._roster(tmp_path)
    doc = "The reviewer agent runs after ship. Uses forge-ship and block_push."
    assert vad._check_doc(doc, roster) == []


def test_check_doc_flags_unmentioned_agent(tmp_path: Path) -> None:
    """A real agent absent from the doc is reported as a coverage problem."""
    _build_repo(tmp_path)
    roster = vad._roster(tmp_path)
    doc = "Only the ship skill is discussed here."
    problems = vad._check_doc(doc, roster)
    assert "coverage: agent 'reviewer' is not mentioned" in problems


def test_check_doc_flags_unmentioned_skill(tmp_path: Path) -> None:
    """A real skill absent from the doc is reported as a coverage problem."""
    _build_repo(tmp_path)
    roster = vad._roster(tmp_path)
    doc = "Only the reviewer agent is discussed here."
    problems = vad._check_doc(doc, roster)
    assert "coverage: skill 'ship' is not mentioned" in problems


def test_check_doc_hooks_not_required_for_coverage(tmp_path: Path) -> None:
    """A real hook absent from the doc produces no problem (deliberate design)."""
    _build_repo(tmp_path)
    roster = vad._roster(tmp_path)
    doc = "The reviewer agent runs the ship skill via forge-ship."
    problems = vad._check_doc(doc, roster)
    assert not any("block_push" in problem for problem in problems)


def test_check_doc_flags_dangling_hook(tmp_path: Path) -> None:
    """A block_* token not in the hook roster is reported as dangling."""
    _build_repo(tmp_path)
    roster = vad._roster(tmp_path)
    doc = "The reviewer agent runs the ship skill and guards with block_ghost."
    problems = vad._check_doc(doc, roster)
    assert "dangling: hook 'block_ghost' does not exist" in problems


def test_check_doc_flags_dangling_cli(tmp_path: Path) -> None:
    """A forge-* token not in the CLI roster is reported as dangling."""
    _build_repo(tmp_path)
    roster = vad._roster(tmp_path)
    doc = "The reviewer agent runs the ship skill and calls forge-ghost."
    problems = vad._check_doc(doc, roster)
    assert "dangling: CLI 'forge-ghost' does not exist" in problems


def test_check_doc_flags_dangling_skill(tmp_path: Path) -> None:
    """A /name token not in the skill roster is reported as dangling."""
    _build_repo(tmp_path)
    roster = vad._roster(tmp_path)
    doc = "The reviewer agent invokes /ghost to finish the ship skill."
    problems = vad._check_doc(doc, roster)
    assert "dangling: skill '/ghost' does not exist" in problems


# --- _SKILL_RE ---------------------------------------------------------------


def test_skill_re_does_not_match_shebang_line() -> None:
    """_SKILL_RE does not misclassify a shell shebang as a skill mention."""
    assert vad._SKILL_RE.findall("#!/usr/bin/env bash") == []


# --- _parse_blocks ------------------------------------------------------------


def test_parse_blocks_pipe_label_both_arrowheads() -> None:
    """Piped labels on solid and dotted arrows both parse to verb-stripped Edges."""
    doc = "```mermaid\nA -->|invokes| B\nA -.->|guards| C\n```\n"
    blocks = vad._parse_blocks(doc)
    edges, _ = blocks[0]
    assert edges == [vad.Edge("A", "invokes", "B"), vad.Edge("A", "guards", "C")]


def test_parse_blocks_dotted_spaced_label_form() -> None:
    """The dotted spaced-label form (`A -. text .-> B`) parses to an Edge."""
    doc = "```mermaid\nA -. some text .-> B\n```\n"
    blocks = vad._parse_blocks(doc)
    edges, _ = blocks[0]
    assert edges == [vad.Edge("A", "some text", "B")]


def test_parse_blocks_class_map_single_block_unions_two_lines() -> None:
    """Two `class <id> <name>` lines for the same id union into one class set."""
    doc = "```mermaid\nclass A agent\nclass A reporter\n```\n"
    blocks = vad._parse_blocks(doc)
    _, classes = blocks[0]
    assert classes == {"A": {"agent", "reporter"}}


def test_parse_blocks_separates_classes_per_block() -> None:
    """Mermaid classes are scoped per block — a class in block 1 isn't in block 2.

    SCENARIO: two mermaid blocks both declare node A; only block 1 classes
    it `agent`. Mermaid itself scopes node declarations per block (an
    unclassed node in block 2 simply doesn't render as an agent there), so
    `_parse_blocks` must keep the two blocks' class maps independent rather
    than merging them.
    """
    doc = (
        "```mermaid\nclass A agent\n```\n"
        "some prose between blocks\n"
        "```mermaid\nclass B agent\n```\n"
    )
    blocks = vad._parse_blocks(doc)
    assert "A" in blocks[0][1]
    assert "A" not in blocks[1][1]


def test_parse_blocks_unlabeled_edge_yields_no_edges() -> None:
    """An unlabeled edge (`A --> B`, no `|verb|`) is silently unparsed.

    Regression lock: curated docs label every edge, so an unlabeled arrow
    is intentionally invisible to `_parse_blocks` rather than treated as a
    verb-less edge — widening the regex to match it would risk matching
    incidental `-->` text outside a real edge line.
    """
    doc = "```mermaid\nA --> B\n```\n"
    blocks = vad._parse_blocks(doc)
    edges, _ = blocks[0]
    assert edges == []


# --- _node_kind_ids -----------------------------------------------------------


def test_node_kind_ids_maps_hyphenated_names_and_prefixes() -> None:
    """Hyphenated agent/skill/CLI names get `-`→`_` plus their kind prefix.

    Hooks keep their name as-is (no `-`→`_`) and get only the `hk_` prefix —
    hook filenames are already underscore-separated (`block_push.sh`).
    """
    roster = {
        "agents": {"design-checker"},
        "skills": {"pr-manager"},
        "hooks": {"block_push"},
        "clis": {"forge-precommit"},
    }
    kind_ids = vad._node_kind_ids(roster)
    assert kind_ids["agent"] == {"design_checker"}
    assert kind_ids["skill"] == {"sk_pr_manager"}
    assert kind_ids["hook"] == {"hk_block_push"}
    assert kind_ids["cli"] == {"cli_forge_precommit"}


# --- _discover_invokes ---------------------------------------------------------


def test_discover_invokes_multiple_pairs_and_claude_skills_dir(tmp_path: Path) -> None:
    """Multiple `subagent_type=` lines in one SKILL.md yield multiple pairs.

    A skill under `.claude/skills/` (the consumer-wrapper location, not
    just `skills/`) is discovered too.
    """
    _write(
        tmp_path / "skills" / "ship" / "SKILL.md",
        "# ship\n"
        'Task(subagent_type="forge:reviewer")\n'
        'Task(subagent_type="forge:security-checker")\n',
    )
    _write(
        tmp_path / ".claude" / "skills" / "local-ship" / "SKILL.md",
        '# local-ship\nTask(subagent_type="forge:reviewer")\n',
    )
    pairs = vad._discover_invokes(tmp_path)
    assert pairs == {
        ("ship", "reviewer"),
        ("ship", "security-checker"),
        ("local-ship", "reviewer"),
    }


# --- _guard_map -----------------------------------------------------------


def test_guard_map_happy_path_returns_configured_table(tmp_path: Path) -> None:
    """A well-formed guarded_by table (agent -> list[str] hooks) is returned intact."""
    _write(
        tmp_path / "pyproject.toml",
        '[tool.forge.agent_doc.guarded_by]\nreviewer = ["block_push"]\n',
    )
    assert vad._guard_map(tmp_path) == {"reviewer": ["block_push"]}


def test_guard_map_returns_empty_when_not_a_dict(tmp_path: Path) -> None:
    """A bare-string `guarded_by` value (not a table) degrades to empty."""
    _write(
        tmp_path / "pyproject.toml",
        '[tool.forge.agent_doc]\nguarded_by = "oops"\n',
    )
    assert vad._guard_map(tmp_path) == {}


def test_guard_map_drops_entry_with_non_list_value(tmp_path: Path) -> None:
    """A guarded_by entry whose value isn't a list is dropped."""
    _write(
        tmp_path / "pyproject.toml",
        '[tool.forge.agent_doc.guarded_by]\nreviewer = "block_push"\n',
    )
    assert vad._guard_map(tmp_path) == {}


def test_guard_map_drops_entry_with_non_string_list_item(tmp_path: Path) -> None:
    """A guarded_by entry whose list contains a non-string item is dropped."""
    _write(
        tmp_path / "pyproject.toml",
        "[tool.forge.agent_doc.guarded_by]\nreviewer = [1, 2]\n",
    )
    assert vad._guard_map(tmp_path) == {}


# --- _check_endpoints -----------------------------------------------------


def test_check_endpoints_flags_unclassified_node() -> None:
    """A node with no class declaration in its block is flagged."""
    roster = {"agents": {"reviewer"}, "skills": set(), "hooks": set(), "clis": set()}
    kind_ids = vad._node_kind_ids(roster)
    blocks = [
        ([vad.Edge("ghost", "calls", "reviewer")], {"reviewer": {"agent"}}),
    ]
    problems = vad._check_endpoints(blocks, kind_ids)
    assert problems == ["edge: node 'ghost' has no class declaration in its block"]


def test_check_endpoints_flags_classed_agent_not_in_roster() -> None:
    """A node classed 'agent' but absent from the agent roster is flagged."""
    roster = {"agents": {"reviewer"}, "skills": set(), "hooks": set(), "clis": set()}
    kind_ids = vad._node_kind_ids(roster)
    blocks = [
        (
            [vad.Edge("ghost", "calls", "reviewer")],
            {"ghost": {"agent"}, "reviewer": {"agent"}},
        ),
    ]
    problems = vad._check_endpoints(blocks, kind_ids)
    assert problems == [
        "edge: node 'ghost' (classed agent) matches no repo agent/skill/hook/CLI"
    ]


def test_check_endpoints_exempts_structural_classes() -> None:
    """Nodes classed person, orchestrator, or policy are exempt from roster matching."""
    roster = {"agents": set(), "skills": set(), "hooks": set(), "clis": set()}
    kind_ids = vad._node_kind_ids(roster)
    blocks = [
        (
            [vad.Edge("human", "asks", "orch"), vad.Edge("orch", "follows", "rule")],
            {"human": {"person"}, "orch": {"orchestrator"}, "rule": {"policy"}},
        ),
    ]
    assert vad._check_endpoints(blocks, kind_ids) == []


def test_check_endpoints_resolves_multi_class_node_via_any() -> None:
    """A node classed both 'agent' and 'reporter' resolves via the agent class.

    'reporter' is a modifier class with no roster counterpart of its own;
    the endpoint check must not fail just because one of a node's several
    classes doesn't map to a kind — `any()` across its classes is enough.
    """
    roster = {
        "agents": {"reviewer"},
        "skills": {"ship"},
        "hooks": set(),
        "clis": set(),
    }
    kind_ids = vad._node_kind_ids(roster)
    blocks = [
        (
            [vad.Edge("sk_ship", "invokes", "reviewer")],
            {"sk_ship": {"skill"}, "reviewer": {"agent", "reporter"}},
        ),
    ]
    assert vad._check_endpoints(blocks, kind_ids) == []


# --- _check_invokes ---------------------------------------------------------


def test_check_invokes_flags_missing_edge_for_wired_delegation(tmp_path: Path) -> None:
    """A skill's wired subagent_type= delegation with no matching doc edge is flagged.

    SCENARIO: skills/ship/SKILL.md delegates to the reviewer agent, both of
    which are in the roster, but the doc's edge set is empty.
    EXPECTED BEHAVIOR: one problem naming the expected sk_ship -> reviewer edge.
    """
    _write(tmp_path / "agents" / "reviewer.md", "# reviewer\n")
    _write(
        tmp_path / "skills" / "ship" / "SKILL.md",
        '# ship\nTask(subagent_type="forge:reviewer")\n',
    )
    roster = vad._roster(tmp_path)
    invokes = vad._discover_invokes(tmp_path)
    problems = vad._check_invokes(invokes, roster, set())
    assert problems == [
        (
            "edge: skill '/ship' delegates to agent 'reviewer'"
            " (subagent_type=) but the doc has no sk_ship -> reviewer edge"
        )
    ]


def test_check_invokes_passes_when_edge_present_under_any_verb(tmp_path: Path) -> None:
    """A wired delegation is satisfied by a matching edge under any verb."""
    _write(tmp_path / "agents" / "reviewer.md", "# reviewer\n")
    _write(
        tmp_path / "skills" / "ship" / "SKILL.md",
        '# ship\nTask(subagent_type="forge:reviewer")\n',
    )
    roster = vad._roster(tmp_path)
    invokes = vad._discover_invokes(tmp_path)
    edge_pairs = {("sk_ship", "reviewer")}
    assert vad._check_invokes(invokes, roster, edge_pairs) == []


def test_check_invokes_skips_delegation_to_agent_absent_from_roster(
    tmp_path: Path,
) -> None:
    """A delegation to an agent the repo doesn't define produces no problem.

    Only the agent-absent branch of the `skill not in roster or agent not
    in roster` guard is reachable in practice: `_discover_invokes` globs
    the same `SKILL.md` paths `_roster` does, so a discovered skill is
    always in the skill roster too. The agent side, however, can
    legitimately name a plugin-shipped agent (e.g. `forge:reviewer`) that
    this repo does not define — no node exists to draw an edge to, so it's
    skipped rather than flagged.
    """
    _write(
        tmp_path / "skills" / "ship" / "SKILL.md",
        '# ship\nTask(subagent_type="forge:ghost")\n',
    )
    roster = vad._roster(tmp_path)
    invokes = vad._discover_invokes(tmp_path)
    assert vad._check_invokes(invokes, roster, set()) == []


# --- _check_guard_map -------------------------------------------------------


def test_check_guard_map_happy_path_returns_empty(tmp_path: Path) -> None:
    """A guard entry naming a real agent + real hook with a matching edge passes."""
    _build_repo(tmp_path)
    roster = vad._roster(tmp_path)
    guard_map = {"reviewer": ["block_push"]}
    edge_pairs = {("reviewer", "hk_block_push")}
    assert vad._check_guard_map(guard_map, roster, edge_pairs) == []


def test_check_guard_map_flags_missing_edge(tmp_path: Path) -> None:
    """A guard entry with a real agent + hook but no matching edge is flagged."""
    _build_repo(tmp_path)
    roster = vad._roster(tmp_path)
    guard_map = {"reviewer": ["block_push"]}
    problems = vad._check_guard_map(guard_map, roster, set())
    assert problems == [
        (
            "edge: guard map declares hook 'block_push' on agent 'reviewer'"
            " but the doc has no reviewer -> hk_block_push edge"
        )
    ]


def test_check_guard_map_flags_unknown_hook(tmp_path: Path) -> None:
    """A guard entry naming a hook that does not exist in the roster is flagged."""
    _build_repo(tmp_path)
    roster = vad._roster(tmp_path)
    guard_map = {"reviewer": ["block_ghost"]}
    problems = vad._check_guard_map(guard_map, roster, set())
    assert problems == ["guard-map: hook 'block_ghost' does not exist"]


def test_check_guard_map_flags_unknown_agent(tmp_path: Path) -> None:
    """A guard entry keyed on an agent id absent from the roster is flagged."""
    _build_repo(tmp_path)
    roster = vad._roster(tmp_path)
    guard_map = {"ghost": ["block_push"]}
    problems = vad._check_guard_map(guard_map, roster, set())
    assert problems == ["guard-map: agent 'ghost' does not match any repo agent"]


def test_check_guard_map_short_circuits_on_bad_agent(tmp_path: Path) -> None:
    """An entry with both a bad agent key and a bad hook reports only the agent problem.

    SCENARIO: `_check_guard_map` `continue`s immediately after flagging an
    unknown agent id, so its hooks are never inspected — a hook that would
    independently be flagged as unknown never gets its own problem in the
    same pass.
    """
    _build_repo(tmp_path)
    roster = vad._roster(tmp_path)
    guard_map = {"ghost": ["block_ghost"]}
    problems = vad._check_guard_map(guard_map, roster, set())
    assert problems == ["guard-map: agent 'ghost' does not match any repo agent"]


# --- _check_edges -----------------------------------------------------------


def test_check_edges_self_skips_when_no_mermaid_no_delegations_no_guards(
    tmp_path: Path,
) -> None:
    """A doc with no mermaid blocks, no wired delegations, and no guard map is clean."""
    _build_repo(tmp_path)
    roster = vad._roster(tmp_path)
    doc = "Plain prose describing the reviewer agent and ship skill."
    assert vad._check_edges(tmp_path, doc, roster) == []


def test_check_edges_combines_endpoint_and_guard_map_problems(tmp_path: Path) -> None:
    """`_check_edges` unions problems across the endpoint and guard-map checks.

    SCENARIO: the doc's mermaid block has one edge with an unclassed node
    ('ghost', an endpoint problem), and `[tool.forge.agent_doc].guarded_by`
    names a hook that does not exist (a guard-map problem).
    EXPECTED BEHAVIOR: the combined problem list carries both, not just
    whichever check ran first.
    """
    _write(tmp_path / "agents" / "reviewer.md", "# reviewer\n")
    _write(
        tmp_path / "pyproject.toml",
        '[tool.forge.agent_doc]\npath = "docs/agent-architecture.md"\n'
        'guarded_by = { reviewer = ["block_ghost"] }\n',
    )
    doc = "```mermaid\nghost -->|calls| reviewer\nclass reviewer agent\n```\n"
    roster = vad._roster(tmp_path)
    problems = vad._check_edges(tmp_path, doc, roster)
    assert any("node 'ghost' has no class declaration" in p for p in problems)
    assert any("hook 'block_ghost' does not exist" in p for p in problems)


def test_check_edges_and_main_pass_on_well_formed_full_repo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fully-wired repo (delegation + guard + matching mermaid edges) is clean.

    SCENARIO: skills/ship/SKILL.md delegates to the reviewer agent via
    subagent_type=, [tool.forge.agent_doc].guarded_by wires reviewer to
    block_push, and the doc's single mermaid block draws both the
    sk_ship -> reviewer and reviewer -> hk_block_push edges with every node
    classed. This is the steady-state "everything lines up" case, exercised
    through both `_check_edges` directly and `main()` end-to-end.
    EXPECTED BEHAVIOR: `_check_edges` returns no problems and `main()` exits 0.
    MOCK SETUP: repo_root pinned to tmp_path; argv patched so argparse does
    not consume pytest's own arguments.
    """
    _build_repo(tmp_path)
    _write(
        tmp_path / "skills" / "ship" / "SKILL.md",
        '# ship\nTask(subagent_type="forge:reviewer")\n',
    )
    _write(
        tmp_path / "pyproject.toml",
        '[project.scripts]\nforge-ship = "x:main"\n'
        '[tool.forge.agent_doc]\npath = "docs/agent-architecture.md"\n'
        'guarded_by = { reviewer = ["block_push"] }\n',
    )
    doc = (
        "The reviewer agent runs after the ship skill.\n"
        "```mermaid\n"
        "sk_ship -->|delegates via| reviewer\n"
        "reviewer -.->|guarded by| hk_block_push\n"
        "class reviewer agent\n"
        "class sk_ship skill\n"
        "class hk_block_push hook\n"
        "```\n"
    )
    _write(tmp_path / "docs" / "agent-architecture.md", doc)
    roster = vad._roster(tmp_path)
    assert vad._check_edges(tmp_path, doc, roster) == []

    monkeypatch.setattr(vad, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(sys, "argv", ["verify-forge-agent-doc"])
    assert vad.main() == 0


def test_check_edges_combines_endpoint_invokes_and_guard_map_problems(
    tmp_path: Path,
) -> None:
    """`_check_edges` unions problems across all three structural checks at once.

    SCENARIO: the doc's mermaid block has one edge with an unclassed node
    ('ghost', an endpoint problem); skills/ship/SKILL.md wires a
    subagent_type= delegation to the reviewer agent with no matching
    sk_ship -> reviewer edge in the doc (an invokes problem); and
    [tool.forge.agent_doc].guarded_by wires reviewer to the real block_push
    hook with no matching reviewer -> hk_block_push edge (a guard-map
    problem).
    EXPECTED BEHAVIOR: the combined problem list carries all three, not
    just whichever check ran first.
    """
    _write(tmp_path / "agents" / "reviewer.md", "# reviewer\n")
    _write(
        tmp_path / "skills" / "ship" / "SKILL.md",
        '# ship\nTask(subagent_type="forge:reviewer")\n',
    )
    _write(tmp_path / "claude-hooks" / "block_push.sh", "#!/bin/sh\n")
    _write(
        tmp_path / "pyproject.toml",
        '[tool.forge.agent_doc]\npath = "docs/agent-architecture.md"\n'
        'guarded_by = { reviewer = ["block_push"] }\n',
    )
    doc = "```mermaid\nghost -->|calls| reviewer\nclass reviewer agent\n```\n"
    roster = vad._roster(tmp_path)
    problems = vad._check_edges(tmp_path, doc, roster)
    assert any("node 'ghost' has no class declaration" in p for p in problems)
    assert any("skill '/ship' delegates to agent 'reviewer'" in p for p in problems)
    assert any(
        "guard map declares hook 'block_push' on agent 'reviewer'" in p
        for p in problems
    )


# --- main -------------------------------------------------------------------


def test_main_self_skips_when_no_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """main() exits 0 when [tool.forge.agent_doc].path is unset.

    MOCK SETUP: repo_root pinned to an empty tmp_path (no pyproject.toml,
    so _config_doc_path returns None); argv patched so argparse does not
    consume pytest's arguments.
    """
    monkeypatch.setattr(vad, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(sys, "argv", ["verify-forge-agent-doc"])
    assert vad.main() == 0


def test_main_returns_one_when_doc_file_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """main() exits 1 when the configured doc file does not exist on disk.

    MOCK SETUP: repo_root pinned to tmp_path with a config pointing at a
    doc path that is never created; argv patched.
    """
    _write(
        tmp_path / "pyproject.toml",
        '[tool.forge.agent_doc]\npath = "docs/agent-architecture.md"\n',
    )
    monkeypatch.setattr(vad, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(sys, "argv", ["verify-forge-agent-doc"])
    assert vad.main() == 1


def test_main_returns_one_when_out_of_sync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """main() exits 1 and reports problems when the doc is out of sync.

    MOCK SETUP: full repo tree with a doc that omits the real skill;
    repo_root pinned to tmp_path and argv patched.
    """
    _build_repo(tmp_path)
    _write(
        tmp_path / "docs" / "agent-architecture.md",
        "Only the reviewer agent is discussed here.",
    )
    monkeypatch.setattr(vad, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(sys, "argv", ["verify-forge-agent-doc"])
    assert vad.main() == 1


def test_main_returns_zero_when_in_sync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """main() exits 0 when the configured doc covers every agent and skill.

    MOCK SETUP: full repo tree with a doc mentioning the agent, skill,
    hook, and CLI; repo_root pinned to tmp_path and argv patched.
    """
    _build_repo(tmp_path)
    _write(
        tmp_path / "docs" / "agent-architecture.md",
        "The reviewer agent runs after ship. Uses forge-ship and block_push.",
    )
    monkeypatch.setattr(vad, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(sys, "argv", ["verify-forge-agent-doc"])
    assert vad.main() == 0


def test_main_returns_one_when_edge_structure_out_of_sync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """main() exits 1 when a wired skill→agent delegation has no matching doc edge.

    MOCK SETUP: full repo tree, with skills/ship/SKILL.md additionally wired
    to delegate to the reviewer agent via subagent_type=; the doc mentions
    both by name (satisfying coverage) but its mermaid block draws an
    sk_ship -> hk_block_push edge instead of the required sk_ship ->
    reviewer one. repo_root pinned to tmp_path and argv patched.
    """
    _build_repo(tmp_path)
    _write(
        tmp_path / "skills" / "ship" / "SKILL.md",
        '# ship\nTask(subagent_type="forge:reviewer")\n',
    )
    _write(
        tmp_path / "docs" / "agent-architecture.md",
        "The reviewer agent runs after ship. Uses forge-ship and block_push.\n"
        "```mermaid\n"
        "sk_ship -->|starts| hk_block_push\n"
        "class sk_ship skill\n"
        "class hk_block_push hook\n"
        "```\n",
    )
    monkeypatch.setattr(vad, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(sys, "argv", ["verify-forge-agent-doc"])
    assert vad.main() == 1


def test_diff_report_rejects_dash_prefixed_base(tmp_path: Path) -> None:
    """A dash-prefixed --diff base is refused before reaching git.

    SCENARIO: a `base` like `--output=<path>` would be parsed by git as an
    option (not a ref) and write to an attacker-chosen path despite the `--`
    pathspec separator. The guard must reject it before the subprocess runs.
    EXPECTED BEHAVIOR: empty report, and no file created at the injected path.
    """
    pwned = tmp_path / "pwned.txt"
    assert vad._diff_report(tmp_path, f"--output={pwned}") == []
    assert not pwned.exists()


def test_diff_report_happy_path_extracts_edge_lines(tmp_path: Path) -> None:
    """Added lines matching an edge pattern are returned as structured entries.

    SCENARIO: a real git repo gets a baseline commit, then agents/foo.md is
    amended to add a `subagent_type=` delegation line alongside unrelated
    prose. `_diff_report` must surface only the graph-relevant addition,
    classified via `_classify_mention` rather than as a raw diff line.
    """
    _write(tmp_path / "agents" / "foo.md", "# foo\n")
    _git_commit_all(tmp_path, "initial")
    _write(
        tmp_path / "agents" / "foo.md",
        '# foo\nDelegates via subagent_type="forge:design-checker".\nsome prose\n',
    )
    lines = vad._diff_report(tmp_path, "HEAD")
    assert "added agents/foo.md: delegates → design-checker" in lines
    assert not any("prose" in line for line in lines)


def test_diff_report_filters_non_graph_lines(tmp_path: Path) -> None:
    """A diff touching only non-graph-relevant lines returns an empty list."""
    _write(tmp_path / "agents" / "foo.md", "# foo\nOriginal description.\n")
    _git_commit_all(tmp_path, "initial")
    _write(tmp_path / "agents" / "foo.md", "# foo\nRevised description, still prose.\n")
    assert vad._diff_report(tmp_path, "HEAD") == []


def test_diff_report_returns_empty_on_bad_ref(tmp_path: Path) -> None:
    """An unresolvable base ref hits the CalledProcessError fallback, not a raise."""
    _write(tmp_path / "agents" / "foo.md", "# foo\n")
    _git_commit_all(tmp_path, "initial")
    assert vad._diff_report(tmp_path, "no-such-ref-xyz") == []


def test_main_diff_branch_returns_zero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """main() takes the --diff branch and returns 0 without checking sync.

    MOCK SETUP: full repo tree plus a committed git history so `_diff_report`
    can run a real `git diff`; repo_root pinned to tmp_path and argv patched
    to `--diff HEAD`.
    """
    _build_repo(tmp_path)
    _write(
        tmp_path / "docs" / "agent-architecture.md",
        "Only the reviewer agent is discussed here.",
    )
    _git_commit_all(tmp_path, "initial")
    monkeypatch.setattr(vad, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(sys, "argv", ["verify-forge-agent-doc", "--diff", "HEAD"])
    assert vad.main() == 0


def test_main_rejects_doc_path_escaping_repo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """main() exits 1 when the configured doc path escapes the repo root.

    SCENARIO: [tool.forge.agent_doc].path is set to "../evil.md", which
    resolves outside tmp_path.
    MOCK SETUP: repo_root pinned to tmp_path; argv patched.
    EXPECTED BEHAVIOR: the `is_relative_to(root)` guard rejects the path
    before any file-existence check runs.
    """
    _write(
        tmp_path / "pyproject.toml",
        '[tool.forge.agent_doc]\npath = "../evil.md"\n',
    )
    monkeypatch.setattr(vad, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(sys, "argv", ["verify-forge-agent-doc"])
    assert vad.main() == 1


def test_diff_report_surfaces_bare_slash_skill_mention(tmp_path: Path) -> None:
    """A bare ``/name`` mention with no other edge token is now surfaced.

    SCENARIO: before ``edge_re`` gained ``_SKILL_RE``, a diff line mentioning
    a skill only as a bare slash-command ("use `/commit`") — with no
    ``subagent_type=``, ``Skill(skill=``, CLI, or hook token alongside it —
    was invisible to `_diff_report`. It must now be surfaced, classified as
    a "mentions skill" edge.
    """
    _write(tmp_path / "skills" / "ship" / "SKILL.md", "# ship\n")
    _git_commit_all(tmp_path, "initial")
    _write(
        tmp_path / "skills" / "ship" / "SKILL.md",
        "# ship\nAfter shipping, use `/commit` to finish up.\n",
    )
    lines = vad._diff_report(tmp_path, "HEAD")
    assert any("mentions skill /commit" in line for line in lines)


def test_diff_report_reports_removed_line_with_removed_sign(tmp_path: Path) -> None:
    """A deleted graph-relevant line is reported with the `removed` sign.

    SCENARIO: agents/foo.md starts with a `subagent_type=` delegation line
    that is then deleted entirely. `_diff_report` must surface it as a
    `removed` entry, mirroring how an addition is surfaced as `added`.
    """
    _write(
        tmp_path / "agents" / "foo.md",
        '# foo\nDelegates via subagent_type="forge:design-checker".\n',
    )
    _git_commit_all(tmp_path, "initial")
    _write(tmp_path / "agents" / "foo.md", "# foo\n")
    lines = vad._diff_report(tmp_path, "HEAD")
    assert any(line.startswith("removed ") for line in lines)
    assert "removed agents/foo.md: delegates → design-checker" in lines


def test_diff_report_attributes_deleted_file_correctly(tmp_path: Path) -> None:
    """A whole-file deletion is attributed to the deleted file, not its neighbor.

    SCENARIO: agents/foo.md (containing a `subagent_type=` delegation line) is
    deleted entirely, and agents/alpha.md — which sorts before foo.md and thus
    precedes it in the diff — is modified in the same commit. On a whole-file
    delete git emits `+++ /dev/null` for foo.md's hunk, so code keying only off
    the `+++ b/<path>` header would misattribute foo.md's removed line to
    whichever file's `+++ b/` header last appeared, i.e. alpha.md.
    EXPECTED BEHAVIOR: `_diff_report` tracks the `--- a/<file>` side too and
    attributes the removed line to agents/foo.md; no entry mentions alpha.md.
    """
    _write(tmp_path / "agents" / "alpha.md", "# alpha\nOriginal alpha description.\n")
    _write(
        tmp_path / "agents" / "foo.md",
        '# foo\nDelegates via subagent_type="forge:design-checker".\n',
    )
    _git_commit_all(tmp_path, "initial")
    (tmp_path / "agents" / "foo.md").unlink()
    _write(tmp_path / "agents" / "alpha.md", "# alpha\nRevised alpha description.\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    lines = vad._diff_report(tmp_path, "HEAD")
    assert lines == ["removed agents/foo.md: delegates → design-checker"]
    assert not any("alpha.md" in line for line in lines)


def test_diff_report_skips_sh_comment_but_reports_md_heading(tmp_path: Path) -> None:
    """A `#`-comment added in a .sh file is skipped; a `#`-heading in .md is not.

    SCENARIO: claude-hooks/guard.sh and agents/foo.md both start committed
    with minimal content, then each gains a `#`-prefixed line naming a
    graph token — a shell comment (`# guarded by block_push`) and a
    markdown heading (`# use /ship next`) respectively. The shell comment
    is a no-op guard mention (skipped, .sh-scoped, since `#` there is a
    real comment); the markdown heading still carries a real mention
    (a leading `#` in .md is a heading, not a comment).
    """
    _write(tmp_path / "claude-hooks" / "guard.sh", "#!/bin/sh\n")
    _write(tmp_path / "agents" / "foo.md", "# foo\n")
    _git_commit_all(tmp_path, "initial")
    _write(
        tmp_path / "claude-hooks" / "guard.sh",
        "#!/bin/sh\n# guarded by block_push\n",
    )
    _write(tmp_path / "agents" / "foo.md", "# foo\n# use /ship next\n")
    lines = vad._diff_report(tmp_path, "HEAD")
    assert not any("block_push" in line for line in lines)
    assert any("mentions skill /ship" in line for line in lines)


# --- _classify_mention -------------------------------------------------------


def test_classify_mention_delegates_on_subagent_type() -> None:
    """A `subagent_type="forge:X"` mention classifies as a delegation edge."""
    text = 'Task(subagent_type="forge:design-checker", prompt="...")'
    assert vad._classify_mention(text) == "delegates → design-checker"


def test_classify_mention_chains_skill_on_skill_call() -> None:
    """A `Skill(skill="X")` mention classifies as a skill-chaining edge."""
    text = 'Skill(skill="forge:commit")'
    assert vad._classify_mention(text) == "chains skill /commit"


def test_classify_mention_invokes_cli_on_forge_prefixed_token() -> None:
    """A bare `forge-*` CLI token classifies as a CLI-invocation edge."""
    text = "Runs `forge-precommit` before committing."
    assert vad._classify_mention(text) == "invokes CLI forge-precommit"


def test_classify_mention_guarded_by_hook_on_block_prefixed_token() -> None:
    """A `block_*` token classifies as a hook-guard edge."""
    text = "Enforced by the block_push hook."
    assert vad._classify_mention(text) == "guarded by hook block_push"


def test_classify_mention_mentions_skill_on_bare_slash_name() -> None:
    """A bare `/name` mention classifies as a skill mention edge."""
    text = "After shipping, use `/commit` to finish up."
    assert vad._classify_mention(text) == "mentions skill /commit"


def test_classify_mention_none_on_plain_prose() -> None:
    """Plain prose with no graph-relevant mention classifies as None."""
    assert vad._classify_mention("This is just an ordinary sentence.") is None
