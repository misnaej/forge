"""forge-audit-agents — measure agents against the canonical template.

Works in **two contexts**:

- **Forge itself**: audits the foundation agents under ``agents/`` and
  reports against ``FOUNDATION.md``.
- **Consumer repos that adopt forge**: same CLI, same checks — scans
  ``.claude/agents/`` (where Claude Code loads consumer agents) unioned
  with repo-root ``agents/``, against the consumer's ``FOUNDATION.md``
  (synced by ``install-forge-claude-md``). ``--roots`` overrides the
  default union; consumer reporter agents are classified via
  ``[tool.forge.audit_agents]`` (additive to the shipped lists).

Audits every ``*.md`` under the scan roots (skipping ``_TEMPLATE.md``
and other underscore-prefixed files) against the structural and length
policy documented in ``agents/_TEMPLATE.md``. Writes
``code_health/audit_agents.log`` with one row per agent plus
per-finding details.

**Non-blocking by design.** `main()` always returns 0 regardless of
finding severity, so this audit runs in CI without gating commits. Once
the Layer 3 trim PRs converge the agent set on the length budget,
promotion to blocking is a one-line change.

Measurements per agent:
    - ``word_count`` — body words after frontmatter, with code blocks
      stripped.
    - ``tools_count`` + reporter-tool-violation check.
    - ``frontmatter_complete`` — required keys present.
    - ``description_shape`` — routing-trigger vs role-label heuristic.
    - ``missing_canonical_sections`` — required H2 sections present.
    - ``section_order`` — present required H2 sections appear in
      ``_TEMPLATE.md`` order.
    - ``inline_foundation_restatements`` — 8+ word substrings shared
      with FOUNDATION.md.
    - ``cross_agent_duplicates`` — 8+ word substrings shared across
      two or more agent files.
"""

from __future__ import annotations

import logging
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from forge.audit.common import (
    Finding,
    Scope,
    Severity,
    make_audit_parser,
    write_log,
)
from forge.config import AGENT_DEFINITION_DIRS, read_tool_forge_section
from forge.git_utils import configure_cli_logging, repo_root


configure_cli_logging()
logger = logging.getLogger(__name__)


# Length thresholds — see agents/_TEMPLATE.md "Length budget".
WORD_CAP_HIGH = 1500  # Above this → HIGH severity finding.
WORD_CAP_MEDIUM = 800  # Above this → MEDIUM finding (trim candidate).

# Minimum substring length (in whitespace-split tokens) that counts as
# "shared" between an agent body and FOUNDATION.md or another agent.
# Smaller → more false positives on common phrases. Larger → misses
# partial restatements. 8 is one clause / short sentence.
SHARED_TOKEN_MIN = 8

REQUIRED_FRONTMATTER_KEYS = ("name", "description", "tools", "model")
REQUIRED_SECTIONS = ("Workflow", "Scope Boundaries", "Output", "Success Criteria")
REPORTER_FORBIDDEN_TOOLS = ("Write", "Edit")

# Explicit allowlist of agent names subject to the reporter-agent contracts
# (no mutating tools; emit `verified-at:` header). Heuristic detection via
# description text fails on phrases like "does not fix" or "Verify and fix"
# (negations / hybrid verbs) — name-based classification is unambiguous and
# matches the agents `_TEMPLATE.md` calls out as reporters.
REPORTER_AGENT_NAMES = (
    "design-checker",
    "docs-types-checker",
    "knowledge-search",
    "security-checker",
    "weekly-summary",
)

# Reporter-shape agents that legitimately mutate as part of producing an
# artifact (see _TEMPLATE.md "Tool sets per role" → Reporter-with-artifact).
# These agents still emit the `verified-at:` header (their report IS the
# artifact) but are exempt from the "no Write/Edit" check. Keep the list
# tight: each name here documents an explicit design exception.
_REPORTER_WITH_ARTIFACT_NAMES = (
    "docs-types-checker",  # in-place docstring fixes (Edit)
    "weekly-summary",  # writes .plan/weekly_summary_*.md (Write)
)

