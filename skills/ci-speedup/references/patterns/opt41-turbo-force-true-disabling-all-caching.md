---
pattern: OPT41
impact: HIGH
class: static
detector: regex
match: 'TURBO_FORCE:\s*[\"'']?true'
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml'
fix_strategy: turbo-force-true-disabling-all-caching
title_template: '`TURBO_FORCE: true` Disabling All Caching'
category: 8
---

# OPT41 — `TURBO_FORCE: true` Disabling All Caching

**Anti-pattern**: Setting `TURBO_FORCE: true` in release workflows, which forces all tasks to re-execute and ignores both local and remote cache.

**Detection heuristic**:

```bash
grep -rn 'TURBO_FORCE' .github/workflows/
```

**Fix**: Remove `TURBO_FORCE: true` unless there's a documented reason for it. If freshness is needed, invalidate specific caches instead.

---
