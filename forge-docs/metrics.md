# Per-PR metrics (`forge-metrics`)

What each merged PR cost, kept so PRs can be compared and a forge change
checked for whether it made work cheaper.

## What is recorded

One row per merged PR:

- **Merge day** (date only, no time) and the tool (`claude-code`).
- **Model** answering most main-session requests, from the transcripts
  (empty when unknown). Stored only when it matches
  `[A-Za-z0-9._:-]{1,64}`; anything else is stored as `other`, since
  transcript text is free-form.
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
  repo's own wrappers often carry its name, `forge:`-prefixed or not. An
  unreadable roster stops recording with an error rather than storing
  every forge agent as `other`.
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
deletes the old file and its empty directories. Every import write
replaces rather than adds, so an import interrupted before the delete
is safely repeated.

## What is not stored

The database holds no name: no repo name or `owner/repo`, no checkout
path, no PR number, title or head branch, no file names, no commit
messages, no merge time of day. Instead:

- **`repo_id`** — a random UUID minted the first time a repo records a PR.
- **`pr_key`** — `HMAC-SHA256(secret, repo_id + ":" + PR number)` in
  hex: stable, so re-recording a PR replaces its row, but the number is
  not stored and cannot be recovered by hashing small numbers with an
  exported `repo_id`.

The one file tying an id to a repo is **`<data dir>/repo-ids.json`**
(`{"<owner>/<repo>": "<repo_id>", "_secret": "<hex>"}`, owner-readable
only), local to this machine. `_secret` is 32 random bytes minted on
first use; it is never exported. `record` reads the file to find the id
and key; `report --repo` reads it to resolve a name to an id. Nothing
`report`, `--html` or `export` prints comes from it. Delete it and the
database stays readable but unattributable: a repo recorded again gets a
new id, and a re-recorded PR a new key (a second row).

A database of an earlier layout is upgraded on open: schema 2 (plain
`sha256` keys) is rekeyed, and schema 2 or 3 gains the bench tables. Any other schema version is refused with the expected
and found versions named.

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

## Comparing versions (`forge-metrics bench`)

Real PRs differ too much to tell whether a forge version made the same
work cheaper. `bench` runs five fixed tasks the same way against a chosen
version so versions can be compared on equal work.

> **Cost.** A full set (five tasks, two runs each) spends about as many
> tokens as ten PRs: every run goes through the full forge workflow up to
> a posted PR wrap-up. Run it by hand, for a version you want to measure;
> nothing runs it automatically.

- `forge-metrics bench init --repo <owner>/<name>` — create the repo on
  GitHub first, **private and empty** (no README); `init` refuses any
  other. It fills it with a small sample project shipped in forge, sets it
  up as a forge consumer with the running forge, pushes `main` and tags it
  `bench-base-1`. Pushing `main` from a CLI is allowed here only because
  it is the first commit of a sandbox nobody else uses, checked empty and
  private just before, through forge's no-force push seam. The repo name
  is kept in `<data dir>/bench.json` (local only, never in the database).
- `forge-metrics bench run --version vX.Y.Z [--task T]... [--runs 2]
  [--model M] [--timeout-min 45]` — per task × run, in a temporary
  directory: a clone of the sandbox at `bench-base-1`, a throwaway venv
  holding forge at tag `vX.Y.Z` (and the sample), that version's
  bootstrap committed on branch `bench/<task>-<vX>-<n>`, then a headless
  `claude -p` with the task's prompt, the tag's plugin (`--plugin-dir`),
  and `--permission-mode dontAsk` plus a settings file allowing the usual
  tools: nothing is prompted, anything not allowed is denied, and forge's
  hooks still guard every call. After the agent stops (or hits the
  timeout) the run's facts are collected, the PR is closed unmerged, the
  branch deleted (lease-pinned, never the base branch) and the directory
  removed. A failed run is recorded as facts and the set continues.
- `forge-metrics bench report [--task T]` — per task, one line per
  version (release order): median new tokens, cache %, active and wall
  time, review rounds and lines changed, then counts of runs where each
  fact held. No pass/fail verdict: the figures inform decisions.

Tasks: `add-command` (new CLI subcommand with tests), `fix-bug` (a planted
bug and its failing test), `docs` (a README section), `refactor` (extract
a helper, behaviour unchanged), `review` (review a provided diff and fix
its findings). Each states its request in full, tells the agent never to
ask and to stop after the wrap-up, and carries outcome checks (test files
added only after the run, a symbol or text that must exist).

Stored per run, in `bench_runs` and `bench_tokens` (keyed by a random
`run_id`): forge version under test, task, run number, model, base tag,
start and end time, setup ready, timed out, `claude` exit code, PR
opened, wrap-ups posted, sandbox tests green, task checks held / total,
`forge-precommit --verdict`, files and lines changed and commits since
the setup commit, review rounds, pre-commit and smart-test runs, active
and wall time, and tokens per agent type (folded as for PRs). No sandbox
name, branch or path is stored. Tokens come from the transcripts of the
run's temporary directory on the run's branch; those transcripts stay in
`~/.claude/projects/` like any other session's. `export` does not include
bench runs.
