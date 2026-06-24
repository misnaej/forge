workspace "Forge" "Python CI/CD & code-quality foundation: deterministic CLIs + pre-commit hook + optional Claude Code plugin" {

    model {
        forge_developer = person "Forge developer" "Maintains forge and runs its pre-commit gate"
        consumer_repo = person "Consumer repo" "A repo that adopts forge's governance layer"
        forge = softwareSystem "Forge" "Python CI/CD & code-quality foundation: deterministic CLIs + pre-commit hook + optional Claude Code plugin" {
            forge_scripts = container "forge-scripts" "Every forge CLI + the pre-commit dispatcher" "Python pip package" {
                pre_commit_dispatcher = component "Pre-commit dispatcher" "forge.precommit"
                audit_suite = component "Audit suite" "forge.audit"
                verifiers = component "Verifiers" "forge.verify_docstrings, forge.verify_docstring_coverage, forge.verify_manifest, forge.verify_plugin_version, forge.verify_repo_structure, forge.verify_test_naming, forge.verify_cli_wiring, forge.verify_doc_consistency, forge.verify_cve_usage, forge.verify_main_tags"
                installers = component "Installers" "forge.install_bootstrap, forge.install_githooks, forge.install_claudemd, forge.install_claude_settings, forge.install_labels, forge.install_readme_badges, forge.claude_settings_schema"
                doc_generators = component "Doc generators" "forge.gen_api_digest, forge.gen_cli_reference, forge.gen_c4, forge.gen_commit_types, forge.gen_common"
                git_hook_entrypoints = component "Git-hook entrypoints" "forge.post_merge, forge.post_checkout, forge._hook_helpers"
                release_tooling = component "Release tooling" "forge.next_prep, forge.upgrade, forge.pr_squash_comment, forge.pr_delta, forge.continuation_append"
                config_shared = component "Config + shared" "forge.config, forge.forge_config, forge.git_utils, forge.run_context, forge.fix_ruff, forge.doctor, forge.slow_tests_report"
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
        audit_suite -> config_shared "uses"
        doc_generators -> audit_suite "uses"
        doc_generators -> config_shared "uses"
        doc_generators -> release_tooling "uses"
        git_hook_entrypoints -> config_shared "uses"
        git_hook_entrypoints -> release_tooling "uses"
        installers -> config_shared "uses"
        installers -> release_tooling "uses"
        pre_commit_dispatcher -> config_shared "uses"
        release_tooling -> config_shared "uses"
        release_tooling -> installers "uses"
        verifiers -> config_shared "uses"
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
        component forge_scripts "Components" {
            include *
            autolayout lr
        }
        theme default
    }
}
