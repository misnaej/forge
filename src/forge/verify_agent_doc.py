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

Agent *edges* (delegations, invocations) are not gated here — that is the
diff-scoped agent verify (Layer 2, see the ``--diff`` report + ``docs-types-checker``).

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
from typing import TYPE_CHECKING

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
_SKILL_RE = re.compile(r"(?<![\w/])/([a-z][a-z0-9-]+)\b")


def _config_doc_path(root: Path) -> str | None:
    """Return the configured agent-doc path, or ``None`` to self-skip.

    Args:
        root: Repository root directory.

    Returns:
        The repo-relative path string under ``[tool.forge.agent_doc].path``,
        or ``None`` when the key (or its table) is absent.
    """
    pyproject = root / "pyproject.toml"
    if not pyproject.is_file():
        return None
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    table = data.get("tool", {}).get("forge", {}).get("agent_doc", {})
    path = table.get("path")
    return path if isinstance(path, str) and path else None


def _roster(root: Path) -> dict[str, set[str]]:
    """Discover the repo's agents, skills, hooks, and CLIs.

    Args:
        root: Repository root directory.

    Returns:
        A mapping with keys ``agents`` / ``skills`` / ``hooks`` / ``clis``, each
        a set of names. Underscore-prefixed agent files (e.g. ``_TEMPLATE``) are
        excluded — they are templates, not agents.
    """
    agents = {
        p.stem
        for d in ("agents", ".claude/agents")
        for p in (root / d).glob("*.md")
        if not p.stem.startswith("_")
    }
    skills = {
        p.parent.name
        for d in ("skills", ".claude/skills")
        for p in (root / d).glob("*/SKILL.md")
    }
    hooks = {p.stem for p in (root / "claude-hooks").glob("*.sh")}
    clis: set[str] = set()
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        clis = set(data.get("project", {}).get("scripts", {}))
    return {"agents": agents, "skills": skills, "hooks": hooks, "clis": clis}


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
        if match not in roster["hooks"]
    )
    problems.extend(
        f"dangling: CLI '{match}' does not exist"
        for match in sorted(set(_CLI_RE.findall(doc)))
        if match not in roster["clis"]
    )
    problems.extend(
        f"dangling: skill '/{match}' does not exist"
        for match in sorted(set(_SKILL_RE.findall(doc)))
        if match not in roster["skills"]
    )
    return problems


# Ordered (pattern, description-template) pairs classifying a graph-relevant
# mention in a changed diff line — the first match wins. The template's ``{name}``
# is filled with the matched target so the report reads as an edge, not raw text.
_MENTION_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"subagent_type=[\"']?(?:forge:)?([a-z0-9-]+)"), "delegates → {name}"),
    (re.compile(r"Skill\(skill=[\"']?(?:forge:)?([a-z0-9-]+)"), "chains skill /{name}"),
    (
        re.compile(r"\b((?:forge|verify-forge|install-forge|fix-forge)-[a-z0-9-]+)\b"),
        "invokes CLI {name}",
    ),
    (re.compile(r"\b(block_[a-z0-9_]+)\b"), "guarded by hook {name}"),
    (re.compile(r"(?<![\w/])/([a-z][a-z0-9-]+)\b"), "mentions skill /{name}"),
)


def _classify_mention(text: str) -> str | None:
    """Describe the first graph-relevant mention in *text*, or ``None``.

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


def _diff_report(root: Path, base: str) -> list[str]:
    """Classify the graph-relevant mentions a PR added or removed vs *base*.

    The Layer-2 helper: it does not judge whether the doc is correct — it
    surfaces the delegation/invocation/guard mentions the diff touched, each as
    an ``added``/``removed`` entry tagged with its file, so ``docs-types-checker``
    can check ``docs/agent-architecture.md`` against just those changes.

    Args:
        root: Repository root directory.
        base: Git ref to diff against (e.g. ``origin/main``).

    Returns:
        One ``<added|removed> <file>: <edge>`` line per changed graph-relevant
        mention; empty when the diff touches nothing graph-relevant.
    """
    # `base` is user-supplied (the --diff arg). A real ref/SHA never starts with
    # a dash; a dash-prefixed value would be parsed as a git *option* (e.g.
    # `--output=…`) despite the `--` pathspec separator, which only guards paths.
    # Reject it rather than let it reach git.
    if base.startswith("-"):
        logger.error("agent_doc --diff: %r is not a valid base ref.", base)
        return []
    paths = ["agents", ".claude/agents", "skills", ".claude/skills", "claude-hooks"]
    try:
        diff = subprocess.run(
            ["git", "-C", str(root), "diff", "--end-of-options", base, "--", *paths],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        logger.warning("agent_doc --diff: could not run git diff against %s", base)
        return []
    report: list[str] = []
    current = "?"
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            current = line[len("+++ b/") :]
        elif line.startswith(("+", "-")) and not line.startswith(("+++", "---")):
            edge = _classify_mention(line[1:])
            if edge:
                sign = "added" if line[0] == "+" else "removed"
                report.append(f"{sign} {current}: {edge}")
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


def _handle_normal_mode(doc: str, roster: dict[str, set[str]], path: str) -> int:
    """Check the agent doc for coverage and dangling references in normal mode.

    Args:
        doc: The agent-doc text.
        roster: The repo roster from :func:`_roster`.
        path: Configured doc file path.

    Returns:
        Process exit code: ``0`` in sync, ``1`` on a problem.
    """
    problems = _check_doc(doc, roster)
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
        return _handle_normal_mode(doc, roster, path)


if __name__ == "__main__":
    sys.exit(main())
