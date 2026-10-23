---
pattern: OPT53
impact: MEDIUM
class: static
detector: repo-file-check
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml'
fix_strategy: unstable-env-vars-invalidating-turbo-cache
title_template: 'Unstable Env Vars Invalidating Turbo Cache'
category: 11
---

# OPT53 — Unstable Env Vars Invalidating Turbo Cache

**Anti-pattern**: Environment variables that change between runs (timestamps, build numbers) included in Turbo's env hash, causing cache misses.

**Detection heuristic**:

```bash
# Check turbo.json for globalEnv and task-level env
cat turbo.json | jq '.globalEnv, (.tasks // .pipeline | .[].env)'
# Flag known-unstable vars: GITHUB_RUN_ID, GITHUB_RUN_NUMBER, BUILD_NUMBER
```

**Fix**: Remove unstable env vars from Turbo's env configuration, or use `globalPassThroughEnv` for vars that should be available but not affect caching.

---
