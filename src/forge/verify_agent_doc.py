"""verify-forge-agent-doc — keep a hand-maintained agent-architecture doc in line.

A repo may keep a hand-curated document describing how its agents, skills,
hooks, and CLIs interact (forge's own lives at ``docs/agent-architecture.md``).
Its **nodes** are discoverable from the repo, so they can be gated deterministically
even though the **edges** stay curated. This verifier is the deterministic floor
(Layer 1) under that doc:

- **coverage** — every real agent (``agents/*.md``) and skill (``skills/*/``) is
  *mentioned* in the doc. Add an agent, forget to document it → flagged. Hooks
  are exempt: most are low-level bash guards unrelated to the agent graph, so
  only the few guarding an agent action appear (a missing-but-relevant hook is
  Layer 2's call, not a deterministic gate).
- **no-dangling** — every distinctly-shaped reference the doc makes to a hook
  (``block_*``), a CLI (``forge-*`` / ``verify-forge-*`` / ``install-forge-*``),
  or a skill (``/name``) resolves to something that exists. Rename or delete one,
  leave a stale mention → flagged.
- **edge structure** — the mermaid edges themselves get a structural floor:
  (a) every skill→agent delegation wired in a ``SKILL.md``
  (``subagent_type=...``) must appear as an edge from that skill's node to
  that agent's node, (b) every ``[tool.forge.agent_doc].guarded_by`` entry
  (agent → guard hooks) must appear as an agent→hook edge, and (c) every
  edge endpoint must resolve to a real roster node via the mechanical id
  scheme (``sk_``/``hk_``/``cli_`` prefixes, ``-``→``_``) — nodes classed
  ``policy`` / ``person`` / ``orchestrator`` are structural and exempt.
  Edge *verbs* stay curated: checks match endpoints, never label text, so a
  descriptive verb (``records validated plan via``) is as valid as
  ``invokes``.

Edge *semantics* beyond that floor (is the verb right? is a curated
delegation still real?) remain the diff-scoped agent verify's call (Layer 2,
see the ``--diff`` report + ``docs-types-checker``).

**Self-skips** when ``[tool.forge.agent_doc].path`` is unset or the file is
absent, so consumer repos without such a doc are unaffected.

Exit codes:
    0  in sync, or self-skipped (no configured doc)
    1  a real agent/skill/hook is undocumented, the doc names something that does
       not exist, or the config/doc could not be read

Integration:
    Called by ``forge-precommit`` as the ``agent_doc`` step; output is written to
    ``code_health/agent_doc.log``.
"""

from __future__ import annotations

import argparse
import logging
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from importlib import resources
from typing import TYPE_CHECKING

from forge.config import (
    AGENT_DEFINITION_DIRS,
    HOOK_DEFINITION_DIRS,
    SKILL_DEFINITION_DIRS,
    installed_console_scripts,
    read_tool_forge_section,
)
from forge.git_utils import capturing_to_step_log, configure_cli_logging, repo_root


if TYPE_CHECKING:
    from pathlib import Path


configure_cli_logging()
logger = logging.getLogger(__name__)

# Distinctly-shaped references the doc can make, each verifiable against a roster.
# The hook pattern is deliberately scoped to ``block_*`` (the guard hooks that
# gate agent actions and so appear in the graph); ``check_*`` / ``warn_*`` hooks
# exist but are not dangling-checked, since widening the prefix would match plain
# prose words like "check_something" and raise false positives.
_HOOK_RE = re.compile(r"\bblock_[a-z0-9_]+\b")
_CLI_RE = re.compile(r"\b(?:forge|verify-forge|install-forge|fix-forge)-[a-z0-9-]+\b")
# The lookbehind also excludes `!` so a shell shebang (`#!/usr/bin/env`) does
# not classify as a skill mention `/usr`. An optional `plugin:` qualifier
# (`/forge:pr`) is tolerated and resolution happens on the bare name —
# group 1 stays the skill name, matching `_DELEGATE_RE`'s prefix handling.
_SKILL_RE = re.compile(r"(?<![\w/!])/(?:[a-z][a-z0-9-]*:)?([a-z][a-z0-9-]+)\b")
# A skill's wired delegation to an agent — shared by the Layer-2 mention
# classifier and the invokes-edge discovery so they can never drift (§12).
_DELEGATE_RE = re.compile(r"subagent_type=[\"']?(?:forge:)?([a-z0-9-]+)")

