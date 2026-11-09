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
# Flag ${{ github.run_id }} only without a compatible restore-keys prefix; always flag date, timestamp, random
```

**Fix**: Match the key shape to what is cached. Dependency download stores use a deterministic lockfile + tool-version + OS key. Tool-result and build-output caches change as the job runs, so use a rolling primary key such as `<tool>-<os>-<lockfile-hash>-${{ github.sha }}` with a stable `restore-keys` prefix such as `<tool>-<os>-<lockfile-hash>-`. An exact primary-key hit is not saved again; the rolling suffix lets the updated cache be saved while the stable prefix restores the newest compatible result. Each rolling entry consumes cache budget, so reserve the per-commit shape for small tool-result caches. Large dependency or build stores need a separately measured key and retention strategy; a stable exact key can also prevent updated mutable state from being saved.

---
