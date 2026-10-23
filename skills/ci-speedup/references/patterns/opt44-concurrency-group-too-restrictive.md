---
pattern: OPT44
impact: MEDIUM
class: static
detector: yaml-path
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml'
fix_strategy: concurrency-group-too-restrictive
title_template: 'Concurrency Group Too Restrictive'
category: 9
---

# OPT44 — Concurrency Group Too Restrictive

**Anti-pattern**: Concurrency groups that are too narrow, causing jobs to queue or get cancelled unnecessarily.

**Detection heuristic**:

```bash
grep -rn 'concurrency:' .github/workflows/ -A 3
# Check group key granularity
```

**Fix**: Use broader groups (e.g., per-workflow per-branch instead of per-job per-branch).

---
