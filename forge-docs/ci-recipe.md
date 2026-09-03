# Running forge in CI — recipe

A pasteable GitHub Actions setup for keeping `forge-scripts` current
in a consumer repo. Channel-pinned, no third-party bot, no per-version
pin maintenance.

> See also: [README "Running forge in CI"](../README.md#running-forge-in-ci)
> for the summary. This page is the full pasteable copy.

---

## 1. Pin a channel in `pyproject.toml`

```toml
[project.optional-dependencies]
dev = [
    "forge-scripts @ git+https://github.com/misnaej/forge.git@main",
    # ... your other dev deps
]
```

`@main` = every release (forge is single-track). See the
[pin table](../README.md#pick-a-pin) in the README.

A tag pin (`@v1.9.1`) is also supported for one-off frozen releases.
For ongoing automated upgrades the channel pin is the recommended
default: it requires no per-version maintenance and the scheduled
workflow below handles the rest. Switching an existing branch pin to a
tag pin? Read ["About branch pins (`@main`)"](../README.md#about-branch-pins-main)
first — branch-only refresh wrappers silently stop updating forge after
the switch; `forge-upgrade` and `forge-doctor` now detect the mismatch.

## 2. Per-PR CI

`.github/workflows/ci.yml`:

```yaml
name: ci
on:
  push:
    branches: [main, dev]
  pull_request:
    # ready_for_review added to the default set so a draft PR (the /pr
    # skill's early-visibility escape hatch) gets its first CI run the
    # moment it is marked ready.
    types: [opened, synchronize, reopened, ready_for_review]

# Defense-in-depth: this workflow only reads — see docs/security.md
# "Least-privilege GITHUB_TOKEN". Jobs needing writes (the upgrade /
# resync / tag-on-merge workflows below) declare their own overrides.
permissions:
  contents: read

jobs:
  test:
    # Draft PRs skip CI: /pr's verification-first flow runs the same checks
    # locally before publishing, so CI on an unfinished draft is waste. A
    # skipped job satisfies required status checks.
    if: github.event_name != 'pull_request' || github.event.pull_request.draft == false
    runs-on: ubuntu-latest
    steps:
      # SHA-pin actions (tag comment for humans) — tags are movable, SHAs are
      # not; see docs/security.md "Pin GitHub Actions". To keep pinned SHAs
      # current, enable Dependabot's `github-actions` ecosystem in your repo
      # (or run `gha-update`).
      - uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683  # v4.2.2
      - uses: actions/setup-python@0b93645e9fea7318ecaed2b359559ac225c90a2b  # v5.3.0
        with:
          python-version: "3.11"

      - name: Install project + forge-scripts
        run: pip install -e ".[dev]"

      - name: Bootstrap forge artifacts (idempotent)
        run: install-forge-bootstrap

      - name: Verify no drift
        run: install-forge-bootstrap --check

      - name: Run forge pre-commit
        run: forge-precommit

      - name: Tests
        run: pytest -q --durations=25 --durations-min=1.0 | tee code_health/pytest.log

      - name: Slow tests report
        if: always()
        run: forge-slow-tests-report --log code_health/pytest.log --out code_health/slow_tests.log
```

Notes:

- The `--durations` flags are passed **explicitly on the command line**
  so the report works regardless of how your repo configures pytest (or
  whether it does at all). Don't rely on a `[tool.pytest.ini_options]`
  block existing — that's a per-repo choice, and a consumer who copies a
  bare `pytest` would get an empty report.
- If you also want timings on a bare local `pytest`, mirror the flags in
  *your* pytest config — `addopts` under `[tool.pytest.ini_options]`
  (pyproject.toml), `[pytest]` (pytest.ini), or `[tool:pytest]`
  (tox.ini / setup.cfg). Optional convenience; the CI command above does
  not depend on it.
- `forge-slow-tests-report` parses that log, merges every durations
  section, and prints the slowest tests ranked. `if: always()` runs it
  even when tests fail — slow + failing is when you most want the list.
  It is read-only and always exits `0`, so it never changes the job's
  pass/fail.

- `install-forge-bootstrap` is idempotent — running it on every CI
  job is cheap and guarantees the managed artifacts (`FOUNDATION.md`,
  `docs/cli-reference.md`, label schema, etc.) match the installed
  forge version.
- `install-forge-bootstrap --check` fails the step if anything
  drifted — drop this in a PR-required check to refuse merges that
  would land out-of-sync content.
- `doctor` and `audit-deps` self-skip in CI (FOUNDATION §15):
  `forge.run_context.is_non_interactive()` returns true under GitHub
  Actions / GitLab CI / etc., so the gates fire automatically. No
  `--skip` flags needed.

### Stranded-changelog gate as a required PR check (single-track repos)

The `changelog_version` step detects entries stranded under an
already-released heading — but a one-shot check goes stale: a PR green
when checked can strand *afterwards*, when a sibling PR merges first
and the base cuts the tag, with no merge conflict to warn (see
[`consumer-release.md`](../docs/consumer-release.md) "Enforcement"). To catch
that pre-merge, run the step as its own **required status check**:

```yaml
  changelog-gate:
    if: github.event_name == 'pull_request'
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683  # v4.2.2
        with:
          # Full history + tags: the gate compares against the LIVE
          # latest tag, which a shallow checkout does not carry.
          fetch-depth: 0
      - uses: actions/setup-python@0b93645e9fea7318ecaed2b359559ac225c90a2b  # v5.3.0
        with:
          python-version: "3.11"
      - name: Install project + forge-scripts
        run: pip install -e ".[dev]"
      - name: Changelog gate against the live tag
        run: forge-precommit --only changelog_version
```

- **The branch-protection pairing IS the re-evaluation mechanism**:
  mark the job a required status check AND enable *"require branches
  to be up to date before merging"*. When the base advances (a sibling
  merge cuts a tag), the PR must take the base back in, which re-runs
  this job against the now-live tag — a stranded PR goes red *before*
  merge instead of blocking the post-merge tagger.
- **Trigger on `pull_request`** (as above): the stranded half of the
  check identifies the PR branch via `GITHUB_HEAD_REF` on the detached
  merge-ref checkout; a `push`-triggered run still validates heading
  structure but has no PR branch to diff against.
- Repos where the step self-skips (plugin-manifest repos) satisfy the
  required check via the skip — safe to require everywhere.
- The job is read-only and **inherits the workflow-level
  `permissions: contents: read`** added in the §2 snippet — no
  per-job permissions block needed.

### Smart-test cadence in CI (the classic schema)

When CI is the testing fleet (most repos), pair
`[tool.forge.smart_test].cadence_mode = "external"` (or `"advisory"`
for mixed fleets) with two jobs: tiered smart tests on PRs, the full
suite on every main-branch push. Event-driven — the main path needs no
clock; add the `schedule:` trigger as a quiet-period backstop.

```yaml
  smart-test-pr:            # PR job: change-scoped tiers
    if: github.event_name == 'pull_request'
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with: { fetch-depth: 0 }
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: pip install -e ".[dev]"
      - run: forge-smart-test --depth 2

  full-suite-main:          # main-push (+ optional cron) job: truly all
    if: github.event_name != 'pull_request'
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: pip install -e ".[dev]"
      - run: forge-smart-test --depth full --all-tests
      # OPTIONAL stamp refresh — keeps `external` mode's 2x-window
      # broken-pipeline detector honest. Needs `contents: write` on
      # THIS job only; skip it and expect (truthful) stale-stamp WARNs.
      # - run: |
      #     git config user.name github-actions
      #     git config user.email github-actions@github.com
      #     git add .forge-full-run
      #     git commit -m "chore: refresh full-run cadence stamp" || true
      #     git push
```

- Add `schedule: [{cron: "0 4 * * *"}]` to the workflow triggers if
  main can go quiet for days — the scheduler then owns the cadence
  clock instead of the merge stream.
- Workstations in these repos set `cadence_mode = "advisory"` (or the
  step stays unopted) — the committed-stamp escalation is for
  repos whose testing genuinely happens locally. Decision table:
  smart-test doc, "Who carries the cadence".

## 3. Scheduled `forge-upgrade --apply` workflow

`.github/workflows/forge-upgrade.yml`:

```yaml
name: forge-upgrade
on:
  schedule:
    - cron: "0 5 * * 1"  # every Monday 05:00 UTC
  workflow_dispatch:

permissions:
  contents: write
  pull-requests: write

jobs:
  upgrade:
    runs-on: ubuntu-latest
    steps:
      # SHA-pinned per docs/security.md "Pin GitHub Actions" — see the §2
      # note for the rationale and how to keep the SHAs current.
      - uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683  # v4.2.2
      - uses: actions/setup-python@0b93645e9fea7318ecaed2b359559ac225c90a2b  # v5.3.0
        with:
          python-version: "3.11"

      - name: Install project + forge-scripts
        run: pip install -e ".[dev]"

      - name: Run forge-upgrade --apply
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: forge-upgrade --apply

      - name: Open PR if anything changed
        uses: peter-evans/create-pull-request@c5a7806660adbe173f04e3e038b0ccdcd758773c  # v6.1.0
        with:
          commit-message: "chore: forge-upgrade (automated)"
          title: "chore: forge-upgrade"
          body: |
            Automated forge-scripts re-sync. Review the diff for
            `FOUNDATION.md`, `.githooks/`, and the generated docs.
          branch: forge-upgrade/automated
          delete-branch: true
```

How it works:

- `forge-upgrade --apply` force-reinstalls `forge-scripts` from the
  pinned ref (`@main`) and re-runs `install-forge-bootstrap`.
  Because pip caches branch refs by `(package_name, version)`, the
  `--force-reinstall --no-deps` inside `--apply` is what actually
  pulls the new content; a plain `pip install` would silent-no-op.
- The `block_install_deps` Claude hook refuses `--apply` for agents,
  but CI has no such guard — that is the point of `--apply`.
- `GITHUB_TOKEN` is detected by `forge.run_context.git_auth_mode()`
  → `"https-token"`. The pip install uses the HTTPS URL form, which
  the token can authenticate against (relevant for forks of forge
  into a private repo).
- The scheduled run opens a PR only when something changed on disk.
  Empty diffs no-op.
- The PR's `pull_request` event triggers the per-PR CI in step 2 —
  every forge upgrade is exercised by the full quality gate before
  you merge it.

Pick a cadence that matches how aggressively you want forge updates:

| Cron | Effect |
|---|---|
| `0 5 * * 1`        | Once a week (Monday 05:00 UTC) — default in the snippet above. |
| `0 5 * * *`        | Daily. |
| `0 5 1 * *`        | Monthly. |

### Scheduled resync (drift without a pin change)

An `@main`-pinned repo drifts even when its pin never moves:
each forge release changes the canonical content of committed managed
artifacts (`FOUNDATION.md`, generated docs, hook wrappers). Upgrade ≠
resync — the workflow above PRs *pin* changes; `forge-resync` PRs the
*artifact regen*, with a built-in dedup guard (an open
`chore/forge-resync-*` PR → it reports the URL and stops). In sync →
exit 0, no PR. Same permissions block as the upgrade workflow.

```yaml
name: forge-resync
on:
  schedule:
    - cron: "0 6 * * 1"  # Mondays, after the upgrade run
  workflow_dispatch:

permissions:
  contents: write
  pull-requests: write

jobs:
  resync:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683  # v4.2.2
      - uses: actions/setup-python@0b93645e9fea7318ecaed2b359559ac225c90a2b  # v5.3.0
        with:
          python-version: "3.11"

      - name: Install project + forge-scripts
        run: pip install -e ".[dev]"

      - name: Resync managed artifacts (PR on drift)
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: forge-resync
```

The resync PR body reminds the reviewer that mechanical regen does not
surface adoption-required changes — `forge-upgrade --check` lists the
pending `**Action:**` items for the version range.

---

## 4. Tag-on-merge release automation

Cutting the release tag on every merge removes the human-latency window
in which further PRs pile onto an already-declared version, and closes
the stale-local-tag gap entirely — the remote is always current.

### Single-track repos (CHANGELOG-declared version)

Add a job to your CI workflow that runs after your tests, on pushes to
the base branch only. `forge-release --from-changelog` cuts the version
the CHANGELOG top heading declares; it is idempotent (already tagged →
exit 0) and race-tolerant (a concurrent manual cut of the same version
counts as success), so re-runs and races are safe.

```yaml
  tag-release:
    needs: test            # gate on your test job — recommended
    if: github.event_name == 'push' && github.ref == 'refs/heads/main'
    runs-on: ubuntu-latest
    permissions:
      contents: write
    steps:
      # SHA-pin actions in write-capable jobs (v4.2.2 / v5.3.0 shown —
      # resolve current SHAs for your copies).
      - uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683  # v4.2.2
        with:
          fetch-depth: 0   # setuptools-scm + tag comparison need history
          # checkout defaults to github.sha on push events — the exact
          # commit the gate job validated; do not override with a branch ref
      - uses: actions/setup-python@0b93645e9fea7318ecaed2b359559ac225c90a2b  # v5.3.0
        with:
          python-version: '3.13'
      - run: pip install "forge-scripts @ git+https://github.com/misnaej/forge.git@main"
      - run: forge-release --from-changelog
```

The `needs: test` gate means a broken merge never becomes a released
tag. Repos that prefer tag-immediately + forward-fix (a patch PR opens
the next heading) can drop the `needs:` line — both modes are
supported; gated is the recommended default because a `v*` tag is
distribution the moment it lands.

The job runs on a detached `HEAD`; `forge-release --from-changelog`
verifies it is the tip of `origin/<base_branch>` instead of requiring a
branch checkout. Only the `push` trigger is safe here — never run a
`contents: write` job from `pull_request_target`.

### Dual-track plugin repos (manifest-declared version)

Same idea, tagging with the rolling-next version from
`.claude-plugin/plugin.json`. **The reference implementation is forge's
own `.github/workflows/tag-release.yml`** — a workflow separate from
the read-only CI one, gated via `workflow_run` (single-track: one
tag-on-merge job).

One `workflow_run` gotcha: GitHub evaluates the trigger from the
workflow definition on the repo's **default branch** — a
`workflow_run`-based tag workflow starts firing only once the file
itself has landed there. Until then the manual tagging path (or an
inline `needs:` job, which has no such lag) keeps working.

### Safety details (both flavors)

- **Keep the write surface separate.** A dedicated tag workflow (or at
  minimum a dedicated job) holds the only `contents: write`; your CI
  workflow stays read-only. Cross-workflow gating uses `workflow_run` +
  a `conclusion == 'success'` check.
- **Filter to `push` events.** CI also completes for PR runs — fork PRs
  included — so a `workflow_run`-triggered write job must check
  `github.event.workflow_run.event == 'push'` (and the branch) before
  doing anything.
- **Tag the validated commit, never the tip.** Check out
  `${{ github.event.workflow_run.head_sha }}` (or `${{ github.sha }}`
  in an inline job) — a branch `ref:` re-resolves the tip and can tag a
  fast-following, unvalidated merge. With `forge-next-prep`, pass
  `--no-sync` so the CLI stays on that pinned commit.
- **SHA-pin actions** in any write-capable workflow.

With either job in place, `/next`'s tag step becomes a no-op fallback
(both CLIs are idempotent), and the manual `forge-release` recipe in
[`consumer-release.md`](../docs/consumer-release.md) remains available for
repos without the workflow.

## Auth troubleshooting

| Symptom | Fix |
|---|---|
| `forge-upgrade --apply` aborts with `git_auth_mode() == "none"` in CI | Add `GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}` to the step's `env`. |
| pip hangs on credential prompt | The runner lacks both SSH keys and a GitHub token. Inject `GITHUB_TOKEN` as above. |
| Fork is private, CI can't clone | See [`ci-access.md`](../docs/ci-access.md) for deploy-key + token recipes. |

`forge.run_context.git_auth_mode()` picks the URL form
(`ssh` / `https-token` / `https-anonymous` / `none`) based on what the
runner can actually authenticate against — see
[FOUNDATION §15](../FOUNDATION.md#15-runtime-context-awareness).

---

## Why no `--skip doctor --skip audit-deps`?

`forge-doctor` and `forge-audit-deps` both consult
`forge.run_context.is_non_interactive()` and self-skip when stdin
isn't a TTY or `$CI` is set. The bootstrap CLI announces each skip
in its log line — explicit, not silent. CI jobs need no `--skip`
flags.

To force `doctor` to run in CI (e.g. on a release-gate job where you
want the install report), invert the gate by setting `CI=` (empty)
for that step. The skip is convenience, not a guarantee.
