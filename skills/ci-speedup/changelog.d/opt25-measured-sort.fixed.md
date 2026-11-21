- **2026-11-21** — Keep OPT25's positional-splitter warning for custom sort
  keys and comparators unless they demonstrably read timings or weights (#467).
- **2026-11-21** — Keep OPT25's positional-splitter warning quiet for
  timing-sorted lists that read a compound-named timings map
  (`weights_map`, `test_durations`, `testTimings`, `durationMs`) or a
  same-file key helper, and say when an imported key could not be read (#467).
