"""Forge audit pack: deterministic scripts that surface design issues.

Each submodule exposes a ``main()`` entry point used by a console script
(see ``pyproject.toml``). All scripts write findings to
``code_health/audit_<name>.log`` — the foundation `code_health/` convention.
Agents read these logs as the source of truth; the scripts are runnable by
any human or CI.

See ``forge/docs/audit-pack.md`` for the per-script reference and the link
to Robert C. Martin's package-design principles (ADP, SDP, SAP, CCP, CRP).
"""