# Pre-write reporters: no mutating tools, but their evidence header is
# `prior-art-searched:` (digest hash + query count) rather than a git
# SHA — they fire before any diff exists. See _TEMPLATE.md "Pre-write
# agent header contract".
PRIOR_ART_AGENT_NAMES = ("prior-art",)

# Description shape heuristics. Role-label descriptions tend to start
# with "Agent for" or "<Name> agent that"; everything else is treated
# as trigger-shaped.
_ROLE_LABEL_PATTERNS = (
    re.compile(r"^\s*agent\s+(for|that)\b", re.IGNORECASE),
    re.compile(r"^\s*[A-Z][a-z-]+\s+agent\s+(for|that)\b"),
)


# ---------------------------------------------------------------------------
# AgentDoc — parsed view of a single agents/<name>.md file
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentDoc:
    """Parsed view of one ``agents/*.md`` file.

    Attributes:
        path: Repo-relative path to the agent file.
        frontmatter: Parsed key→value map from the YAML-ish frontmatter.
            Lists (``tools:``) are kept as ``tuple[str, ...]``.
        body: Markdown body after the frontmatter, with leading/trailing
            whitespace trimmed.
        body_no_code: Body with fenced code blocks stripped — used for
            word-count and substring-match measurements so embedded code
            doesn't inflate the score.
    """

    path: str
    frontmatter: dict[str, str | tuple[str, ...]]
    body: str
    body_no_code: str


def _split_frontmatter(text: str) -> tuple[dict[str, str | tuple[str, ...]], str]:
    """Split YAML-ish frontmatter from the rest of an agent file.

    Args:
        text: Full file contents.

    Returns:
        Tuple ``(frontmatter_dict, body)``. The dict maps simple
        ``key: value`` lines to strings, and ``key:`` blocks with
        ``- item`` lines to a tuple of items. Empty dict + full text if
        there is no frontmatter block.
    """
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}, text
    block = text[4:end]
    body = text[end + 5 :]
    fm: dict[str, str | tuple[str, ...]] = {}
    lines = block.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue
        if ":" in line and not line.startswith(("  -", "\t-")):
            key, _, value = line.partition(":")
            key = key.strip()
            value = value.strip()
            if value:
                fm[key] = value
                i += 1
                continue
            # Multi-line list collection.
            items: list[str] = []
            i += 1
            while i < len(lines) and (
                lines[i].startswith("  -") or lines[i].startswith("\t-")
            ):
                item = lines[i].split("-", 1)[1].strip()
                items.append(item)
                i += 1
            fm[key] = tuple(items)
            continue
        i += 1
    return fm, body


def _strip_code_blocks(body: str) -> str:
    """Remove fenced code blocks (``` ... ```) from *body*.

    Embedded code snippets shouldn't inflate the agent's word count or
    accidentally match FOUNDATION content.

    Args:
        body: Markdown text.

    Returns:
        Same text with everything between matched ``` fences removed.
    """
    return re.sub(r"```.*?```", "", body, flags=re.DOTALL)


def _parse_agent(path: Path, repo_root_path: Path) -> AgentDoc:
    """Read and parse one agent file.

    Args:
        path: Absolute path to the agent markdown file.
        repo_root_path: Repo root, for computing the relative path.

    Returns:
        ``AgentDoc`` capturing frontmatter, body, and code-stripped body.
    """
    text = path.read_text(encoding="utf-8")
    fm, body = _split_frontmatter(text)
    rel = str(path.relative_to(repo_root_path))
    return AgentDoc(
        path=rel,
        frontmatter=fm,
        body=body.strip(),
        body_no_code=_strip_code_blocks(body).strip(),
    )


# ---------------------------------------------------------------------------
# Per-agent checks
# ---------------------------------------------------------------------------


def _word_count(body_no_code: str) -> int:
    """Return the whitespace-split token count of *body_no_code*.

    Args:
        body_no_code: Agent body with code blocks already stripped.

    Returns:
        Integer token count.
    """
    return len(body_no_code.split())


