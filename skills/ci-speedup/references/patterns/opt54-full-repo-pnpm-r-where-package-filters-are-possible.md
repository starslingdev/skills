---
pattern: OPT54
impact: MEDIUM
class: static
detector: yaml-path
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml'
fix_strategy: full-repo-pnpm-r-where-package-filters-are-possible
title_template: 'Full-Repo `pnpm -r` Where Package Filters Are Possible'
category: 11
---

# OPT54 — Full-Repo `pnpm -r` Where Package Filters Are Possible

**Anti-pattern**: Running `pnpm -r <command>` across all packages when only a subset needs the command.

**Detection heuristic**:

```bash
grep -rn 'pnpm -r\|pnpm --recursive\|pnpm run -r' .github/workflows/
```

**Fix**: Use `pnpm --filter <package>` or Turbo's `--filter` to scope commands to relevant packages.

---
