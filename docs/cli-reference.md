# CLI Reference

Forge's console-script CLIs are its real public surface. This page documents each CLI's command-line interface, captured from its `--help` output.

> **Generated file — do not edit by hand.** Regenerate with `forge-gen-cli-reference`; check for drift with `forge-gen-cli-reference --check`.

## fix-forge-ruff

```text
usage: fix-forge-ruff [-h] [--scope {all,diff}] [dirs ...]

Run `ruff format` + `ruff check --fix --unsafe-fixes` in-place, re-stage
modified tracked files, and write code_health/ruff.log.

positional arguments:
  dirs                Source dirs to fix. If empty, resolve from
                      [tool.forge].source_dirs / smart auto-detect.

options:
  -h, --help          show this help message and exit
  --scope {all,diff}  'all' (whole source tree, the default) or 'diff' (only
                      files modified vs main). 'diff' ignores positional dirs.
```

## forge-audit-agents

```text
usage: forge-audit-agents [-h] [--scope {full,changed}] [--roots [ROOTS ...]]
                          [--output OUTPUT]

Measure forge agents against the canonical template in agents/_TEMPLATE.md.
Non-blocking initially; promoted after Layer 3 trim PRs.

options:
  -h, --help            show this help message and exit
  --scope {full,changed}
                        Audit scope. 'full' scans roots; 'changed' scans
                        modified files vs main.
  --roots [ROOTS ...]   Source dirs to scan when --scope=full. Auto-detected
                        if omitted.
  --output OUTPUT       Override log path. Defaults to
                        code_health/audit_<name>.log.
```

## forge-audit-all

```text
usage: forge-audit-all [-h] [--scope {full,changed}] [--roots [ROOTS ...]]
                       [--only [{suppressions,agents,dup,deps,orphans,data,claims} ...]]
                       [--output OUTPUT]

Run every forge-audit-* script and aggregate results.

options:
  -h, --help            show this help message and exit
  --scope {full,changed}
  --roots [ROOTS ...]
  --only [{suppressions,agents,dup,deps,orphans,data,claims} ...]
                        Run only these sub-audits (default: all).
  --output OUTPUT       Override summary log path (default:
                        code_health/audit_summary.log).
```

## forge-audit-claims

```text
usage: forge-audit-claims [-h] [--scope {full,changed}] [--roots [ROOTS ...]]
                          [--output OUTPUT] [--no-default-lexicon]

Extract domain claims from docstrings/comments for verification.

options:
  -h, --help            show this help message and exit
  --scope {full,changed}
                        Audit scope. 'full' scans roots; 'changed' scans
                        modified files vs main.
  --roots [ROOTS ...]   Source dirs to scan when --scope=full. Auto-detected
                        if omitted.
  --output OUTPUT       Override log path. Defaults to
                        code_health/audit_<name>.log.
  --no-default-lexicon  Disable the built-in lexicon (use only forge-audit-
                        claims.toml).
```

## forge-audit-data

```text
usage: forge-audit-data [-h] [--scope {full,changed}] [--roots [ROOTS ...]]
                        [--output OUTPUT]

Structured-data integrity (CSV alignment + parse checks).

options:
  -h, --help            show this help message and exit
  --scope {full,changed}
                        Audit scope. 'full' scans roots; 'changed' scans
                        modified files vs main.
  --roots [ROOTS ...]   Source dirs to scan when --scope=full. Auto-detected
                        if omitted.
  --output OUTPUT       Override log path. Defaults to
                        code_health/audit_<name>.log.
```

## forge-audit-deps

