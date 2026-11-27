"""forge-config — show what forge config this repo sets, and what it should.

Answers "which ``[tool.*]`` sections does forge actually read, and what
do I still need to set?" in one command, so a consumer never has to hunt
through docs or source.

Two responsibilities:

1. **List** every ``[tool.forge.*]`` key forge reads, its current value
   (or ``<default>`` when unset), and a one-line description.
2. **Advise** — for any recommended-but-unset key, print what to add.

Transparency over wrapping: forge reads some third-party tools from
their **own native sections** (notably ``[tool.interrogate]`` for
docstring coverage). Rather than hide that behind a forge namespace,
this CLI names it explicitly — forge reads ``[tool.interrogate]``; it is
the tool's own config, not a forge wrapper. Forge-specific keys the tool
has no concept of (``badge``, ``paths``) live under
``[tool.forge.docstring_coverage]``.

Read-only: prints a report and exits ``0``. Surfaced as a post-install
nudge by ``install-forge-bootstrap`` and on demand via ``forge-config``.
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from pathlib import Path

from forge.config import read_pyproject_raw, resolve_model_section
from forge.git_utils import configure_cli_logging


configure_cli_logging()
logger = logging.getLogger(__name__)


# Generated regions in the config reference, in page order. A key naming one
# renders into that region's richer table instead of being documented by hand
# beside the generated index — so its default has one home, not two.
DOC_GROUP_C4_PDF = "c4-pdf"
DOC_GROUP_C4_RENDER = "c4-render"
DOC_GROUP_C4_RENDER_ADVANCED = "c4-render-advanced"
DOC_GROUPS: tuple[str, ...] = (
    DOC_GROUP_C4_PDF,
    DOC_GROUP_C4_RENDER,
    DOC_GROUP_C4_RENDER_ADVANCED,
)


@dataclass(frozen=True)
class KeyDoc:
    """Reference-table copy for a key documented in a generated region.

    Carried separately from :attr:`ConfigKey.description`, which is the
    one-line text ``forge-config`` prints to a terminal: *detail* is
    markdown with emphasis and links, and would render raw there.

    Attributes:
        group: Which generated region renders the key's row — one of
            :data:`DOC_GROUPS`.
        detail: Markdown prose for the region's "What it does" column.
        mermaid: The underlying Mermaid setting and its scope, for regions
            that document a pass-through. Empty when the key is not one.
    """

    group: str
    detail: str
    mermaid: str = ""


@dataclass(frozen=True)
class ConfigKey:
    """One ``[tool.forge.*]`` key forge reads in a consumer repo.

    Attributes:
        path: Full key path under the TOML root, e.g.
            ``("tool", "forge", "base_branch")``.
        default: Value forge falls back to when the key is unset.
        description: One-line purpose, shown beside the value.
        recommended: When ``True``, the advisor nudges the consumer to
            set the key if it is currently absent.
        doc: Reference-table copy when the key is documented in a generated
            region; ``None`` for a key the index table covers alone.
    """

    path: tuple[str, ...]
    default: object
    description: str
    recommended: bool = False
    doc: KeyDoc | None = None


# The forge config surface, declared once. ``forge-config`` is the single
# place that enumerates what forge reads — there is no metadata registry
# elsewhere to keep in sync, so new ``[tool.forge.*]`` keys are added here.
CONFIG_KEYS: tuple[ConfigKey, ...] = (
    ConfigKey(
        ("tool", "forge", "base_branch"),
        "main",
        "Protected base branch every PR targets and every release tags.",
    ),
    ConfigKey(
        ("tool", "forge", "source_dirs"),
        "smart-detect (src/ or top-level packages)",
        "Repo source roots — the single definition every layout-aware tool "
        "(ruff, api-digest, docstring-coverage, doctest, typecheck) scans. "
        "Unset → smart auto-detect: src/ if present, else top-level packages.",
    ),
    ConfigKey(
        ("tool", "forge", "test_dirs"),
        "smart-detect (tests/ or test/)",
        "Repo test roots (added for tools that scan tests too, e.g. ruff, "
        "coverage). Unset → smart auto-detect of tests/ then test/.",
    ),
    ConfigKey(
        ("tool", "forge", "exclude"),
        default=[],
        description="Repo-wide glob patterns (fnmatch on repo-relative paths; "
        "a bare dir name excludes its subtree) skipped by whole-tree steps "
        "(docstring_verification, test_naming_check) — vendored / generated "
        "Python you don't author. --scope all also scopes to source_dirs + "
        "test_dirs, so paths outside them need no listing here.",
    ),
    ConfigKey(
        ("tool", "forge", "cli_wiring", "enabled"),
        default=False,
        description="Opt into the cli_wiring pre-commit step (every [project.scripts] "
        "reachable from a wiring source).",
    ),
    ConfigKey(
        ("tool", "forge", "agent_doc", "path"),
        "unset (agent_doc step self-skips)",
        "Repo-relative path of the hand-maintained agent-architecture doc "
        "checked by the agent_doc pre-commit step (verify-forge-agent-doc); "
        "setting it opts the step in.",
    ),
    ConfigKey(
        ("tool", "forge", "agent_doc", "guarded_by"),
        "unset (guard-map edge check self-skips)",
        "Table mapping an agent's doc node id to the guard hooks protecting "
        'it (e.g. precommit_fixer = ["block_fixer_recon"]); each entry '
        "must appear as an agent->hook edge in the agent doc.",
    ),
    ConfigKey(
        ("tool", "forge", "agent_doc", "extra_clis"),
        "unset (no extra known CLIs)",
        "Extra CLI names the agent doc may reference beyond [project.scripts] "
        "and the installed forge-scripts console scripts (resolution only, "
        "never required coverage).",
    ),
    ConfigKey(
        ("tool", "forge", "agent_doc", "extra_hooks"),
        "unset (no extra known hooks)",
        "Extra hook stems the agent doc may reference beyond claude-hooks/, "
        ".claude/hooks/, and the shipped plugin roster (resolution only).",
    ),
    ConfigKey(
        ("tool", "forge", "agent_doc", "extra_skills"),
        "unset (no extra known skills)",
        "Extra skill names the agent doc may reference beyond the repo's "
        "skill dirs and the shipped plugin roster (resolution only).",
    ),
    ConfigKey(
        ("tool", "forge", "vibe", "enabled"),
        default=False,
        description="Opt into the Mistral Vibe integration: install-forge-vibe "
        "writes .vibe/ hooks+agents, .agents/skills/ and AGENTS.md. Off by "
        "default so consumer repos are never written unasked.",
    ),
    ConfigKey(
        ("tool", "forge", "docstring_coverage", "badge"),
        default=False,
        description="Generate interrogate's coverage badge to "
        ".badges/docstring-coverage.svg for README embedding.",
    ),
    ConfigKey(
        ("tool", "forge", "docstring_coverage", "paths"),
        "source_dirs + test_dirs",
        "Per-tool override of the coverage scan roots; otherwise inherits "
        "the repo-wide [tool.forge].source_dirs + test_dirs.",
    ),
    ConfigKey(
        ("tool", "forge", "docstring_verification", "paths"),
        "source_dirs + test_dirs",
        "Per-tool override of verify-forge-docstrings' --scope all roots; "
        "otherwise inherits the repo-wide [tool.forge].source_dirs + test_dirs.",
    ),
    ConfigKey(
        ("tool", "forge", "test_naming_check", "paths"),
        "test_dirs (smart-detect)",
        "Per-tool override of the test-naming scan roots; otherwise inherits "
        "the repo-wide [tool.forge].test_dirs (tests only, no source).",
    ),
    ConfigKey(
        ("tool", "forge", "slow_tests_report", "paths"),
        "source_dirs (smart-detect)",
        "Per-tool override of the roots forge-slow-tests-report counts "
        "statements under; otherwise inherits the repo-wide "
        "[tool.forge].source_dirs (source only, no tests).",
    ),
    ConfigKey(
        ("tool", "forge", "ruff", "paths"),
        "source_dirs + test_dirs",
        "Per-tool override of ruff's scan roots; otherwise inherits the "
        "repo-wide [tool.forge].source_dirs + test_dirs.",
    ),
    ConfigKey(
        ("tool", "forge", "api_digest", "paths"),
        "source_dirs",
        "Per-tool override of api-digest's scan roots; otherwise inherits "
        "the repo-wide [tool.forge].source_dirs (source only, no tests).",
    ),
    ConfigKey(
        ("tool", "forge", "precommit", "disable"),
        default=[],
        description="Force-skip these pre-commit steps by name (over each "
        "step's own self-skip).",
    ),
    ConfigKey(
        ("tool", "forge", "precommit", "enable"),
        default=[],
        description="Opt into normally-off pre-commit steps by name "
        "(doctest, typecheck, doc_consistency).",
    ),
    ConfigKey(
        ("tool", "forge", "precommit", "scope"),
        default="all",
        description="Default file scope for scope-aware steps (ruff, "
        "docstring_verification, test_naming_check): 'all' (whole tracked "
        "tree) or 'diff' (modified files vs main).",
    ),
    ConfigKey(
        ("tool", "forge", "precommit", "scope_overrides"),
        default={},
        description="Per-step scope overrides, e.g. {ruff = 'diff'}. Each "
        "value is 'all' or 'diff'; wins over the global 'scope' key.",
    ),
    ConfigKey(
        ("tool", "forge", "precommit", "echo_ci_output"),
        default=True,
        description="In CI, print each blocking step's own output after its "
        "log pointer; false keeps only the pointer.",
    ),
    ConfigKey(
        ("tool", "forge", "doctest", "paths"),
        "source_dirs (smart-detect)",
        "Per-tool override of doctest's scan roots; otherwise inherits the "
        "repo-wide [tool.forge].source_dirs (source only, no tests).",
    ),
    ConfigKey(
        ("tool", "forge", "doctest", "blocking"),
        default=False,
        description="Make the doctest step fail the commit on a broken "
        "example (default: non-blocking WARN).",
    ),
    ConfigKey(
        ("tool", "forge", "typecheck", "paths"),
        "source_dirs (smart-detect)",
        "Per-tool override of typecheck's scan roots; otherwise inherits the "
        "repo-wide [tool.forge].source_dirs (source only, no tests).",
    ),
    ConfigKey(
        ("tool", "forge", "typecheck", "blocking"),
        default=False,
        description="Make the typecheck step fail the commit on a checker "
        "error (default: non-blocking WARN).",
    ),
    ConfigKey(
        ("tool", "forge", "typecheck", "file_set"),
        default="roots",
        description="Which files the typecheck step checks in `all` scope: "
        "'roots' (default) hands pyrefly the root directories and lets it "
        "walk them itself; 'tracked' hands it the git-tracked files under "
        "the roots, so gitignored paths are never selected and tracked "
        "files in dot-directories are included.",
    ),
    ConfigKey(
        ("tool", "forge", "typecheck", "include_tests"),
        default=False,
        description="Scan test_dirs alongside source_dirs as typecheck roots "
        "(default: source only). Ignored when [tool.forge.typecheck].paths "
        "is set — paths is a full replacement.",
    ),
    ConfigKey(
        ("tool", "forge", "pip_audit", "blocking"),
        default=False,
        description="Make the pip_audit step fail the commit on a CVE "
        "finding (default: non-blocking WARN; a missing pip-audit binary "
        "stays a WARN regardless).",
    ),
    ConfigKey(
        ("tool", "forge", "pip_audit", "cadence"),
        default="branch",
        description="How often the CVE scan actually runs: 'branch' (once "
        "per branch, the default), 'always' (every commit, the old "
        "behaviour), or 'hours' with max_age_hours. PR finalization "
        "forces a scan regardless, so nothing publishes unscanned.",
    ),
    ConfigKey(
        ("tool", "forge", "pip_audit", "max_age_hours"),
        default=24.0,
        description="Maximum age of a previous CVE scan before it is "
        "re-run, when cadence is 'hours'.",
    ),
    ConfigKey(
        ("tool", "forge", "cve_usage", "paths"),
        "source_dirs + test_dirs",
        "Per-tool override of the CVE-usage scan roots; otherwise inherits "
        "the repo-wide [tool.forge].source_dirs + test_dirs.",
    ),
    ConfigKey(
        ("tool", "forge", "release", "auto"),
        default="",
        description='Fragments-mode release cadence: "merge" lets the '
        "tag-release workflow auto-tag every fragment-carrying merge "
        "(forge-changelog auto-tag: last tag + strongest new fragment "
        "level). Any other value downgrades the job to a loud "
        "pending-fragments warning — never silent.",
    ),
    ConfigKey(
        ("tool", "forge", "changelog", "blocking"),
        default=True,
        description="Make the opt-in changelog_version / changelog_updated "
        "steps fail the commit on a finding (default: blocking; set false "
        "for staged adoption as a WARN).",
    ),
    ConfigKey(
        ("tool", "forge", "changelog", "exempt_paths"),
        default=[],
        description="Path prefixes the changelog_updated step ignores — "
        "scratch/experimental dirs whose changes need no CHANGELOG.md entry.",
    ),
    ConfigKey(
        ("tool", "forge", "changelog", "require_paths"),
        default=[],
        description="Path prefixes that always require a CHANGELOG.md entry, "
        "overriding exempt_paths (a shipped subtree inside an otherwise "
        "exempt dir). Checked before exempt_paths.",
    ),
    ConfigKey(
        ("tool", "forge", "audit_agents", "reporter_agents"),
        default=[],
        description="Consumer agent names classified as reporters by "
        "forge-audit-agents (additive to the shipped list — never "
        "replaces it), so the no-Write/Edit and verified-at checks "
        "apply to consumer reporter agents too.",
    ),
    ConfigKey(
        ("tool", "forge", "audit_agents", "reporter_with_artifact_agents"),
        default=[],
        description="Consumer reporter agents exempt from the no-Write/Edit "
        "check because their report IS a written artifact (additive to "
        "the shipped list).",
    ),
    ConfigKey(
        ("tool", "forge", "audit_agents", "exclude"),
        default=[],
        description="Repo-relative fnmatch globs or directory prefixes "
        "forge-audit-agents skips and names in its log, with --roots too "
        "(frontmatter-less folder READMEs and underscore-prefixed files "
        "are always skipped).",
    ),
    ConfigKey(
        ("tool", "forge", "pr", "docs_only_globs"),
        default=[],
        description="Extra globs a PR diff may consist entirely of and still "
        "take the docs-only light finalization path (additive to the "
        "built-in extension-anchored *.md / *.rst / *.txt set; "
        "high-blast-radius paths always force the full round). Prefer "
        "extension-anchored globs; a catch-all like '*' widens the "
        "review-skip to every non-blast-radius path.",
    ),
    ConfigKey(
        ("tool", "forge", "plan_check", "hub_paths"),
        default=[],
        description="Extra fnmatch globs (matched against the full path "
        "and the basename) for shared files forge-plan-check drift / "
        "overlap list but never count as plan drift — additive to the "
        "built-in set (CLAUDE.md, pyproject.toml, lock files, README.md, "
        "CHANGELOG.md, REPO_STRUCTURE.md, FOUNDATION.md, ...). A glob "
        "that matches source files (`*`, `*.py`, `src/*`) is refused with "
        "a warning: it would hide every real drift.",
    ),
    ConfigKey(
        ("tool", "forge", "changelog", "precommit_enforce"),
        default=True,
        description="Gate changelog_updated at every local commit (default; "
        "set false for deferred mode: local commits self-skip, the entry is "
        "written at PR wrap-up, and CI stays red until it lands). Orthogonal "
        "to blocking — severity vs timing.",
    ),
    ConfigKey(
        ("tool", "forge", "changelog", "mode"),
        "headings",
        "Changelog model: 'headings' (default — per-PR bullets under the "
        "shared top `## vX.Y.Z` heading) or 'fragments' (per-PR "
        "changelog.d/<slug>.<type>.md files, level-only bump front-matter, "
        "assembled into CHANGELOG.md once at release by forge-changelog — "
        "zero merge conflicts by construction).",
    ),
    ConfigKey(
        ("tool", "forge", "layering", "paths"),
        "unset (source_dirs → auto-detect)",
        "Granular scan-root override for forge-audit-layering (source-only "
        "resolution; test dirs are never scanned unless named via --roots).",
    ),
    ConfigKey(
        ("tool", "forge", "layering", "layer"),
        "unset (audit + step skipped)",
        "Array of layer tables (name, package OR packages, composes_all_of, "
        "exempt) for forge-audit-layering: every direct child of a layer's "
        "package(s) must reach each composes_all_of layer in its import "
        "closure. Blocking only for added/moved modules.",
    ),
    ConfigKey(
        ("tool", "forge", "layering", "require_all_classified"),
        default=False,
        description="Coverage gate: every top-level source package must be "
        "classified (reached by some layer's package prefix) or listed in "
        "unclassified_allow — an unclassified package is a blocking HIGH "
        "finding. Guards package-dissolve refactors where a promoted "
        "package silently drops out of the layering gate.",
    ),
    ConfigKey(
        ("tool", "forge", "layering", "unclassified_allow"),
        default=[],
        description="Top-level packages deliberately exempt from "
        "require_all_classified — visible REVIEW findings, never silent; "
        "stale entries (matching no package) are flagged.",
    ),
    ConfigKey(
        ("tool", "forge", "smart_test", "paths"),
        "source_dirs + test_dirs",
        "Per-tool override of the import-graph scan roots; otherwise inherits "
        "the repo-wide [tool.forge].source_dirs + test_dirs.",
    ),
    ConfigKey(
        ("tool", "forge", "smart_test", "follow_mock_patches"),
        default=False,
        description="Add unittest.mock.patch string targets as test→module "
        "graph edges, so patch-only couplings select their target's tests.",
    ),
    ConfigKey(
        ("tool", "forge", "smart_test", "coverage_validate"),
        default=False,
        description="Union tests whose recorded per-test coverage contexts "
        "touch a changed line — catches runtime-only (fixture / dynamic) "
        "links the static import graph misses. Needs coverage_json.",
    ),
    ConfigKey(
        ("tool", "forge", "smart_test", "coverage_json"),
        "unset",
        "Path to a `coverage json --show-contexts` export consumed when "
        "coverage_validate is on. Regenerate on full runs (stale → "
        "under-selects).",
    ),
    ConfigKey(
        ("tool", "forge", "smart_test", "commit_directive_re"),
        "built-in [depth-N] / [full] regex",
        "Override the commit-message directive pattern --from-commit-message "
        "reads to drive the tier from a CI commit.",
    ),
    ConfigKey(
        ("tool", "forge", "smart_test", "lifecycle_skip_days"),
        default=30,
        description="Quiet window (days) after which an untouched "
        "development-marked test file leaves ordinary full runs "
        "(lifecycle-skipped, always reported; --all-tests re-includes).",
    ),
    ConfigKey(
        ("tool", "forge", "smart_test", "nonpython_ignore"),
        "built-in list (*.md, .plan/*, .gitignore)",
        "fnmatch globs for changed non-Python paths that cannot affect "
        "tests; any other non-Python change escalates the run to full "
        "(the safe-fallback guarantee).",
    ),
    ConfigKey(
        ("tool", "forge", "smart_test", "safe_fallback"),
        "escalate",
        "What an unmappable non-Python change does: 'escalate' (run the "
        "full suite), 'warn' (log the paths, keep the selected depth) or "
        "'off' (ignore them). A conftest outside the test roots always "
        "escalates; unknown values escalate with a warning.",
    ),
    ConfigKey(
        ("tool", "forge", "continuation", "judgment_max_lines"),
        default=60,
        description="Line budget for CONTINUATION.md's written section (the "
        "part below the generated status panel); every write over it warns "
        "and `forge-continuation check` reports usage.",
    ),
    ConfigKey(
        ("tool", "forge", "continuation", "judgment_max_items"),
        default=12,
        description="Item budget for CONTINUATION.md's written section: "
        "top-level headings and list bullets, fenced code blocks excluded. "
        "Unlike the line budget, rewording cannot satisfy it — compliance "
        "means deleting an entry or moving it to a Detail pointer file. "
        "`forge-continuation check` reports usage; `--strict` gates on it.",
    ),
    ConfigKey(
        ("tool", "forge", "memory_audit", "threshold"),
        default=5,
        description="New agent memories since the last /forge-memory-audit at "
        "which `forge-memory-audit status` (run by /forge-next) says to offer the "
        "audit.",
    ),
    ConfigKey(
        ("tool", "forge", "intake", "dir"),
        "docs/intake",
        "Root of the intake: one file per sighting under lessons/ and "
        "findings/ (FOUNDATION §1, §12). A relative path inside the repo.",
    ),
    ConfigKey(
        ("tool", "forge", "intake", "redact"),
        [],
        "Private names forge-intake check refuses in findings "
        "(case-insensitive); built-in secret patterns apply regardless.",
    ),
    ConfigKey(
        ("tool", "forge", "telemetry", "sample_interval"),
        default=1.0,
        description="Seconds between resource samples taken by forge-telemetry "
        "(and forge-smart-test --telemetry); floored at 0.1.",
    ),
    ConfigKey(
        ("tool", "forge", "telemetry", "plot"),
        default=True,
        description="Render code_health/telemetry.png alongside the text log "
        "when matplotlib is installed (the [telemetry] extra).",
    ),
    ConfigKey(
        ("tool", "forge", "env_sync", "blocking"),
        default=True,
        description="Block the commit when env_sync finds a declared "
        "[project.scripts] CLI missing from the install (stale editable "
        "install). Set false to downgrade to a non-blocking WARN.",
    ),
    ConfigKey(
        ("tool", "forge", "env_sync", "rebuild_command"),
        "unset (auto_rebuild step self-skips)",
        "Command the auto_rebuild step runs to heal a stale install before "
        "env_sync blocks; never a bare pip install (FOUNDATION §2).",
    ),
    ConfigKey(
        ("tool", "forge", "badges", "enabled"),
        default=False,
        description="Opt into the README status-badge block written by "
        "install-forge-readme-badges (and the bootstrap readme-badges step).",
    ),
    ConfigKey(
        ("tool", "forge", "badges", "readme"),
        "README.md",
        "README file the badge managed-block is written into.",
    ),
    ConfigKey(
        ("tool", "forge", "badges", "workflow"),
        "ci.yml / ci.yaml, else first pull_request workflow",
        "GitHub Actions workflow filename for the CI badge (under "
        ".github/workflows); otherwise ci.yml / ci.yaml, else the first "
        "workflow triggered on pull_request.",
    ),
    ConfigKey(
        ("tool", "forge", "c4", "config"),
        "c4.toml (if present)",
        "Path to a standalone C4 model file (Structurizr DSL skeleton) read "
        "by forge-gen-c4. Keeps the verbose model out of pyproject. The "
        "section's presence (or a c4.toml) is the opt-in signal. A path that "
        "resolves outside the repository — by symlink too — is refused.",
    ),
    ConfigKey(
        ("tool", "forge", "c4", "output"),
        "docs/architecture.dsl",
        "Repo-relative path forge-gen-c4 writes the emitted Structurizr DSL "
        "to (overridable inline or in the model file).",
    ),
    ConfigKey(
        ("tool", "forge", "c4", "readme"),
        "unset (no README block)",
        "When set to a README path, forge-gen-c4 also keeps a managed Mermaid "
        "C4 block (between <!-- forge:c4:start/end --> markers) in sync, and "
        "the c4 pre-commit step fails on drift.",
    ),
    ConfigKey(
        ("tool", "forge", "c4", "strict_coverage"),
        default=False,
        description="Make forge-gen-c4 --check fail on any module mapped to no "
        "[[component]] or any declared component prefix matching zero real "
        "modules (default: warn-only — a non-exhaustive map stays valid).",
    ),
    ConfigKey(
        ("tool", "forge", "c4", "paths"),
        "source_dirs (smart-detect)",
        "Per-tool override of the roots forge-gen-c4 builds the import graph "
        "from; otherwise inherits the repo-wide [tool.forge].source_dirs "
        "(source only, no tests).",
    ),
    ConfigKey(
        ("tool", "forge", "c4", "direction"),
        "LR",
        "Graph direction for every generated diagram: 'LR' or 'TB'.",
    ),
    ConfigKey(
        ("tool", "forge", "c4", "edges"),
        "imports",
        "Which edges are drawn: 'imports' / 'both' add import-derived edges, "
        "'declared' draws only [[relationship]] edges.",
    ),
    ConfigKey(
        ("tool", "forge", "c4", "container_edges"),
        "inherit edges",
        "Container-view override of edges.",
    ),
    ConfigKey(
        ("tool", "forge", "c4", "component_edges"),
        "inherit edges",
        "Component-view override of edges.",
    ),
    ConfigKey(
        ("tool", "forge", "c4", "blocking"),
        default=True,
        description="Block the commit when the c4 step finds drift. Set false "
        "to downgrade every non-zero forge-gen-c4 --check exit — drift, an "
        "invalid model, an unsatisfied required relationship — to a "
        "non-blocking WARN, for a repo mid-restructure that wants the drift "
        "reported without it refusing each commit.",
    ),
    ConfigKey(
        ("tool", "forge", "c4", "render", "pdf_fit"),
        "auto",
        "How each PDF page is sized: 'auto' gives one tight page per view, "
        "'contain' and 'width' fit a fixed page.",
        doc=KeyDoc(
            group=DOC_GROUP_C4_PDF,
            detail=(
                "`auto` (default) sizes **each page to its own diagram** — one tight "
                "page per view (needs `pdfunite`/`qpdf`; falls back to `contain` "
                "without them). `contain` scales every view to fit **one fixed page** "
                "(uniform pages; a wide-short view letterboxes). `width` fits a fixed "
                "page's width only (a tall view may span extra pages)."
            ),
        ),
    ),
    ConfigKey(
        ("tool", "forge", "c4", "render", "pdf_page_size"),
        "A4",
        "Fixed-page size for the 'contain' / 'width' PDF fits (A4, A3, A5, "
        "Letter, Legal, Tabloid).",
        doc=KeyDoc(
            group=DOC_GROUP_C4_PDF,
            detail=(
                "Fixed-page size for `contain`/`width`: `A4`, `A3`, `A5`, `Letter`, "
                "`Legal`, `Tabloid` (unknown → A4). Ignored by `auto`."
            ),
        ),
    ),
    ConfigKey(
        ("tool", "forge", "c4", "render", "pdf_orientation"),
        "landscape",
        "Fixed-page PDF orientation: 'landscape' or 'portrait'.",
        doc=KeyDoc(
            group=DOC_GROUP_C4_PDF,
            detail="`landscape` or `portrait` — `contain`/`width` only.",
        ),
    ),
    ConfigKey(
        ("tool", "forge", "c4", "render", "pdf_margin"),
        10,
        "PDF page margin, in millimetres.",
        doc=KeyDoc(
            group=DOC_GROUP_C4_PDF,
            detail="Page margin in millimetres.",
        ),
    ),
    ConfigKey(
        ("tool", "forge", "c4", "render", "wrapping_width"),
        220,
        "Pixel width a component description wraps at in the HTML view.",
        doc=KeyDoc(
            group=DOC_GROUP_C4_RENDER,
            detail=(
                "Px width the description wraps at; Mermaid auto-sizes the box. The "
                "label-overflow fix."
            ),
            mermaid="`flowchart.wrappingWidth`",
        ),
    ),
    ConfigKey(
        ("tool", "forge", "c4", "render", "html_labels"),
        None,
        "Render diagram labels as HTML; false avoids a Firefox empty-label bug.",
        doc=KeyDoc(
            group=DOC_GROUP_C4_RENDER,
            detail=(
                "Render labels as HTML. Set `false` to dodge the Firefox empty-label "
                "bug (#5785)."
            ),
            mermaid="`htmlLabels` (root)",
        ),
    ),
    ConfigKey(
        ("tool", "forge", "c4", "render", "font_family"),
        "",
        "Font stack for the HTML view (offline-safe stacks only, no web fonts).",
        doc=KeyDoc(
            group=DOC_GROUP_C4_RENDER,
            detail="Font stack (offline-safe stacks only — no web fonts).",
            mermaid="`fontFamily` (root)",
        ),
    ),
    ConfigKey(
        ("tool", "forge", "c4", "render", "font_size"),
        None,
        "Base font size for the HTML view.",
        doc=KeyDoc(
            group=DOC_GROUP_C4_RENDER,
            detail="Base font size.",
            mermaid="`fontSize` (root)",
        ),
    ),
    ConfigKey(
        ("tool", "forge", "c4", "render", "node_spacing"),
        None,
        "Gap between sibling nodes; honored under the dagre layout only.",
        doc=KeyDoc(
            group=DOC_GROUP_C4_RENDER,
            detail=(
                'Gap between sibling nodes. **Honored under `layout = "dagre"` only** '
                "— the ELK engine ignores it; use `elk_node_spacing` for the ELK "
                "default."
            ),
            mermaid="`flowchart.nodeSpacing`",
        ),
    ),
    ConfigKey(
        ("tool", "forge", "c4", "render", "rank_spacing"),
        None,
        "Gap between ranks and layers; honored under the dagre layout only.",
        doc=KeyDoc(
            group=DOC_GROUP_C4_RENDER,
            detail=(
                'Gap between ranks/layers. **Honored under `layout = "dagre"` only** '
                "— ELK ignores it; use `elk_layer_spacing` under ELK."
            ),
            mermaid="`flowchart.rankSpacing`",
        ),
    ),
    ConfigKey(
        ("tool", "forge", "c4", "render", "elk_node_spacing"),
        None,
        "Gap between sibling nodes under the ELK layout.",
        doc=KeyDoc(
            group=DOC_GROUP_C4_RENDER,
            detail=(
                "Gap between sibling nodes under **ELK** (the ELK counterpart of "
                "`node_spacing`). Raising it visibly widens within-layer gaps. "
                "`unset` keeps the bundle default."
            ),
            mermaid="`elk.spacing.nodeNode`",
        ),
    ),
    ConfigKey(
        ("tool", "forge", "c4", "render", "elk_layer_spacing"),
        None,
        "Gap between layers under the ELK layout.",
        doc=KeyDoc(
            group=DOC_GROUP_C4_RENDER,
            detail="Gap between layers under **ELK** (counterpart of `rank_spacing`).",
            mermaid="`elk.layered.spacing.nodeNodeBetweenLayers`",
        ),
    ),
    ConfigKey(
        ("tool", "forge", "c4", "render", "elk_base_value"),
        None,
        "ELK master spacing that every unset per-pair spacing derives from.",
        doc=KeyDoc(
            group=DOC_GROUP_C4_RENDER,
            detail=(
                "ELK master spacing all unset per-pair spacings derive from — raises "
                "every gap proportionally."
            ),
            mermaid="`elk.spacing.baseValue`",
        ),
    ),
    ConfigKey(
        ("tool", "forge", "c4", "render", "padding"),
        None,
        "Inner padding of each node box.",
        doc=KeyDoc(
            group=DOC_GROUP_C4_RENDER,
            detail="Inner node padding.",
            mermaid="`flowchart.padding`",
        ),
    ),
    ConfigKey(
        ("tool", "forge", "c4", "render", "custom_css"),
        "",
        "Raw CSS injected into the diagram; can style elements by their tag.",
        doc=KeyDoc(
            group=DOC_GROUP_C4_RENDER,
            detail=(
                "Raw-CSS escape hatch injected into the diagram. Every element's "
                "`tags` are emitted as Mermaid CSS classes on its node (e.g. "
                "`.agent`), so this can style by role — see the [element tag "
                "vocabulary](../docs/c4-architecture.md) for the reserved tag set."
            ),
            mermaid="`themeCSS` (root)",
        ),
    ),
    ConfigKey(
        ("tool", "forge", "c4", "render", "layout"),
        "elk",
        "Diagram layout engine: 'elk' or 'dagre'; the organic ELK engines "
        "are rejected.",
        doc=KeyDoc(
            group=DOC_GROUP_C4_RENDER,
            detail=(
                "`elk` (= `elk.layered`) or `dagre` — the two supported, "
                "**hierarchy-aware** engines (they keep C4's clusters separated and "
                "route every edge). ELK routes dense cross-cluster edges more cleanly "
                "and tunes spacing via `elk_node_spacing` / `elk_layer_spacing`; "
                "`dagre` tunes `node_spacing` / `rank_spacing`. The organic ELK "
                "engines (`elk.stress` / `elk.force` / `elk.radial`) are **rejected "
                "at config-load** — they silently drop edges + overlap nodes on "
                "multi-cluster views."
            ),
            mermaid="`layout` (root)",
        ),
    ),
    ConfigKey(
        ("tool", "forge", "c4", "render", "node_placement_strategy"),
        "NETWORK_SIMPLEX",
        "ELK node-placement strategy.",
        doc=KeyDoc(
            group=DOC_GROUP_C4_RENDER,
            detail="ELK node placement (`BRANDES_KOEPF`, `NETWORK_SIMPLEX`, …).",
            mermaid="`elk.nodePlacementStrategy`",
        ),
    ),
    ConfigKey(
        ("tool", "forge", "c4", "render", "force_node_model_order"),
        default=True,
        description="Keep nodes in their declared order.",
        doc=KeyDoc(
            group=DOC_GROUP_C4_RENDER,
            detail="Preserve declared node order.",
            mermaid="`elk.forceNodeModelOrder`",
        ),
    ),
    ConfigKey(
        ("tool", "forge", "c4", "render", "theme"),
        "neutral",
        "Mermaid theme; must be 'base' for theme_colors to apply.",
        doc=KeyDoc(
            group=DOC_GROUP_C4_RENDER_ADVANCED,
            detail='Must be `"base"` for `theme_colors` to apply.',
            mermaid="`theme` (root)",
        ),
    ),
    ConfigKey(
        ("tool", "forge", "c4", "render", "theme_colors"),
        {},
        "Hex colour overrides, applied only under theme = 'base'.",
        doc=KeyDoc(
            group=DOC_GROUP_C4_RENDER_ADVANCED,
            detail=(
                "Hex color overrides (e.g. `primaryColor`, `lineColor`, "
                '`tertiaryColor`); applied only under `theme = "base"`.'
            ),
            mermaid="`themeVariables` (theme vars)",
        ),
    ),
    ConfigKey(
        ("tool", "forge", "c4", "render", "diagram_padding"),
        None,
        "Padding around the whole diagram.",
        doc=KeyDoc(
            group=DOC_GROUP_C4_RENDER_ADVANCED,
            detail="Padding around the whole diagram.",
            mermaid="`flowchart.diagramPadding`",
        ),
    ),
    ConfigKey(
        ("tool", "forge", "c4", "render", "consider_model_order"),
        "",
        "ELK ordering hint.",
        doc=KeyDoc(
            group=DOC_GROUP_C4_RENDER_ADVANCED,
            detail="ELK ordering hint (e.g. `NODES_AND_EDGES`).",
            mermaid="`elk.considerModelOrder`",
        ),
    ),
    ConfigKey(
        ("tool", "forge", "c4", "render", "merge_edges"),
        default=False,
        description="Merge parallel edges into one.",
        doc=KeyDoc(
            group=DOC_GROUP_C4_RENDER_ADVANCED,
            detail="Merge parallel edges.",
            mermaid="`elk.mergeEdges`",
        ),
    ),
    ConfigKey(
        ("tool", "forge", "c4", "render", "cycle_breaking_strategy"),
        "",
        "ELK cycle-breaking strategy.",
        doc=KeyDoc(
            group=DOC_GROUP_C4_RENDER_ADVANCED,
            detail="ELK cycle-breaking (e.g. `GREEDY_MODEL_ORDER`).",
            mermaid="`elk.cycleBreakingStrategy`",
        ),
    ),
    ConfigKey(
        ("tool", "forge", "c4", "render", "include_tags"),
        (),
        "When set, the HTML and PDF views keep only elements carrying one of "
        "these tags.",
        doc=KeyDoc(
            group=DOC_GROUP_C4_RENDER_ADVANCED,
            detail=(
                "When set, the HTML/PDF views keep only elements carrying one of "
                "these tags. The DSL, README block, and `--format mermaid` are "
                "canonical and unaffected."
            ),
            mermaid="_(HTML/PDF view filter)_",
        ),
    ),
    ConfigKey(
        ("tool", "forge", "c4", "render", "exclude_tags"),
        (),
        "The HTML and PDF views drop elements carrying any of these tags.",
        doc=KeyDoc(
            group=DOC_GROUP_C4_RENDER_ADVANCED,
            detail=(
                "The HTML/PDF views drop elements carrying any of these tags (applied "
                "after `include_tags`). The DSL / README / `--format mermaid` are "
                "unaffected."
            ),
            mermaid="_(HTML/PDF view filter)_",
        ),
    ),
    ConfigKey(
        ("tool", "forge", "c4", "render", "route_views"),
        (),
        "Component names that each get an extra HTML/PDF tab scoped to them.",
        doc=KeyDoc(
            group=DOC_GROUP_C4_RENDER_ADVANCED,
            detail=(
                "List of component names; each adds one extra HTML/PDF tab scoped to "
                "that component and the components one edge hop away — isolating a "
                "single relationship path out of a dense Container view. A name "
                "matching no connected component warns and is skipped. Unset adds no "
                "tab (canonical view set unchanged)."
            ),
            mermaid="_(HTML/PDF extra tabs)_",
        ),
    ),
)

# Third-party tools forge reads from their OWN native section rather than
# wrapping under [tool.forge.*]. Named here so consumers see what forge
# reads without it being hidden behind a forge namespace.
# Path tuples match CONFIG_KEYS.path encoding (not dotted strings).
NATIVE_SECTIONS: tuple[tuple[tuple[str, ...], str], ...] = (
    (
        ("tool", "interrogate"),
        (
            "Docstring-coverage gate (fail-under, exclude, ignore-*) read by "
            "verify-forge-docstring-coverage. interrogate's own section — "
            "forge reads it directly, not a forge wrapper."
        ),
    ),
)


_UNSET = object()


def _lookup(data: dict, path: tuple[str, ...]) -> object:
    """Return the value at *path* in nested *data*, or ``_UNSET`` if absent.

    Args:
        data: Parsed ``pyproject.toml`` data.
        path: Key path under the TOML root.

    Returns:
        The configured value, or the ``_UNSET`` sentinel when any segment
        of the path is missing.
    """
    node: object = data
    for segment in path:
        if not isinstance(node, dict) or segment not in node:
            return _UNSET
        node = node[segment]
    return node


def _section_of(key: ConfigKey) -> str:
    """Return the section header (path without the leaf key) for *key*.

    Args:
        key: The config key whose parent section is wanted.

    Returns:
        Dotted section path, e.g. ``"tool.forge.docstring_coverage"``.
    """
    return ".".join(key.path[:-1])


def keys_by_section() -> list[tuple[str, list[ConfigKey]]]:
    """Group :data:`CONFIG_KEYS` by section, in first-appearance order.

    A section's keys need not be contiguous in :data:`CONFIG_KEYS`;
    grouping here keeps every renderer printing each header once.

    Returns:
        ``(dotted section, keys)`` pairs, keys in declaration order.
    """
    groups: dict[str, list[ConfigKey]] = {}
    for key in CONFIG_KEYS:
        groups.setdefault(_section_of(key), []).append(key)
    return list(groups.items())


_C4_PREFIX = ("tool", "forge", "c4")
_C4_NOTE = (
    "  (this section and [tool.forge.c4.render] are read from the resolved "
    "model table: a model file — c4.toml, or the path in config — with any "
    "inline keys overlaid on it)"
)


def _c4_model_path(key: ConfigKey) -> tuple[str, ...] | None:
    """Return *key*'s path inside the resolved C4 model table, if it has one.

    Args:
        key: The config key being reported.

    Returns:
        The path relative to the model table, or ``None`` for a key read
        from ``pyproject.toml`` — anything outside ``[tool.forge.c4]``, and
        ``config`` itself, which selects the model file.
    """
    if key.path[: len(_C4_PREFIX)] != _C4_PREFIX:
        return None
    inner = key.path[len(_C4_PREFIX) :]
    return None if inner == ("config",) else inner


def build_report(data: dict, repo_root: Path) -> list[str]:
    """Build the ``forge-config`` report lines for a repo.

    Most keys are read straight from ``pyproject.toml``. The
    ``[tool.forge.c4]`` keys are read from the *resolved* model table
    instead, because that is where forge itself reads them: reporting them
    from ``pyproject.toml`` alone calls a key set in a model file "not set"
    (:func:`forge.config.resolve_model_section`).

    Args:
        data: Parsed ``pyproject.toml`` data (``{}`` when absent).
        repo_root: Repo whose C4 model table is resolved for the C4 keys.

    Returns:
        Human-readable report lines: per-section key listing with current
        values or defaults, the native-section pointers, and a suggested-
        setup block for recommended keys that are unset.
    """
    lines = ["forge config in this repo", "=" * 42]
    model = resolve_model_section(repo_root) or {}
    missing: list[ConfigKey] = []
    for section, keys in keys_by_section():
        lines.append(f"[{section}]")
        if section == "tool.forge.c4":
            lines.append(_C4_NOTE)
        for key in keys:
            inner = _c4_model_path(key)
            value = _lookup(data, key.path) if inner is None else _lookup(model, inner)
            leaf = key.path[-1]
            if value is _UNSET:
                lines.append(f"  {leaf:<14} = <default: {key.default!r}>   (not set)")
                if key.recommended:
                    missing.append(key)
            else:
                lines.append(f"  {leaf:<14} = {value!r}")

    lines.append("")
    for path, desc in NATIVE_SECTIONS:
        present = _lookup(data, path) is not _UNSET
        flag = "set" if present else "not set"
        section = ".".join(path)
        lines.append(f"[{section}]  ({flag} — native tool section, read by forge)")
        lines.append(f"  {desc}")

    if missing:
        lines.append("")
        lines.append("Suggested setup (forge reads these but you haven't set them):")
        for key in missing:
            lines.append(f"  • [{_section_of(key)}].{key.path[-1]} — {key.description}")
            lines.append(f"        {key.path[-1]} = {key.default!r}")
    return lines


def main() -> int:
    """Entry point for ``forge-config``.

    Returns:
        Always ``0`` — a read-only advisory report, never a gate.
    """
    parser = argparse.ArgumentParser(
        prog="forge-config",
        description=(
            "List the [tool.forge.*] config forge reads in this repo, with "
            "current values / defaults, the native tool sections forge "
            "reads, and advice on recommended-but-unset keys."
        ),
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List forge config + advice (the default action).",
    )
    parser.parse_args()

    root = Path.cwd()
    for line in build_report(read_pyproject_raw(root), root):
        logger.info("%s", line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
