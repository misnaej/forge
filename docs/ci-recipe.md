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

`@main` = slow channel (minor releases only). `@dev` = fast channel
(every patch). See the [release-channel table](../README.md#pick-a-release-channel)
in the README.

A tag pin (`@v1.9.1`) is also supported for one-off frozen releases.
For ongoing automated upgrades the channel pin is the recommended
default: it requires no per-version maintenance and the scheduled
workflow below handles the rest.

## 2. Per-PR CI

`.github/workflows/ci.yml`:

```yaml
name: ci
on:
  push:
    branches: [main, dev]
  pull_request:

jobs:
  test:
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
  channel ref (`@main` / `@dev`) and re-runs `install-forge-bootstrap`.
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

Same idea, tagging the fast channel with the rolling-next version from
`.claude-plugin/plugin.json`. **The reference implementation is forge's
own `.github/workflows/tag-release.yml`** — a workflow separate from
the read-only CI one, gated via `workflow_run`, with a second job that
relocates promoted minor tags on pushes to the base branch
(`forge-check-main-tags --fix`). Copy that file, not a trimmed
illustration.

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
[`consumer-release.md`](consumer-release.md) remains available for
repos without the workflow.

## Auth troubleshooting

| Symptom | Fix |
|---|---|
| `forge-upgrade --apply` aborts with `git_auth_mode() == "none"` in CI | Add `GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}` to the step's `env`. |
| pip hangs on credential prompt | The runner lacks both SSH keys and a GitHub token. Inject `GITHUB_TOKEN` as above. |
| Fork is private, CI can't clone | See [`ci-access.md`](ci-access.md) for deploy-key + token recipes. |

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