# Mermaid parsing for the edge-structure checks. Two edge syntaxes appear in
# hand-curated docs: piped labels (`A -->|verb| B`, `A -.->|verb| B`) and the
# dotted spaced-label form (`A -. text .-> B`).
_MERMAID_BLOCK_RE = re.compile(r"```mermaid\n(.*?)```", re.DOTALL)
_EDGE_PIPE_RE = re.compile(
    r"^\s*([A-Za-z0-9_]+)\s*(?:-->|-\.->)\s*\|([^|]+)\|\s*([A-Za-z0-9_]+)\s*$"
)
_EDGE_DOTTED_RE = re.compile(
    r"^\s*([A-Za-z0-9_]+)\s*-\.\s+(.+?)\s+\.->\s*([A-Za-z0-9_]+)\s*$"
)
_CLASS_RE = re.compile(r"^\s*class\s+([A-Za-z0-9_]+)\s+([a-z]+)\s*$")
# Structural node classes with no roster counterpart — always-valid endpoints.
_STRUCTURAL_CLASSES = frozenset({"person", "orchestrator", "policy"})


def _config_doc_path(root: Path) -> str | None:
    """Return the configured agent-doc path, or ``None`` to self-skip.

    Args:
        root: Repository root directory.

    Returns:
        The repo-relative path string under ``[tool.forge.agent_doc].path``,
        or ``None`` when the key (or its table) is absent.
    """
    table = read_tool_forge_section(root, "agent_doc")
    path = table.get("path")
    return path if isinstance(path, str) and path else None


def _plugin_roster() -> dict[str, set[str]]:
    """Read the shipped roster of forge's plugin skills and hook stems.

    ``data/plugin-roster.toml`` ships with the pip package so a consumer
    repo's agent doc can name plugin-provided skills and hooks without
    the checker calling them dangling (#375). A forge-repo test
    regenerates the file from ``skills/*/SKILL.md`` +
    ``claude-hooks/*.sh`` and diffs, so it cannot go stale.

    Returns:
        ``{"skills": set, "hooks": set}``; both empty when the data file
        is absent (older installed package) or unparseable — enrichment
        degrades, never blocks.
    """
    try:
        text = resources.files("forge").joinpath("data/plugin-roster.toml").read_text()
        data = tomllib.loads(text)
    except (FileNotFoundError, OSError, tomllib.TOMLDecodeError):
        return {"skills": set(), "hooks": set()}
    out: dict[str, set[str]] = {}
    for kind in ("skills", "hooks"):
        names = data.get(kind, {}).get("names", [])
        out[kind] = {n for n in names if isinstance(n, str)}
    return out


def _extra_roster(root: Path) -> dict[str, set[str]]:
    """Read the consumer escape-hatch roster additions from config.

    ``[tool.forge.agent_doc].extra_clis`` / ``extra_hooks`` /
    ``extra_skills`` let a repo resolve names the automatic enrichment
    cannot see — e.g. a plugin version newer than the pinned pip
    package, or a third-party CLI the doc legitimately references.
    Misshapen values are dropped, never raised (same contract as
    :func:`_guard_map`).

    Args:
        root: Repository root directory.

    Returns:
        ``{"clis": set, "hooks": set, "skills": set}``.
    """
    table = read_tool_forge_section(root, "agent_doc")
    out: dict[str, set[str]] = {}
    for key, kind in (
        ("extra_clis", "clis"),
        ("extra_hooks", "hooks"),
        ("extra_skills", "skills"),
    ):
        value = table.get(key)
        out[kind] = (
            {v for v in value if isinstance(v, str)}
            if isinstance(value, list)
            else set()
        )
    return out


