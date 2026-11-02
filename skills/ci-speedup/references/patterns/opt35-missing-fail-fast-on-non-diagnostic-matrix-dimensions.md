---
pattern: OPT35
impact: LOW
class: static
detector: yaml-path
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml'
fix_strategy: missing-fail-fast-on-non-diagnostic-matrix-dimensions
title_template: 'Missing `fail-fast` on Non-Diagnostic Matrix Dimensions'
category: 7
---

# OPT35 — Missing `fail-fast` on Non-Diagnostic Matrix Dimensions

**TL;DR**: A sharded test matrix sets `fail-fast: false`, so when one shard fails the rest keep running and burning minutes on an already-failing run.

**Anti-pattern**: An explicit `fail-fast: false` on a shard-indexed (non-diagnostic) matrix. An ABSENT `fail-fast` is NOT a finding — GitHub Actions defaults to `fail-fast: true`, so only the explicit opt-out wastes compute.

**Detection heuristic**:

```bash
grep -rn 'fail-fast: false' .github/workflows/
# Flag only explicit fail-fast: false on shard-indexed (non-diagnostic) matrices;
# an absent fail-fast already fail-fasts (GHA default: true)
```

**Fix**: Remove the `fail-fast: false` (or set `fail-fast: true`) unless you need all matrix variants to complete for diagnostic purposes (e.g., cross-platform compatibility testing — per-OS/per-version matrices, where `fail-fast: false` is correct).

**Tier-2 render note**: When run history shows an explicit `strategy.fail-fast:
false` shard/partition/chunk matrix where a failed or timed-out shard completed
before sibling shards, OPT35 can promote as measured post-completion waste. The
finding must credit only sibling runtime after the first failed shard, carry
`sizing_basis=measured`, and stamp a `post_completion_waste` certificate whose
evidence names the first failed shard, post-failure minutes, and the diagnostic
matrix carve-out. Static OPT35 hits without failed-run post-failure evidence
remain modeled residual findings in "Also noticed"; a matching static row on a
measured workflow/job is superseded by the measured finding.

---
