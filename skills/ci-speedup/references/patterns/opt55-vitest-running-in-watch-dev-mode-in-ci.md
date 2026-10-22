---
pattern: OPT55
impact: MEDIUM
class: static
detector: regex
match: 'vitest\s+(watch\b|.*--watch)'
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml'
fix_strategy: vitest-running-in-watch-dev-mode-in-ci
title_template: 'vitest Running in Watch/Dev Mode in CI'
category: 11
---

# OPT55 — vitest Running in Watch/Dev Mode in CI

**Anti-pattern**: vitest running in watch mode in CI, which never terminates naturally.

**Detection heuristic**:

```bash
# Check for vitest without --run flag or with --watch
grep -rn 'vitest' .github/workflows/ | grep -v '\-\-run'
```

**Fix**: Always use `vitest --run` in CI, or set `CI=true` (vitest auto-detects CI and disables watch mode).

---
