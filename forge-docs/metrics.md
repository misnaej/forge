# Per-PR metrics (`forge-metrics`)

What each merged PR cost, kept so PRs can be compared and a forge change
checked for whether it made work cheaper.

## What is recorded

One row per merged PR:

- **Size** (from GitHub): files changed, lines added and removed, commits.
- **Review rounds**: the most runs any one reporter
  (`forge:design-checker`, `forge:security-checker`,
  `forge:docs-types-checker`) made on the branch — each review pass runs
  each reporter once.
- **Wrap-ups posted**: PR comments opening with a `verified-at:` header.
- **Tokens**, per agent type (`main` = the main session): requests, input,
  cache write, cache read, output.
- **Time**: wall (first to last transcript record on the branch) and
  active (gaps over 5 minutes left out).
- **Runs**: `forge-precommit` and `forge-smart-test` calls on the branch.
- The forge version that recorded it.

Tokens, time and runs come from Claude Code's transcripts
(`~/.claude/projects/`), whose every record names the git branch it ran
on. A PR's figures are everything recorded on its head branch, in every
checkout of the repo on this machine, up to its merge — and after the
merge of any earlier PR with the same branch name, so a reused name is
not charged twice. Work on the base branch goes to an
`unattributed` table per day instead. No transcripts (CI, another machine)
→ size and review rounds only.

## When

- `forge-metrics record --pr <N>` — after merge: the `/next` cleanup and
  the PR monitor's merge handling run it. Re-recording replaces the PR's
  rows.
- `forge-metrics backfill [--limit N]` — merged PRs not yet recorded whose
  branch still has transcripts. Claude Code purges transcripts after about
  30 days; older PRs cannot be recovered.

## Where

`$XDG_DATA_HOME/forge/<owner>/<repo>/metrics.sqlite` (default
`~/.local/share/...`): one database per repo, shared by all its checkouts,
never committed. `--db PATH` overrides.

## Reading it

- `forge-metrics report [--last N] [--html PATH]` — per-PR table (all
  tokens, `new` tokens, `cache%`, new tokens per changed line, top
  helpers, review rounds, wall and active time) plus a trend: older half
  vs newer half, by median of new tokens. Cache reads dominate the total
  but cost far less, so `new` = input + cache write + output is the
  comparable figure. `--html` writes
  one self-contained page (inline chart, no network fetches).
- `forge-metrics export [--out PATH]` — JSON for sharing with forge.

## Privacy of `export`

Figures only. PRs are keyed by ordinal (1 = oldest); no PR numbers,
titles, branch or file names, dates or messages. Agent types are kept only
for `main` and forge's own (`forge:*`); every other type — a repo's own
wrappers often carry its name — is summed under `other`. Read the file
before sending it.