def _check_word_count(agent: AgentDoc) -> list[Finding]:
    """Flag agent bodies above the length budget.

    Args:
        agent: Parsed agent doc.

    Returns:
        Zero or one finding (HIGH above ``WORD_CAP_HIGH``, MEDIUM above
        ``WORD_CAP_MEDIUM``, else no finding).
    """
    count = _word_count(agent.body_no_code)
    if count > WORD_CAP_HIGH:
        return [
            Finding(
                audit="agents",
                severity=Severity.HIGH,
                path=agent.path,
                line=0,
                message=(
                    f"word_count={count} exceeds hard cap {WORD_CAP_HIGH} "
                    "— trim required (see agents/_TEMPLATE.md)"
                ),
            )
        ]
    if count > WORD_CAP_MEDIUM:
        return [
            Finding(
                audit="agents",
                severity=Severity.MEDIUM,
                path=agent.path,
                line=0,
                message=(
                    f"word_count={count} above target {WORD_CAP_MEDIUM} "
                    "— trim candidate"
                ),
            )
        ]
    return []


def _check_frontmatter(agent: AgentDoc) -> list[Finding]:
    """Flag missing required frontmatter keys.

    Args:
        agent: Parsed agent doc.

    Returns:
        One HIGH finding per missing required key.
    """
    return [
        Finding(
            audit="agents",
            severity=Severity.HIGH,
            path=agent.path,
            line=0,
            message=f"frontmatter missing required key '{key}'",
        )
        for key in REQUIRED_FRONTMATTER_KEYS
        if key not in agent.frontmatter
    ]


def _check_description_shape(agent: AgentDoc) -> list[Finding]:
    """Flag descriptions that read as role labels rather than routing triggers.

    Args:
        agent: Parsed agent doc.

    Returns:
        Zero or one MEDIUM finding.
    """
    desc = agent.frontmatter.get("description", "")
    if not isinstance(desc, str) or not desc:
        return []
    if any(p.search(desc) for p in _ROLE_LABEL_PATTERNS):
        return [
            Finding(
                audit="agents",
                severity=Severity.MEDIUM,
                path=agent.path,
                line=0,
                message=(
                    "description is role-shaped; reshape to a routing trigger "
                    "(e.g. 'Use proactively when …') — see _TEMPLATE.md"
                ),
            )
        ]
    return []


def _sanitize_fragment(text: str, limit: int = 40) -> str:
    """Return consumer-authored *text* made safe for a code_health log line.

    ``code_health/*.log`` files are trusted ground truth for other forge
    agents (FOUNDATION §13), and dual-root discovery now scans
    consumer-authored ``.claude/agents/*.md`` — so frontmatter-derived
    strings must not pass free-form prose into the log. ``repr`` quotes
    and escapes control characters; the cap bounds crafted long values.

    Args:
        text: Untrusted string from an agent file's frontmatter.
        limit: Maximum characters kept before an ellipsis.

    Returns:
        Repr-quoted, length-capped rendering of *text*.
    """
    clipped = text if len(text) <= limit else text[:limit] + "…"
    return repr(clipped)


def _is_reporter_agent(agent: AgentDoc, reporters: frozenset[str]) -> bool:
    """Return True when *agent* is in the effective reporter set.

    Name-based classification avoids false negatives on descriptions
    containing "Verify and fix" (docs-types-checker) or "does not fix"
    (security-checker) — both reporters that a substring heuristic
    would silently misclassify.

    Args:
        agent: Parsed agent doc.
        reporters: Effective reporter names — the shipped
            :data:`REPORTER_AGENT_NAMES` plus any consumer additions
            (see :func:`_effective_reporter_sets`).

    Returns:
        ``True`` when the agent's filename stem is in the set.
    """
    return Path(agent.path).stem in reporters


