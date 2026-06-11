# Security guidelines for forge consumers

This document covers the security boundary forge enforces and what
consumers should layer on top. It is **not** an exhaustive security
manual — for OWASP-grade reference material, see [External
references](#external-references) at the end.

> **Audience**: developers and contributors on forge consumer
> repositories. Applies whether or not you use Claude Code.

---

## What forge enforces

Four concrete mechanisms ship with forge:

| Mechanism | What it does | Where |
|---|---|---|
| `block_protected_files` Claude Code hook | Blocks edits to known secret files (`.env`, `.hf_token`, etc.) before they reach git | `${CLAUDE_PLUGIN_ROOT}/claude-hooks/block_protected_files.sh` |
| `pip_audit` pre-commit step | Scans dependencies for known CVEs on every commit (non-blocking warning) | `forge-precommit` sequence |
| FOUNDATION §2 rules | "No secrets in code or commits"; "No private organizational names in code, docs, or examples" | [`FOUNDATION.md`](../FOUNDATION.md) |
| `forge:security-checker` agent | Reviews PRs for dangerous patterns, secrets, dependency issues, agent prompt safety | `agents/security-checker.md` |

Everything else in this document is **policy + guidance** that
consumer repos and reviewers apply manually.

---

## Secrets

The hard rules (enforced by FOUNDATION §2):

- **Never commit secrets.** Use `.env` (gitignored) or env vars at runtime.
- **Provide `.env.example`** with placeholder values so contributors know what env vars the app needs.
- **Restrict file permissions** on local secret files: `chmod 600 .env`.
- **No private organizational names** in code, docs, or examples — generic placeholders or in-repo concrete names only.

What forge tooling does:

- The pre-commit hook scans for committed secrets via the consumer's secret-scanning step (not bundled — add `gitleaks` or `trufflehog` if you want strict enforcement).
- `block_protected_files` (Claude Code hook) prevents agents from editing known secret files.

What consumers do:

- Use fine-grained, scoped tokens (not blanket ones).
- Rotate periodically.
- Use dedicated secret managers in production (AWS Secrets Manager, Vault, etc.) instead of env vars.

---

## Dependency security

Forge enforces:

- **`pip_audit` step** in the pre-commit sequence — scans installed
  packages against the PyPI advisory DB. Reports CVEs as a yellow
  `WARN` (non-blocking by design — failures shouldn't refuse the
  commit; consumers escalate via CI if desired).

What consumers do:

- **Pin direct dependencies** in `pyproject.toml` (`package>=1.2,<2`).
- **Use a lockfile** in CI (e.g. `pip-compile` + `pip-tools`, or `uv lock`).
- **Update on a schedule** — set up Dependabot or Renovate to open
  PRs for security advisories.

---

## CI/CD

- **Use OIDC** for cloud auth (no long-lived secrets in CI secrets).
- **Pin GitHub Actions** to SHAs, not tags (`uses: actions/checkout@v4` →
  `uses: actions/checkout@<commit-sha>`).
- **Limit `permissions:`** at workflow level to the minimum needed.
- **Never log secrets**, even masked — they end up in CI artifacts.

For forge access from CI runners, see [`docs/ci-access.md`](ci-access.md).
Forge is public — no auth required.

---

## Code patterns to avoid

The `forge:security-checker` agent flags these on review:

- `eval()`, `exec()`, `compile()` on user input — RCE.
- `pickle.loads()` / `marshal.loads()` on untrusted data — RCE.
- `subprocess` with `shell=True` and any user-controlled component — command injection.
- `yaml.load()` without `Loader=yaml.SafeLoader` — RCE.
- SQL queries built via string concatenation — SQL injection.
- File paths built without `Path.resolve().relative_to(allowed_root)` — path traversal.

Safe alternatives:

- `json.loads()` for serialised data.
- `subprocess.run([list, of, args])` (no `shell=True`).
- `yaml.safe_load()`.
- Parameterised SQL queries (`cursor.execute("... WHERE id=%s", (id,))`).

---

## Agent prompt security

If you ship Claude Code agents:

- Treat agent prompts as **untrusted input** when they originate from
  PR comments, issue bodies, or external sources.
- Never let an agent's prompt directly drive privileged operations
  (deploy, drop tables, send email) without a confirmation gate.
- Strip / sanitise any URL → fetch → execute chain.
- Forge's own agents pin their tool list explicitly in frontmatter so
  they can't acquire `Write` / `Edit` permissions a reviewer didn't
  approve.

---

## Pre-merge checklist

Before merging anything security-sensitive:

- [ ] No secrets in the diff (run a secret scanner).
- [ ] No new `eval` / `exec` / `pickle` / `shell=True` on user input.
- [ ] Dependencies pinned; `pip-audit` shows no unresolved high-severity CVEs.
- [ ] If adding a Claude Code agent: tool list is minimal; no `Write` / `Edit` for reporters.
- [ ] If adding a workflow: permissions block is minimal; actions pinned to SHAs.

---

## External references

For comprehensive guidance forge doesn't reproduce:

- [OWASP Top 10](https://owasp.org/Top10/) — web application security.
- [OWASP Cheat Sheet Series](https://cheatsheetseries.owasp.org/) — language-agnostic security patterns.
- [pip-audit docs](https://pypi.org/project/pip-audit/) — dependency scanning details.
- [GitHub OIDC docs](https://docs.github.com/actions/deployment/security-hardening-your-deployments) — cloud auth from CI.
- [supply-chain security (SLSA)](https://slsa.dev/) — build provenance.

---

## See also

- [`FOUNDATION.md` §2](../FOUNDATION.md#2-core-safety-rules) — core safety rules (no secrets, no private org names, etc.).
- [`docs/ci-access.md`](ci-access.md) — CI runner credentials for forge.
- `agents/security-checker.md` — the agent that reviews PRs for the above.