```text
usage: forge-audit-deps [-h] [--scope {full,changed}] [--roots [ROOTS ...]]
                        [--output OUTPUT]
                        [--distance-threshold DISTANCE_THRESHOLD] [--tree]

Module dependency analysis (cycles + Martin I/A/D metrics). Also renders a
readable dependency tree to code_health/audit_deps_tree.log.

options:
  -h, --help            show this help message and exit
  --scope {full,changed}
                        Audit scope. 'full' scans roots; 'changed' scans
                        modified files vs main.
  --roots [ROOTS ...]   Source dirs to scan when --scope=full. Auto-detected
                        if omitted.
  --output OUTPUT       Override log path. Defaults to
                        code_health/audit_<name>.log.
  --distance-threshold DISTANCE_THRESHOLD
                        Report modules with main-sequence distance above this
                        value (default: 0.7).
  --tree                Print the rendered dependency tree to stdout. The tree
                        is always written to code_health/audit_deps_tree.log
                        regardless of this flag.
```

## forge-audit-dup

```text
usage: forge-audit-dup [-h] [--scope {full,changed}] [--roots [ROOTS ...]]
                       [--output OUTPUT] [--min-tokens MIN_TOKENS]
                       [--jaccard-threshold JACCARD_THRESHOLD]
                       [--shingle-size SHINGLE_SIZE]

Detect duplicate / near-duplicate / name-colliding functions.

options:
  -h, --help            show this help message and exit
  --scope {full,changed}
                        Audit scope. 'full' scans roots; 'changed' scans
                        modified files vs main.
  --roots [ROOTS ...]   Source dirs to scan when --scope=full. Auto-detected
                        if omitted.
  --output OUTPUT       Override log path. Defaults to
                        code_health/audit_<name>.log.
  --min-tokens MIN_TOKENS
                        Skip functions with fewer normalized tokens (default:
                        30).
  --jaccard-threshold JACCARD_THRESHOLD
                        Minimum Jaccard similarity for near-duplicate report
                        (default: 0.85).
  --shingle-size SHINGLE_SIZE
                        K-gram shingle window (default: 5).
```

## forge-audit-orphans

```text
usage: forge-audit-orphans [-h] [--scope {full,changed}] [--roots [ROOTS ...]]
                           [--output OUTPUT] [--min-confidence MIN_CONFIDENCE]

Detect unused code via vulture (>= min-confidence).

options:
  -h, --help            show this help message and exit
  --scope {full,changed}
                        Audit scope. 'full' scans roots; 'changed' scans
                        modified files vs main.
  --roots [ROOTS ...]   Source dirs to scan when --scope=full. Auto-detected
                        if omitted.
  --output OUTPUT       Override log path. Defaults to
                        code_health/audit_<name>.log.
  --min-confidence MIN_CONFIDENCE
                        Minimum vulture confidence (0-100) to report (default:
                        80).
```

## forge-audit-suppressions

```text
usage: forge-audit-suppressions [-h] [--scope {full,changed}]
                                [--roots [ROOTS ...]] [--output OUTPUT]

List lint/type/coverage suppressions and resolve rule names.

options:
  -h, --help            show this help message and exit
  --scope {full,changed}
                        Audit scope. 'full' scans roots; 'changed' scans
                        modified files vs main.
  --roots [ROOTS ...]   Source dirs to scan when --scope=full. Auto-detected
                        if omitted.
  --output OUTPUT       Override log path. Defaults to
                        code_health/audit_<name>.log.
```

## forge-check-main-tags

```text
usage: forge-check-main-tags [-h] [--fix] [--dry-run]

Verify (default) or repair (--fix) that every minor release tag vX.Y.0 sits on
the base branch's squash commit, matched by tree equality. Self-skips single-
branch repos.

options:
  -h, --help  show this help message and exit
  --fix       Force-move every misplaced minor tag onto its base commit.
  --dry-run   Print the moves --fix would make; mutate nothing.
```

## forge-config

```text
usage: forge-config [-h] [--list]

List the [tool.forge.*] config forge reads in this repo, with current values /
defaults, the native tool sections forge reads, and advice on recommended-but-
unset keys.

options:
  -h, --help  show this help message and exit
  --list      List forge config + advice (the default action).
```

## forge-continuation-append

