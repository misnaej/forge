# Two of the five squash-message noise categories are lexically detectable

- surface: pr-squash-comment
- release: forge 14.0.0
- seen: 2026-11-18
- branch: fix/budgets-and-post-merge-routine

## Evidence

Rule now lives in `FOUNDATION.md:445` (content rule) and
`src/forge/pr_squash_comment.py:133` (target); neither is checked.

Measured last 60 merged squash messages: 26% carried >=1 bullet of pure
process state or bookkeeping, 9% of all bullet words. Five categories:
CI/test pass claims (6 bullets), forge's own doc word counts (5),
changelog-fragment meta (4), suppression/noqa counts (4), test/row
counts (3). FOUNDATION §6 now states the content rule; prompt-only.

Two categories are lexically detectable with low false-positive cost:
forge's own doc word counts (`\d{3,4}/\d{3,4} words`, `word budget`,
`baseline \d+`) and explicit counts (`\d+ new tests?`, `\+\d+ rows`,
`\d+ inert`), plus fixed phrases `pre-commit green`, `reporters PASS`,
`changelog fragment`. The other three need judgment.

## What it costs

Prompt-only rules rely on an agent remembering them (§12 test: "would
this prevent it with nobody remembering?" — no). The measured 26% is
what remembering achieves.

## What would fix it

Warn-only detector in `pr_squash_comment`, §7 mechanical-first: the
decidable half belongs in code, not prose. Deliberately NOT built with
the rule — an unrequested nag re-creates the rewrite loop the same
change removed, and no test justifies unbuilt code. Build only if the
next measurement shows the prose rule did not hold.

Test shape if built: label the measured 60 as a fixture corpus, assert
zero false positives on the behaviour-bullet half (precision, not
recall). Never blocking.
