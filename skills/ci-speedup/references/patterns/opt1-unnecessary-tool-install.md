---
pattern: OPT1
impact: MEDIUM
class: static
detector: yaml-job-correlated
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml'
fix_strategy: unnecessary-tool-install
title_template: 'Unnecessary Tool Install'
category: 1
---

# OPT1 — Unnecessary Tool Install

**Anti-pattern**: Installing tools (e.g., Playwright browsers) in jobs that don't use them.
A vitest unit-test job that runs `npx playwright install` wastes ~30-90s downloading browsers it never launches.

**Detection heuristic**:

- Search for `playwright install` in jobs whose steps never reference `playwright test` or `@playwright/test`
- Search for tool install steps where the tool binary is never invoked later

```bash
# Find playwright install in non-e2e jobs
gh api repos/{owner}/{repo}/contents/.github/workflows --jq '.[].name' | while read f; do
  content=$(gh api repos/{owner}/{repo}/contents/.github/workflows/$f --jq '.content' | base64 -d)
  if echo "$content" | grep -q 'playwright install' && ! echo "$content" | grep -q 'playwright test'; then
    echo "OPT1 hit: $f installs Playwright but never runs Playwright tests"
  fi
done
```

**Fix**: Remove the install step from jobs that don't need it. If a shared setup action includes it, make it conditional or split the action.

**Real-world example (better-auth)**: Adapter integration jobs installed Playwright browsers despite only running vitest. Removing the install saved ~45s per adapter job (6 matrix variants × 45s = ~270s total).

---