```text
usage: forge-continuation-append [-h] (--commit HASH | --pr NUMBER |
                                 --merge HASH)
                                 subject

Append one line to .plan/CONTINUATION.md's auto-appended activity section.
Single source of truth for the format used by forge:git-commit-push and
forge:pr-manager.

positional arguments:
  subject        Subject line — commit subject, PR title, or merge subject.

options:
  -h, --help     show this help message and exit
  --commit HASH  Record a commit. HASH is the short SHA.
  --pr NUMBER    Record a PR wrap-up. NUMBER is the PR number (no leading #).
  --merge HASH   Record a PR merge on main. HASH is the short SHA.
```

## forge-doctor

```text
usage: forge-doctor [-h] [--json] [--plugin-name PLUGIN_NAME]
                    [--skip-plugin-checks]

Validate a forge install in the current environment.

options:
  -h, --help            show this help message and exit
  --json                Emit JSON instead of human-readable output.
  --plugin-name PLUGIN_NAME
                        Claude Code plugin name to check (default: forge). The
                        plugin checks self-skip if no install is found, so
                        consumers who don't use Claude Code can ignore this
                        flag.
  --skip-plugin-checks  Skip all Claude Code plugin checks entirely. Useful
                        for consumers who only adopt the pip CLIs.
```

## forge-gen-api-digest

```text
usage: forge-gen-api-digest [-h] [--roots [ROOTS ...]] [--check]

Generate docs/api-digest.md indexing top-level functions and classes (public
API and internal helpers).

options:
  -h, --help           show this help message and exit
  --roots [ROOTS ...]  Source dirs to scan. Auto-detected (src/ or packages)
                       if omitted.
  --check              Verify docs/api-digest.md is in sync; do not write.
```

## forge-gen-c4

```text
usage: forge-gen-c4 [-h] [--format {dsl,html,mermaid}] [--roots [ROOTS ...]]
                    [--check] [--output OUTPUT]

Generate a C4 architecture model from the import graph + a [tool.forge.c4] /
c4.toml model. Emits Structurizr DSL (default) or a self-contained offline
HTML view.

options:
  -h, --help            show this help message and exit
  --format {dsl,html,mermaid}
                        Output: 'dsl' (Structurizr + README block, default),
                        'html' (offline view), or 'mermaid' (raw Mermaid to
                        stdout).
  --roots [ROOTS ...]   Source dirs to scan. Defaults to the repo's configured
                        source roots.
  --check               Verify the committed artifact is in sync; do not
                        write.
  --output OUTPUT       Override the output path. Use '-' to write to stdout.
```

## forge-gen-cli-reference

```text
usage: forge-gen-cli-reference [-h] [--check]

Generate docs/cli-reference.md from forge CLI --help output.

options:
  -h, --help  show this help message and exit
  --check     Verify docs/cli-reference.md is in sync; do not write.
```

## forge-gen-commit-types

```text
usage: forge-gen-commit-types [-h] [--check]

Regenerate the conventional-commit alternation in claude-
hooks/check_commit_format.sh from the canonical CONVENTIONAL_COMMIT_TYPES
tuple in forge.pr_squash_comment.

options:
  -h, --help  show this help message and exit
  --check     Verify the managed block matches the canonical alternation
              without writing. Exit 1 on drift.
```

## forge-next-prep

```text
usage: forge-next-prep [-h] [--tag] [--no-prune-branches] [--promotion-status]
                       [--target {dev,base}]

Prepare main for the next task: fetch + pull --ff-only, optionally tag the
rolling-next release, prune stale local branches. Used by the /next skill.

options:
  -h, --help           show this help message and exit
  --tag                Tag plugin.json's version when it's ahead of the latest
                       v* tag and push the tag (forge's rolling-next
                       workflow). Off by default.
  --no-prune-branches  Skip the stale-branch prune step.
  --promotion-status   Read-only: fetch tags, then print the base/dev plugin
                       versions and the ordered list of v* releases pending
                       promotion, and exit. No checkout, pull, tag, or prune.
                       Used by the /promote skill.
  --target {dev,base}  Branch to refresh. Resolved through [tool.forge] in
                       pyproject.toml; falls back to 'main' if the config is
                       absent. Most repos can leave this at the default.
```

