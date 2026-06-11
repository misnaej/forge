# Canonical agent shape

**Not an agent.** Underscore-prefix excludes this file from Claude Code's
plugin auto-discovery. Forge agents copy this structure; the
`forge-audit-agents` CLI flags drift.

See [FOUNDATION §11](../FOUNDATION.md#11-agent-boundary-protocol) for
the policy this template implements.

## Ownership model

- **FOUNDATION** owns policy / numbers / principles. Authoritative
  source for pip-only consumers. Auto-loaded into every Claude Code
  session via the consumer's `CLAUDE.md`.
- **Agents** own enforcement protocol / review cookbook / investigation
  recipes. Each agent OWNS the *how*; FOUNDATION owns the *what*.
- Neither side duplicates the other. Both link.

## Required frontmatter

| Field | Required | Notes |
|---|---|---|
| `name` | yes | Routing key. Lowercase-hyphen. Matches filename minus `.md`. |
| `description` | yes | One sentence. **Routing trigger** ("Use proactively when X" / "Use immediately after Y") — not a role label ("Agent for X"). |
| `tools` | yes | Least-privilege list. See "Tool sets per role" below. |
| `model` | yes | `haiku` / `sonnet` / `opus` / `inherit`. See "Model per role". |

## Required body sections

In this order:

1. `# <Agent Display Name>` — H1.
2. One short paragraph: role + contract (reporter vs actor).
3. `## Source of truth` — single short pointer to FOUNDATION §N.
4. `## Workflow` — numbered protocol. Domain-specific recipes follow as
   their own H2.
5. `## Scope Boundaries` with `### I WILL` and `### I WILL NOT (report and stop)`.
6. `## Output` — exact emitted shape.
7. `## Success Criteria` — measurable done conditions.

## Length budget

- **Target**: 400–800 words body (incl. frontmatter, excl. code fences).
- **Anthropic reference**: 150–400 words. Forge gets headroom for
  repo-specific protocols but stays ≤ 2× the reference.
- **Hard cap**: 1500 words. Above → `forge-audit-agents` flags MAJOR.
- **MINOR threshold**: 800 words. Audit flags as trim candidate.

## Tool sets per role

| Role | Examples | Tools |
|---|---|---|
| **Reporter** | design-checker, security-checker, knowledge-search | `Read`, `Grep`, `Glob`, **read-only `Bash`** (+ `WebFetch` / `WebSearch` if grounding, + `Task` if delegating). Read-only `Bash` is whitelisted for: `git rev-parse` (SHA capture), `git diff` (PR diff read), `git branch` (current branch), `gh pr view --json` (PR-context capture), `forge-audit-*` runs (log refresh). No writes, no commits, no `gh pr comment`. |
| **Reporter-with-artifact** | docs-types-checker (fixes docstrings), weekly-summary (writes summary file) | Reporter tools + the *single* mutating tool the artifact requires (`Edit` for in-place doc fixes, `Write` for a dedicated file). The artifact MUST be the agent's only reason for mutation. |
| **Actor / fixer** | precommit-fixer | `Bash`, `Read`, `Edit`, `Grep`, `Glob` |
| **Orchestrator** | pr-manager, issue-triage | Same as actor + `Task` |
| **Commit/push** | git-commit-push | `Bash`, `Read` only |

Pure Reporters MUST NOT have `Write` or `Edit`. Reporter-with-artifact
agents are the documented exception — `forge-audit-agents` exempts
them by name via the `_REPORTER_WITH_ARTIFACT_NAMES` constant in the
forge package (single source of truth — do not duplicate the list
here).

## Model per role

| Workload | Model |
|---|---|
| Read-only search / lookup | `haiku` |
| Code / doc review (analysis) | `sonnet` |
| Multi-step reasoning + hallucination risk | `opus` |
| Delegated workhorse, match parent context | `inherit` |

## Reporter-agent header contract

Every reporter agent (pure Reporter or Reporter-with-artifact) MUST
emit, as the **first body line** of its report, a single
machine-parseable header:

```
verified-at: <short-sha>   (PR #<num>, branch <branch-name>)
```

Capture snippet (this is the canonical body; reporters reference it,
do not restate):

```bash
sha=$(git rev-parse --short HEAD)
branch=$(git branch --show-current)
pr=$(gh pr view --json number --jq '.number' 2>/dev/null || echo "?")
```

Emit the SHA value the agent computed at the moment of producing its
findings. The `PR #` + branch suffix is human-readable context.

`forge:pr-manager` parses this line on subsequent runs: when every
prior reporter's `verified-at` SHA is reachable from current HEAD AND
the diff since is below the delta threshold (the threshold and the
high-blast-radius path list are constants in the forge package —
single source of truth), the orchestrator skips re-invocation and
posts a short delta comment instead of a full wrap-up. Reporters
that omit the header force a full re-run on every follow-up commit.

`forge-audit-agents` greps each reporter for the `verified-at:`
substring. Missing it fails the audit step.

## Forbidden patterns

- **Restating FOUNDATION rules inline.** Subagents auto-load CLAUDE.md
  and the full memory hierarchy ([code.claude.com/docs/en/sub-agents](https://code.claude.com/docs/en/sub-agents))
  — you already receive FOUNDATION for free. Link to it; do not copy.
- **Same prohibition restated 2+ times** in the same agent.
- **`description` shaped as a role label.** "Agent for X" / "X agent
  that does Y" are wrong. Trigger-shaped is right: "Use proactively
  when X" / "Use immediately after Y".
- **Output shape inconsistent with `## Output` section** of this
  template.
- **Per-rule cookbook duplicated across agents.** If two agents enforce
  related rules, share via FOUNDATION § cross-reference, not by copying
  the cookbook into both.

## Approved cross-reference syntax

The right link target depends on whether the agent ships in *this* repo
(forge itself) or in a consumer repo that adopts forge:

| Target | Forge-internal agent | Consumer-repo agent |
|---|---|---|
| Policy / numbers / principles | `→ see [FOUNDATION §N](../FOUNDATION.md#N-section-anchor)` | `→ see [CLAUDE.md §N](../CLAUDE.md#N-section-anchor)` (the consumer's `CLAUDE.md` `@`-includes `FOUNDATION.md`, so the pointer resolves through it; consumer-specific rules live in `CLAUDE.md` directly) |
| Another agent | `→ delegate to `forge:agent-name`` | `→ delegate to `forge:agent-name`` (foundation agent) OR `→ delegate to `agent-name`` (consumer-shipped wrapper) |
| A CLI | backticked command name (e.g. `forge-precommit`) | same |
| A hook | backticked filename (e.g. `claude-hooks/block_raw_git.sh` for forge hooks; `.claude/hooks/<name>.sh` for consumer-specific hooks) | same |

**Rule of thumb**: never reference forge-internal paths from a consumer
agent (their reader can't follow `../FOUNDATION.md`). Always cite via the
consumer's `CLAUDE.md`, which `@`-includes the foundation.

## Canonical skeleton

```markdown
---
name: <agent-name>
description: <one-sentence routing trigger>
tools:
  - <tool>
  - <tool>
model: <haiku | sonnet | opus | inherit>
---

# <Agent Display Name>

<One paragraph: role + contract (report-only vs mutating).>

## Source of truth

See [FOUNDATION §<N>](../FOUNDATION.md#<N-anchor>). Do not restate
rules inline.

## Workflow

1. <step>
2. <step>
3. <step>

## Scope Boundaries

### I WILL
- <action>
- <action>

### I WILL NOT (report and stop)
- <action> → **Use `<other-agent>`**
- <action> → **Use `<cli>`**

## Output

```
<exact emitted shape — markdown report OR `AGENT-NAME COMPLETE` block>
```

## Success Criteria

- <measurable done condition>
- <measurable done condition>
```

## Audit enforcement

`forge-audit-agents` (in `forge-audit-all`) measures every agent against
this template. Per-agent findings land in `code_health/audit_agents.log`.
Initially non-blocking; promoted to blocking after the Layer 3 trim PRs
converge.

Run standalone:

```bash
forge-audit-agents
forge-audit-all --only agents
```
