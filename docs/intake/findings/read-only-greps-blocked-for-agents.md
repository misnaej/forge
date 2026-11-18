# Guards block read-only searches, not just writes

- surface: claude-hooks
- release: forge 14.5.0
- seen: 2026-11-18
- branch: fix/budgets-reach-their-writers

## Evidence

Three distinct guards refused read-only commands in one session, each
while the agent was reading to verify a claim:

1. `block_vibe_config` (`claude-hooks/block_vibe_config.sh:1`) refused
   `grep -rn "forge.c4:start" .agents/`. Its own message says "do not
   edit, delete, or overwrite it by hand" — a search does none of those.
2. `forge-precommit-fixer`'s Bash allowlist refused `grep -n` on
   `src/forge/pr_wrapup_compose.py` ("limited to forge-precommit and the
   step CLIs"). It reported the gap itself: "that made this review slower
   and more error-prone."
3. `block_raw_git` refused heredocs whose *string literals* contained the
   word "git" four separate times, and once refused a `python -c` sharing
   a line with `git describe`.

Each was worked around with the `Read` tool, so nothing was blocked
outright. One of the three — chaining real git onto a heredoc — was a
correct refusal.

## What it costs

Not safety: it costs accuracy. Every one of these fired while an agent was
checking something before asserting it. The session's recurring defect
class was exactly the opposite failure — claims made on probes that could
not have disproved them — so a guard that makes verification awkward
pushes in the wrong direction. The `Read` fallback is also worse for the
job: no regex, no cross-file sweep, and whole files into context where a
match count would do.

## What would fix it

Separate the predicate from the effect. These guards key on a *word*
appearing in a command, not on the command writing anything. A read-only
form — `grep`, `rg`, `git log`, `git show`, `git describe` — is not the
effect any of them exists to prevent.

Two candidates, neither validated:
- Allow read-only verbs explicitly in the guards that currently match on
  substrings, so the refusal tracks the effect rather than the vocabulary.
- Give `forge-precommit-fixer` read-only search in its allowlist. It
  already holds `Read`; a grep is strictly less access than reading whole
  files into context.

Filed rather than fixed: `claude-hooks/` is propose-only for an agent, and
this needs the guard semantics reviewed rather than a quick edit.
