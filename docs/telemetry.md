# Resource telemetry — profiling test and command runs

`forge-telemetry` answers "what does this run actually cost?": it wraps
any command, samples the command's **process-tree RSS** and the **host
CPU** while it runs, and writes a resource timeline you can read
(`code_health/telemetry.log`) and see (`code_health/telemetry.png`).
The wrapper is transparent — the child's stdout/stderr and exit code
pass through untouched.

Needs the **`[telemetry]` extra** (`psutil` for sampling, `matplotlib`
for the chart):

```bash
pip install "forge-scripts[telemetry]"
```

Without `psutil` the CLI fails loudly with that install hint; without
`matplotlib` the text log still writes and the chart is skipped with a
logged notice.

## Wrapping a command

Everything after the literal `--` is the wrapped command, verbatim:

```bash
forge-telemetry -- python workload.py
forge-telemetry -- pytest tests/test_pipeline.py
```

The child's exit code is propagated unchanged, so the wrapper drops
into scripts and CI steps without altering pass/fail semantics.

## Profiling a smart-test run

```bash
forge-smart-test --depth 1 --telemetry
```

Each pytest batch runs under the same sampler. If `psutil` is absent
the test run proceeds unprofiled (a missing profiler must never fail
the tests) with the install hint in the run output.

## Example output

A staged workload — four ~60 MB allocations, each followed by a CPU
burn and a pause — produces this profile:

![Example telemetry chart: process-tree RSS staircase and host CPU
trace over an 11-second run](images/telemetry-example.png)

The blue staircase is the process tree's resident memory (each step
one allocation stage, held to the end); the orange trace is host CPU,
spiking during each burn and dipping in the pauses. The matching text
log carries the same series plus a summary:

```text
command: python workload.py
duration: 10.9s   exit code: 0   samples: 42

t=     0.0s  rss=      6.6MB  cpu=  0.0%
t=     0.3s  rss=     69.3MB  cpu= 18.1%
...
peak rss: 249.7MB   mean cpu: 12.4%
```

## Reading a profile — what warrants investigation

The chart is a triage tool: most runs need one glance. These shapes
are the ones worth chasing:

- **RSS climbs and never comes back down** while the workload is
  phase-structured (load → process → write): later phases are paying
  for memory the earlier ones no longer need — look for caches,
  accumulating lists, or references pinned past their phase. A
  *held* plateau (like the example's staircase) is only fine when the
  data genuinely must stay live.
- **RSS grows linearly with items processed** in a loop that should
  be O(1) per item — the classic leak signature; bisect by running
  fewer items and comparing peaks.
- **Peak RSS near the runner's memory limit**: a run that passes
  locally at 90% of a CI container's limit is one dependency bump away
  from an OOM kill. Compare `peak rss` against the environment's
  actual ceiling, not your workstation's.
- **CPU flat near zero for long stretches** while wall-clock advances:
  the process is waiting, not working — I/O stalls, lock contention, a
  hung subprocess, or `sleep`-based polling. Long test runs with low
  mean CPU are usually parallelizable or waiting on something they
  shouldn't be.
- **CPU pinned high across the whole run** with modest RSS: compute
  bound — a candidate for the `forge:perf-optimizer` benchmark loop
  rather than more memory.
- **A spike that moved between runs**: compare two saved logs (copy
  them out of `code_health/` first — each run overwrites) before and
  after a change; a new spike at a new timestamp localizes the
  regression to the phase running at that offset.

For per-test wall-clock outliers (which test is slow, rather than what
the run consumes), `forge-slow-tests-report` is the sharper tool — the
two read well together: slow-tests names the test, telemetry shows
what the run was doing while it was slow.

## Configuration

Both entry points read `[tool.forge.telemetry]` — full reference in
[`docs/configuration.md`](configuration.md#tool-forge-telemetry--resource-profiling):

```toml
[tool.forge.telemetry]
sample_interval = 0.25   # seconds between samples (default 1.0, floor 0.1)
plot = true              # render the PNG chart (default true)
```

There is no ambient "enabled" switch — telemetry runs only when
invoked (the CLI or the `--telemetry` flag), so runs that never ask
for it never pay for it.

## Artifacts

| File | Contents |
|---|---|
| `code_health/telemetry.log` | Header (command, duration, exit code, sample count), one line per sample, peak-RSS / mean-CPU summary |
| `code_health/telemetry.png` | Two-axis chart: process-tree RSS (MB) + host CPU (%) over elapsed seconds |

Both land in `code_health/` (FOUNDATION §13's artifact convention,
typically gitignored) and are overwritten on each run — copy one out
if you want to keep a baseline to compare against.
