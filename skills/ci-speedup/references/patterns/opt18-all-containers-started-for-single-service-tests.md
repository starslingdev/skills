---
pattern: OPT18
impact: MEDIUM
class: static
detector: yaml-job-correlated
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml'
fix_strategy: all-containers-started-for-single-service-tests
title_template: 'All Containers Started for Single-Service Tests'
category: 3
---

# OPT18 — All Containers Started for Single-Service Tests

**TL;DR**: A test job starts every Docker service (Postgres, MySQL, Mongo…) when it only needs one or two of them.

**Anti-pattern**: Starting all Docker services (Postgres, MySQL, MongoDB, etc.) for a test job that only needs one of them.

**Detection heuristic**:

```bash
# Check if docker compose up runs without specifying services
grep -rn 'docker compose up' .github/workflows/
# Compare against which services each test job actually connects to
```

**Fix**: Use `docker compose up <service-name>` or Docker Compose profiles to start only needed services.

**Real-world example (better-auth)**: Each adapter job starts all database containers but only tests against one.

---
