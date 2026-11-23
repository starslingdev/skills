- **2026-11-23** — The long-pole ranking now credits each sampled PR's pole to the check that
  finishes last (its start offset plus its p50-capped span) instead of the longest span, so a
  short job that `needs:` a long one, and finishes after it, is counted as the gate the PR
  waits on; `verify_report` re-derives the same rule from new per-PR start offsets (#TBD).