def _roster(root: Path) -> dict[str, set[str]]:
    """Discover the repo's agents, skills, hooks, and CLIs.

    Two tiers with distinct consumers (#375): the repo-local sets
    (``agents`` / ``skills`` / ``hooks`` / ``clis``) drive the coverage
    and edge-requirement checks, while the ``*_known`` resolution sets —
    repo-local U shipped plugin roster U installed forge-scripts console
    scripts U config extras — drive the dangling-reference and mermaid
    endpoint checks, so a consumer doc can name plugin-provided surface
    without being required to document all of it.

    Args:
        root: Repository root directory.

    Returns:
        A mapping with the repo-local keys ``agents`` / ``skills`` /
        ``hooks`` / ``clis`` plus resolution-only ``skills_known`` /
        ``hooks_known`` / ``clis_known``. Underscore-prefixed agent
        files (e.g. ``_TEMPLATE``) are excluded — templates, not agents.
    """
    agents = {
        p.stem
        for d in AGENT_DEFINITION_DIRS
        for p in (root / d).glob("*.md")
        if not p.stem.startswith("_")
    }
    skills = {
        p.parent.name
        for d in SKILL_DEFINITION_DIRS
        for p in (root / d).glob("*/SKILL.md")
    }
    hooks = {p.stem for d in HOOK_DEFINITION_DIRS for p in (root / d).glob("*.sh")}
    clis: set[str] = set()
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        clis = set(data.get("project", {}).get("scripts", {}))
    plugin = _plugin_roster()
    extra = _extra_roster(root)
    forge_clis = installed_console_scripts("forge-scripts") or set()
    return {
        "agents": agents,
        "skills": skills,
        "hooks": hooks,
        "clis": clis,
        "skills_known": skills | plugin["skills"] | extra["skills"],
        "hooks_known": hooks | plugin["hooks"] | extra["hooks"],
        "clis_known": clis | forge_clis | extra["clis"],
    }


def _check_doc(doc: str, roster: dict[str, set[str]]) -> list[str]:
    """Return coverage + dangling problems for *doc* against *roster*.

    Args:
        doc: The agent-doc text.
        roster: The repo roster from :func:`_roster`.

    Returns:
        A list of human-readable problem strings; empty when the doc is in sync.
    """
    problems: list[str] = []
    # Coverage — every agent + skill (the fleet) is mentioned by name. Hooks are
    # NOT fully covered: most are low-level bash guards unrelated to the agent
    # graph; only the few guarding an agent action appear, so requiring all of
    # them would force noise. A missing hook that *should* be shown is Layer 2's
    # (the diff-scoped agent verify) call, not a deterministic gate.
    problems.extend(
        f"coverage: {kind[:-1]} '{name}' is not mentioned"
        for kind in ("agents", "skills")
        for name in sorted(roster[kind])
        if not re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", doc)
    )
    # No-dangling — every distinctly-shaped reference resolves to a real name.
    problems.extend(
        f"dangling: hook '{match}' does not exist"
        for match in sorted(set(_HOOK_RE.findall(doc)))
        if match not in roster["hooks_known"]
    )
    problems.extend(
        f"dangling: CLI '{match}' does not exist"
        for match in sorted(set(_CLI_RE.findall(doc)))
        if match not in roster["clis_known"]
    )
    problems.extend(
        f"dangling: skill '/{match}' does not exist"
        for match in sorted(set(_SKILL_RE.findall(doc)))
        if match not in roster["skills_known"]
    )
    return problems


@dataclass(frozen=True)
class Edge:
    """One hand-curated mermaid edge (``src --[verb]--> dst``) in the agent doc.

    Attributes:
        src: Source node id.
        verb: Edge label text (structurally unchecked — see the module
            docstring's edge-structure section).
        dst: Destination node id.
    """

    src: str
    verb: str
    dst: str