def _check_reporter_tools(
    agent: AgentDoc,
    reporters: frozenset[str],
    artifact_reporters: frozenset[str],
) -> list[Finding]:
    """Flag reporter agents holding mutating tools (`Write`/`Edit`).

    Reporter-with-artifact agents (see :data:`_REPORTER_WITH_ARTIFACT_NAMES`)
    are exempt from this check — their single mutating tool is the
    artifact-producing seam documented in ``_TEMPLATE.md`` "Tool sets
    per role". The `verified-at:` header requirement still applies to
    them via :func:`_check_reporter_verified_at`.

    Args:
        agent: Parsed agent doc.
        reporters: Effective reporter names.
        artifact_reporters: Effective reporter-with-artifact names.

    Returns:
        One MEDIUM finding per forbidden tool present on a non-exempt
        reporter.
    """
    if not _is_reporter_agent(agent, reporters):
        return []
    if Path(agent.path).stem in artifact_reporters:
        return []
    tools = agent.frontmatter.get("tools", ())
    if not isinstance(tools, tuple):
        return []
    return [
        Finding(
            audit="agents",
            severity=Severity.MEDIUM,
            path=agent.path,
            line=0,
            message=(
                f"reporter holds mutating tool {_sanitize_fragment(tool)} — "
                "reporters should be Read/Grep/Glob-only (see _TEMPLATE.md "
                "'Tool sets per role')"
            ),
        )
        for tool in tools
        if tool in REPORTER_FORBIDDEN_TOOLS
    ]


def _check_reporter_verified_at(
    agent: AgentDoc, reporters: frozenset[str]
) -> list[Finding]:
    """Flag reporter agents missing the ``verified-at:`` header instruction.

    Reporters must instruct the runtime to emit a ``verified-at:`` line
    at the top of their report so ``pr-manager`` can short-circuit a
    full re-verification on small follow-up commits. See
    ``_TEMPLATE.md`` "Reporter-agent header contract".

    Args:
        agent: Parsed agent doc.
        reporters: Effective reporter names.

    Returns:
        One MEDIUM finding when the agent is a reporter but its body
        does not mention ``verified-at:``.
    """
    return _check_header_contract(
        agent,
        reporters,
        header="verified-at:",
        rationale=(
            "blocks pr-manager delta-mode (see _TEMPLATE.md "
            "'Reporter-agent header contract')"
        ),
    )


def _check_header_contract(
    agent: AgentDoc,
    names: frozenset[str],
    *,
    header: str,
    rationale: str,
) -> list[Finding]:
    """Flag an agent in *names* whose body never mentions *header*.

    The shared substring-grep behind both header contracts
    (``verified-at:`` for reporters, ``prior-art-searched:`` for
    pre-write agents) — one mechanism, two vocabularies.

    Args:
        agent: Parsed agent doc.
        names: Agent names bound to this contract.
        header: Required header substring.
        rationale: Consequence clause appended to the finding message.

    Returns:
        One MEDIUM finding when bound and missing; else empty.
    """
    if not _is_reporter_agent(agent, names):
        return []
    if header in agent.body:
        return []
    return [
        Finding(
            audit="agents",
            severity=Severity.MEDIUM,
            path=agent.path,
            line=0,
            message=(
                f"agent does not instruct emitting `{header}` header — {rationale}"
            ),
        )
    ]


def _check_required_sections(agent: AgentDoc) -> list[Finding]:
    """Flag missing canonical H2 sections.

    Matches against ``body_no_code`` (fence-stripped), not the raw body:
    a heading swallowed by an unbalanced code fence is not a live
    heading, and treating it as present would hide exactly the
    corruption a botched section move produces.

    Args:
        agent: Parsed agent doc.

    Returns:
        One LOW finding per missing required section.
    """
    return [
        Finding(
            audit="agents",
            severity=Severity.LOW,
            path=agent.path,
            line=0,
            message=f"missing canonical section '## {section}' (see _TEMPLATE.md)",
        )
        for section in REQUIRED_SECTIONS
        if f"\n## {section}" not in agent.body_no_code
        and not agent.body_no_code.startswith(f"## {section}")
    ]


def _check_section_order(agent: AgentDoc) -> list[Finding]:
    """Flag required H2 sections appearing out of template order.

    ``REQUIRED_SECTIONS`` is ordered exactly as ``_TEMPLATE.md`` mandates
    ("Required body sections"). Only sections actually present are
    compared — a missing section is ``_check_required_sections``'s
    finding, not a second one here. Positions come from ``body_no_code``
    (fence-stripped) so a heading inside a code fence never counts.

    Args:
        agent: Parsed agent doc.

    Returns:
        At most one LOW finding naming the out-of-order sequence found.
    """
    text = agent.body_no_code
    positions: list[tuple[int, str]] = []
    for section in REQUIRED_SECTIONS:
        marker = f"## {section}"
        pos = 0 if text.startswith(marker) else text.find(f"\n{marker}")
        if pos != -1:
            positions.append((pos, section))
    expected = [s for _, s in positions]
    found = [s for _, s in sorted(positions)]
    if found == expected:
        return []
    return [
        Finding(
            audit="agents",
            severity=Severity.LOW,
            path=agent.path,
            line=0,
            message=(
                "canonical sections out of template order: found "
                + " → ".join(f"'## {s}'" for s in found)
                + " (see _TEMPLATE.md)"
            ),
        ),
    ]


