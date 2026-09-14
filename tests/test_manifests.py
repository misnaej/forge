"""Static integrity tests for forge's own plugin/marketplace manifests + layout."""

from __future__ import annotations

import json
from pathlib import Path

from forge import precommit, run_context


REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_DIR = REPO_ROOT / ".claude-plugin"


def test_plugin_json_present_and_well_formed() -> None:
    """`.claude-plugin/plugin.json` exists and parses, with required fields."""
    path = MANIFEST_DIR / "plugin.json"
    assert path.is_file()
    data = json.loads(path.read_text())
    for field in ("name", "version", "description", "author", "license"):
        assert field in data, f"plugin.json missing {field}"
    assert data["name"] == "forge"
    assert data["license"] == "MIT"


def test_plugin_json_inline_hooks() -> None:
    """plugin.json declares hooks inline (required by Claude Code 2.1.x)."""
    data = json.loads((MANIFEST_DIR / "plugin.json").read_text())
    assert "hooks" in data, (
        "plugin.json should declare hooks inline, not via hooks.json"
    )
    assert "PreToolUse" in data["hooks"]


def test_marketplace_json_present_and_well_formed() -> None:
    """`.claude-plugin/marketplace.json` exists and parses."""
    path = MANIFEST_DIR / "marketplace.json"
    assert path.is_file()
    data = json.loads(path.read_text())
    assert data["name"] == "forge"
    assert "plugins" in data
    assert len(data["plugins"]) >= 1


def test_marketplace_plugin_source_format() -> None:
    """marketplace.json plugin entry uses the supported `"./"` source format."""
    data = json.loads((MANIFEST_DIR / "marketplace.json").read_text())
    forge_plugin = next(p for p in data["plugins"] if p["name"] == "forge")
    assert forge_plugin["source"] == "./"


def test_no_top_level_hooks_json() -> None:
    """hooks.json at repo root is deprecated; hooks must live in plugin.json."""
    assert not (REPO_ROOT / "hooks.json").exists(), (
        "hooks.json was removed; hooks now declared inline in plugin.json"
    )


def test_expected_plugin_dirs_present() -> None:
    """Plugin ships agents/, skills/, claude-hooks/ with content."""
    for sub in ("agents", "skills", "claude-hooks"):
        d = REPO_ROOT / sub
        assert d.is_dir(), f"{sub}/ missing"
        assert any(d.iterdir()), f"{sub}/ is empty"


def test_all_shipped_skills_user_invocable() -> None:
    """Every shipped skill under skills/ declares `user-invocable: true`.

    A skill missing this frontmatter key is silently untypeable as a slash
    command even though it ships to consumers, so every entry is asserted
    directly against the real tree rather than a hand-maintained list.
    """
    skill_files = sorted((REPO_ROOT / "skills").glob("*/SKILL.md"))
    assert skill_files, "expected skills/*/SKILL.md files, glob returned none"

    for path in skill_files:
        lines = path.read_text().splitlines()
        assert lines[:1] == ["---"], f"{path}: missing opening `---` frontmatter fence"

        close_idx = next(
            (i for i, line in enumerate(lines[1:], start=1) if line == "---"),
            None,
        )
        assert close_idx is not None, f"{path}: frontmatter fence never closes"

        frontmatter = {}
        for line in lines[1:close_idx]:
            if ":" not in line:
                continue
            key, _, value = line.partition(":")
            frontmatter[key.strip()] = value.strip()

        assert frontmatter.get("user-invocable") == "true", (
            f"{path}: expected `user-invocable: true`, "
            f"got {frontmatter.get('user-invocable')!r}"
        )


def test_pyproject_entry_points_declared() -> None:
    """pyproject.toml declares all forge CLI entry points."""
    pyproject = (REPO_ROOT / "pyproject.toml").read_text()
    for cli in (
        "forge-precommit",
        "fix-forge-ruff",
        "verify-forge-docstrings",
        "install-forge-labels",
        "forge-doctor",
    ):
        assert cli in pyproject, f"pyproject missing entry point for {cli}"


def test_license_file_exists() -> None:
    """MIT LICENSE file is present."""
    license_file = REPO_ROOT / "LICENSE"
    assert license_file.is_file()
    contents = license_file.read_text()
    assert "MIT License" in contents
    assert "Jean Simonnet" in contents


