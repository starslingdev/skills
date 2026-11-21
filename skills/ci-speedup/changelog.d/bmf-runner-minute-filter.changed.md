- **2026-11-20** — Recommend runner-minute-saving or neutral changes by default, with `--bmf` as an explicit opt-in for wall-clock levers that add runner use or change to an unpriced larger runner (#459).
  The held set is enumerated: OPT24 sharding (including the Go variant), OPT83's sibling-job split,
  OPT81 runner moves other than a same-speed downsize, OPT25's split-the-slow-leg case, OPT97's
  own-job move, and an OPT21 edge whose measured dependency failures add runner minutes. Log-leaf
  remedies that shard across jobs or runners keep their measured drill and in-job route, with the
  cross-job option held back, and an unsized skip, cache or dedupe lever is never held.