def _parse_blocks(doc: str) -> list[tuple[list[Edge], dict[str, set[str]]]]:
    """Parse each mermaid block into its edges and node-class declarations.

    Blocks are kept separate because mermaid scopes node declarations per
    block — an edge endpoint must be classed in its own block to render.

    Args:
        doc: The agent-doc text.

    Returns:
        One ``(edges, classes)`` pair per mermaid block, where ``classes``
        maps a node id to the set of classes ``class <id> <name>`` lines
        assign it (kind classes like ``agent`` plus modifiers like
        ``reporter``).
    """
    blocks: list[tuple[list[Edge], dict[str, set[str]]]] = []
    for block in _MERMAID_BLOCK_RE.findall(doc):
        edges: list[Edge] = []
        classes: dict[str, set[str]] = {}
        for line in block.splitlines():
            match = _EDGE_PIPE_RE.match(line) or _EDGE_DOTTED_RE.match(line)
            if match:
                src, verb, dst = match.groups()
                edges.append(Edge(src, verb.strip(), dst))
                continue
            class_match = _CLASS_RE.match(line)
            if class_match:
                classes.setdefault(class_match.group(1), set()).add(
                    class_match.group(2)
                )
        blocks.append((edges, classes))
    return blocks


def _node_kind_ids(roster: dict[str, set[str]]) -> dict[str, set[str]]:
    """Map each node kind to the mermaid ids its roster names produce.

    The id scheme is mechanical: agents use their name with ``-``→``_``;
    skills, hooks, and CLIs prefix ``sk_`` / ``hk_`` / ``cli_`` on top.

    Args:
        roster: The repo roster from :func:`_roster`.

    Returns:
        A mapping from kind class (``agent`` / ``skill`` / ``hook`` /
        ``cli``) to the set of valid node ids for that kind.
    """
    return {
        "agent": {name.replace("-", "_") for name in roster["agents"]},
        "skill": {"sk_" + name.replace("-", "_") for name in roster["skills_known"]},
        "hook": {"hk_" + name for name in roster["hooks_known"]},
        "cli": {"cli_" + name.replace("-", "_") for name in roster["clis_known"]},
    }


def _discover_invokes(root: Path) -> set[tuple[str, str]]:
    """Collect the skill→agent delegations wired in the repo's skill files.

    Args:
        root: Repository root directory.

    Returns:
        ``(skill_name, agent_name)`` pairs, one per distinct
        ``subagent_type=`` reference in a ``SKILL.md``.
    """
    return {
        (path.parent.name, agent)
        for d in SKILL_DEFINITION_DIRS
        for path in (root / d).glob("*/SKILL.md")
        for agent in _DELEGATE_RE.findall(path.read_text(encoding="utf-8"))
    }


def _guard_map(root: Path) -> dict[str, list[str]]:
    """Read the configured agent→hooks guard map, degrading to empty on misshape.

    Args:
        root: Repository root directory.

    Returns:
        The ``[tool.forge.agent_doc].guarded_by`` table (agent node id →
        guard-hook names), with non-table values and non-string-list entries
        dropped rather than raising — a config footgun degrades the check,
        never the whole verifier.
    """
    table = read_tool_forge_section(root, "agent_doc").get("guarded_by")
    if not isinstance(table, dict):
        return {}
    return {
        agent: hooks
        for agent, hooks in table.items()
        if isinstance(hooks, list) and all(isinstance(h, str) for h in hooks)
    }


def _check_endpoints(
    blocks: list[tuple[list[Edge], dict[str, set[str]]]],
    kind_ids: dict[str, set[str]],
) -> list[str]:
    """Return problems for edge endpoints that resolve to no real node.

    Args:
        blocks: Per-block edges + class declarations from :func:`_parse_blocks`.
        kind_ids: Valid node ids per kind from :func:`_node_kind_ids`.

    Returns:
        A list of human-readable problem strings; empty when every endpoint
        is either structural (person / orchestrator / policy) or matches a
        roster node of its declared kind.
    """
    problems: list[str] = []
    for edges, classes in blocks:
        node_ids = {edge.src for edge in edges} | {edge.dst for edge in edges}
        for node_id in sorted(node_ids):
            node_classes = classes.get(node_id, set())
            if not node_classes:
                problems.append(
                    f"edge: node '{node_id}' has no class declaration in its block"
                )
            elif node_classes & _STRUCTURAL_CLASSES:
                continue
            elif not any(node_id in kind_ids.get(cls, set()) for cls in node_classes):
                problems.append(
                    f"edge: node '{node_id}' (classed"
                    f" {'/'.join(sorted(node_classes))}) matches no repo"
                    " agent/skill/hook/CLI"
                )
    return problems