# ---------------------------------------------------------------------------
# Shared-substring detection
# ---------------------------------------------------------------------------


def _tokens(text: str) -> list[str]:
    """Return whitespace-split lowercase tokens of *text*.

    Args:
        text: Source text.

    Returns:
        List of lowercased tokens.
    """
    return text.lower().split()


def _ngrams(tokens: list[str], n: int) -> set[str]:
    """Return the set of *n*-token windows from *tokens*.

    Args:
        tokens: Token sequence.
        n: Window size.

    Returns:
        Set of space-joined n-grams.
    """
    if len(tokens) < n:
        return set()
    return {" ".join(tokens[i : i + n]) for i in range(len(tokens) - n + 1)}


def _check_foundation_restatements(
    agent: AgentDoc, foundation_ngrams: set[str]
) -> list[Finding]:
    """Flag substrings of ``SHARED_TOKEN_MIN`` tokens shared with FOUNDATION.

    Args:
        agent: Parsed agent doc.
        foundation_ngrams: Pre-computed n-gram set from FOUNDATION.md.

    Returns:
        Zero or one HIGH finding summarising the overlap, with up to
        five matched n-grams as evidence.
    """
    agent_ngrams = _ngrams(_tokens(agent.body_no_code), SHARED_TOKEN_MIN)
    shared = agent_ngrams & foundation_ngrams
    if not shared:
        return []
    sample = sorted(shared)[:5]
    return [
        Finding(
            audit="agents",
            severity=Severity.HIGH,
            path=agent.path,
            line=0,
            message=(
                f"{len(shared)} restatement(s) of FOUNDATION content "
                f"(≥{SHARED_TOKEN_MIN}-token substrings); link to FOUNDATION "
                "instead — see _TEMPLATE.md 'Forbidden patterns'"
            ),
            evidence=tuple(f"shared: {s}" for s in sample),
        )
    ]


def _cross_agent_duplicate_findings(agents: list[AgentDoc]) -> list[Finding]:
    """Flag n-grams that appear in two or more agent files.

    Args:
        agents: All parsed agents.

    Returns:
        One MEDIUM finding per agent that shares ≥1 n-gram with another
        agent. The finding summarises how many shared n-grams and lists
        up to five examples.
    """
    by_ngram: dict[str, list[str]] = defaultdict(list)
    for a in agents:
        for ngram in _ngrams(_tokens(a.body_no_code), SHARED_TOKEN_MIN):
            by_ngram[ngram].append(a.path)
    # Build per-agent shared-ngram lists, keeping only ngrams in ≥ 2 agents.
    min_agents_for_share = 2  # ngram counts as "shared" only across ≥ 2 agents
    per_agent: dict[str, list[str]] = defaultdict(list)
    for ngram, paths in by_ngram.items():
        if len(set(paths)) < min_agents_for_share:
            continue
        for p in set(paths):
            per_agent[p].append(ngram)
    findings: list[Finding] = []
    for agent_path, shared in sorted(per_agent.items()):
        sample = sorted(shared)[:5]
        findings.append(
            Finding(
                audit="agents",
                severity=Severity.MEDIUM,
                path=agent_path,
                line=0,
                message=(
                    f"{len(shared)} substring(s) shared with another agent "
                    "— extract shared content to FOUNDATION or a referenced "
                    "section, see _TEMPLATE.md"
                ),
                evidence=tuple(f"shared: {s}" for s in sample),
            )
        )
    return findings


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


@dataclass
class AgentsConfig:
    """Configuration for ``forge-audit-agents``.

    Attributes:
        output: Override log path (else ``code_health/audit_agents.log``).
    """

    output: Path | None = None


