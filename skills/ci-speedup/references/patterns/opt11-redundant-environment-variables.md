---
pattern: OPT11
impact: LOW
class: static
detector: yaml-path
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml'
fix_strategy: redundant-environment-variables
title_template: 'Redundant Environment Variables'
category: 2
---

# OPT11 — Redundant Environment Variables

**Anti-pattern**: Setting the same env var at both the workflow/job level AND the step level, creating noise and maintenance burden.

**Detection heuristic**:

```bash
# Parse workflow YAML for env blocks at workflow, job, and step levels
# Compare for duplicates within the same scope chain
```

**Fix**: Set env vars at the highest applicable scope only. Remove step-level overrides that match job/workflow-level values.

**Real-world example (better-auth)**: `TURBO_TOKEN` and `TURBO_TEAM` set globally in ci.yml AND repeated in individual steps.

---