def _check_invokes(
    invokes: set[tuple[str, str]],
    roster: dict[str, set[str]],
    edge_pairs: set[tuple[str, str]],
) -> list[str]:
    """Return problems for wired skill→agent delegations missing from the doc.

    Only pairs whose skill AND agent are both in the roster are required —
    a delegation to a plugin-shipped agent the repo does not define has no
    node to draw an edge to.

    Args:
        invokes: Wired delegations from :func:`_discover_invokes`.
        roster: The repo roster from :func:`_roster`.
        edge_pairs: Every ``(src, dst)`` edge in the doc, any verb.

    Returns:
        A list of human-readable problem strings; empty when every wired
        delegation has a matching edge.
    """
    problems: list[str] = []
    for skill, agent in sorted(invokes):
        if skill not in roster["skills"] or agent not in roster["agents"]:
            continue
        pair = ("sk_" + skill.replace("-", "_"), agent.replace("-", "_"))
        if pair not in edge_pairs:
            problems.append(
                f"edge: skill '/{skill}' delegates to agent '{agent}'"
                f" (subagent_type=) but the doc has no"
                f" {pair[0]} -> {pair[1]} edge"
            )
    return problems


def _check_guard_map(
    guard_map: dict[str, list[str]],
    roster: dict[str, set[str]],
    edge_pairs: set[tuple[str, str]],
) -> list[str]:
    """Return problems for configured agent→hook guards missing from the doc.

    Args:
        guard_map: The configured guard map from :func:`_guard_map`.
        roster: The repo roster from :func:`_roster`.
        edge_pairs: Every ``(src, dst)`` edge in the doc, any verb.

    Returns:
        A list of human-readable problem strings; empty when every declared
        guard names a real agent and hook and has a matching edge.
    """
    problems: list[str] = []
    for agent_id, hooks in sorted(guard_map.items()):
        if agent_id.replace("_", "-") not in roster["agents"]:
            problems.append(
                f"guard-map: agent '{agent_id}' does not match any repo agent"
            )
            continue
        for hook in hooks:
            if hook not in roster["hooks"]:
                problems.append(f"guard-map: hook '{hook}' does not exist")
            elif (agent_id, "hk_" + hook) not in edge_pairs:
                problems.append(
                    f"edge: guard map declares hook '{hook}' on agent"
                    f" '{agent_id}' but the doc has no"
                    f" {agent_id} -> hk_{hook} edge"
                )
    return problems


def _check_edges(root: Path, doc: str, roster: dict[str, set[str]]) -> list[str]:
    """Run the three structural edge checks against *doc*.

    Args:
        root: Repository root directory.
        doc: The agent-doc text.
        roster: The repo roster from :func:`_roster`.

    Returns:
        The combined problem list from the endpoint, invokes, and guard-map
        checks; empty when the doc's edge structure is in sync (including
        the trivial case of a doc with no mermaid blocks).
    """
    blocks = _parse_blocks(doc)
    edge_pairs = {(edge.src, edge.dst) for edges, _ in blocks for edge in edges}
    return [
        *_check_endpoints(blocks, _node_kind_ids(roster)),
        *_check_invokes(_discover_invokes(root), roster, edge_pairs),
        *_check_guard_map(_guard_map(root), roster, edge_pairs),
    ]