def _iter_agent_files(
    repo_root_path: Path, roots: list[Path] | None = None
) -> list[Path]:
    """Return every public agent markdown file under the scan roots.

    Filters out ``_TEMPLATE.md`` and other underscore-prefixed files —
    those are template / documentation, not actual agents.

    Args:
        repo_root_path: Repo root.
        roots: Explicit scan roots (from ``--roots``); ``None`` or empty
            scans the :data:`forge.config.AGENT_DEFINITION_DIRS` union.

    Returns:
        Sorted, de-duplicated list of absolute paths to agent files.
    """
    dirs = roots or [repo_root_path / d for d in AGENT_DEFINITION_DIRS]
    files = {
        p
        for d in dirs
        if d.is_dir()
        for p in d.glob("*.md")
        if not p.name.startswith("_")
    }
    return sorted(files)


def _effective_reporter_sets(
    repo_root_path: Path,
) -> tuple[frozenset[str], frozenset[str]]:
    """Return the effective reporter / reporter-with-artifact name sets.

    Consumer additions from ``[tool.forge.audit_agents]``
    (``reporter_agents`` / ``reporter_with_artifact_agents``) are
    **unioned onto** the shipped tuples — additive only, so a consumer
    cannot accidentally un-classify forge's own agents. Non-list values
    and non-string items degrade silently to no addition.

    Asymmetry guard: adding a name to ``reporter_agents`` only ADDS
    scrutiny, so any name is accepted; adding one to
    ``reporter_with_artifact_agents`` REMOVES the Write/Edit check, so
    shipped reporter names are ignored there — a consumer can grant the
    artifact exemption only to its own custom reporters, never
    re-classify a shipped canonical one.

    Args:
        repo_root_path: Repo root (for the ``pyproject.toml`` read).

    Returns:
        Tuple ``(reporters, artifact_reporters)``.
    """
    cfg = read_tool_forge_section(repo_root_path, "audit_agents")

    def _names(key: str) -> frozenset[str]:
        value = cfg.get(key)
        if not isinstance(value, list):
            return frozenset()
        return frozenset(v for v in value if isinstance(v, str))

    return (
        frozenset(REPORTER_AGENT_NAMES) | _names("reporter_agents"),
        frozenset(_REPORTER_WITH_ARTIFACT_NAMES)
        | (_names("reporter_with_artifact_agents") - frozenset(REPORTER_AGENT_NAMES)),
    )


def _per_agent_findings(
    agent: AgentDoc,
    foundation_ngrams: set[str],
    reporters: frozenset[str],
    artifact_reporters: frozenset[str],
) -> list[Finding]:
    """Run every per-agent check and return the combined finding list.

    Args:
        agent: Parsed agent.
        foundation_ngrams: Pre-computed FOUNDATION n-grams.
        reporters: Effective reporter names.
        artifact_reporters: Effective reporter-with-artifact names.

    Returns:
        Flat list of findings, in check order.
    """
    findings: list[Finding] = []
    findings.extend(_check_word_count(agent))
    findings.extend(_check_frontmatter(agent))
    findings.extend(_check_description_shape(agent))
    prior_art = frozenset(PRIOR_ART_AGENT_NAMES)
    findings.extend(
        _check_reporter_tools(agent, reporters | prior_art, artifact_reporters)
    )
    findings.extend(_check_reporter_verified_at(agent, reporters))
    findings.extend(
        _check_header_contract(
            agent,
            prior_art,
            header="prior-art-searched:",
            rationale=(
                "the /pr consumption gate and the refusal contract depend on "
                "it (see _TEMPLATE.md 'Pre-write agent header contract')"
            ),
        )
    )
    findings.extend(_check_required_sections(agent))
    findings.extend(_check_section_order(agent))
    findings.extend(_check_foundation_restatements(agent, foundation_ngrams))
    return findings


