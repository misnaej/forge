# Token optimization

What drains billed tokens in agent sessions, how the providers bill prompt
caching, how to measure with forge, and the levers that move the number.
Policy: [FOUNDATION §18 "Cost Goals"](../FOUNDATION.md#18-cost-goals--test-economy-library-performance-tokens).

"Prompt tokens" = the usage fields the API reports per request, never a
cost by itself. Billed cost is the fresh slice at full price plus cache
reads at a discount.

## How providers bill the cache

| | Anthropic (Claude Code) | Mistral (Vibe) |
|---|---|---|
| Usage fields | `input_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens` | `prompt_tokens` (all prompt), `prompt_tokens_details.cached_tokens` |
| Cache write | **1.25×** base input (5-min TTL), **2×** (1-hour TTL) | no write premium |
| Cache read | **0.10×** base input (some models 0.05×/0.025×) | **10%** of standard input price |
| Billable input | `input + 1.25·cache_creation + 0.10·cache_read` | `(prompt_tokens − cached_tokens) + 0.10·cached_tokens` |
| Hit conditions | stable prefix before the last `cache_control` breakpoint; TTL refreshed on each use | identical shared prefix; 64-token blocks (prompts < 64 tokens never hit); stable `prompt_cache_key` raises, never guarantees |

Sources (re-check when multipliers change — they do):

- <https://platform.claude.com/docs/en/about-claude/pricing> and <https://docs.anthropic.com/en/docs/build-with-claude/prompt-caching>
- <https://docs.mistral.ai/studio/conversations/advanced/prompt-caching>, <https://docs.mistral.ai/api>, <https://mistral.ai/pricing/api/>

Plain English: the provider remembers the *front* of your prompt instead of
re-reading it. Fresh tokens cost full price. Storing that memory once costs
a premium on Anthropic — 1.25× for a 5-minute memory, 2× for 1-hour (the
clock resets on each use; pick 1-hour only when gaps between requests
exceed 5 minutes). Reusing it costs 0.10× — 90% off — so a write pays for
itself after one read (5-min) or two (1-hour). Example: 100k fixed tokens
reused across 50 requests ≈ $15 uncached vs ≈ $1.85 cached (Sonnet rates).
Mistral is simpler: no write premium, cached part at 10%, rest full price.
The caller picks the TTL via `ttl` on the `cache_control` breakpoint
(`ephemeral` = 5m default, `"ttl": "1h"` = 1h at 2×); a read restarts the
5-min clock but never upgrades it. In agent sessions the harness sets this,
not the agent: Claude Code ≥ v2.1.24 via `promptCacheTtl` /
`CLAUDE_CODE_PROMPT_CACHE_TTL`; Mistral has no TTL, just prefix matching
with `prompt_cache_key`. The whole thing only works while the prompt front
is byte-identical — one changing timestamp early in the prompt and every
token becomes fresh full-price input. Consequence: raw prompt can be 10×
billed input on a cache-heavy session; rank by billed-equivalent and watch
the **fresh slice** (`input + cache_write`).

## Cache scope and session lifecycle

- The cache key is a hash of the prompt up to the breakpoint —
  account-wide, cross-request, not tied to a conversation or agent id. In
  practice that is "per agent type": each agent's prefix differs (its agent
  file), so each agent type writes once and reads thereafter; two spawns of
  the same type within the TTL share the entry. Main session vs subagent:
  separate entries.
- **`/clear`** keeps the static prefix (tools, system prompt, instructions,
  agent file) byte-identical → first request of the cleared session is
  mostly cache-read (within TTL). The old history stops being sent — those
  tokens were only cache-read anyway. `/clear` costs context, not cache
  money; `.plan/CONTINUATION.md` carries the state across it.
- **Compaction** (`/compact`, auto-compact) replaces history with a short
  summary: the message layer misses once, then every later request sends
  far fewer tokens. One rewrite buys a permanently smaller tail; the fixed
  prefix stays cached. Never put a per-request changing value at or before
  the breakpoint — a changing breakpoint block writes a fresh entry every
  request and the lookback never hits.

## What busts the cache

- Anything changing before the cached prefix: timestamps, session ids,
  per-turn injected reminders, re-ordered tool results.
- Anthropic: changing thinking/effort settings can invalidate blocks; the
  5-min TTL expires if the session idles.
- Mistral: no identical prefix, no hit; prompts < 64 tokens never hit.
- Forge-side: see the audit below.

## Forge's static surface stays byte-stable

Cache hits need a byte-identical prefix, so the static files are managed
for stability, not just correctness:

- **Shipped once, never hand-edited.** The plugin surface (`agents/`,
  `skills/`, `claude-hooks/`, `plugin.json`) ships inside the pip package
  under `src/forge/data/` (`SHIPPED_SOURCES`, `instruction_files.py`).
  Consumers get a generated wrapper only: `CLAUDE.md` with an
  `@FOUNDATION.md` include (Claude resolves it natively), or a flattened
  `AGENTS.md` on Vibe — the flattener resolves `@`-includes at generation
  time and deliberately leaves markdown links as lazy read-on-demand
  pointers, to keep instruction context small.
