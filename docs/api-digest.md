# API Digest

A compact index of this codebase's symbols — every top-level function and class, with its signature and one-line summary. Both public API and internal helpers are indexed; internal helpers are tagged _(internal)_. Use it to check whether a helper for a task already exists before writing a new one (DRY) — reuse candidates are often private.

> **Generated file — do not edit by hand.** Regenerate with `forge-gen-api-digest`; check for drift with `forge-gen-api-digest --check`.

_47 modules, 473 symbols._

## `forge._hook_helpers`

- `run_foundation_drift_check(hook_name: str) -> int` — Run ``install-forge-claude-md --check --quiet``.
- `run_hook_extensions(hook_name: str) -> None` — Run consumer extension scripts under ``.githooks/<hook_name>.d/``.

## `forge.audit.agents`

- `class AgentDoc` — Parsed view of one ``agents/*.md`` file.
- `_split_frontmatter(text: str) -> tuple[dict[str, str | tuple[str, ...]], str]` _(internal)_ — Split YAML-ish frontmatter from the rest of an agent file.
- `_strip_code_blocks(body: str) -> str` _(internal)_ — Remove fenced code blocks (``` ... ```) from *body*.
- `_parse_agent(path: Path, repo_root_path: Path) -> AgentDoc` _(internal)_ — Read and parse one agent file.
- `_word_count(body_no_code: str) -> int` _(internal)_ — Return the whitespace-split token count of *body_no_code*.
- `_check_word_count(agent: AgentDoc) -> list[Finding]` _(internal)_ — Flag agent bodies above the length budget.
- `_check_frontmatter(agent: AgentDoc) -> list[Finding]` _(internal)_ — Flag missing required frontmatter keys.
- `_check_description_shape(agent: AgentDoc) -> list[Finding]` _(internal)_ — Flag descriptions that read as role labels rather than routing triggers.
- `_is_reporter_agent(agent: AgentDoc) -> bool` _(internal)_ — Return True when *agent* is in :data:`REPORTER_AGENT_NAMES`.
- `_check_reporter_tools(agent: AgentDoc) -> list[Finding]` _(internal)_ — Flag reporter agents holding mutating tools (`Write`/`Edit`).
- `_check_reporter_verified_at(agent: AgentDoc) -> list[Finding]` _(internal)_ — Flag reporter agents missing the ``verified-at:`` header instruction.
- `_check_required_sections(agent: AgentDoc) -> list[Finding]` _(internal)_ — Flag missing canonical H2 sections.
- `_tokens(text: str) -> list[str]` _(internal)_ — Return whitespace-split lowercase tokens of *text*.
- `_ngrams(tokens: list[str], n: int) -> set[str]` _(internal)_ — Return the set of *n*-token windows from *tokens*.
- `_check_foundation_restatements(agent: AgentDoc, foundation_ngrams: set[str]) -> list[Finding]` _(internal)_ — Flag substrings of ``SHARED_TOKEN_MIN`` tokens shared with FOUNDATION.
- `_cross_agent_duplicate_findings(agents: list[AgentDoc]) -> list[Finding]` _(internal)_ — Flag n-grams that appear in two or more agent files.
- `class AgentsConfig` — Configuration for ``forge-audit-agents``.
- `_iter_agent_files(repo_root_path: Path) -> list[Path]` _(internal)_ — Return every public agent markdown file under ``agents/``.
- `_per_agent_findings(agent: AgentDoc, foundation_ngrams: set[str]) -> list[Finding]` _(internal)_ — Run every per-agent check and return the combined finding list.
- `_render_summary(agents: list[AgentDoc], findings: list[Finding]) -> str` _(internal)_ — Render the per-agent summary table for the log header.
- `run(scope: Scope, _roots: list[Path], config: AgentsConfig) -> int` — Walk every agent file and emit findings to ``code_health/audit_agents.log``.
- `main() -> int` — CLI entry point for ``forge-audit-agents``.

## `forge.audit.all`

- `class SubResult` — Outcome of running one sub-audit.
- `_read_finding_count(log_text: str) -> int` _(internal)_ — Parse the ``# findings: N`` header line from a log.
- `_run_one(name: str, scope: str, roots: list[str] | None) -> SubResult` _(internal)_ — Invoke a sub-audit CLI and parse its log.
- `_render_summary(results: list[SubResult]) -> str` _(internal)_ — Render the aggregate summary log text.
- `main() -> int` — Run every sub-audit and write ``code_health/audit_summary.log``.

## `forge.audit.claims`

- `class ClaimsConfig` — Tunable knobs for the claims audit.
- `_is_suppression_comment(line_text: str) -> bool` _(internal)_ — Return ``True`` if a comment is a known lint/type-checker directive.
- `_looks_like_claim(text: str) -> bool` _(internal)_ — Return ``True`` if ``text`` matches any of the claim patterns.
- `_matched_terms(text: str, lexicon: frozenset[str]) -> list[str]` _(internal)_ — Return the lexicon terms that appear in ``text`` (case-insensitive).
- `_docstring_findings(source_lines: list[str], docstring: str, docstring_lineno: int, rel: str, lexicon: frozenset[str]) -> list[Finding]` _(internal)_ — Build claim findings from one docstring.
- `_locate_claim_line(source_lines: list[str], start_line: int, claim_text: str, *, fallback_offset: int) -> int` _(internal)_ — Find the absolute line number containing ``claim_text``.
- `_docstring_node_findings(tree: ast.Module, source_lines: list[str], rel: str, lexicon: frozenset[str]) -> list[Finding]` _(internal)_ — Scan every module / class / function docstring in a tree.
- `_comment_findings(text: str, source_lines: list[str], rel: str, lexicon: frozenset[str]) -> list[Finding]` _(internal)_ — Scan every inline ``#`` comment for claims.
- `_scan_file(path: Path, lexicon: frozenset[str]) -> list[Finding]` _(internal)_ — Scan a single ``.py`` file for claim candidates.
- `load_repo_lexicon(*, use_default: bool = True) -> frozenset[str]` — Read ``forge-audit-claims.toml`` (if present) and merge with default.
- `run(scope: Scope, roots: list[Path], config: ClaimsConfig) -> int` — Execute the claims-extraction pipeline.
- `main() -> int` — CLI entry point for ``forge-audit-claims``.

## `forge.audit.common`

- `class Scope` — Audit scope selector.
- `class Severity` — Finding severity tier.
- `class Finding` — One audit observation with provenance.
  - `render(self) -> str` — Render this finding as a single block in the log file.
- `make_audit_parser(prog: str, description: str) -> argparse.ArgumentParser` — Build the shared CLI surface for an audit script.
- `resolve_roots(roots: list[str] | None) -> list[Path]` — Resolve the effective scan roots.
- `_is_excluded(path: Path) -> bool` _(internal)_ — Return ``True`` if ``path`` lies under any default-excluded directory.
- `iter_files(scope: Scope, roots: list[Path], *, suffix: str = '.py') -> Iterator[Path]` — Yield matching files under ``roots`` respecting ``scope``.
- `relpath(path: Path) -> str` — Render ``path`` relative to the repo root for log stability.
- `write_log(name: str, findings: Iterable[Finding], summary: str, *, output: Path | None = None) -> Path` — Write findings + summary to ``code_health/audit_<name>.log``.
- `exit_code_for(findings: Iterable[Finding]) -> int` — Map findings to a process exit code.
- `count_by_severity(findings: Iterable[Finding]) -> dict[Severity, int]` — Tally findings per severity tier.

## `forge.audit.data`

- `class DataConfig` — Tunable knobs for the data audit.
- `_gather_files(scope: Scope, roots: list[Path], suffixes: tuple[str, ...]) -> list[Path]` _(internal)_ — Collect candidate data files across the configured suffixes.
- `_check_csv(path: Path) -> list[Finding]` _(internal)_ — Verify CSV column count is consistent across every row.
- `_check_json(path: Path) -> list[Finding]` _(internal)_ — Parse a JSON file; report any decode error.
- `_check_jsonschema(path: Path, data: object) -> list[Finding]` _(internal)_ — Validate a parsed JSON document against ``<path>.schema.json`` if present.
- `_check_toml(path: Path) -> list[Finding]` _(internal)_ — Parse a TOML file; report any decode error.
- `_check_yaml(path: Path) -> list[Finding]` _(internal)_ — Parse a YAML file; report any decode error.
- `_check_one(path: Path) -> list[Finding]` _(internal)_ — Dispatch a single file to the appropriate parser.
- `run(scope: Scope, roots: list[Path], config: DataConfig) -> int` — Execute the data-integrity audit.
- `main() -> int` — CLI entry point for ``forge-audit-data``.

## `forge.audit.deps`

- `class ModuleNode` — One Python module after parsing.
- `class DepsConfig` — Tunable knobs for the dependency-analysis pipeline.
- `_resolve_module_name(path: Path, package_roots: list[Path]) -> str | None` _(internal)_ — Translate a ``.py`` path to a dotted module name.
- `_extract_imports(tree: ast.Module, current_module: str) -> set[str]` _(internal)_ — Return the set of fully-qualified import-candidate targets.
- `_closest_known(target: str, modules: dict[str, ModuleNode]) -> str | None` _(internal)_ — Walk up the dotted name until a known module is found.
- `_abstractness(tree: ast.Module) -> tuple[int, int]` _(internal)_ — Count abstract vs total class definitions in a module.
- `class _TarjanState` _(internal)_ — Mutable scratch space shared across Tarjan recursion frames.
- `_pop_scc(state: _TarjanState, root: str) -> None` _(internal)_ — Pop nodes off the DFS stack down to ``root``, forming one SCC.
- `_strongconnect(node: str, graph: dict[str, set[str]], state: _TarjanState) -> None` _(internal)_ — Tarjan inner step rooted at ``node``.
- `_tarjan_scc(graph: dict[str, set[str]]) -> list[list[str]]` _(internal)_ — Compute strongly-connected components via Tarjan's algorithm.
- `_compute_couplings(graph: dict[str, set[str]]) -> tuple[dict[str, int], dict[str, int]]` _(internal)_ — Compute afferent and efferent coupling counts.
- `_instability(ca: int, ce: int) -> float` _(internal)_ — Compute the Martin instability metric.
- `_build_cycle_findings(sccs: list[list[str]], modules: dict[str, ModuleNode]) -> list[Finding]` _(internal)_ — Render multi-node SCCs as CRITICAL ADP-violation findings.
- `_build_distance_findings(modules: dict[str, ModuleNode], ca: dict[str, int], ce: dict[str, int], *, threshold: float) -> list[Finding]` _(internal)_ — Render main-sequence-distance violations as LOW findings.
- `_run_tach() -> list[Finding]` _(internal)_ — Run optional ``tach check`` and translate violations to findings.
- `_scan_module(path: Path, package_roots: list[Path]) -> tuple[str, ModuleNode, set[str]] | None` _(internal)_ — Parse a single file into (name, node, raw-imports).
- `_build_internal_graph(modules: dict[str, ModuleNode], raw_imports: dict[str, set[str]]) -> dict[str, set[str]]` _(internal)_ — Project raw imports onto the known-module graph.
- `render_dependency_tree(graph: dict[str, set[str]], sccs: list[list[str]]) -> str` — Render the internal dependency graph as a readable plain-text tree.
- `_write_tree_log(tree: str, *, output: Path | None) -> Path` _(internal)_ — Write the rendered dependency tree to ``code_health/audit_deps_tree.log``.
- `build_module_graph(scope: Scope, roots: list[Path]) -> tuple[dict[str, ModuleNode], dict[str, set[str]]]` — Scan source roots into a module map + internal import graph.
- `run(scope: Scope, roots: list[Path], config: DepsConfig) -> int` — Execute the full dependency-analysis pipeline.
- `main() -> int` — CLI entry point for ``forge-audit-deps``.

## `forge.audit.dup`

- `class CodeUnit` — One function definition extracted from the source tree.
- `_strip_docstring(body: list[ast.stmt]) -> list[ast.stmt]` _(internal)_ — Return ``body`` with a leading docstring (if any) removed.
- `_normalize_body(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str` _(internal)_ — Render the function body to canonical source (no docstring).
- `_tokenize_body(source: str) -> list[str]` _(internal)_ — Tokenize ``source`` into a stable string sequence for shingling.
- `_shingles(tokens: list[str], k: int) -> frozenset[tuple[str, ...]]` _(internal)_ — Return the set of ``k``-grams over the token sequence.
- `_walk_functions(tree: ast.Module) -> Iterable[tuple[_FuncDef, str]]` _(internal)_ — Yield every function definition with its qualified-name prefix.
- `extract_units(path: Path, *, min_tokens: int, shingle_size: int) -> list[CodeUnit]` — Extract every function-sized unit from a single file.
- `_group_by_hash(units: list[CodeUnit]) -> list[list[CodeUnit]]` _(internal)_ — Group units sharing an identical body hash.
- `_jaccard(a: frozenset[tuple[str, ...]], b: frozenset[tuple[str, ...]]) -> float` _(internal)_ — Jaccard similarity between two shingle sets.
- `_find_near_dups(units: list[CodeUnit], exact_dup_ids: set[int], *, threshold: float) -> list[tuple[CodeUnit, CodeUnit, float]]` _(internal)_ — Pairwise scan for near-duplicate pairs above the Jaccard threshold.
- `_find_name_collisions(units: list[CodeUnit], exact_dup_ids: set[int]) -> list[list[CodeUnit]]` _(internal)_ — Group units sharing a bare name across files but with different bodies.
- `_exact_severity(paths: set[str]) -> Severity` _(internal)_ — Pick severity for an exact-duplicate group.
- `_build_exact_findings(groups: list[list[CodeUnit]]) -> tuple[list[Finding], set[int]]` _(internal)_ — Render exact-duplicate groups as ``Finding`` records.
- `_build_near_findings(pairs: list[tuple[CodeUnit, CodeUnit, float]]) -> list[Finding]` _(internal)_ — Render near-duplicate pairs as ``Finding`` records.
- `_build_name_findings(groups: list[list[CodeUnit]]) -> list[Finding]` _(internal)_ — Render name-collision groups as informational findings.
- `_summary(n_units: int, n_exact: int, n_near: int, n_name: int) -> str` _(internal)_ — Render the one-paragraph audit summary.
- `class DupConfig` — Tunable knobs for the duplicate-detection pipeline.
- `run(scope: Scope, roots: list[Path], config: DupConfig) -> int` — Execute the full duplicate-detection pipeline.
- `main() -> int` — CLI entry point for ``forge-audit-dup``.

## `forge.audit.orphans`

- `class OrphansConfig` — Tunable knobs for the orphans audit.
- `_load_vulture() -> object` _(internal)_ — Import the vulture module or exit with an install hint.
- `_severity(confidence: int) -> Severity` _(internal)_ — Map a vulture confidence percentage to a finding severity.
- `_build_findings(items: list[object]) -> list[Finding]` _(internal)_ — Translate vulture items to ``Finding`` records.
- `_scavenge_paths(scope: Scope, roots: list[Path]) -> list[Path]` _(internal)_ — Decide what paths to hand to ``Vulture.scavenge``.
- `run(scope: Scope, roots: list[Path], config: OrphansConfig) -> int` — Execute the orphans audit.
- `main() -> int` — CLI entry point for ``forge-audit-orphans``.

## `forge.audit.suppressions`

- `class SuppressionsConfig` — Tunable knobs for the suppressions audit.
- `_parse_codes(raw: str | None) -> list[str]` _(internal)_ — Split a comma-separated suppression-code string into trimmed codes.
- `resolve_ruff_rule(code: str, cache: dict[str, tuple[str, str] | None]) -> tuple[str, str] | None` — Return ``(name, summary)`` for a ruff rule code, or ``None`` if unknown.
- `_noqa_findings(path: str, line_no: int, line: str, rule_cache: dict[str, tuple[str, str] | None]) -> list[Finding]` _(internal)_ — Build findings for any ``# noqa`` directive on ``line``.
- `_type_ignore_findings(path: str, line_no: int, line: str) -> list[Finding]` _(internal)_ — Build findings for any ``# type: ignore`` directive on ``line``.
- `_pragma_findings(path: str, line_no: int, line: str) -> list[Finding]` _(internal)_ — Build findings for ``# pragma: no cover`` directives on ``line``.
- `_iter_comments(text: str) -> list[tuple[int, str]]` _(internal)_ — Yield ``(line_no, line_text)`` for every line that holds a COMMENT.
- `_scan_file(path: Path, rule_cache: dict[str, tuple[str, str] | None]) -> list[Finding]` _(internal)_ — Scan one source file for suppression directives.
- `run(scope: Scope, roots: list[Path], config: SuppressionsConfig) -> int` — Execute the suppressions audit.
- `main() -> int` — CLI entry point for ``forge-audit-suppressions``.

## `forge.claude_settings_schema`

- `scaffold() -> dict[str, object]` — Return a fresh deep copy of the empty-file settings scaffold.
- `marketplace_entry(ref: str) -> dict[str, object]` — Return forge's ``extraKnownMarketplaces[forge]`` value for *ref*.
- `read_marketplace_ref(settings: dict[str, object]) -> str | None` — Return ``extraKnownMarketplaces.forge.source.ref`` from *settings*.

## `forge.config`

- `detect_source_dirs(repo_root: Path) -> list[str]` — Smart-detect the repo's source roots when ``source_dirs`` is unset.
- `detect_test_dirs(repo_root: Path) -> list[str]` — Smart-detect the repo's test roots when ``test_dirs`` is unset.
- `class ForgeConfig` — Repo configuration sourced from ``[tool.forge]``.
  - `dual_track(self) -> bool` — Return ``True`` when base and dev are distinct branches.
- `read_pyproject_raw(repo_root: Path) -> dict` — Return the full parsed ``pyproject.toml`` dict, or ``{}`` on failure.
- `load_config(repo_root: Path) -> ForgeConfig` — Read ``[tool.forge]`` from *repo_root*'s ``pyproject.toml``.
- `_existing_dirs(repo_root: Path, dirs: list[str]) -> list[str]` _(internal)_ — Filter *dirs* to existing in-repo paths, de-duplicated, order-preserving.
- `resolve_tool_roots(repo_root: Path, tool: str, *, include_tests: bool = False) -> list[str]` — Resolve the scan roots a layout-consuming *tool* should use.

## `forge.continuation_append`

- `_today_iso() -> str` _(internal)_ — Return today's date as ``YYYY-MM-DD``.
- `_ensure_file_and_section(path: Path) -> None` _(internal)_ — Create the file with the canonical headers if missing.
- `_append_line(path: Path, line: str) -> None` _(internal)_ — Append *line* to *path* with a trailing newline.
- `main() -> int` — Append one activity-log line to ``.plan/CONTINUATION.md``.

## `forge.doctor`

- `class CheckResult` — Outcome of one diagnostic check.
- `_expected_clis() -> list[str]` _(internal)_ — Return the console-script names shipped by ``forge-scripts``.
- `_check_clis() -> list[CheckResult]` _(internal)_ — One result per expected CLI entry point on PATH.
- `_check_gh() -> list[CheckResult]` _(internal)_ — Check `gh` is installed and authenticated.
- `_find_plugin_dir(plugin_name: str) -> Path | None` _(internal)_ — Locate a Claude Code plugin cache directory by name.
- `_check_plugin_install(plugin_name: str) -> CheckResult` _(internal)_ — Verify Claude Code has installed the named plugin locally.
- `_read_json(path: Path) -> tuple[dict, str | None]` _(internal)_ — Read a JSON file. Returns (data, error_message_or_None).
- `_find_install_dir(plugin_root: Path) -> Path | None` _(internal)_ — Walk the Claude Code cache layout to find the active plugin install.
- `_version_key(name: str) -> tuple[int, ...]` _(internal)_ — Return a sortable key for a version-shaped directory name.
- `_check_plugin_manifests(plugin_root: Path | None, plugin_name: str) -> list[CheckResult]` _(internal)_ — Validate plugin.json + marketplace.json under the installed plugin root.
- `_check_plugin_contents(plugin_root: Path | None) -> list[CheckResult]` _(internal)_ — Verify the expected plugin sub-directories contain files.
- `_check_step_tools(repo_root: Path) -> list[CheckResult]` _(internal)_ — Verify the external tool for each enabled pre-commit step is on PATH.
- `_check_under_used_capabilities(repo_root: Path) -> list[CheckResult]` _(internal)_ — Surface installed-but-never-run forge capabilities.
- `_print_human(results: list[CheckResult]) -> None` _(internal)_ — Print a human-readable report, separating blocking and INFO results.
- `main() -> int` — Run all forge-doctor checks and print the results.

## `forge.fix_ruff`

- `_restage_modified(repo_root: Path, source_dirs: list[str]) -> list[str]` _(internal)_ — ``git add`` tracked files modified inside *source_dirs*.
- `_validate_paths(repo_root: Path, paths: list[str]) -> list[str]` _(internal)_ — Ensure every entry in *paths* resolves inside *repo_root*.
- `main() -> int` — Apply ruff fixes and write ``code_health/ruff.log``.

## `forge.forge_config`

- `class ConfigKey` — One ``[tool.forge.*]`` key forge reads in a consumer repo.
- `_lookup(data: dict, path: tuple[str, ...]) -> object` _(internal)_ — Return the value at *path* in nested *data*, or ``_UNSET`` if absent.
- `_section_of(key: ConfigKey) -> str` _(internal)_ — Return the section header (path without the leaf key) for *key*.
- `build_report(data: dict) -> list[str]` — Build the ``forge-config`` report lines from parsed pyproject data.
- `main() -> int` — Entry point for ``forge-config``.

## `forge.gen_api_digest`

- `class Symbol` — One top-level symbol extracted from a module.
- `class ModuleDigest` — The top-level symbols of a single module.
- `detect_roots(root: Path, explicit: list[str] | None) -> list[Path]` — Resolve the source roots to scan for Python modules.
- `_is_test_module(path: Path) -> bool` _(internal)_ — Return whether a module path is a test module to skip.
- `iter_modules(roots: list[Path]) -> Iterator[Path]` — Yield Python module files under the given source roots.
- `_annotation(node: ast.expr | None) -> str` _(internal)_ — Render an AST annotation node as source text.
- `_format_arg(arg: ast.arg, default: ast.expr | None) -> str` _(internal)_ — Render a single argument with its annotation and default.
- `_positional_args(args: ast.arguments) -> list[str]` _(internal)_ — Render the positional (and positional-only) arguments.
- `_keyword_only_args(args: ast.arguments) -> list[str]` _(internal)_ — Render the keyword-only arguments, including the ``*`` marker.
- `format_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str` — Reconstruct a function's signature from its AST node.
- `_summary_line(node: ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) -> str` _(internal)_ — Return the first line of an AST node's docstring.
- `_is_public(name: str) -> bool` _(internal)_ — Return whether a symbol name is public.
- `_is_dunder(name: str) -> bool` _(internal)_ — Return whether a symbol name is a dunder name.
- `_class_methods(node: ast.ClassDef) -> tuple[tuple[str, str], ...]` _(internal)_ — Extract the public methods of a class.
- `extract_symbols(tree: ast.Module) -> tuple[Symbol, ...]` — Extract top-level symbols from a parsed module.
- `_dotted_name(path: Path, root: Path) -> str` _(internal)_ — Render a module path as a dotted name relative to the repo root.
- `build_digest(root: Path, roots: list[Path]) -> list[ModuleDigest]` — Build the per-module digest for every module under the roots.
- `_render_symbol(symbol: Symbol) -> list[str]` _(internal)_ — Render one symbol (and any methods) as markdown lines.
- `count_symbols(digests: list[ModuleDigest]) -> int` — Return the total number of top-level symbols across all modules.
- `render_digest(digests: list[ModuleDigest]) -> str` — Render the full API digest markdown document.
- `main() -> int` — Generate or verify the API digest doc.

## `forge.gen_c4`

- `class Person` — A C4 actor — someone who uses the system (System Context level).
- `class External` — An external software system the system depends on.
- `class Container` — A deployable unit inside the system (Container level).
- `class Component` — A named component and the module prefixes that constitute it.
- `class Relationship` — A human-declared component-to-component relationship.
- `class C4Config` — The human-authored ``[tool.forge.c4]`` model skeleton.
- `class _IdMaps` _(internal)_ — Maps display names to unique DSL-safe identifiers.
- `class _IdAllocator` _(internal)_ — Allocates unique, DSL-safe identifiers from display names.
  - `allocate(self, name: str, fallback: str) -> str` — Return a unique identifier derived from *name*.
- `_slug(name: str) -> str` _(internal)_ — Slugify *name* into a DSL-safe identifier fragment.
- `_q(text: str) -> str` _(internal)_ — Quote *text* as a Structurizr DSL string literal.
- `_coerce_list(raw: object) -> list[dict]` _(internal)_ — Return *raw* as a list of dicts, tolerating a single table.
- `_read_toml_file(path: Path) -> dict | None` _(internal)_ — Parse a standalone TOML file, degrading to ``None`` on any failure.
- `resolve_model_section(root: Path) -> dict | None` — Locate the C4 model table — external file or inline pyproject.
- `_parse_components(section: dict) -> tuple[Component, ...]` _(internal)_ — Parse components from rich ``[[component]]`` tables + the simple map.
- `load_c4_config(root: Path) -> C4Config | None` — Load the C4 model skeleton for the repo.
- `assign_components(modules: list[str], components: tuple[Component, ...]) -> tuple[dict[str, str], list[str]]` — Map each module to a component by longest-prefix match.
- `_under_prefix(module: str, prefix: str) -> bool` _(internal)_ — Return whether *module* equals *prefix* or is a dotted child of it.
- `derive_component_edges(graph: dict[str, set[str]], assigned: dict[str, str]) -> set[tuple[str, str]]` — Collapse module-level import edges to component-level edges.
- `render_dsl(config: C4Config, edges: set[tuple[str, str]]) -> str` — Render the full Structurizr DSL workspace text.
- `_render_model(config: C4Config, ids: _IdMaps) -> list[str]` _(internal)_ — Render the ``model`` block's element declarations.
- `_component_description(component: Component) -> str` _(internal)_ — Return a component's box description for C4 rendering.
- `_render_relationships(config: C4Config, ids: _IdMaps, edges: set[tuple[str, str]]) -> list[str]` _(internal)_ — Render the relationship statements of the ``model`` block.
- `_render_views(config: C4Config, sys_id: str, container_ids: dict[str, str]) -> list[str]` _(internal)_ — Render the ``views`` block.
- `build_model(root: Path, roots: list[Path]) -> tuple[C4Config, set[tuple[str, str]], list[str]] | None` — Assemble the C4 model: config, derived edges, and unmatched modules.
- `generate(root: Path, roots: list[Path]) -> tuple[str, list[str]] | None` — Build the DSL text and unmatched-module list for the repo.
- `_m(text: str) -> str` _(internal)_ — Escape label *text* for safe embedding in a Mermaid node label.
- `render_mermaid(config: C4Config, edges: set[tuple[str, str]]) -> str` — Render the model as a Mermaid flowchart (offline-renderable).
- `_mermaid_box(name: str, technology: str, description: str) -> str` _(internal)_ — Build a multi-line Mermaid box label: bold name, technology, description.
- `_mermaid_edges(config: C4Config, person_ids: dict[str, str], external_ids: dict[str, str], container_ids: dict[str, str], component_ids: dict[str, str], edges: set[tuple[str, str]]) -> list[str]` _(internal)_ — Render the Mermaid relationship lines.
- `render_html(config: C4Config, mermaid_text: str) -> str` — Wrap a Mermaid diagram in a self-contained, offline HTML page.
- `_copy_vendored_mermaid(dest_dir: Path) -> None` _(internal)_ — Write the vendored Mermaid bundle next to an emitted HTML file.
- `_warn_unmatched(unmatched: list[str]) -> None` _(internal)_ — Log a coverage warning naming modules in no component.
- `render_readme_block(mermaid_text: str) -> str` — Render the managed README block embedding the Mermaid diagram.
- `_splice_readme(readme_text: str, block: str) -> str | None` _(internal)_ — Replace the managed C4 block in *readme_text* with *block*.
- `_readme_path(root: Path, config: C4Config) -> Path` _(internal)_ — Return the configured README path under *root*.
- `sync_readme(root: Path, config: C4Config, mermaid_text: str, *, check: bool) -> int` — Write or verify the managed C4 block inside the configured README.
- `_emit_mermaid(config: C4Config, edges: set[tuple[str, str]], output: str | None) -> int` _(internal)_ — Print or write the canonical Mermaid source.
- `_emit_html(root: Path, config: C4Config, edges: set[tuple[str, str]], args: argparse.Namespace) -> int` _(internal)_ — Write or verify the offline HTML view (+ vendored Mermaid sidecar).
- `_emit_dsl(root: Path, config: C4Config, edges: set[tuple[str, str]], args: argparse.Namespace) -> int` _(internal)_ — Write or verify the canonical DSL artifact and the README C4 block.
- `main() -> int` — Generate or verify the C4 artifacts (DSL + README block, or HTML).
- `_parse_args() -> argparse.Namespace` _(internal)_ — Parse the ``forge-gen-c4`` command-line arguments.
- `_resolve_roots(root: Path, explicit: list[str] | None) -> list[Path]` _(internal)_ — Resolve the source roots to scan for the import graph.

## `forge.gen_cli_reference`

- `class CliEntry` — A single forge console-script CLI.
- `discover_clis(distribution: str = DISTRIBUTION) -> list[CliEntry]` — Discover the console-script CLIs shipped by a distribution.
- `capture_help(entry: CliEntry) -> str` — Capture the ``--help`` output of a single CLI.
- `render_reference(entries: list[CliEntry]) -> str` — Render the full CLI reference markdown document.
- `main() -> int` — Generate or verify the forge CLI reference doc.

## `forge.gen_commit_types`

- `_alternation() -> str` _(internal)_ — Render ``CONVENTIONAL_COMMIT_TYPES`` as a `|`-joined regex alternation.
- `_expected_line() -> str` _(internal)_ — Return the canonical ``CONVENTIONAL_TYPES='...'`` shell line.
- `_rewrite(content: str) -> str` _(internal)_ — Return *content* with the managed block updated to the canonical line.
- `main() -> int` — Entry point for ``forge-gen-commit-types``.

## `forge.gen_common`

- `check_doc_drift(root: Path, doc_relpath: str, generated: str, regen_cmd: str) -> int` — Compare freshly generated content against a committed doc.

## `forge.git_utils`

- `repo_root() -> Path` — Return the git repo root for the current working directory.
- `configure_cli_logging() -> None` — Apply forge's canonical CLI logging setup.
- `emit(msg: str) -> None` — Write *msg* to stdout with a trailing newline.
- `parse_semver(version: str) -> tuple[int, int, int] | None` — Parse the leading ``X.Y.Z`` (optional ``v`` prefix) of a version string.
- `latest_v_tag(root: Path) -> str | None` — Return the highest ``v*`` git tag by semver sort, or ``None`` if none.
- `require_cli(name: str, *, caller: str | None = None) -> None` — Abort with a clear install hint if *name* isn't on PATH.
- `write_step_log(repo_root: Path, name: str, output: str) -> Path` — Write *output* to ``code_health/<name>.log`` under *repo_root*.
- `capturing_to_step_log(repo_root: Path, name: str) -> Iterator[None]` — Tee root-logger output into ``code_health/<name>.log`` for the block.
- `gh_api(*args: str, timeout: int = 10) -> str | None` — Run ``gh api`` with *args* and return stripped stdout, or ``None``.
- `_run_git(*args: str) -> str` _(internal)_ — Run a git command and return stdout.
- `run_git(*args: str, cwd: Path | None = None, check: bool = True) -> str` — Run ``git`` with *args* in *cwd* and return stripped stdout.
- `get_tree_sha(repo_root: Path, ref: str) -> str | None` — Return the git **tree** SHA of *ref*, or ``None`` when unresolvable.
- `read_plugin_version_at_ref(repo_root: Path, ref: str) -> str | None` — Return ``plugin.json["version"]`` at *ref*, or ``None`` when absent.
- `read_local_plugin_version(repo_root: Path) -> str | None` — Return the working-tree ``.claude-plugin/plugin.json["version"]``.
- `_parse_files(output: str, *, suffix: str, prefix: str | tuple[str, ...] | None) -> list[str]` _(internal)_ — Parse git diff output into a filtered file list.
- `get_modified_files(*, suffix: str = '.py', prefix: str | tuple[str, ...] | None = None) -> list[str]` — Get list of modified files from git.
- `get_tracked_files(*, suffix: str = '.py', prefix: str | tuple[str, ...] | None = None) -> list[str]` — Get all git-tracked files matching the suffix/prefix filters.

## `forge.install_bootstrap`

- `class Step` — One bootstrap step.
- `_gate_skip_in_ci(_root: Path) -> str | None` _(internal)_ — Skip a step when running non-interactively per FOUNDATION §15.
- `_gate_labels(_root: Path) -> str | None` _(internal)_ — Skip ``install-forge-labels`` when ``gh`` or the GitHub remote is missing.
- `_gate_c4(root: Path) -> str | None` _(internal)_ — Skip ``forge-gen-c4`` when no C4 model is configured.
- `_run_step(step: Step, *, check_mode: bool, root: Path) -> int` _(internal)_ — Execute one bootstrap step. Return its exit code.
- `_resolve_steps(skip: Iterable[str]) -> list[Step]` _(internal)_ — Return the ordered step list with *skip* entries removed.
- `main() -> int` — Run every install / generator step in order. Return non-zero on failure.

## `forge.install_claude_settings`

- `_resolve_ref(repo_root: Path, cli_ref: str | None) -> str` _(internal)_ — Resolve the marketplace ref: ``--ref`` → pip-pin ref → ``"main"``.
- `_load_settings(path: Path) -> dict[str, object] | None` _(internal)_ — Return the parsed ``.claude/settings.json``.
- `_is_current(settings: dict[str, object], ref: str) -> bool` _(internal)_ — Return True when the forge marketplace (at *ref*) and plugin enable are set.
- `_merge(settings: dict[str, object], ref: str) -> dict[str, object]` _(internal)_ — Return *settings* with forge's marketplace + plugin enable merged in.
- `main() -> int` — CLI entry point.

## `forge.install_claudemd`

- `_foundation_text() -> str` _(internal)_ — Return the bundled FOUNDATION.md text shipped with the pip package.
- `_forge_version() -> str` _(internal)_ — Return the installed ``forge-scripts`` version, or ``unknown``.
- `_build_foundation_file(*, foundation: str, version: str) -> str` _(internal)_ — Render the full ``FOUNDATION.md`` content including markers.
- `_has_managed_markers(text: str) -> bool` _(internal)_ — Return True if *text* contains a forge-managed START/END pair.
- `_normalize(text: str) -> str` _(internal)_ — Strip the version-stamped comment for drift comparison.
- `sync_foundation(foundation_path: Path, *, check_only: bool = False, force: bool = False) -> bool` — Write or update ``FOUNDATION.md`` with the shipped foundation text.
- `_claudemd_has_include(text: str) -> bool` _(internal)_ — Return True if *text* has an ``@FOUNDATION.md`` include directive.
- `scaffold_claudemd(claudemd_path: Path) -> bool` — Write a minimal scaffold ``CLAUDE.md`` if the file does not exist.
- `scaffold_claude_settings(settings_path: Path) -> bool` — Write a minimal ``.claude/settings.json`` if the file does not exist.
- `ensure_claude_hooks_dir(hooks_dir: Path) -> bool` — Create ``.claude/hooks/`` with a README documenting the path convention.
- `_installed_forge_scripts_version() -> str | None` _(internal)_ — Return the installed ``forge-scripts`` distribution version.
- `_plugin_entry_version(entry: object) -> str | None` _(internal)_ — Pull the ``version`` field out of a single forge@forge entry.
- `_installed_plugin_version(plugins_file: Path) -> str | None` _(internal)_ — Read the installed Claude Code plugin version from the manifest.
- `_read_configured_channel(settings_path: Path) -> str | None` _(internal)_ — Return the marketplace ``ref`` consumers set to track a forge release channel.
- `_upstream_cache_path() -> Path` _(internal)_ — Return the upstream-version-check cache file path.
- `class ChannelTags` — Latest release tag on each of forge's two upstream branches.
- `_read_upstream_cache(cache_path: Path, ttl_hours: int) -> ChannelTags | None` _(internal)_ — Return the cached channel tags if the cache is still fresh.
- `_write_upstream_cache(cache_path: Path, tags: ChannelTags) -> None` _(internal)_ — Persist the channel-tag snapshot + check timestamp.
- `_fetch_upstream_channel_tags() -> ChannelTags` _(internal)_ — Query GitHub for the latest tag on each forge release channel.
- `_is_behind(installed: str | None, latest: str | None) -> bool` _(internal)_ — Return ``True`` when *installed* is strictly older than *latest*.
- `_render_channel_warning(*, installed: str, subject: str, tags: ChannelTags, upgrade_hint_main: str, upgrade_hint_dev: str) -> str | None` _(internal)_ — Build the channel-aware warning text, or ``None`` when not behind.
- `_append_channel_hint(warning: str, settings_file: Path) -> str` _(internal)_ — Append a channel-switch hint to *warning* when a marketplace ref is set.
- `check_upstream(*, plugins_file: Path | None = None, settings_file: Path | None = None, cache_ttl_hours: int = _UPSTREAM_CACHE_TTL_HOURS_DEFAULT, fetch: Callable[[], ChannelTags] = _fetch_upstream_channel_tags) -> None` — Warn (only) when the installed forge is behind either release channel.
- `warn_claudemd_missing_include(claudemd_path: Path) -> None` — Log a warning when ``CLAUDE.md`` lacks the ``@FOUNDATION.md`` include.
- `migrate_inline_block(claudemd_path: Path) -> bool` — Convert a v1.1.2-style inline-block ``CLAUDE.md`` to the split layout.
- `main() -> int` — CLI entry point.

## `forge.install_githooks`

- `_installed_forge_version() -> str` _(internal)_ — Return the installed ``forge-scripts`` version, or ``0.0.0`` if absent.
- `_compute_body_sha(body: str) -> str` _(internal)_ — Return a short hex SHA-256 digest of *body* for marker embedding.
- `managed_marker(body_sha: str | None = None) -> str` — Render the managed-hook marker line.
- `_parse_marker(content: str) -> dict[str, str] | None` _(internal)_ — Extract the managed marker's fields from full hook content.
- `class HookSpec` — A git hook the installer maintains.
- `_hook_content(spec: HookSpec) -> str` _(internal)_ — Render the full file content for *spec*.
- `_is_managed(hook: Path) -> bool` _(internal)_ — Return True if *hook* carries any forge-managed marker.
- `_hook_body_from_content(content: str) -> str` _(internal)_ — Return the body portion of a managed hook file's full content.
- `_wrapper_is_unmodified(hook: Path, spec: HookSpec) -> bool` _(internal)_ — Return True when *hook* still carries the body forge originally wrote.
- `_backup_hook(hook: Path, forge_version: str) -> Path` _(internal)_ — Save the current hook content to a versioned backup file.
- `_write_hook(hook: Path, spec: HookSpec, forge_version: str, *, force: bool, refresh: bool = False) -> bool` _(internal)_ — Write *spec* to *hook*, honoring the wrapper-pattern contract.
- `_set_hooks_path(repo: Path, *, force: bool) -> None` _(internal)_ — Set ``core.hooksPath`` to ``.githooks``.
- `_write_version_sidecar(githooks_dir: Path, forge_version: str) -> None` _(internal)_ — Record the installed forge version in the gitignored sidecar.
- `_ensure_sidecar_gitignored(root: Path) -> None` _(internal)_ — Ensure the version sidecar path is listed in the repo ``.gitignore``.
- `main() -> int` — CLI entry point.

## `forge.install_labels`

- `_existing_labels(repo: str | None) -> set[str]` _(internal)_ — Return set of existing label names in the repo.
- `_create_label(label: dict[str, str], repo: str | None) -> bool` _(internal)_ — Create one label. Returns True on success.
- `main() -> int` — Install canonical foundation labels in the current GitHub repo.

## `forge.install_readme_badges`

- `_shields_static(label: str, message: str, color: str) -> str` _(internal)_ — Build a static shields.io badge image URL.
- `_md(alt: str, image: str, link: str | None = None) -> str` _(internal)_ — Render one markdown badge (optionally wrapped in a link).
- `_git_remote_slug(root: Path) -> str | None` _(internal)_ — Return ``owner/repo`` from the ``origin`` remote, or ``None``.
- `_ci_badge(root: Path, slug: str | None, workflow: str | None) -> str | None` _(internal)_ — Build the GitHub Actions CI badge for the chosen workflow, if any.
- `_python_badge(data: dict) -> str | None` _(internal)_ — Build the Python-version badge from ``requires-python``.
- `_license_badge(data: dict) -> str | None` _(internal)_ — Build the License badge from ``[project].license``.
- `_ruff_badge() -> str` _(internal)_ — Return the static Ruff endpoint badge.
- `_claude_code_badge() -> str` _(internal)_ — Return the static Claude Code badge.
- `_forge_badge(root: Path) -> str` _(internal)_ — Build a forge-channel badge from the ``forge-scripts`` pip pin.
- `_coverage_badge(root: Path) -> str | None` _(internal)_ — Reference the local docstring-coverage SVG when forge has generated it.
- `build_badges(root: Path) -> list[str]` — Assemble the ordered list of markdown badges for this repo.
- `render_block(badges: list[str]) -> str` — Wrap *badges* in the forge-managed marker block.
- `inject(readme: str, block: str) -> str` — Insert or replace the managed badge block in *readme* (drift-aware).
- `_get_readme_path(root: Path) -> tuple[Path | None, int]` _(internal)_ — Load and validate the README path from config.
- `main() -> int` — CLI entry point.

## `forge.next_prep`

- `_check_promote_pending_message(repo_root: Path, dev_branch: str, base_branch: str) -> str | None` _(internal)_ — Return a one-line user-facing prompt when promotion is pending, else ``None``.
- `_changelog_lacks_entry(changelog_text: str, minor_tag: str) -> bool` _(internal)_ — Return True when *changelog_text* has no ``## <minor_tag>`` heading.
- `_promotion_status_lines(repo_root: Path, dev_branch: str, base_branch: str) -> list[str]` _(internal)_ — Build the read-only promotion-status report.
- `_is_newer(plugin_ver: str, latest_tag: str | None) -> bool` _(internal)_ — Return True when ``v<plugin_ver>`` would sort *after* ``latest_tag``.
- `tag_staleness_warning(repo_root: Path) -> str | None` — Return a warning when the integration branch owes a rolling-next tag.
- `_maybe_tag_release(repo_root: Path) -> str | None` _(internal)_ — Tag and push ``v<plugin.json.version>`` when newer than the latest tag.
- `_gone_branches(repo_root: Path) -> list[str]` _(internal)_ — Return local branch names whose tracking remote is ``[origin/...: gone]``.
- `_prune_gone_branches(repo_root: Path) -> tuple[list[str], list[str]]` _(internal)_ — ``git branch -d`` every branch whose remote is gone.
- `_emit_promotion_status(repo_root: Path, dev_branch: str, base_branch: str) -> int` _(internal)_ — Fetch tags and log the read-only promotion-status report.
- `_log_prune_result(repo_root: Path) -> None` _(internal)_ — Prune stale local branches and log the outcome.
- `main() -> int` — Refresh main, optionally tag the release, prune stale local branches.

## `forge.post_checkout`

- `main(argv: list[str] | None = None) -> int` — Run the forge-managed post-checkout actions. Return an exit code.

## `forge.post_merge`

- `main(argv: list[str] | None = None) -> int` — Run the forge-managed post-merge actions. Return an exit code.

## `forge.pr_delta`

- `extract_verified_shas(text: str) -> list[str]` — Return every ``verified-at:`` SHA referenced in *text*.
- `touches_high_blast_radius(changed_paths: list[str]) -> list[str]` — Return the subset of *changed_paths* under :data:`HIGH_BLAST_RADIUS_PATHS`.
- `delta_decision(*, line_count: int, changed_paths: list[str]) -> tuple[bool, str]` — Decide whether a follow-up diff qualifies for delta-mode re-check.

## `forge.pr_squash_comment`

- `class ValidationError` — Raised when the input fails a FOUNDATION §6 squash-merge rule.
- `_validate_title(title: str) -> None` _(internal)_ — Reject titles outside the conventional-commit format.
- `_validate_bullets(bullets: list[str]) -> None` _(internal)_ — Enforce bullet count + non-empty content.
- `_validate_word_count(title: str, bullets: list[str]) -> None` _(internal)_ — Enforce the ≤ ``MAX_WORDS`` cap on title + bullets combined.
- `_validate_no_ai_attribution(title: str, bullets: list[str]) -> None` _(internal)_ — Reject Claude / AI attribution per FOUNDATION §2.
- `build_body(title: str, bullets: list[str]) -> str` — Build the GitHub comment body around a validated message.
- `validate(title: str, bullets: list[str]) -> None` — Run every FOUNDATION §6 check in order.
- `_post_new_comment(pr_number: int, body: str) -> int` _(internal)_ — Post *body* as a new comment on PR ``pr_number``.
- `_patch_existing_comment(comment_id: int, body: str) -> int` _(internal)_ — Rewrite an existing PR comment via the REST API.
- `_current_repo() -> str | None` _(internal)_ — Return ``<owner>/<repo>`` for the current working directory.
- `main() -> int` — Validate the message, build the body, and post (or print) it.

## `forge.precommit`

- `_color(code: str) -> str` _(internal)_ — Return *code* if stdout is a TTY, else an empty string.
- `class StepResult` — Outcome of a single pre-commit step.
- `class StepDef` — A registry entry: a step's name, its function, and whether it runs by default.
- `_forge_step_config(repo_root: Path, step: str) -> dict[str, object]` _(internal)_ — Return the ``[tool.forge.<step>]`` table, or ``{}`` when absent.
- `_resolve_scope(repo_root: Path, step: str) -> str` _(internal)_ — Resolve a step's file-selection scope: per-step override → global → ``"all"``.
- `_run(cmd: list[str], cwd: Path) -> tuple[bool, str]` _(internal)_ — Run *cmd* and capture combined output.
- `step_ruff(repo_root: Path) -> StepResult` — Run ``fix-forge-ruff`` — owns the ruff phase end-to-end.
- `step_docstrings(repo_root: Path) -> StepResult` — Run ``verify-forge-docstrings`` over the resolved scope.
- `step_docstring_coverage(repo_root: Path) -> StepResult` — Run ``verify-forge-docstring-coverage`` — full-codebase % reporter.
- `step_test_naming(repo_root: Path) -> StepResult` — Run ``verify-forge-test-naming`` over the resolved scope.
- `step_repo_structure(repo_root: Path) -> StepResult` — Run ``verify-forge-repo-structure``; hard-fail if missing (FOUNDATION §2).
- `step_manifest_json(repo_root: Path) -> StepResult` — Run ``verify-forge-manifest`` — owns the manifest-JSON validation phase.
- `step_commit_types_parity(repo_root: Path) -> StepResult` — Run ``forge-gen-commit-types --check`` — managed-block parity guard.
- `step_c4(repo_root: Path) -> StepResult` — Run ``forge-gen-c4 --check`` — C4 model + README-block drift guard.
- `_count_pip_audit_advisories(output: str) -> int` _(internal)_ — Count advisory ID occurrences in a ``pip-audit`` text-mode output.
- `step_pip_audit(repo_root: Path) -> StepResult` — Run ``pip-audit --skip-editable`` and report findings as non-blocking.
- `step_cve_usage(repo_root: Path) -> StepResult` — Run ``verify-forge-cve-usage`` — the usage-scoped second stage on pip_audit.
- `step_cli_wiring(repo_root: Path) -> StepResult` — Run ``verify-forge-cli-wiring`` — assert every script has a real caller.
- `_cli_wiring_enabled(repo_root: Path) -> bool` _(internal)_ — Return True when the repo has opted into the cli_wiring check.
- `step_plugin_version(repo_root: Path) -> StepResult` — Run ``verify-forge-plugin-version`` — owns the rolling-next guard.
- `_one_step_successors(tag: tuple[int, int, int]) -> set[tuple[int, int, int]]` _(internal)_ — Return the three valid rolling-next successors of a tagged release.
- `step_release_tag_guard(repo_root: Path) -> StepResult` — Block when an intermediate rolling-next release was never tagged (#66).
- `_cfg_str_list(cfg: dict[str, object], key: str, default: list[str]) -> list[str]` _(internal)_ — Return a ``[tool.forge.*]`` list-valued key narrowed to ``list[str]``.
- `step_doctest(repo_root: Path) -> StepResult` — Run ``pytest --doctest-modules`` over docstring examples (opt-in).
- `step_typecheck(repo_root: Path) -> StepResult` — Run pyrefly over the source tree (opt-in).
- `step_doc_consistency(repo_root: Path) -> StepResult` — Run ``verify-forge-doc-consistency`` — doc claims vs repo state (opt-in).
- `_write_log(repo_root: Path, result: StepResult) -> None` _(internal)_ — Persist *result*'s output to ``code_health/<name>.log``.
- `_print_step_line(result: StepResult) -> None` _(internal)_ — Print a one-line status for *result* (SKIP/PASS/WARN/FAIL).
- `_validate_step_names(names: Sequence[str]) -> None` _(internal)_ — Raise ``ValueError`` listing any *names* that are not registered steps.
- `_resolve_steps(repo_root: Path, *, skip: Sequence[str] = (), only: Sequence[str] = ()) -> list[StepDef]` _(internal)_ — Resolve which steps to run, in registry order.
- `run_all(repo_root: Path | None = None, *, print_progress: bool = True, skip: Sequence[str] = (), only: Sequence[str] = ()) -> list[StepResult]` — Run the resolved step sequence in order and return their results.
- `_split_csv(values: Sequence[str]) -> list[str]` _(internal)_ — Flatten repeatable / comma-separated CLI values into a clean name list.
- `main() -> int` — CLI entry point.

## `forge.run_context`

- `is_non_interactive() -> bool` — Return True when running without a human at the terminal.
- `_stdin_is_tty() -> bool` _(internal)_ — Return ``sys.stdin.isatty()`` defensively (handles closed stdin).
- `git_auth_mode() -> AuthMode` — Detect the git / pip auth context the environment can actually use.
- `_ssh_agent_has_identity() -> bool` _(internal)_ — Return True when ``ssh-add -l`` reports at least one loaded key.
- `progress_logger(step_name: str, *, out: TextIO | None = None) -> Iterator[Callable[[str], None]]` — Yield a flushed printer; emit start / end markers with elapsed time.

## `forge.slow_tests_report`

- `class Duration` — One test-phase timing parsed from a pytest durations section.
- `parse_durations(text: str) -> list[Duration]` — Extract and rank every durations entry in a pytest log.
- `format_report(durations: list[Duration], top: int) -> str` — Render a ranked durations table as plain text.
- `_read_source(log: str) -> str` _(internal)_ — Read the pytest log from a file path or stdin.
- `main() -> int` — Entry point for ``forge-slow-tests-report``.

## `forge.upgrade`

- `_ref_type(value: str) -> str` _(internal)_ — Argparse type validator for ``--to``.
- `class Pin` — A forge-scripts pin parsed from a consumer's ``pyproject.toml``.
- `find_pin(repo_root: Path) -> Pin | None` — Locate the ``forge-scripts`` pin in *repo_root*'s ``pyproject.toml``.
- `_rewrite_pin(pin: Pin, new_ref: str) -> str` _(internal)_ — Return the file content with *pin*'s line rewritten to *new_ref*.
- `_git_url_for(auth_mode: AuthMode, ref: str) -> str` _(internal)_ — Return the ``git+...`` URL pip should resolve for *ref* under *auth_mode*.
- `_pip_command(ref: str, *, auth_mode: AuthMode = 'https-anonymous') -> str` _(internal)_ — Return the exact ``pip install`` line for a given pin ref.
- `_resolve_target_ref_or_none(args: argparse.Namespace, current_ref: str | None) -> str | None` _(internal)_ — Resolve the target ref from CLI flags, falling back to current.
- `_resolve_target_ref(args: argparse.Namespace, current_ref: str | None) -> str` _(internal)_ — Resolve the target ref or exit when undetermined.
- `_write_pyproject_atomic(path: Path, content: str) -> None` _(internal)_ — Replace *path*'s contents with *content*, atomically.
- `_run_phase1(args: argparse.Namespace, root: Path) -> tuple[int, str | None]` _(internal)_ — Phase 1 — detect the pin, rewrite it, print the pip command.
- `_read_changelog() -> str | None` _(internal)_ — Return forge's packaged ``CHANGELOG.md`` text, or ``None`` if unavailable.
- `_consumer_upgrade_notes(changelog_text: str, *, max_versions: int = 3) -> str | None` _(internal)_ — Extract the most recent ``⚠️ Upgrade notes`` lanes from the changelog.
- `_print_upgrade_notes() -> None` _(internal)_ — Surface consumer-action upgrade notes after a successful upgrade.
- `_run_phase2() -> int` _(internal)_ — Phase 2 — run install-forge-bootstrap; print plugin reminder.
- `_run_pip_install(ref: str, *, auth_mode: AuthMode, timeout_seconds: int | None) -> int` _(internal)_ — Run the force-reinstall pip command, wrapped in a progress logger.
- `_run_apply(args: argparse.Namespace, root: Path) -> int` _(internal)_ — ``--apply``: do phase 1 + run pip + do phase 2, in one command.
- `main() -> int` — One-command forge upgrade entry point.

## `forge.verify_cli_wiring`

- `_entry_module_path(entry_point: str) -> str` _(internal)_ — Translate a ``[project.scripts]`` entry point to its source path.
- `_expand_source(root: Path, source: str) -> list[Path]` _(internal)_ — Expand a :data:`WIRING_SOURCES` entry into concrete files.
- `_build_wiring_index(root: Path) -> list[tuple[Path, str]]` _(internal)_ — Read every wiring source once. Returns ``(path, text)`` pairs.
- `_reachable(name: str, self_path: Path, index: list[tuple[Path, str]]) -> list[Path]` _(internal)_ — Return wiring files where *name* appears, excluding *self_path*.
- `_read_exempt(root: Path) -> dict[str, str]` _(internal)_ — Load the optional ``cli_wiring_exempt.toml`` exempt list.
- `_classify_scripts(root: Path, scripts: dict[str, str], exempt: dict[str, str], index: list[tuple[Path, str]]) -> tuple[list[str], list[str]]` _(internal)_ — Classify each script as reachable, exempt, or unreachable.
- `_emit_report(unreachable: list[str], stale_exempt: list[str]) -> None` _(internal)_ — Log findings: unreachable scripts and stale exempt entries.
- `_check_wiring(root: Path) -> int` _(internal)_ — Run the reachability check and report findings.
- `main() -> int` — Entry point for ``verify-forge-cli-wiring``.

## `forge.verify_cve_usage`

- `class Finding` — One matched vulnerable-usage occurrence.
- `load_patterns(root: Path) -> dict[str, dict[str, object]] | None` — Load the consumer's ``cve_usage_patterns.toml`` map.
- `active_cve_ids(root: Path) -> set[str] | None` — Return the advisory / CVE IDs pip-audit currently reports.
- `_iter_source_lines(root: Path) -> Iterable[tuple[str, int, str]]` _(internal)_ — Yield ``(repo_relative_path, line_no, text)`` for every source line.
- `scan(root: Path, patterns: dict[str, dict[str, object]], active: set[str]) -> list[Finding]` — Grep the source for the patterns of every active, mapped CVE.
- `_render(findings: list[Finding]) -> str` _(internal)_ — Render findings as the ``code_health/cve_usage.log`` body.
- `main() -> int` — CLI entry point.

## `forge.verify_doc_consistency`

- `_check_cli_coverage(repo_root: Path) -> list[str]` _(internal)_ — Return findings for ``[project.scripts]`` names missing from the CLI reference.
- `main() -> int` — CLI entry point.

## `forge.verify_docstring_coverage`

- `_interrogate_config(data: dict) -> tuple[InterrogateConfig, float, list[str]]` _(internal)_ — Build the interrogate config + threshold + excludes from TOML data.
- `_badge_enabled(data: dict) -> bool` _(internal)_ — Return True when the consumer opted into badge generation.
- `_write_badge(repo_root: Path, results: object) -> Path` _(internal)_ — Write a coverage SVG badge under ``.badges/`` and return its path.
- `_emit_missing_list(results: object) -> None` _(internal)_ — Print a parseable ``MISSING:`` section listing every undocumented symbol.
- `_scan_paths(repo_root: Path) -> list[str]` _(internal)_ — Resolve the docstring-coverage scan roots via the shared resolver.
- `main() -> int` — CLI entry point for ``verify-forge-docstring-coverage``.

## `forge.verify_docstrings`

- `class Issue` — Represents a docstring issue found during verification.
- `class DocstringVerifier` — AST visitor to verify docstrings match function signatures.
  - `visit_Module(self, node: ast.Module) -> None` — Check module-level docstring.
  - `visit_ClassDef(self, node: ast.ClassDef) -> None` — Check class docstring.
  - `visit_FunctionDef(self, node: ast.FunctionDef) -> None` — Check function/method docstring.
  - `visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None` — Check async function/method docstring.
- `verify_file(filepath: Path) -> list[Issue]` — Verify docstrings in a single file.
- `_group_issues_by_severity(all_issues: list[Issue]) -> tuple[list[Issue], list[Issue], list[Issue]]` _(internal)_ — Split issues into errors, warnings, and info lists.
- `_short_path(full: str, repo_root_str: str) -> str` _(internal)_ — Return *full* relative to repo root (best-effort).
- `_log_at_level(level: int, label: str, issues: list[Issue], repo_root_str: str) -> None` _(internal)_ — Log issues at a given severity level, one per line.
- `_log_warnings_grouped(warnings: list[Issue], repo_root_str: str) -> None` _(internal)_ — Log warnings grouped by file.
- `_log_issues(errors: list[Issue], warnings: list[Issue], infos: list[Issue], repo_root: Path, *, file_count: int) -> None` _(internal)_ — Log categorized issues and print a summary line.
- `main() -> int` — Main entry point for docstring verification.

## `forge.verify_main_tags`

- `class _TagState` _(internal)_ — Where a minor tag currently sits versus where it belongs.
  - `needs_move(self) -> bool` — ``True`` when a base commit reproduces the tag but it sits elsewhere.
- `_short(sha: str | None) -> str` _(internal)_ — Return a 9-char abbreviation of *sha*, or ``(none)`` when absent.
- `_minor_tags(repo_root: Path) -> list[str]` _(internal)_ — Return every ``vX.Y.0`` tag (patch == 0), semver-sorted ascending.
- `_base_tree_index(repo_root: Path, base_ref: str) -> dict[str, str]` _(internal)_ — Map each commit tree SHA on *base_ref* to its commit SHA.
- `_tag_states(repo_root: Path, base_ref: str) -> list[_TagState]` _(internal)_ — Resolve every minor tag's current vs. target commit on *base_ref*.
- `_force_move_tag(repo_root: Path, tag: str, commit_sha: str) -> None` _(internal)_ — Annotated-retag *tag* at *commit_sha* and force-push it.
- `_report_unreproduced(states: list[_TagState], base_ref: str) -> None` _(internal)_ — Warn about minor tags whose tree no base commit reproduces.
- `_verify(states: list[_TagState], base_ref: str) -> int` _(internal)_ — Report drift read-only and return the process exit code.
- `_repair(repo_root: Path, states: list[_TagState], base_ref: str, *, dry_run: bool) -> int` _(internal)_ — Move every misplaced minor tag onto its base commit (or preview).
- `main() -> int` — Verify or repair minor release tags on the base branch.

## `forge.verify_manifest`

- `_parse_json_error(manifest: Path) -> str | None` _(internal)_ — Return a formatted error if *manifest* is invalid JSON, else None.
- `main() -> int` — Validate every ``.claude-plugin/*.json`` file and write the log.

## `forge.verify_plugin_version`

- `_is_release_commit(repo_root: Path) -> bool` _(internal)_ — Return True when ``HEAD``'s tree reproduces ANY published ``v*`` tag.
- `main() -> int` — Enforce plugin.json version > latest git tag.

## `forge.verify_repo_structure`

- `should_ignore(name: str) -> bool` — Check whether a top-level path name should be ignored.
- `_filter_paths(paths: set[str]) -> set[str]` _(internal)_ — Filter out non-filesystem strings from extracted paths.
- `_add_inline_paths(line: str, paths: set[str]) -> None` _(internal)_ — Extract backtick paths and top-level references from a single line.
- `extract_paths_from_markdown(content: str) -> set[str]` — Extract filesystem paths mentioned in REPO_STRUCTURE.md.
- `path_is_covered(path: str, documented_paths: set[str]) -> bool` — Check whether a path is covered by the documented paths.
- `get_actual_top_level(root: Path) -> set[str]` — Get the top-level items that should be documented.
- `verify_documented_paths_exist(documented_paths: set[str], root: Path) -> set[str]` — Find documented paths that do not exist on disk.
- `verify_structure(root: Path, *, verbose: bool = False) -> tuple[set[str], set[str], int]` — Verify REPO_STRUCTURE.md against the actual repository tree.
- `_log_issues(not_found: set[str], not_documented: set[str]) -> None` _(internal)_ — Log details about the drift found.
- `_log_fix_instructions(not_found: set[str], not_documented: set[str]) -> None` _(internal)_ — Log instructions for resolving the detected drift.
- `main() -> int` — Verify REPO_STRUCTURE.md is in sync with the repository tree.

## `forge.verify_test_naming`

- `class Issue` — Represents a test naming issue found during verification.
- `class TestNamingVerifier` — AST visitor to verify test naming standards.
  - `visit_FunctionDef(self, node: ast.FunctionDef) -> None` — Check function definition for naming issues.
  - `visit_ClassDef(self, node: ast.ClassDef) -> None` — Visit class definition and track class context.
  - `visit_Assign(self, node: ast.Assign) -> None` — Check module-level variable assignments for naming conventions.
- `verify_file(filepath: Path) -> list[Issue]` — Verify test naming standards in a single file.
- `_check_file_name_alignment(filepath: Path) -> list[Issue]` _(internal)_ — Verify the ``test_`` prefix on test file names (Rule 2).
- `_check_duplicate_file_names(all_files: list[Path]) -> list[Issue]` _(internal)_ — Check for duplicate or ambiguous file names.
- `_resolve_test_files(repo_root: Path, target: str | None, scope: str) -> list[str]` _(internal)_ — Return repo-relative test file paths from CLI arg, scope, or git diff.
- `_scan_files(py_files: list[str], repo_root: Path) -> tuple[list[Issue], list[str], int]` _(internal)_ — Verify each file, plus a cross-file duplicate-name check.
- `_log_warnings(warnings: list[Issue]) -> None` _(internal)_ — Print warnings grouped by file.
- `_report(files_scanned: int, all_issues: list[Issue], files_with_issues: list[str]) -> None` _(internal)_ — Print the verification summary.
- `main() -> int` — Main entry point for test naming verification.
