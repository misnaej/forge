# Per-PR metrics (`forge-metrics`)

What each merged PR cost, kept so PRs can be compared and a forge change
checked for whether it made work cheaper.

## What is recorded

One row per merged PR:

- **Merge day** (date only, no time) and the tool (`claude-code`).
- **Model** answering most main-session requests, from the transcripts
  (empty when unknown).
- **Size** (from GitHub): files changed, lines added and removed, commits.
- **Review rounds**: the most runs any one reporter
  (`forge:design-checker`, `forge:security-checker`,
  `forge:docs-types-checker`) made on the branch — each review pass runs
  each reporter once.
- **Wrap-ups posted**: PR comments opening with a `verified-at:` header.
- **Tokens**, per agent type (`main` = the main session): requests, input,
  cache write, cache read, output. Only `main`, the agents forge ships
  (the installed package's roster) and Claude Code's built-ins
  (`Explore`, `Plan`, `general-purpose`, `claude-code-guide`) keep their
  name; every other type is summed under `other` before it is stored — a
  repo's own wrappers often carry its name, `forge:`-prefixed or not.
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
`unattributed` table per repo id and day instead. No transcripts (CI, another machine)
→ size and review rounds only.

## When

- `forge-metrics record --pr <N>` — after merge: the `/next` cleanup and
  the PR monitor's merge handling run it. Re-recording replaces the PR's
  rows.
- `forge-metrics backfill [--limit N]` — merged PRs not yet recorded whose
  branch still has transcripts. Claude Code purges transcripts after about
  30 days; older PRs cannot be recovered.

## Where

`$XDG_DATA_HOME/forge/metrics.sqlite` (default
`~/.local/share/forge/metrics.sqlite`): one database per machine user,
shared by every repo and checkout, never committed. It lives outside the
per-checkout `code_health/` because one PR can be worked from any
checkout of the repo. `--db PATH` overrides.

Earlier versions kept one database per repo at
`<data dir>/<owner>/<repo>/metrics.sqlite`. The first run of any
`forge-metrics` command (default `--db` only) imports those rows into the
machine database, dropping titles, branch names and PR numbers, then
deletes the old file and its empty directories.

## What is not stored

The database holds no name: no repo name or `owner/repo`, no checkout
path, no PR number, title or head branch, no file names, no commit
messages, no merge time of day. Instead:

- **`repo_id`** — a random UUID minted the first time a repo records a PR.
- **`pr_key`** — `sha256(repo_id + ":" + PR number)` in hex: stable, so
  re-recording a PR replaces its row, but the number is not stored.

The one file tying an id to a repo is **`<data dir>/repo-ids.json`**
(`{"<owner>/<repo>": "<repo_id>"}`), local to this machine. `record`
reads it to find the id; `report --repo` reads it to resolve a name to
an id. Nothing `report`, `--html` or `export` prints reads names from it.
Delete it and the database stays readable, just unattributable (a repo
recorded again then gets a new id).

`record` parses only transcript files written since the day before the
PR was opened that name its branch or the base branch; `backfill` reads
every transcript once and splits it across the PRs it records.

## Reading it

- `forge-metrics report [--last N] [--html PATH] [--repo R]` — per-PR
  table (all tokens, `new` tokens, `cache%`, new tokens per changed line,
  top helpers, review rounds, wall and active time) plus a trend: older
  half vs newer half, by median of new tokens. Cache reads dominate the
  total but cost far less, so `new` = input + cache write + output is the
  comparable figure. A PR is labelled `<merge day> <first 6 of repo_id>#<n>`,
  `n` = its place among that repo's recorded PRs (1 = oldest). All repos
  by default; `--repo .` (this checkout's repo), `--repo owner/repo`
  (resolved through `repo-ids.json`) or `--repo <id prefix>` narrows it.
  `--html` writes one self-contained page (inline chart, no network
  fetches).
- `forge-metrics export [--out PATH]` — the stored rows as JSON, plus the
  database schema version, for sharing with forge.

## Privacy of `export`

The export is the stored rows as they are, so it holds exactly what the
database holds (see "What is not stored"): `repo_id`, `pr_key`, merge
day, tool, model, forge version, figures and tokens per agent type. The
`repo_id` lets rows of one repo be grouped without naming it. Read the
file before sending it.