- **Regeneration is a no-op when content is unchanged.**
  `install-forge-vibe` runs on every merge/checkout (`run_vibe_regen`) but
  writes through `_write_if_changed`; `gen_vibe_hooks` emits sorted,
  deterministic TOML. Same bytes → same cache hash (the cache hashes
  content, not mtime).
- **Hooks are version-free.** `.githooks/*` wrappers carry no version; it
  lives in the gitignored `.forge-hook-version` sidecar, so upgrades never
  re-stamp hook files.
- **Drift is detected, never silent.** `check_foundation_sync.sh`
  (SessionStart) warns when `CLAUDE.md` drifted from installed
  `FOUNDATION.md`.
- **Forge never sets `cache_control` / `prompt_cache_key` itself** — the
  harness owns breakpoints and TTL. Forge's lever is the byte-stable
  prefix.

## Audit: forge elements that hurt the cache

Measured on one laptop: Claude main sessions 97–99% cache_read, Claude
subagents 83–90%, **Vibe only 47–48%** — on Mistral pricing that means
Vibe pays full price on most of its context. Findings:

1. **`load_continuation.sh` inlines the whole `.plan/CONTINUATION.md` at
   SessionStart (Claude).** The panel is rewritten on every commit, so
   across sessions this variable block changes the prefix hash → full
   cache miss on each new session whenever the note changed. The Vibe twin
   does it right: `vibe_session_start.sh` injects only a "read the file
   now" reminder; content enters via a tool read, i.e. the message layer
   after the breakpoint. *Candidate remedy: make `load_continuation.sh`
   print a one-line pointer and let the agent Read the note on demand.*
2. **Per-turn PostToolUse reminders** (`warn_stale_wrapup.sh`,
   `warn_pr_checks.sh`, `warn_merge_conflicts.sh`) print into the
   conversation tail. Safe for the prefix, but the tail grows and is
   re-sent every request (0.10× on Claude, full price on Vibe).
   *Candidate remedy: write reminders to `code_health/` and print only
   on actionable events* (`warn_stale_wrapup` is already conditional).
3. **Good patterns to keep:** `log_agent_timing.sh` writes only the ledger
   file; `vibe_continuation_state.sh` runs every main turn but redirects
   to /dev/null; SessionStart constants (`caveman_mode.sh` instruction, the
   version line) are cache-safe because they are constant per release.
4. **No `prompt_cache_key` wiring** in forge's generated Vibe config.
   *Candidate remedy: add it to the forge model presets if the harness
   supports it* — Mistral cross-request reuse needs a stable key.

## Measuring

`forge-agent-profile --tokens` scans this checkout's Claude transcripts and
the Vibe unified session store; ranks runs/skills by prompt size with
fixed-context share and waste flags (`single-call`, `fixed-context`,
`large-result`, `long-report`, `repeated-read`). Read it as: **fresh =
input + cache_write** (full price — the cost driver, compare week over
week); **cache_read share** per harness (cache health); **fixed ctx** (what
one spawn pays to load instructions). Per-harness cache% per row is a
planned addition so the Vibe gap stays visible.

## Levers, ranked by measured drain

1. **Shrink what is re-sent every request** — compact or `/clear` between
   tasks, carry state in `.plan/CONTINUATION.md`, read each file once.
2. **Cap tool output** — a ≥20k-char tool result is re-sent on every later
   turn. Forge CLIs write evidence to `code_health/*.log`: print a verdict
   + path, never the whole log. Big command output → file + grep.
3. **Keep the prefix stable** — per-turn injections late in the prompt or
   behind a read-on-demand pointer (see audit item 1).
4. **Pay per-spawn fixed context once** — every subagent spawn loads system
   prompt + FOUNDATION + CLAUDE.md + its agent file + tool defs (tens of k
   tokens; 1.25× write on Anthropic, full price uncached on Mistral).
   Batch delegations; prefer a CLI over an agent step for mechanical work
   (§7); keep agent files in the 400–800-word template budget.
5. **Kill wasted delegations** — `single-call` runs are direct commands
   wearing an agent's fixed context; cap `long-report` hand-backs at a
   summary + pointer.

## Harness notes

- Claude Code transcripts: `~/.claude/projects/<encoded-cwd>/` (main
  `*.jsonl`, subagents `<session>/subagents/agent-*.jsonl`).
- Vibe: unified session store journals (`journal/*.jsonl`); usage fields
  `input_tokens` / `cached_input_tokens` / `cache_write_tokens` /
  `output_tokens`; cumulative `projection_delta` records are a fallback,
  not per-request usage.
