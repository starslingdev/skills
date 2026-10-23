---
pattern: OPT34
impact: MEDIUM
class: static
detector: yaml-on-trigger
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml'
fix_strategy: no-changed-package-filtering
title_template: 'No Changed-Package Filtering'
category: 7
---

# OPT34 — No Changed-Package Filtering

**Anti-pattern**: In monorepos, running all tests/builds regardless of which packages changed.

**Detection heuristic**:

```bash
# Check if turbo is used with --filter for PR workflows
grep -rn 'turbo' .github/workflows/ | grep -v 'TURBO_'
# Flag turbo run commands without --filter=...[base]
```

**Fix**: Use `turbo --filter=...[origin/main]` to only run tasks for changed packages and their dependents (`main` is illustrative — substitute the repo's actual base branch; on PR-triggered runs `origin/${{ github.base_ref }}` resolves it dynamically, so this doesn't break on repos whose default branch isn't `main`). The base ref must exist in the clone: the default `actions/checkout` is a shallow, single-branch clone where that base ref does NOT resolve — add `fetch-depth: 0` (or a targeted `git fetch origin <base-branch>` step) to that job's checkout, and treat this as an explicit exception to OPT28's shallow-checkout guidance.

**Required-status-check caveat**: if the per-package jobs are required status checks, gate them with a job-level `if:` (or an in-job filter like turbo's) so skipped packages still report Success — never by narrowing the workflow's `paths:`/triggers, which leaves required checks "Pending" and blocks the merge (see OPT32's caveat for the full mechanism). Treat required-status UNKNOWN as required when branch protection/rulesets are unreadable.

---
