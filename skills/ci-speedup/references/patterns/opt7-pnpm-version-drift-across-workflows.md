---
pattern: OPT7
impact: LOW
class: static
detector: yaml-workflow-correlated
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml'
fix_strategy: pnpm-version-drift-across-workflows
title_template: 'pnpm Version Drift Across Workflows'
category: 1
---

# OPT7 — pnpm Version Drift Across Workflows

**Anti-pattern**: Different workflows or jobs specifying different pnpm versions, reducing cache compatibility between them.

**Detection heuristic**:

```bash
# Extract all pnpm versions across workflows
grep -rn 'version:' .github/workflows/ | grep -i pnpm
grep -rn 'packageManager' package.json
```

**Fix**: Pin pnpm version in `package.json` `packageManager` field and reference it in all workflows, or use `pnpm/action-setup` without explicit version to auto-detect from `packageManager`.

---
