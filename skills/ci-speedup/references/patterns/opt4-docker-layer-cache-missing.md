---
pattern: OPT4
impact: MEDIUM
class: static
detector: yaml-path-absent
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml'
fix_strategy: docker-layer-cache-missing
title_template: 'Docker Layer Cache Missing'
category: 1
---

# OPT4 — Docker Layer Cache Missing

**Anti-pattern**: Building Docker images in CI without layer caching, causing full rebuilds every run.

**Detection heuristic**:

```bash
# Search for docker build without --cache-from or buildx cache
grep -rn 'docker build\|docker compose build' .github/workflows/
# Check for docker/build-push-action without cache-from/cache-to
grep -rn 'docker/build-push-action' .github/workflows/
```

**Fix**: Use `docker/build-push-action` with `cache-from: type=gha` and `cache-to: type=gha,mode=max`, or use `docker compose build` with BuildKit cache mounts.

---