def _render_summary(agents: list[AgentDoc], findings: list[Finding]) -> str:
    """Render the per-agent summary table for the log header.

    Args:
        agents: All parsed agents (in display order).
        findings: All findings (used to count per-agent severity).

    Returns:
        Multi-line summary string.
    """
    by_path: dict[str, list[Finding]] = defaultdict(list)
    for f in findings:
        by_path[f.path].append(f)

    header = (
        "| agent                           | words | tools | severity counts          |"
        "\n|---------------------------------|-------|-------|------------------------|"
    )
    rows = []
    for a in agents:
        name = Path(a.path).stem
        words = _word_count(a.body_no_code)
        tools = a.frontmatter.get("tools", ())
        tools_count = len(tools) if isinstance(tools, tuple) else 0
        counts: dict[str, int] = defaultdict(int)
        for f in by_path.get(a.path, []):
            counts[f.severity.value] += 1
        sev_str = (
            ", ".join(
                f"{sev}={counts[sev]}"
                for sev in ("high", "medium", "low")
                if counts.get(sev)
            )
            or "clean"
        )
        rows.append(f"| {name:<31} | {words:>5} | {tools_count:>5} | {sev_str:<24} |")
    return header + "\n" + "\n".join(rows)


def run(scope: Scope, roots: list[Path], config: AgentsConfig) -> int:
    """Walk every agent file and emit findings to ``code_health/audit_agents.log``.

    ``scope`` is accepted for parity with other ``forge-audit-*`` CLIs but
    is unused — agent files are always scanned in full (the set is small
    and changes rarely).

    Args:
        scope: Audit scope (ignored).
        roots: Explicit scan roots from ``--roots``; empty scans the
            default union of ``agents/`` and ``.claude/agents/``.
        config: Audit configuration (output path override).

    Returns:
        Always ``0``. Non-blocking by design until Layer 3 trim PRs
        converge.
    """
    del scope  # unused — agent set is small enough to scan in full
    root = repo_root()
    foundation_path = root / "FOUNDATION.md"

    # Missing FOUNDATION.md is auditable, not silent. Record a CRITICAL
    # finding in the log so consumers running this in CI see the
    # misconfiguration; exit code stays 0 (non-blocking contract).
    if not foundation_path.is_file():
        logger.error("FOUNDATION.md not found at %s", foundation_path)
        findings = [
            Finding(
                audit="agents",
                severity=Severity.CRITICAL,
                path="FOUNDATION.md",
                line=0,
                message=(
                    f"FOUNDATION.md not found at {foundation_path} — "
                    "FOUNDATION-restatement check cannot run"
                ),
            )
        ]
        write_log("agents", findings, "FOUNDATION.md missing", output=config.output)
        return 0
    foundation_text = foundation_path.read_text(encoding="utf-8")
    foundation_ngrams = _ngrams(_tokens(foundation_text), SHARED_TOKEN_MIN)

    agent_paths = _iter_agent_files(root, roots)
    if not agent_paths:
        scanned = (
            ", ".join(str(r) for r in roots)
            if roots
            else " and ".join(f"{root}/{d}" for d in AGENT_DEFINITION_DIRS)
        )
        logger.info("No agent files under %s", scanned)
        write_log("agents", [], "No agent files found.", output=config.output)
        return 0
    agents = [_parse_agent(p, root) for p in agent_paths]

    reporters, artifact_reporters = _effective_reporter_sets(root)
    findings = []
    for agent in agents:
        findings.extend(
            _per_agent_findings(agent, foundation_ngrams, reporters, artifact_reporters)
        )
    findings.extend(_cross_agent_duplicate_findings(agents))

    summary = _render_summary(agents, findings)
    write_log("agents", findings, summary, output=config.output)
    return 0


def main() -> int:
    """CLI entry point for ``forge-audit-agents``.

    Returns:
        Always ``0`` — non-blocking until promoted.
    """
    parser = make_audit_parser(
        prog="forge-audit-agents",
        description=(
            "Measure forge agents against the canonical template in "
            "agents/_TEMPLATE.md. Non-blocking initially; promoted after "
            "Layer 3 trim PRs."
        ),
    )
    args = parser.parse_args()
    scope = Scope(args.scope)
    config = AgentsConfig(output=args.output)
    root = repo_root()
    roots = [(root / r).resolve() for r in (args.roots or []) if (root / r).is_dir()]
    return run(scope, roots, config)


if __name__ == "__main__":
    sys.exit(main())
