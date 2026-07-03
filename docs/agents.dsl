workspace "Forge Agent Fleet" "The delegation graph of forge's foundation subagents" {

    model {
        developer = person "Developer" ""
        pr_manager = person "pr-manager" ""
        design_checker = person "design-checker" ""
        security_checker = person "security-checker" ""
        docs_types_checker = person "docs-types-checker" ""
        precommit_fixer = person "precommit-fixer" ""
        git_commit_push = person "git-commit-push" ""
        test_advisor = person "test-advisor" ""
        test_writer = person "test-writer" ""
        forge_agent_fleet = softwareSystem "Forge Agent Fleet" "The delegation graph of forge's foundation subagents" {
        }
        forge_precommit = softwareSystem "forge-precommit" "quality gate"
        forge_pr_squash_comment = softwareSystem "forge-pr-squash-comment" "squash-message CLI"

        # relationships
        developer -> forge_agent_fleet "runs /commit, /pr, /test"
        pr_manager -> forge_agent_fleet "orchestrates PR finalization"
        design_checker -> forge_agent_fleet "uses"
        security_checker -> forge_agent_fleet "uses"
        docs_types_checker -> forge_agent_fleet "uses"
        precommit_fixer -> forge_agent_fleet "uses"
        git_commit_push -> forge_agent_fleet "uses"
        test_advisor -> forge_agent_fleet "uses"
        test_writer -> forge_agent_fleet "uses"
        forge_agent_fleet -> forge_precommit "uses"
        forge_agent_fleet -> forge_pr_squash_comment "uses"
        developer -> pr_manager "triggers"
        pr_manager -> design_checker "delegates"
        pr_manager -> security_checker "delegates"
        pr_manager -> docs_types_checker "delegates"
        pr_manager -> precommit_fixer "delegates"
        precommit_fixer -> docs_types_checker "delegates"
        precommit_fixer -> design_checker "delegates"
        precommit_fixer -> git_commit_push "hands off"
        test_advisor -> test_writer "delegates"
        test_writer -> test_advisor "review"
        precommit_fixer -> forge_precommit "invokes"
        pr_manager -> forge_pr_squash_comment "invokes"
    }

    views {
        systemContext forge_agent_fleet "SystemContext" {
            include *
            autolayout tb
        }
        container forge_agent_fleet "Containers" {
            include *
            autolayout tb
        }
        theme default
    }
}
