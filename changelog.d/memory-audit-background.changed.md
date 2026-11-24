bump: minor
- **`/memory-audit` is now dispatched to a background agent at implementation start** — `/next` at task pickup and `/sentinel` at plan pickup spawn it as a background child that runs propose-only and writes its report to `code_health/memory-audit-proposals.md` (gitignored, survives the session). No more competing for attention mid-task, and a decline no longer vanishes: the proposals file is the trace.
- **One re-offer after the wrap-up posts**: `/pr` offers `/memory-audit` once more when pending proposals exist — the natural pause — never blocking, one re-ask only.
- The rule-surface PR offer and the on-demand invocation are unchanged; interactive audits still confirm-first and never run silently.
