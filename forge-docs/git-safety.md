# Git safety

Procedure behind [`FOUNDATION.md` §2](../FOUNDATION.md#2-core-safety-rules) git rules: gate kinds, guard bypass, destructive forms, base sync. Rules live in §2; this page is how.

## Gate kinds

Two kinds, not equally strong. Know which one you are at.

- **Technical.** Human running `! <command>` is unreachable by an agent.
  **No bypass** hooks (`block_git_rebase`, `block_amend_pushed_commit`,
  `block_git_destructive`, `block_branch_deletion`) rest on this; their
  `! …` form is the human acting, not an escape granted to the agent.
- **Policy + audit trail.** `FORGE_EMERGENCY_ACK=1` is an ordinary env var
  an agent could set itself. Held by policy plus a trail the agent cannot
  suppress: `forge-emergency` files a public ledger issue before the
  sentinel exists.

Never treat second kind as first. A bare variable with no ledger behind it
is no gate — a suggestion.

Not a gate kind: **automated host-classifier refusal.** The harness's own
automated permission check (Claude Code's auto-mode classifier) can refuse
a command no forge guard blocks. It is not forge's to enforce and not the
user's to run by hand: retry the same command once, or the safer form the
refusal suggests — only an allowed command that reaches no forge-guarded
effect. Still refused → report the refusal verbatim and ask the user how to
proceed. Never prescribe `! <command>` or a settings change for it — the
`!` handoff belongs to the technical kind above.

Never retried:

- **A user's denial** (the user declined a tool prompt). That is the user's
  word: stop and ask. Unsure whether the user or the classifier refused →
  treat it as the user's denial.
- **A forge guard block** — any refusal from a forge hook (`block_*`) or a
  forge CLI gate (`forge-pr-create`, `forge-pr-wrapup post`,
  `forge-commit`, …). It is enforcement of forge's rules: stop and report
  (§2 "On deviation").

This list is not exhaustive: a refusal that fits no kind here is reported,
not classified.

## Bypassing a guard

- Never on agent judgment. Never reach blocked effect another way: writing
  file a gate demanded, regenerating what a check refused, *is* bypassing.
- User asks → confirm **once**, naming protection dropped and for what,
  then act. Confirmation buys attribution (cheapest proof a human was
  present), not friction — friction teaches routing around controls
  (how `--no-verify` becomes habit).
- Record every use: what was bypassed, why, that a human authorised it.
- Recurring bypass = evidence guard is wrong or too costly. Never grounds
  to loosen rule.

## Destructive git forms

Authority: `claude-hooks/block_git_destructive.sh` (shipped in the forge plugin)
(header comment + code). Summary below; hook wins on any disagreement.
Enforced with **no bypass**; human runs blocked form via `! git ...`.

Blocked:

- `git reset` — ANY form.
- Forced `git clean` (`-f` / `-d` / `-x` / `-X` / `--force`). Only verb
  with no recovery at all. Dry run (`git clean -n`) allowed.
- `git checkout .` / `git restore .` (discard-everything).
- Restore of a path holding uncommitted work: `git checkout [<ref>] -- <path>`
  / `git restore <path>` over modified, staged or untracked file, or over
  paths guard cannot determine.
- Forced switch: `git checkout -f`, `git switch -f` / `--discard-changes`.
- Branch reset dropping commits: `git checkout -B` / `git switch -C` onto
  existing branch whose tip start point does not contain.
- `git stash` beyond `list` / `show`.
- Any of these verbs after a variable in command-word slot (`$G reset`):
  hook cannot tell what runs, so every form blocks.

Allowed:

- Reset under a new branch name, after `git branch -m` frees old one, or
  when it only fast-forwards.
- Unstage: `git restore --staged <path>`.
- Restoring clean paths, plain branch switch, conflict resolution
  (`--ours` / `--theirs`).

Why: rewinds un-commit published history on a synced branch; rest destroy
uncommitted or untracked work — in a checkout other sessions share, maybe
not yours.

## Remote branch checks

- **Before any force push** (user-approved only): check divergence with
  `git fetch origin && git log origin/<branch>`. `block_force_push` covers
  `--force` / `--force-with-lease`, the `-f` / `-uf` cluster, and
  `+`-prefixed refspecs.
- **Is a branch protected?** Check BOTH
  `gh api repos/{owner}/{repo}/rules/branches/{branch}` and
  `.../rulesets` — never conclude "unprotected" from a 404 on the legacy
  `/branches/{branch}/protection` endpoint alone (FOUNDATION §1 "Absence of
  evidence").


## Base sync

One command: `forge-update-branch` (`--base <parent>` for a stacked
branch, `--no-push` to keep it local). Never rebase, never stash, never
`git reset --hard`.

1. Refuses on the protected branch or a detached HEAD; fetches the base;
   already up to date → exit 0.
2. Dirty tree → a `wip-sync:` checkpoint commit first (the whole tree,
   untracked included; gate deferred to the next real commit; the PR
   squash erases it). `forge-commit --wip-sync -m "wip-sync: <what>"` is
   the same checkpoint by hand.
3. Merges without committing, then resolves what is mechanical:
   - **files the branch already holds** — the base's version appears in
     this branch's own history, so the base brings nothing new and the
     branch side wins;
   - **generated artifacts** — regenerated from the merged tree once no
     source conflicts remain, never textually merged;
   - **the version slot** — `forge-rebump`'s resolution.
4. All resolved → commits the merge (no local gate, like a merge git
   completes itself: CI and the next commit check it) and pushes.
5. Otherwise exit 3: the merge stays in progress, the paths are listed,
   an intake lesson records the stop. `forge:merge-resolver` edits the
   files (or the main agent does), then `forge-update-branch --continue`
   stages marker-free files, re-runs step 3, refuses any staged change
   outside the conflicted files, and commits — through the pre-commit
   gate when a file was edited by hand. The main agent adds why the
   conflict needed judgment to the intake lesson; a recurring class is
   filed as an issue.

`forge-commit` refuses while a merge is in progress and names
`--continue`: a merge is mostly reviewed base work, which no list of the
caller's own files describes.

**Stacked PRs conflict on every parent landing — expected, not a
mistake.** Forge squash-merges, and a squash commit shares no ancestry
with the parent branch, so when the child syncs, git cannot tell the
child already holds the parent's changes, and every file the child
changed after the parent conflicts. The base side there is exactly the
parent's final version, already in the child's history, so step 3
resolves it; only files the base changed beyond the parent reach a
person.

Notes:

- `FORGE_MERGE_SYNC` (the merge commit's gate deferral) is set only by
  the command and honoured only mid-merge; `check_commit_format` refuses
  it typed. Humans: never `export FORGE_WIP_SYNC` in a persistent shell —
  it silently defers the gate on every later commit.
- `git merge --abort` permitted **only after** a checkpoint secured work
  (git documents it lossy for uncommitted changes).
- Stranded-changelog recovery mid-branch: `forge-update-branch` first,
  then `forge-changelog restrand`.
