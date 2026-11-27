workspace "Forge" "Python CI/CD & code-quality foundation: deterministic CLIs + pre-commit hook + optional Claude Code plugin" {

    model {
        forge_developer = person "Forge developer" "Maintains forge and runs its pre-commit gate"
        consumer_repo = person "Consumer repo" "A repo that adopts forge's governance layer"
        forge = softwareSystem "Forge" "Python CI/CD & code-quality foundation: deterministic CLIs + pre-commit hook + optional Claude Code plugin" {
            forge_scripts = container "forge-scripts" "Every forge CLI + the pre-commit dispatcher" "Python pip package" {
                pre_commit_dispatcher = component "Pre-commit dispatcher" "The single quality gate — runs each step by shelling out, reading back exit code + code_health/<step>.log" "Python"
                audit_suite = component "Audit suite" "Deeper code-health audits: dependency graph, duplication, dead code, doc-claim checks" "Python (AST)"
                verifiers = component "Verifiers" "Single-responsibility pre-commit checks: docstrings, naming, repo structure, wiring, CVE usage" "Python (AST)"
                installers = component "Installers" "Install/refresh git hooks, CLAUDE.md, labels, README badges, and Claude settings; bootstrap umbrella" "Python + gh"
                doc_generators = component "Doc generators" "Generate drift-checked docs: API digest, CLI reference, config tables, this C4 model; verify the agent-architecture doc" "Python"
                git_hook_entrypoints = component "Git-hook entrypoints" "Managed post-merge / post-checkout hooks: foundation drift check + backgrounded self-refresh" "Python"
                release_tooling = component "Release tooling" "Rolling-next versioning, changelog fragments, tag-per-merge releases, install-surface and CI tree-gate checks" "Python + git/gh"
                smart_test = component "Smart-test" "Change-driven test selection by import depth, with per-run resource profiling: git diff + reverse import graph (+ opt-in mock-patch edges / coverage union) run in depth-tiered pytest batches" "Python (ast) + git/pytest"
                config_shared = component "Config + shared" "Shared foundation: pyproject/[tool.forge] config, git + logging utils, CI run-context, ruff/doctor, single-scan pip-audit helper" "Python"
                pr_workflow = component "PR workflow" "Publishes and tends a PR: create, wrap-up compose and post, squash message, review comments, watch, evidence and plan freshness" "Python + gh"
                guarded_vcs_surface = component "Guarded VCS surface" "The version-control surface agents are routed through: commit, base-branch sync, checkpoint recovery" "Python"
                backlog_planning = component "Backlog + planning" "GitHub-native backlog: index curation, intake of sightings, plan readiness, drift and overlap checks, tracks" "Python + gh"
                harness_integration = component "Harness integration" "Turns the shipped agents, skills and guard wiring into each harness's generated form" "Python"
                cost_metrics = component "Cost metrics" "What a session and a PR cost: agent session profiling, token accounting, and the PR metrics and charts built on them" "Python"
                emergency_bypass = component "Emergency bypass" "Human-armed ceremony bypass: what was skipped, who armed it, and what verification is still owed afterwards" "Python"
                continuation = component "Continuation" "State between sessions: the generated status panel, the written hand-off below it, and the agent-memory audit count" "Python"
            }
        }
        github = softwareSystem "GitHub" "Hosts repos, PRs, issues, labels, releases"

        # relationships
        forge_developer -> forge "develops, commits, runs CLIs"
        consumer_repo -> forge "installs + invokes forge CLIs/hooks"
        forge -> github "reads/writes via gh"
        pre_commit_dispatcher -> verifiers "runs each verifier via subprocess"
        pre_commit_dispatcher -> audit_suite "runs audit steps via subprocess"
        pre_commit_dispatcher -> doc_generators "runs --check drift steps"
        installers -> doc_generators "bootstrap runs generators"
        audit_suite -> config_shared "imports"
        backlog_planning -> config_shared "imports"
        backlog_planning -> pr_workflow "imports"
        backlog_planning -> release_tooling "imports"
        config_shared -> harness_integration "imports"
        config_shared -> release_tooling "imports"
        config_shared -> smart_test "imports"
        continuation -> config_shared "imports"
        continuation -> pr_workflow "imports"
        continuation -> release_tooling "imports"
        cost_metrics -> config_shared "imports"
        cost_metrics -> doc_generators "imports"
        cost_metrics -> harness_integration "imports"
        cost_metrics -> release_tooling "imports"
        doc_generators -> audit_suite "imports"
        doc_generators -> config_shared "imports"
        doc_generators -> pr_workflow "imports"
        emergency_bypass -> config_shared "imports"
        emergency_bypass -> pr_workflow "imports"
        git_hook_entrypoints -> config_shared "imports"
        guarded_vcs_surface -> backlog_planning "imports"
        guarded_vcs_surface -> config_shared "imports"
        guarded_vcs_surface -> cost_metrics "imports"
        guarded_vcs_surface -> doc_generators "imports"
        guarded_vcs_surface -> emergency_bypass "imports"
        guarded_vcs_surface -> installers "imports"
        guarded_vcs_surface -> pr_workflow "imports"
        guarded_vcs_surface -> pre_commit_dispatcher "imports"
        guarded_vcs_surface -> release_tooling "imports"
        harness_integration -> config_shared "imports"
        harness_integration -> cost_metrics "imports"
        harness_integration -> installers "imports"
        installers -> config_shared "imports"
        installers -> harness_integration "imports"
        installers -> release_tooling "imports"
        pr_workflow -> audit_suite "imports"
        pr_workflow -> backlog_planning "imports"
        pr_workflow -> config_shared "imports"
        pr_workflow -> continuation "imports"
        pr_workflow -> emergency_bypass "imports"
        pr_workflow -> pre_commit_dispatcher "imports"
        pr_workflow -> release_tooling "imports"
        pre_commit_dispatcher -> backlog_planning "imports"
        pre_commit_dispatcher -> config_shared "imports"
        pre_commit_dispatcher -> continuation "imports"
        pre_commit_dispatcher -> emergency_bypass "imports"
        pre_commit_dispatcher -> installers "imports"
        pre_commit_dispatcher -> pr_workflow "imports"
        pre_commit_dispatcher -> release_tooling "imports"
        release_tooling -> config_shared "imports"
        release_tooling -> doc_generators "imports"
        release_tooling -> installers "imports"
        smart_test -> config_shared "imports"
        smart_test -> pr_workflow "imports"
        verifiers -> audit_suite "imports"
        verifiers -> config_shared "imports"
        verifiers -> harness_integration "imports"
        verifiers -> pr_workflow "imports"
    }

    views {
        systemContext forge "SystemContext" {
            include *
            autolayout tb
        }
        container forge "Containers" {
            include *
            autolayout tb
        }
        component forge_scripts "forge-scripts Components" {
            include *
            autolayout tb
        }
        theme default
    }
}
