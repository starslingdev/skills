---
pattern: OPT63
impact: MEDIUM
class: static
detector: yaml-job-correlated
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml'
fix_strategy: dependency-install-with-cache-disabled
title_template: 'Dependency Install with Cache Disabled'
category: 12
---

# OPT63 — Dependency Install with Cache Disabled

**Anti-pattern**: Package manager invoked with explicit no-cache flags on persistent runners, defeating the benefit of workspace persistence.

**Detection heuristic**:

```bash
grep -rn '\-\-no-cache\|--no-cache-dir\|--force-reinstall\|--cache /dev/null' /tmp/workflows/
```

Flag only when the job runs on `self-hosted` runners. On GitHub-hosted runners, there's no persistent cache to defeat.

**Fix**: Remove the no-cache flag. Package managers (uv, pip, npm) handle cache invalidation correctly — the flag is unnecessarily conservative on persistent runners.