## forge-post-checkout

```text
usage: forge-post-checkout [-h] [prev_head] [new_head] [branch_flag]

Forge-managed post-checkout git-hook entrypoint. Invoked by the thin
.githooks/post-checkout wrapper. Runs the foundation drift check only when the
HEAD actually moved (branch_flag == '1'). No-ops in non-interactive contexts
(FOUNDATION §15).

positional arguments:
  prev_head    prior HEAD (passed by git)
  new_head     new HEAD (passed by git)
  branch_flag  '1' for branch-changing checkouts; '0' for file-level checkouts

options:
  -h, --help   show this help message and exit
```

## forge-post-merge

```text
usage: forge-post-merge [-h] [squash_flag]

Forge-managed post-merge git-hook entrypoint. Invoked by the thin
.githooks/post-merge wrapper. Runs the foundation drift check and backgrounds
a self-refresh of managed hook wrappers. No-ops in non-interactive contexts
(FOUNDATION §15).

positional arguments:
  squash_flag  squash-merge status flag passed by git (1=squash, 0=otherwise);
               ignored

options:
  -h, --help   show this help message and exit
```

## forge-pr-squash-comment

```text
usage: forge-pr-squash-comment [-h] (--pr PR | --patch COMMENT_ID | --dry-run)
                               --title TITLE [--bullet TEXT]

Validate, fence-wrap, and post a squash-merge message as a PR comment.
Replaces hand-built heredoc templates in pr-manager. Rules per FOUNDATION §6.

options:
  -h, --help          show this help message and exit
  --pr PR             PR number to comment on (creates a new comment).
  --patch COMMENT_ID  Rewrite an existing comment instead of posting a new
                      one.
  --dry-run           Print the wrapped body to stdout; do not call gh.
  --title TITLE       Squash title (conventional-commit format).
  --bullet TEXT       Bullet line. Repeat 3-5 times.
```

## forge-precommit

```text
usage: forge-precommit [-h] [--json] [--skip STEP[,STEP...]]
                       [--only STEP[,STEP...]]

Run the forge pre-commit check sequence: ruff (format + check, self-healing
with --unsafe-fixes on failure) + docstring verification (diff vs main) +
test-name verification (diff vs main) + repo-structure verification
(REPO_STRUCTURE.md vs the tree) + plugin manifest JSON + plugin version drift
guard + pip-audit (non-blocking) — when applicable. Ruff fixes apply
automatically on every run; fixed files are re-staged. Pytest is not in the
default sequence — run it in CI or wire it into .githooks/pre-commit
explicitly. Used by any repo that adopts forge via install-forge-githooks.

options:
  -h, --help            show this help message and exit
  --json                Emit a JSON summary on stdout instead of human output.
  --skip STEP[,STEP...]
                        Force-skip these steps for this run (repeatable or
                        comma-separated).
  --only STEP[,STEP...]
                        Run exactly these steps (repeatable or comma-
                        separated).
```

## forge-slow-tests-report

```text
usage: forge-slow-tests-report [-h] [--log LOG] [--top TOP] [--out OUT]

Parse pytest --durations sections from a log (or stdin) and print the slowest
tests, merged across all batches.

options:
  -h, --help  show this help message and exit
  --log LOG   Path to the pytest log to parse, or '-' for stdin (default:
              code_health/pytest.log).
  --top TOP   Number of slowest tests to show (default: 25).
  --out OUT   Also write the report to this file (e.g.
              code_health/slow_tests.log).
```

## forge-upgrade