def test_strict_mode_agent_command_forces_cve_scan() -> None:
    """`precommit-fixer`'s strict-mode command carries the force flag.

    SCENARIO: the CVE scan runs once per branch, so strict mode escalating
    "remaining advisories" would escalate a stale answer unless its own
    invocation forces a rescan. The agent doc is what the agent executes,
    so the claim and the command have to be the same string — this pins
    them together across a rename of either.

    EXPECTED BEHAVIOR: the doc contains the env var precommit reads,
    prefixed onto a single `forge-precommit` invocation.
    """
    body = (REPO_ROOT / "agents" / "precommit-fixer.md").read_text()
    forced = f"{precommit._PIP_AUDIT_FORCE_ENV}=1 forge-precommit"
    assert forced in body, f"strict-mode Phase 1 must run `{forced}`"


def test_git_commit_push_agent_carries_changelog_contract() -> None:
    """The shipped commit agent is forbidden from authoring fragments.

    SCENARIO: `forge:git-commit-push` was observed inventing a
    `changelog.d/` fragment to clear the `changelog_updated` gate,
    picking slug, type and `bump:` level — and with them the released
    version — inside a commit told to stage two files. The contract that
    stops a repeat only works if it ships, and the agent doc is what the
    agent reads.

    EXPECTED BEHAVIOR: the shipped body marks `changelog.d/` REPORT ONLY,
    forbids every write verb, and hands ownership to the PR author.
    """
    body = (REPO_ROOT / "agents" / "git-commit-push.md").read_text()
    unwrapped = " ".join(body.split())
    assert "**`changelog.d/` — REPORT ONLY.**" in unwrapped
    assert "you never author, edit, rename, or delete one" in unwrapped
    assert "The fragment is the PR author's." in unwrapped


def test_issue_triage_agent_never_signs_off_a_validated_plan() -> None:
    """The triage agent refuses to author a human sign-off claim.

    SCENARIO: `issue-triage` is the sole writer of the `[issue-triage]
    plan-validated:` comment that `/sentinel` treats as authorization to
    implement an issue unattended. A human attribution line ("validated
    by <name>") is the one claim in that payload nothing can corroborate
    — the agent posting it cannot establish who validated the plan, so
    the line would manufacture exactly the trust the gate depends on.
    The `I WILL NOT` list is template structure (`agents/_TEMPLATE.md`),
    so pinning the bullet survives rewording and fails only if it is
    deleted or defanged; `strip` is the obligation verb separating
    "don't add one" from "remove one you were handed" — the half a
    rewrite loses silently.

    EXPECTED BEHAVIOR: the boundary bullet naming `plan-validated` also
    names the sign-off, and the workflow body states both the claim it
    refuses to write and the duty to strip an inherited one.
    """
    body = (REPO_ROOT / "agents" / "issue-triage.md").read_text()

    boundary_heading = "### I WILL NOT (report and stop)"
    assert boundary_heading in body, f"missing `{boundary_heading}` section"
    boundary_start = body.index(boundary_heading)
    boundary_end = body.index("\n## ", boundary_start)
    boundary = body[boundary_start:boundary_end]

    bullets = [" ".join(chunk.split()) for chunk in boundary.split("\n- ")[1:]]
    signing = [bullet for bullet in bullets if "plan-validated" in bullet]
    assert signing, "no `I WILL NOT` bullet mentions `plan-validated`"
    for bullet in signing:
        assert "sign-off" in bullet, (
            f"`plan-validated` boundary bullet dropped the sign-off: {bullet}"
        )

    workflow_start = body.index("\n## Workflow\n")
    workflow_end = body.index("\n## ", workflow_start + 1)
    workflow = " ".join(body[workflow_start:workflow_end].split())
    assert "sign-off claim" in workflow, (
        "workflow must name the unverifiable `sign-off claim` it refuses"
    )
    assert "strip" in workflow, (
        "workflow must oblige stripping a sign-off from an inherited plan"
    )


