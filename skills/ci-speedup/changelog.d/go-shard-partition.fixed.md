- **2026-11-21** — Require Go matrix filters to partition readable package
  tests disjointly and exhaustively before calling a job already sharded (#466).
  A provable non-partition (repeated value, single leg, a test in no leg or in
  several) gets the normal OPT24 finding naming the test; a split that cannot
  be checked (generated or file-fed values, wildcard or unreadable package,
  subtest filters, build-constrained tests) produces no finding and is listed
  under a new "test sharding: held back" row. The package may follow the filter,
  sit under a working directory or be named by import path; include entries and
  step env indirection are read, and `TestMain` is not counted as a test.
