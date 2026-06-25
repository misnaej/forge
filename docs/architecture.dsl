workspace "Forge" "Python CI/CD & code-quality foundation: deterministic CLIs + pre-commit hook + optional Claude Code plugin" {

    model {
        forge_developer = person "Forge developer" "Maintains forge and runs its pre-commit gate"
        consumer_repo = person "Consumer repo" "A repo that adopts forge's governance layer"
        forge = softwareSystem "Forge" "Python CI/CD & code-quality foundation: deterministic CLIs + pre-commit hook + optional Claude Code plugin" {
            forge_scripts = container "forge-scripts" "Every forge CLI + the pre-commit dispatcher" "Python pip package" {
                pre_commit_dispatcher = component "Pre-commit dispatcher" "The single quality gate — runs each step by shelling out, reading back exit code + code_health/<step>.log" "Python"
                audit_suite = component "Audit suite" "Deeper code-health audits: dependency graph, duplication, dead code, doc-claim checks" "Python (AST)"
                verifiers = component "Verifiers" "Single-responsibility pre-commit checks: docstrings, naming, repo structure, manifest, wiring, CVE usage" "Python (AST)"
                installers = component "Installers" "Install/refresh git hooks, CLAUDE.md, labels, README badges, and Claude settings; bootstrap umbrella" "Python + gh"
                doc_generators = component "Doc generators" "Generate drift-checked docs: API digest, CLI reference, and this C4 model" "Python"
                git_hook_entrypoints = component "Git-hook entrypoints" "Managed post-merge / post-checkout hooks: foundation drift check + backgrounded self-refresh" "Python"
                release_tooling = component "Release tooling" "Rolling-next versioning, dev→main promotion, PR squash messages, continuation log" "Python + git/gh"
                config_shared = component "Config + shared" "Shared foundation: pyproject/[tool.forge] config, git + logging utils, CI run-context, ruff/doctor, single-scan pip-audit helper" "Python"
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
        doc_generators -> audit_suite "imports"
        doc_generators -> config_shared "imports"
        doc_generators -> release_tooling "imports"
        git_hook_entrypoints -> config_shared "imports"
        git_hook_entrypoints -> release_tooling "imports"
        installers -> config_shared "imports"
        installers -> release_tooling "imports"
        pre_commit_dispatcher -> config_shared "imports"
        release_tooling -> config_shared "imports"
        release_tooling -> installers "imports"
        verifiers -> config_shared "imports"
    }

    views {
        systemContext forge "SystemContext" {
            include *
            autolayout lr
        }
        container forge "Containers" {
            include *
            autolayout lr
        }
        component forge_scripts "forge-scripts Components" {
            include *
            autolayout lr
        }
        theme default
    }
}
