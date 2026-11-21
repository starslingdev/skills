- **2026-11-21** — A `<<:` merge under `jobs:` now resolves in the line-attribution
  walker exactly as in the loaded workflow: a job merged in through a chained
  merge is scanned at its own lines (a template injection in one was missed), a
  duplicate job key inside a merge source takes its last value, and a mapping
  merged into itself no longer stops the scan with a RecursionError (#453).
