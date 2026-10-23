---
pattern: OPT23
impact: MEDIUM
class: static
detector: yaml-path
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml'
fix_strategy: single-threaded-matrix-max-parallel-1
title_template: 'Single-Threaded Matrix (`max-parallel: 1`)'
category: 4
---

# OPT23 — Single-Threaded Matrix (`max-parallel: 1`)

**Anti-pattern**: Setting `max-parallel: 1` on a matrix strategy, running all variants sequentially.

**Detection heuristic**:

```bash
grep -rn 'max-parallel' .github/workflows/
```

**Fix**: Remove `max-parallel` or increase it. If sequential execution is needed for resource constraints, document why.

---
