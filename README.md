# Forge

<!-- forge:badges:start -->
[![CI](https://github.com/misnaej/forge/actions/workflows/ci.yml/badge.svg)](https://github.com/misnaej/forge/actions/workflows/ci.yml) [![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/downloads/) [![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff) ![License](https://img.shields.io/badge/License-MIT-green) ![forge](https://img.shields.io/badge/forge-enabled-blue) ![Claude Code](https://img.shields.io/badge/Claude_Code-555?logo=claude) ![Docstring coverage](.badges/docstring-coverage.svg)
<!-- forge:badges:end -->

AI agents write code quickly, and without hard limits quality drifts:
lint rules get switched off, docstrings go stale, and "please don't
force-push" is a sentence an agent can ignore. Forge turns a repository's
standards into checks that run on every commit, and gives coding agents
guardrails that refuse unsafe actions outright. The same tools run for a
person at a terminal, in CI, and for an agent.

## What you get

- **A pre-commit gate.** One hook runs lint, formatting, docstring
  accuracy, test naming, structure, type and dependency checks, and
  writes each result to a log that people and agents read back.
- **A Claude Code plugin** (optional). Agents and skills for the whole
  loop — plan an issue, write and test the change, review it, open the
  PR with a verified wrap-up — each step backed by a forge command, not
  by the model's judgement.
- **Safety hooks.** Agents cannot force-push, rebase, merge a PR, delete
  a protected branch, bypass the pre-commit hook or install dependencies.
- **Backlog on GitHub.** A label schema, triage, and a pipeline that
  turns an issue into a validated plan before anyone works on it.
- **Audits.** Dead code, duplication, dependency layering, suppressions
  and claims that no longer match the code.

Everything except the plugin works without any AI tool.

## Try it

```bash
pip install "git+https://github.com/misnaej/forge.git@vX.Y.Z"   # latest release
install-forge-bootstrap        # hooks, labels, docs, plugin, then forge-doctor
git commit                     # the gate now runs on every commit
```

Pin a [release tag](https://github.com/misnaej/forge/releases), and add
the same pin to your project's dev dependencies so the version is
tracked. `install-forge-bootstrap` is safe to re-run after every upgrade.

## How it works

Every check is a plain command (`forge-precommit`, `forge-doctor`, the
`verify-forge-*` and `forge-audit-*` tools), configured under
`[tool.forge]` in `pyproject.toml`. The git hook calls those commands;
the plugin's agents call the same commands and act on their output, and
its hooks stop an agent before a forbidden git or GitHub operation runs.
The model decides what to write; forge decides whether it passes.

## Documentation

| I want to… | Read |
|---|---|
| Adopt forge one layer at a time, upgrade it, or pin a version | [Adopting forge](docs/adopting.md) |
| Set options and see every key | [Configuration](forge-docs/configuration.md) |
| Run forge in CI | [CI recipe](forge-docs/ci-recipe.md) |
| Use the agents, skills and hooks | [Claude Code plugin](docs/claude-code-plugin.md) · [Mistral Vibe](docs/vibe.md) · [Agent architecture](docs/agent-architecture.md) |
| Add my own pre-commit step | [Customising pre-commit](docs/customizing-precommit.md) |
| Run code-health audits | [Audit pack](docs/audit-pack.md) |
| Release my own package with forge | [Consumer releases](docs/consumer-release.md) |
| Look up a command | [CLI reference](docs/cli-reference.md) |
| Read the rules forge gives every agent | [FOUNDATION.md](FOUNDATION.md) |
| Understand forge's own design | [Architecture](docs/architecture.md) · [Repository map](REPO_STRUCTURE.md) |

## Status

Forge is open source and maintained around its owner's own work. It is
being consolidated until early 2027 and takes no new directions before
then. Reports go through forge itself, with `/report-to-forge`; see
[CONTRIBUTING](CONTRIBUTING.md).

## License

[MIT](LICENSE).