```text
usage: forge-upgrade [-h] [--channel {main,dev} | --to REF] [--continue]
                     [--check] [--apply] [--pip-timeout SECONDS]

Two-phase forge upgrade. Phase 1 (default): rewrite the forge-scripts pin in
pyproject.toml + print the exact pip command. Phase 2 (--continue): after
running pip, re-sync managed artifacts via install-forge-bootstrap.

options:
  -h, --help            show this help message and exit
  --channel {main,dev}  Pin to a channel — `main` (slow, minor-only) or `dev`
                        (every patch).
  --to REF              Pin to a specific git ref (e.g. `v1.3.0`).
  --continue            Phase 2: run install-forge-bootstrap to re-sync
                        managed artifacts. Use after the phase-1 pip command
                        has been run.
  --check               Dry-run: print what would change without rewriting the
                        pin.
  --apply               One-shot: rewrite the pin + run pip install --force-
                        reinstall + re-sync managed artifacts. For human-run
                        setup scripts only — Claude Code agents are blocked
                        from this flag by block_install_deps (FOUNDATION §2).
  --pip-timeout SECONDS
                        Wall-clock cap on the pip subprocess during --apply.
                        Default: no timeout interactively, 600s in CI
                        (detected via FORGE_NON_INTERACTIVE / CI / stdin TTY).
                        Returns exit 124 on timeout (matches GNU timeout(1)).
```

## install-forge-bootstrap

```text
usage: install-forge-bootstrap [-h] [--check] [--skip SLUG] [--strict]

One-shot consumer onboarding to forge's full capability set. Runs every
install-forge-* installer + every forge-gen-* / forge-audit-* generator in
dependency order. Idempotent.

options:
  -h, --help   show this help message and exit
  --check      Dry-run. Each step that supports --check runs in check mode;
               others just print their intent.
  --skip SLUG  Skip a step by slug. Repeatable. Known slugs: githooks, claude-
               md, claude-settings, labels, readme-badges, api-digest, cli-
               reference, c4, audit-deps, doctor, config.
  --strict     Abort on the first failed step. Default is continue-on-fail.
```

## install-forge-claude-md

```text
usage: install-forge-claude-md [-h] [--check] [--quiet] [--migrate] [--force]

Sync the forge foundation into this repo. Writes/updates FOUNDATION.md
(managed by forge); scaffolds CLAUDE.md with an `@FOUNDATION.md` include if it
doesn't exist; creates `.claude/hooks/` with a README documenting the
`${CLAUDE_PROJECT_DIR}/.claude/hooks/<name>.sh` path convention; writes a
minimal `.claude/settings.json` if missing. Existing consumer-owned files are
never touched.

options:
  -h, --help  show this help message and exit
  --check     Exit non-zero if FOUNDATION.md drifts from the installed forge
              version. Also warns if CLAUDE.md exists without the
              `@FOUNDATION.md` include.
  --quiet     Suppress 'already in sync' info logs (intended for git hooks).
  --migrate   Convert a v1.1.2-or-earlier inline-block CLAUDE.md to the split
              layout (FOUNDATION.md + @FOUNDATION.md include).
  --force     Overwrite an existing FOUNDATION.md that lacks the forge-managed
              markers. Use sparingly.
```

## install-forge-claude-settings

```text
usage: install-forge-claude-settings [-h] [--ref REF] [--check]

Enable the forge Claude Code plugin in this repo by writing the marketplace +
enabledPlugins block to .claude/settings.json (per-repo, never global).
Idempotent and merge-preserving.

options:
  -h, --help  show this help message and exit
  --ref REF   Marketplace ref (branch/tag) to pin. Defaults to the forge-
              scripts pip-pin ref in pyproject.toml, else 'main'.
  --check     Verify the block is present without writing (exit 1 on drift).
```

## install-forge-githooks

