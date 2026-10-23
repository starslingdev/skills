---
pattern: OPT17
impact: MEDIUM
class: static
detector: regex
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml'
fix_strategy: sleep-based-container-readiness
title_template: 'Sleep-Based Container Readiness'
category: 3
---

# OPT17 — Sleep-Based Container Readiness

**Anti-pattern**: Using `sleep N` to wait for Docker containers to become ready instead of proper healthchecks.

**Detection heuristic**:

```bash
# Search for sleep in workflow files and docker compose files
grep -rn 'sleep [0-9]' .github/workflows/
grep -rn 'sleep [0-9]' docker-compose*.yml
```

**Fix**: Add healthchecks to `docker-compose.yml` services and use `--wait` flag with `docker compose up`. Starting the service as a `background: true` step and stopping it with a `cancel:` step is a native pattern for a service the job runs itself (the `cancel:` is needed because the job waits for every background step before cleanup, so a service that never exits would hold the job open) (self-hosted runners: 2.336.0 or later; OPT77's fix covers the runner requirement), but it does not make the service ready: the steps that use it still need a readiness probe, not a fixed `sleep`.

**Real-world example (better-auth)**: PR #8010 replaced `sleep 10` with Docker healthchecks across all adapter integration jobs.

---
