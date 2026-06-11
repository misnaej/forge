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
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
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

      - run: pytest -q
```

Notes:

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
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install project + forge-scripts
        run: pip install -e ".[dev]"

      - name: Run forge-upgrade --apply
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: forge-upgrade --apply

      - name: Open PR if anything changed
        uses: peter-evans/create-pull-request@v6
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
