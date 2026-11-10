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
- **Forge version** the PR was built with, as a release `X.Y.Z`, read
  from its merge commit (fetched once when missing locally): in forge
  itself the newest release tag the merge contains; in a consumer the
  forge-scripts pin in `pyproject.toml` at that commit (`==X.Y.Z` or a
  git URL `@vX.Y.Z`; dev suffixes dropped). Empty when it cannot be told
  (branch pin, commit unreachable) — never the version installed when
  recording.

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
- `forge-metrics backfill [--limit N] [--refresh]` — merged PRs not yet
  recorded whose branch still has transcripts. Claude Code purges
  transcripts after about 30 days; older PRs cannot be recovered.
  `--refresh` also rewrites the forge version of PRs already recorded
  (among GitHub's newest 100 merged), leaving their other figures alone.

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
`sha256` keys) is rekeyed, and schema 2 or 3 gains the bench tables. Any
other schema version is refused with the expected and found versions
named.

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
  `--html` writes one self-contained page (inline charts, no network
  fetches), adding medians per forge version and repo id (new tokens per
  line, active minutes, review rounds), new tokens per PR over time and a
  version × repo table; PRs without token data are gaps, never zeros.
  The page covers every recorded PR whatever `--last` says.
- `forge-metrics export [--out PATH]` — the stored rows as JSON, plus the
  database schema version, for sharing with forge.

## Publishing a public page

`forge-metrics publish --repo OWNER/NAME` pushes a public variant of the
page to a dedicated repo served by GitHub Pages: `index.html` and
`metrics.json` (same rows as data, with the schema version), plus a short
`README.md` and `.nojekyll`. While metrics publishing is an experiment,
only forge's maintainer may publish: it refuses any other `gh` user, and
any target repo outside the maintainer's account (`--dry-run` stays open
to everyone). The public variant has no dates at all (no over-time chart,
no merge days), no PR keys, no names and no repo ids: forge's own repo
shows as `forge` and every other repo merges into one `external` series.
PRs are grouped by forge version and put in an order fixed by a hash of each PR's key, so their
order dates nothing and an unchanged database republishes identical files
(nothing is pushed). It keeps forge version, tool, model, and the token,
time, review-round and size numbers. The page repo's commit history still
shows roughly when rows were added (no names), and each commit carries the
publisher's git identity.

Before writing anything it checks the target exists, is public (Pages
needs that), and that you have admin rights on it; then it clones it to a
temporary directory and refuses unless it tracks nothing but those four
files (an empty repo is fine). It never force-pushes and touches no other
repo. `--dry-run DIR` writes `index.html` and `metrics.json` to `DIR`
without any network call, for a look first.

One-time setup by the owner:

1. `gh repo create OWNER/forge-metrics --public`
2. `forge-metrics publish --repo OWNER/forge-metrics`
3. If it says Pages is off, run the `gh api -X POST repos/OWNER/forge-metrics/pages ...`
   command it prints. The page then lives at `https://OWNER.github.io/forge-metrics/`.

## Privacy of `export`

The export is the stored rows as they are, so it holds exactly what the
database holds (see "What is not stored"): `repo_id`, `pr_key`, merge
day, tool, model, forge version, figures and tokens per agent type. The
`repo_id` lets rows of one repo be grouped without naming it. Read the
file before sending it.

## Comparing versions (`forge-metrics bench`)

Real PRs differ too much to tell whether a forge version made the same
work cheaper. `bench` gives five fixed tasks and a private sandbox repo to
run them in, by hand, so versions can be compared on equal work.

> **Cost.** Each run is a real session taken through the full forge
> workflow to a posted PR wrap-up: about one PR's worth of tokens. Run
> the tasks for a version you want to measure; nothing runs them
> automatically.

- `forge-metrics bench init --repo <owner>/<name>` — create the repo on
  GitHub first, **private and empty** (no README); `init` refuses a repo
  that is public, not yours (no admin rights) or has a branch. It fills
  it with a small sample project shipped in forge, sets it up as a forge
  consumer with the running forge, pushes `main` and tags it
  `bench-base-1`. Pushing `main` from a CLI is allowed here only because
  it is the first commit of a sandbox nobody else uses, checked just
  before, through forge's no-force push seam. The repo name is kept in
  `<data dir>/bench.json` (local only, never in the database). Every
  later command checks again that the sandbox is private and yours.
