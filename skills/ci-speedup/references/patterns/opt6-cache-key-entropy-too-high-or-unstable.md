---
pattern: OPT6
impact: MEDIUM
class: static
detector: regex
match: 'key:\s*.*\$\{\{\s*github\.(sha|run_id|run_number)'
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml'
fix_strategy: cache-key-entropy-too-high-or-unstable
title_template: 'Cache Key Entropy Too High or Unstable'
category: 1
---

# OPT6 — Cache Key Entropy Too High or Unstable

**Anti-pattern**: Cache keys that include timestamps, random values, or non-deterministic content, causing cache misses on every run.

**Detection heuristic**:

```bash
# Search for cache keys with dynamic values
grep -rn 'actions/cache' .github/workflows/ -A 5 | grep 'key:'
# Flag keys containing: ${{ github.run_id }}, date, timestamp, random
```

**Fix**: Use deterministic cache keys based on lockfile hashes, tool versions, and OS. Use `restore-keys` for fallback matching.

---
