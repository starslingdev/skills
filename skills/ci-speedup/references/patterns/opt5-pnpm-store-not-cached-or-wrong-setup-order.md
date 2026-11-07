---
pattern: OPT5
impact: MEDIUM
class: static
detector: yaml-job-correlated
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml'
fix_strategy: pnpm-store-not-cached-or-wrong-setup-order
title_template: 'pnpm Store Not Cached (or Wrong Setup Order)'
category: 1
---

# OPT5 — pnpm Store Not Cached (or Wrong Setup Order)

**TL;DR**: The pnpm package store isn't cached between runs, so every run re-downloads all dependencies from scratch.

**Anti-pattern**: Running `pnpm install` without caching the pnpm store, or setting up Node before pnpm (so the store path isn't available for cache key computation).

**Detection heuristic**:

```bash
# Check setup order: pnpm/action-setup should come before actions/setup-node
# Check if setup-node has cache: 'pnpm' set
grep -rn 'actions/setup-node' .github/workflows/ | head -20
grep -rn 'pnpm/action-setup' .github/workflows/ | head -20
```

**Fix**: Ensure `pnpm/action-setup` runs before `actions/setup-node`, and `setup-node` has `cache: 'pnpm'`.

---