```text
usage: install-forge-githooks [-h] [--force] [--refresh] [--quiet]

Install forge's managed git hooks (pre-commit, post-merge, post-checkout) and
set core.hooksPath. Idempotent. Use --force to overwrite user-customized hooks
or an existing core.hooksPath value.

options:
  -h, --help  show this help message and exit
  --force     Overwrite user-customized hook files and any existing
              non-.githooks core.hooksPath value.
  --refresh   Rewrite managed hook files unconditionally (used by the post-
              merge auto-refresh to pick up a new forge version). Does not
              override user-customized hooks — pair with --force for that.
  --quiet     Suppress INFO logs (used by the post-merge auto-refresh).
```

## install-forge-labels

```text
usage: install-forge-labels [-h] [--repo REPO]

Install Forge canonical labels.

options:
  -h, --help   show this help message and exit
  --repo REPO  OWNER/REPO (defaults to current dir's remote)
```

## install-forge-readme-badges

```text
usage: install-forge-readme-badges [-h] [--check]

Write a drift-aware status-badge block into the README. Opt-in via
[tool.forge.badges] enabled = true.

options:
  -h, --help  show this help message and exit
  --check     Verify the block is current without writing (exit 1 on drift).
```

## verify-forge-cli-wiring

```text
usage: verify-forge-cli-wiring [-h]

Verify every [project.scripts] entry in pyproject.toml is reachable from at
least one wiring source path (install-forge-bootstrap STEPS, forge.precommit
steps, audit/, git hooks, claude-hooks, dev/, agents/, skills/) or is listed
in cli_wiring_exempt.toml with a reason.

options:
  -h, --help  show this help message and exit
```

## verify-forge-cve-usage

```text
usage: verify-forge-cve-usage [-h]

Second-stage CVE filter: report only CVEs whose vulnerable code path is
actually used. Reads cve_usage_patterns.toml; skips cleanly when absent or
pip-audit is unavailable.

options:
  -h, --help  show this help message and exit
```

## verify-forge-doc-consistency

```text
usage: verify-forge-doc-consistency [-h]

Check that every [project.scripts] CLI is documented in docs/cli-reference.md.
Non-blocking reporter for the doc_consistency pre-commit step.

options:
  -h, --help  show this help message and exit
```

## verify-forge-docstring-coverage

```text
usage: verify-forge-docstring-coverage [-h]

Measure docstring coverage with interrogate. Reads [tool.interrogate] for the
gate and [tool.forge.docstring_coverage].badge for SVG output. Writes
code_health/docstring_coverage.log.

options:
  -h, --help  show this help message and exit
```

## verify-forge-docstrings

```text
usage: verify-forge-docstrings [-h] [--scope {all,diff}] [target]

Verify docstring accuracy against actual code signatures.

positional arguments:
  target              Optional file path to check. Overrides --scope.

options:
  -h, --help          show this help message and exit
  --scope {all,diff}  'all' (every tracked .py file, the default) or 'diff'
                      (files modified vs main). Ignored when a target path is
                      given.
```

## verify-forge-manifest

```text
usage: verify-forge-manifest [-h]

Validate that every .claude-plugin/*.json file parses as JSON. Writes
code_health/manifest_json.log.

options:
  -h, --help  show this help message and exit
```

## verify-forge-plugin-version

```text
usage: verify-forge-plugin-version [-h]

Assert .claude-plugin/plugin.json['version'] is strictly greater than the
latest git tag. Writes code_health/plugin_version.log.

options:
  -h, --help  show this help message and exit
```

## verify-forge-repo-structure

```text
usage: verify-forge-repo-structure [-h] [--verbose]

Verify REPO_STRUCTURE.md is in sync with actual structure.

options:
  -h, --help     show this help message and exit
  --verbose, -v  Show all extracted paths.
```

## verify-forge-test-naming

```text
usage: verify-forge-test-naming [-h] [--scope {all,diff}] [target]

Verify test naming standards on auto-detected or given files.

positional arguments:
  target              Optional test file to check. Overrides --scope.

options:
  -h, --help          show this help message and exit
  --scope {all,diff}  'all' (every tracked test file, the default) or 'diff'
                      (test files modified vs main). Ignored when a target is
                      given.
```
