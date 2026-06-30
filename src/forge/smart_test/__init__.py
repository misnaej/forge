"""forge.smart_test — change-driven test selection by import depth.

Given the files a changeset touched, select only the tests that exercise
that code — directly or transitively through imports — and run them in
escalating depth tiers (0 → 1 → 2 → full). Static ``ast`` import-graph
analysis only; no runtime instrumentation. See FOUNDATION §17 for the
depth model and the speed/coverage trade-off.

Modules:

- :mod:`forge.smart_test.git_helpers` — diff-base resolution + changed-file
  enumeration (layered on :mod:`forge.git_utils`).
- :mod:`forge.smart_test.dependencies` — reverse test→source import graph
  and depth expansion (built on :mod:`forge.import_graph`).
- :mod:`forge.smart_test.runner` — a single ``pytest`` invocation per
  depth, with import-cache hygiene.
- :mod:`forge.smart_test.cli` — the ``forge-smart-test`` entry point.
"""
