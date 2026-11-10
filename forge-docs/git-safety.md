# Git safety

Procedure behind [`FOUNDATION.md` §2](../FOUNDATION.md#2-core-safety-rules) git rules: gate kinds, guard bypass, destructive forms, dirty-tree sync ladder. Rules live in §2; this page is how.

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
  word: stop and ask.
- **A forge guard block** — any refusal naming a `block_*` hook. It is
  enforcement of forge's rules: stop and report (§2 "On deviation").

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

## Sync ladder

Sanctioned dirty-tree base sync. Never `git reset --hard`, never stash
(stash is in no sanctioned procedure).

1. **Probe**: `git merge-tree --write-tree origin/<base> HEAD`. Does real
   merge, touches neither tree nor index. Judge by **exit status**
   (0 clean / 1 conflicts), never output emptiness.
2. **Probe clean and index clean** → plain `git merge origin/<base>`. Git's
   own dirty-overlap guard is backstop.
3. **Otherwise** secure work as **checkpoint commit** first:
   `git add -A`, then `FORGE_WIP_SYNC=1 git commit -m "wip-sync: <what>"`.
   Gate defers to next real commit; PR squash erases checkpoint. Then merge
   on clean tree.

Notes:

- Merge git completes itself commits through `pre-merge-commit`, a hook
  forge does not install → no local gate, by design: CI runs
  `forge-precommit` on every PR and push, next commit's gate checks merged
  state, check logs are tree-stamped so `--freshness` and PR evidence read
  pre-merge logs as stale.
- Conflicted merge → resolve, stage, then `forge-commit -m "<msg>" <paths>`
  naming every staged path (`git diff --cached --name-only`); gated as
  usual.
- `git merge --abort` permitted **only after** a checkpoint secured work
  (git documents it lossy for uncommitted changes).
- Humans: never `export FORGE_WIP_SYNC` in a persistent shell — silently
  defers gate on every later commit. Use inline, once.
- Who runs it: main agent, never `forge-commit`; a `wip-sync:`
  checkpoint (env `FORGE_WIP_SYNC=1`) is the one commit whose gate
  legitimately defers.
- Stranded-changelog recovery mid-branch: `git merge origin/<base>` via
  this ladder first, then `forge-changelog restrand`.