# Ordered (pattern, description-template) pairs classifying a graph-relevant
# mention in a changed diff line — the first match wins. The template's ``{name}``
# is filled with capture group 1 so the report reads as an edge, not raw text.
# The CLI / hook / skill patterns REUSE the no-dangling constants above (wrapping
# a capture group where they lack one) so the classifier and the dangling check
# can never drift apart (FOUNDATION §12).
_MENTION_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (_DELEGATE_RE, "delegates → {name}"),
    (re.compile(r"Skill\(skill=[\"']?(?:forge:)?([a-z0-9-]+)"), "chains skill /{name}"),
    (re.compile(f"({_CLI_RE.pattern})"), "invokes CLI {name}"),
    (re.compile(f"({_HOOK_RE.pattern})"), "guarded by hook {name}"),
    (_SKILL_RE, "mentions skill /{name}"),  # already captures the /name group
)


def _classify_mention(text: str) -> str | None:
    """Describe the highest-priority graph-relevant mention in *text*, or ``None``.

    Only ONE edge is reported per line — the first rule in :data:`_MENTION_RULES`
    that matches wins, by rule priority (not text order). A line naming two
    distinct edge types (e.g. a CLI *and* a hook) surfaces only the
    higher-priority one; this is acceptable for an advisory delta report where
    such lines are rare and a human reconciles against the doc anyway.

    Args:
        text: A single changed diff line (without its ``+``/``-`` marker).

    Returns:
        A human-readable edge description (e.g. ``"delegates → pr-manager"``),
        or ``None`` when the line carries no graph-relevant mention.
    """
    for pattern, template in _MENTION_RULES:
        match = pattern.search(text)
        if match:
            return template.format(name=match.group(1))
    return None


