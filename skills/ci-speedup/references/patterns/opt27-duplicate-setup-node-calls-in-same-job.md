---
pattern: OPT27
impact: LOW
class: static
detector: yaml-job-correlated
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml'
fix_strategy: duplicate-setup-node-calls-in-same-job
title_template: 'Duplicate `setup-node` Calls in Same Job'
category: 5
---

# OPT27 — Duplicate `setup-node` Calls in Same Job

**Anti-pattern**: Calling `actions/setup-node` more than once in the same job.

**Detection heuristic**:

```bash
# Count setup-node occurrences per job
# Parse YAML and count within each job's steps
```

**Fix**: Remove duplicate calls. If different Node versions are needed, use a matrix instead.

**Real-world example (better-auth)**: release.yml calls `setup-node` twice in the same job.

---
