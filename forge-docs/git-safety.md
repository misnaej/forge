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

**Where a guard thinks a command starts decides what it can see.** The
shared anchors in `claude-hooks/git_anchor.sh` start one at line start or
after `;`, `&`, `|`, `(` or `)`. `)` counts because a `case` arm begins its
command there — `case x in x) git push -f …` — and wrapping a command that
way once hid it from every guard at the same time.

That separator set is **not** in one place, and a change to it must grep
for the copies: five hooks (`block_raw_ruff`, `block_raw_git`,
`block_continuation_delete`, `block_unverified_pr_create`,
`block_no_verify`) spell the same class by hand, and the `guard_chunks`
callers (`block_fixer_recon`, `block_install_deps`, `block_no_verify`,
`block_git_destructive`) each pass their own charset, chosen for what is
safe to split in that hook. `block_raw_ruff` is the one that also cuts its
run out of `GIT_ANCHOR`, and it refuses every command when that cut stops
matching — which is how a divergence announces itself rather than going
quiet.

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

## Rebase forms

Authority: `claude-hooks/block_git_rebase.sh` (header comment + code); hook
wins on any disagreement. **No bypass**; human runs blocked form via `! …`.

What is forbidden is **rewriting branch history**, not the tool or transport
that reaches it.

Blocked:

- `git rebase` — any form, `-i` included. A bare `--help` is allowed.
- `git pull --rebase` / `-r`.
- `gh pr update-branch --rebase` — GitHub rewrites the PR branch
  server-side. No local rebase and no push, so neither the git stages nor
  `block_force_push` can see it; the hook's gh stages are what cover it.
- `gh api graphql` carrying `updatePullRequestBranch` with
  `updateMethod: REBASE` — the mutation gh calls for that flag, either token
  order (REBASE often arrives as a separate `-f` variable).

Allowed:

- `gh pr update-branch` without the flag, and the REST endpoint
  `PUT …/pulls/<N>/update-branch` (takes only `expected_head_sha`, cannot
  rebase). Both merge the base in, rewriting nothing, so neither is a
  rewrite for this guard to refuse — and a branch that is merely behind is
  an agent's own to resolve (§6, through `forge-update-branch`). Forge does
  not *use* them: GitHub authors a merge commit no local pre-commit gate
  sees, and the PR head moves with no push for `warn_stale_wrapup` to
  notice. A process cost, not a reason to block.
- `updateMethod: MERGE` in the same mutation.

Blind spots. Tripwires on the routes agents take, not a sandbox:

- A **pipe inside a command substitution** before the flag ends the matched
  segment: `gh pr update-branch $(gh pr view -q .n | head -1) --rebase`
  passes. A substitution without a pipe is covered.
- A GraphQL query read from a file (`-F query=@q.graphql`), from stdin
  (`--input -`), from a shell variable or from a substitution. A `<<<`
  here-string IS visible and blocks.
- The verb held in a variable: `G=gh; $G pr update-branch 5 --rebase`.
- Wrappers `git_anchor.sh` does not model — `watch`, `parallel`, `flock`,
  `su -c` — in front of the command.
- A `gh alias` standing for the blocked command.

Why: a rebase discards the exact commits a reviewer saw. Forge squash-merges
every PR, so a branch's commit shape never reaches the base branch anyway —
`forge-update-branch` syncs by merge and rewrites nothing.

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
  `--force` / `--force-with-lease`, the `-f` / `-uf` cluster,
  `+`-prefixed refspecs, and the server-side form below.
