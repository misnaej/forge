# forge-update-branch stopped on 1 conflict(s)

- surface: forge-update-branch
- release: forge 13.0.1.dev2+g7c0bbf594.d20261117
- seen: 2026-11-17
- branch: feat/claude-converter-drop-plugin

## What happened

Merging origin/main stopped on paths that need judgment:
- `.claude-plugin/plugin.json`

Why they needed judgment: delete/modify — this branch retires the plugin
and deletes the manifest, while main's release assembly bumped the
version value inside it. Mechanical rules cannot tell a deliberate
retirement from a lost upstream edit, so both intents had to be read
before the deletion could win. The upstream hunk was confirmed to be the
version value alone (one line, `13.0.0` → `13.1.0`, byte-compared against
the merge base) before anything was dropped.

That part worked as designed and is not the finding.

## What it costs

**The finding is the second refusal.** With every conflict resolved and
staged, `forge-update-branch --continue` refused:

    refused — AI attribution pattern detected: 'claude'
    (in 'feat/claude-converter-drop-plugin')

The default merge message is `Merge remote-tracking branch 'origin/main'
into <branch>`, so it carries the branch name — and this branch is named
for the thing it retires, which contains a vendor word. The guard fires
on the name, not on anything written by hand.

The cost is that there is **no in-tool path forward**: `--continue` takes
no `-m`, the message is generated, and the merge sits half-finished with
every conflict already resolved. It is reachable by branch name alone, so
any branch named for a vendor word dead-ends the same way, and it strikes
at the worst moment — after the judgment work is done. Neither
`forge-precommit-fixer` nor the resolver can clear it, because it is not a
pre-commit verdict.

## Fix, if known

Candidates, not yet chosen:

1. `--continue` accepts `-m`, so the caller can supply a message the
   guard accepts. Smallest change; leaves the trap for anyone who does
   not know the flag exists.
2. The generated merge message names the base and the SHA rather than the
   branch (`Merge origin/main (2d0f282)`), so a branch name can never
   reach the attribution check. Removes the class rather than offering an
   escape, and the branch name adds nothing a reader needs.
3. The attribution guard ignores text it did not author — a generated
   merge message is forge's own, not a commit message someone wrote.
   Widest blast radius; needs care so it cannot be used to smuggle
   attribution through a generated path.

Option 2 looks right: the guard exists to keep vendor attribution out of
authored commit text, and a branch name in a generated merge subject is
neither authored nor informative.
