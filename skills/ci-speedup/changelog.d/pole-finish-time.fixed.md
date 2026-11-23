- **2026-11-23** — The long-pole ranking and the report's "gates N/M" counts now credit each
  sampled PR's pole to the check that finishes last (its start offset plus its p50-capped span)
  instead of the longest span, so a short job that `needs:` a long one, and finishes after it,
  counts as the gate the PR waits on. Such a job is no longer dropped as bookkeeping or as a
  short required aggregator when its `needs:` reach a measured check of 60 s or more; when it is
  an aggregation sink, its drill points at its slowest upstream job. `verify_report` re-derives
  the same rule from new per-PR start offsets (#491).