- **Is a branch protected?** Check BOTH
  `gh api repos/{owner}/{repo}/rules/branches/{branch}` and
  `.../rulesets` — never conclude "unprotected" from a 404 on the legacy
  `/branches/{branch}/protection` endpoint alone (FOUNDATION §1 "Absence of
  evidence").

### Ref writes through the API

A ref moved by asking GitHub is the same act as moving it with a push: no
local git runs, nothing is pushed, and the git-only stages of the guards
see nothing. Agent credentials bypass server-side rulesets (FOUNDATION §2),
so GitHub is no second line of defence.

`block_force_push` refuses a `gh api` call that names `git/refs` in its
endpoint and carries **any** method or body flag — `-X`, `--method`, `-f`,
`-F`, `--field`, `--raw-field`, `--input`, their attached forms, and short
clusters such as `-iXPATCH`. Any ref, tags included: a force-moved release
tag is worse than a moved branch. It also refuses GraphQL `updateRef`
carrying a `force` at all. Authority is the hook's own header and code;
this page holds the rationale and the limits.

**No flag VALUE is read, deliberately.** A hook cannot match gh's flag
parsing: `-f` takes the whole following word, so `-fforce=true` has no `=`
split to find; a short cluster hides a flag (`-iXPATCH` is `-i` plus
`-X PATCH`); gh honours the LAST of a repeated `force=`. Each of those is a
spelling where a value-reading guard disagreed with gh and let a force
through. A ref write that cannot be told apart from a force is therefore
treated as one.

**This is a tripwire, not a sandbox** — the same standing as the rest of
the guard family. It stops the routes an agent would plausibly take; it
cannot stop one that hides the endpoint, or the flags, from the shell's own
text.

Still allowed: **reading** refs — `gh api repos/{o}/{r}/git/refs/heads/<b>`,
with `-q` / `--jq` / `--paginate`, carries no method or body flag.
Everything outside `git/refs` is untouched, writes included: a comment
whose body merely discusses `git/refs` passes, because the match requires
`git/refs` in a word with no `=` before it, and a substitution in a
non-ref endpoint (`…/pulls/$(gh pr view …)/files`) passes too, as does a
GraphQL mutation other than a forced `updateRef`.

What it costs: a non-force ref update, which can only fast-forward, is
refused along with the rest. No forge path performs one; an agent that
wants a branch moved uses `forge-update-branch`, and a human keeps `!`.

Blind spots, on top of the ones listed under "Rebase forms". Each hides the
endpoint or the flags from the text a hook can read:

- **An endpoint whose `git/refs` never appears literally** — held in a
  variable (`E=…; gh api "$E" -X PATCH`), spelled through brace expansion
  (`git/re{f,g}s/…`), a glob (`git/ref*/…`) or `$'\x72efs'`. A
  substitution that merely *contains* the literal is caught, and so is one
  whose `;` / `&` / `|` would otherwise cut the command short.
- **Flags the text does not spell either** — held in a variable
  (`F=-XPATCH; gh api <endpoint> $F`), split by brace expansion
  (`-{X,}PATCH`), or assembled through `$IFS`. The endpoint being visible
  does not help: the stage needs a flag token to see.
- **Flags supplied out of band** — piped in through `xargs`, or carried by
  a `gh alias` / `gh extension` that expands to `api -XPATCH`.
- **GraphQL `updateRef` cannot be judged per branch.** Its `refId` is an
  opaque node id, so no stage can say which ref it names; the force test
  catches it whatever ref that is.
- **A GraphQL query or variable object read from a file or stdin**
  (`-F query=@q.graphql`, `-F input=@vars.json`, `--input -`): the `force`
  never appears in the command text.
- **A different client altogether** — `curl -X PATCH https://api.github.com/…`
  with a token, or `git send-pack --force`, neither of which carries a
  `push` verb or a `gh api` call for any stage to match. Local
  `git update-ref` is likewise unguarded, though it moves nothing published.
- **Other endpoints that write the base** are not covered — `POST …/merges`,
  `PUT …/contents/<path>` with `branch=<base>`,
  `PUT …/branches/<base>/protection`, `POST …/branches/<base>/rename`, a
  `default_branch` change, and the GraphQL mutations `createCommitOnBranch`,
  `mergeBranch`, `createRef` and `deleteRef`. The real invariant is "no
  `gh api` write naming the protected branch"; these are recorded as a
  sighting rather than guarded, since nothing has reached for them.


## Base sync

One command: `forge-update-branch` (`--base <parent>` for a stacked
branch). It commits the merge locally but never pushes — publish via
`forge-commit`, whose push seam gates on the pre-commit verdict. Never
rebase, never stash, never `git reset --hard`.

A stacked branch's *first* commit, before any PR exists, has no PR to
read a base from: `forge-commit --base origin/<parent-branch> -m ...
<files>` tells the pre-commit gate the real base (the one-fragment gate
would otherwise count the unmerged parent's fragment and fail with the
remedy named in its refusal). The flag only exists for that explicit
pass — it never becomes a hidden default.

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
   completes itself: CI and the next commit check it). The branch is not
   pushed; `forge-commit --push-only` publishes it through the verdict
   gate, so deferred checks are paid at push time instead of never.
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