def test_plan_batch_skill_self_skips_when_non_interactive() -> None:
    """`/plan-batch` gates its fan-out on the real run-context probe.

    SCENARIO: the skill fans out up to three billed drafting agents whose
    entire output requires a human to validate before anything acts on
    it, so firing that from automation bills for drafts nobody is present
    to accept. FOUNDATION §15 makes that a run-context decision rather
    than a default, which is why the probe has to be named.
    Asserting the imported symbol rather than a prose sentence catches
    two regressions with one pair: the self-skip clause being dropped
    from the skill, and a `run_context` rename leaving the skill pointing
    at a function that no longer exists.

    EXPECTED BEHAVIOR: the shipped skill names the guard under the name
    `forge.run_context` actually exports — the attribute access below
    is itself the rename check, raising before the assertion runs.
    """
    guard = run_context.is_non_interactive
    skill = (REPO_ROOT / "skills" / "plan-batch" / "SKILL.md").read_text()
    assert guard.__name__ in skill, (
        f"plan-batch must self-skip on `{guard.__name__}()` (FOUNDATION §15)"
    )


def test_plan_batch_delegation_target_exists() -> None:
    """The section `/plan-batch` delegates into is still present.

    SCENARIO: `plan-batch`'s dispatch prompt sends each drafter to
    `/plan-issue` draft-only mode, and the whole drafter contract — what
    to run, what to return, what not to touch — lives in that one
    section. `verify-forge-agent-doc`'s dangling-reference check resolves
    a `/skill-name` mention to a skill DIRECTORY and never to a section
    inside it, so deleting or renaming the heading breaks every fan-out
    silently with no gate catching it. A `##` heading is the most
    rename-resistant token a prose file offers.

    EXPECTED BEHAVIOR: `plan-issue` still publishes the heading, and
    `plan-batch` still routes drafters to that mode.
    """
    heading = "## Draft-only mode"
    plan_issue = (REPO_ROOT / "skills" / "plan-issue" / "SKILL.md").read_text()
    assert heading in plan_issue, (
        f"plan-issue lost `{heading}` — plan-batch's fan-out has no contract"
    )

    plan_batch = (REPO_ROOT / "skills" / "plan-batch" / "SKILL.md").read_text()
    assert "draft-only" in plan_batch, (
        "plan-batch must delegate to /plan-issue draft-only mode"
    )


def test_advisory_screen_variant_is_defined_by_its_owner() -> None:
    """The `advisory` token two skills rely on is documented where it is honoured.

    SCENARIO: `/sentinel` and `/plan-batch` both request a no-mutation
    `plan-readiness` run by naming `advisory`, having dropped the
    hand-maintained skip lists that used to spell the suppression out.
    That trade only holds while the owning agent defines the word: a
    bare token whose definition is deleted fails open, because an
    unrecognised mode falls through to the default run that rewrites
    the Backlog Index and applies labels. Nothing else detects that —
    `verify-forge-agent-doc` resolves agent and skill names, never a
    mode named inside one.

    EXPECTED BEHAVIOR: both callers name the variant, and the agent
    that serves it defines the same word.
    """
    triage = (REPO_ROOT / "agents" / "issue-triage.md").read_text()
    assert "`advisory` mode" in triage, (
        "issue-triage must define the advisory variant its callers request"
    )

    for skill_name in ("sentinel", "plan-batch"):
        skill = (REPO_ROOT / "skills" / skill_name / "SKILL.md").read_text()
        assert "advisory" in skill, f"/{skill_name} must name the advisory variant"


def test_planning_is_gated_on_contributor_authorship() -> None:
    """Every surface of the plannability gate still carries it.

    SCENARIO: anyone can open an issue, and a validated plan is what
    turns issue text into work `/sentinel` performs unattended — so an
    issue is plannable only if a collaborator authored it or endorsed
    it with the literal `[endorsed]` marker. The rule spans FOUNDATION,
    the screening agent and the interactive planner, and a rewrite that
    drops any one of them leaves the other two describing a gate that
    no longer closes. The marker is the stable part: it is what makes
    the check mechanical rather than a judgment about approving prose.

    EXPECTED BEHAVIOR: FOUNDATION states the rule and the marker, and
    both enforcing surfaces name the marker too.
    """
    foundation = " ".join((REPO_ROOT / "FOUNDATION.md").read_text().split())
    assert "Only a contributor's issue is plannable" in foundation
    assert "`[endorsed]`" in foundation, "FOUNDATION must name the literal marker"
    assert "authorAssociation" in foundation, (
        "FOUNDATION must warn off GitHub's weaker authorAssociation field"
    )

    for path in (
        REPO_ROOT / "agents" / "issue-triage.md",
        REPO_ROOT / "skills" / "plan-issue" / "SKILL.md",
    ):
        assert "[endorsed]" in path.read_text(), f"{path.name} dropped the gate"