def _run_git_diff(root: Path, base: str, paths: list[str]) -> str | None:
    """Execute git diff and return its output, or None on error.

    Handles validation of the base ref and subprocess error handling.

    Args:
        root: Repository root directory.
        base: Git ref to diff against (e.g. ``origin/main``).
        paths: Paths to include in the diff.

    Returns:
        Diff output, or None if git command failed or base ref is invalid.
    """
    # `base` is user-supplied (the --diff arg). A real ref/SHA never starts with
    # a dash; a dash-prefixed value would be parsed as a git *option* (e.g.
    # `--output=…`) despite the `--` pathspec separator, which only guards paths.
    # Reject it rather than let it reach git.
    if base.startswith("-"):
        logger.error("agent_doc --diff: %r is not a valid base ref.", base)
        return None
    try:
        return subprocess.run(
            ["git", "-C", str(root), "diff", "--end-of-options", base, "--", *paths],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        logger.warning("agent_doc --diff: could not run git diff against %s", base)
        return None


def _update_file_context(line: str, a_file: str, b_file: str) -> tuple[str, str] | None:
    """Process a diff header line and return updated file context.

    Tracks both sides of the diff: removed lines are attributed to the
    ``--- a/`` file and added lines to the ``+++ b/`` file. ``/dev/null``
    resets that side to "?".

    Args:
        line: A single line from the diff output.
        a_file: Current file context for deletions.
        b_file: Current file context for additions.

    Returns:
        Updated (a_file, b_file) tuple if the line is a diff header,
        or None if it is not a header line.
    """
    if line.startswith("--- a/"):
        return (line[len("--- a/") :], b_file)
    if line.startswith("--- /dev/null"):
        return ("?", b_file)
    if line.startswith("+++ b/"):
        return (a_file, line[len("+++ b/") :])
    if line.startswith("+++ /dev/null"):
        return (a_file, "?")
    return None


def _process_content_line(line: str, a_file: str, b_file: str) -> str | None:
    """Classify a diff content line and return a report entry if graph-relevant.

    Args:
        line: A diff line starting with + or -.
        a_file: Current file context for deletions.
        b_file: Current file context for additions.

    Returns:
        A report line like "added <file>: <edge>", or None if not graph-relevant.
    """
    sign, where = ("added", b_file) if line[0] == "+" else ("removed", a_file)
    content = line[1:]
    # In shell files a `#` line is a comment: it names hooks without
    # wiring anything, so classifying it reports a phantom edge. The
    # skip is scoped to .sh — in markdown a leading `#` is a heading,
    # which can carry a real mention.
    if where.endswith(".sh") and content.lstrip().startswith("#"):
        return None
    edge = _classify_mention(content)
    return f"{sign} {where}: {edge}" if edge else None


def _diff_report(root: Path, base: str) -> list[str]:
    """Classify the graph-relevant mentions a PR added or removed vs *base*.

    The Layer-2 helper: it does not judge whether the doc is correct — it
    surfaces the delegation/invocation/guard mentions the diff touched so
    ``docs-types-checker`` can verify the configured agent doc against just
    those changes.

    Args:
        root: Repository root directory.
        base: Git ref to diff against (e.g. ``origin/main``).

    Returns:
        One ``<added|removed> <file>: <edge>`` line per changed graph-relevant
        mention; empty when the diff touches nothing graph-relevant.
    """
    paths = [*AGENT_DEFINITION_DIRS, *SKILL_DEFINITION_DIRS, *HOOK_DEFINITION_DIRS]
    diff = _run_git_diff(root, base, paths)
    if diff is None:
        return []

    report: list[str] = []
    a_file = b_file = "?"
    for line in diff.splitlines():
        updated = _update_file_context(line, a_file, b_file)
        if updated is not None:
            a_file, b_file = updated
        elif line.startswith(("+", "-")):
            entry = _process_content_line(line, a_file, b_file)
            if entry:
                report.append(entry)
    return report


def _handle_diff_mode(root: Path, path: str, base: str) -> None:
    """Report graph-relevant changes in diff mode.

    Args:
        root: Repository root directory.
        path: Configured doc file path.
        base: Git ref to diff against.
    """
    changes = _diff_report(root, base)
    if not changes:
        logger.info(
            "agent_doc: no graph-relevant changes vs %s — %s needs no review.",
            base,
            path,
        )
        return
    logger.info(
        "agent_doc: %d graph-relevant change(s) vs %s — verify %s reflects them:",
        len(changes),
        base,
        path,
    )
    for change in changes:
        logger.info("  %s", change)


def _handle_normal_mode(
    root: Path,
    doc: str,
    roster: dict[str, set[str]],
    path: str,
) -> int:
    """Check coverage, dangling references, and edge structure in normal mode.

    Args:
        root: Repository root directory.
        doc: The agent-doc text.
        roster: The repo roster from :func:`_roster`.
        path: Configured doc file path.

    Returns:
        Process exit code: ``0`` in sync, ``1`` on a problem.
    """
    problems = _check_doc(doc, roster) + _check_edges(root, doc, roster)
    if problems:
        logger.error("agent_doc: %s out of sync with the repo:", path)
        for problem in problems:
            logger.error("  %s", problem)
        return 1
    logger.info(
        "agent_doc: %s is in sync (%d agents, %d skills, %d hooks).",
        path,
        len(roster["agents"]),
        len(roster["skills"]),
        len(roster["hooks"]),
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run the agent-doc verifier.

    Args:
        argv: Optional argument vector (defaults to ``sys.argv``).

    Returns:
        Process exit code: ``0`` in sync or self-skipped, ``1`` on a problem.
    """
    parser = argparse.ArgumentParser(prog="verify-forge-agent-doc")
    parser.add_argument(
        "--diff",
        metavar="BASE",
        help="Report graph-relevant mentions changed vs BASE (Layer-2 helper).",
    )
    args = parser.parse_args(argv)
    root = repo_root()

    with capturing_to_step_log(root, "agent_doc"):
        path = _config_doc_path(root)
        if path is None:
            logger.info("agent_doc: no [tool.forge.agent_doc].path — skipping.")
            return 0
        doc_file = root / path
        if not doc_file.resolve().is_relative_to(root.resolve()):
            logger.error("agent_doc: configured doc %s escapes the repo root.", path)
            return 1
        if not doc_file.is_file():
            logger.error("agent_doc: configured doc %s does not exist.", path)
            return 1
        doc = doc_file.read_text(encoding="utf-8")
        roster = _roster(root)

        if args.diff is not None:
            _handle_diff_mode(root, path, args.diff)
            return 0
        return _handle_normal_mode(root, doc, roster, path)


if __name__ == "__main__":
    sys.exit(main())
