bump: patch

- `forge-slow-tests-report --coverage-json` and `forge-smart-test --coverage-json` now stream the `coverage json --show-contexts` export one file entry at a time through a shared loader, instead of parsing the whole document into memory. Peak RSS no longer scales with the export size (measured on a 0.85 GiB export: ~3.5 GiB whole-document vs ~60 MiB streamed), so large suites no longer get the reporter killed by the OS with no output.
- An export too large for the memory actually available (file size × 2 against cgroup/`MemAvailable`/physical memory) is now declined with a warning naming the export size and the available memory — a diagnosable decline instead of a silent signal death.
- The decline prints the reporter's usual "no usable coverage export" skip line and still exits 0, so the documented non-gating contract now holds under memory pressure too.