- `forge-metrics bench start <task> --forge X.Y.Z [--clone DIR]` — in
  your local clone of the sandbox (default `<data dir>/bench/sandbox`,
  cloned on first use): fetch, then, **only if the working tree is
  clean** (a dirty clone is refused, nothing is discarded), create branch
  `bench/<task>-<n>` at the base tag and write the task's setup files. It
  prints the line to start a session (`cd <clone> && claude`) and the
  prompt to paste. The run in progress is kept in
  `<data dir>/bench-run.json`; one at a time.
- `--forge` names the forge release to measure (`1.2.3` or `v1.2.3`; it
  must be a published release). Before the setup files, `start` pins that
  release in the sandbox's `pyproject.toml` and commits the pin as the
  branch's first commit, builds the clone's `.venv` with it (kept as is
  when it already holds that release), and installs the matching Claude
  Code plugin. If the plugin step fails, for example because `claude` is
  not installed, `start` says the plugin is not on that release, prints
  the command to run by hand before the session and carries on; `finish`
  warns about such a run again. The pin commit is not counted in the
  run's lines or commits.
- Run the prompt in that session; it asks for the normal workflow up to
  a posted wrap-up, without questions.
- `forge-metrics bench finish [--clone DIR]` — records the run, closes
  its PR unmerged, deletes its branch on GitHub (lease-pinned, never the
  base branch; the local branch is kept) and clears the run file. Then
  it shares the results with the sandbox (below).
- `forge-metrics bench report [--task T] [--from-sandbox [--clone DIR]]`
  — per task, one line per forge version (release order): median new
  tokens, cache %, active and wall time, review rounds and lines changed,
  then counts of runs where each fact held. No pass/fail verdict: the
  figures inform decisions. `--from-sandbox` reads every machine's runs
  from the sandbox's `results/benchmarks.json` (after a fetch) instead of
  this machine's database.

### Results in the sandbox

So it does not matter which machine runs the benchmark, `bench finish`
also commits the results to the sandbox's `main`:

- `results/benchmarks.json` — `{"schema": 1, "runs": {<run_id>: {...}}}`.
  Each entry: forge version, task, run number, model, base tag, the day
  it was recorded, tokens per agent type (requests, input, cache write,
  cache read, output), active and wall time, review rounds, lines added
  and removed, and the outcome facts. No host, user, path or branch.
  Every run in this machine's database is merged in, keyed by run id:
  the same run shared again replaces its entry, never duplicates it, and
  other machines' runs are kept as they are. A file of a newer schema is
  refused, not rewritten.
- `results/index.html` — the bench report regenerated from the merged
  file: one self-contained page, every value escaped, nothing fetched.

The commit is made on `main` in the clone itself, through the sandbox's
own hooks: `finish` needs the clone's working tree clean (a dirty clone
is refused and nothing is discarded; the results stay local), switches
to `main`, fast-forwards it to the sandbox's (a diverged `main` is
refused), commits and pushes, and leaves the clone on `main` — the next
`start` branches from the base tag anyway. The bench branch's commits
are not touched. A push rejected
because another machine pushed first is retried after merging the new
`main` (results files are rewritten from both sides' runs), up to 3
times; then the results stay in the local database, `finish` says so,
and the next `finish` shares them. Pushing the sandbox's `main` from a
CLI is allowed only here: a private sandbox nobody else uses, checked
just before, through forge's no-force push seam. Runs keep starting
from the `bench-base` tag, so results never change what a run sees.

Tasks: `add-command` (new CLI subcommand with tests), `fix-bug` (a planted
bug and its failing test), `docs` (a README section), `refactor` (extract
a helper, behaviour unchanged), `review` (review a provided diff and fix
its findings). Each states its request in full, asks for no questions and
a stop after the wrap-up, and carries outcome checks (test files added
only while `finish` checks, a symbol or text that must exist).

Stored per run, in `bench_runs` and `bench_tokens` (keyed by a random
`run_id`): the forge version measured (the release `start` installed in
the clone's `.venv`), task, run
number, model, base tag, start and finish time, PR opened, wrap-ups
posted, sandbox tests green, task checks held / total,
`forge-precommit --verdict`, files and lines changed and commits since
the pin commit, review rounds, pre-commit and smart-test runs, active and
wall time, and tokens per agent type (folded as for PRs). Tokens and
time are the run's branch in the clone's transcripts since `start`. No
sandbox name, branch or path is stored. `export` does not include bench
runs.
