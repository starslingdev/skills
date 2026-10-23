---
pattern: OPT20
impact: LOW
class: static
detector: regex
match: 'image:\s*[\"'']?[\w./-]+:latest\b'
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml'
fix_strategy: unpinned-docker-image-tags
title_template: 'Unpinned Docker Image Tags'
category: 3
---

# OPT20 — Unpinned Docker Image Tags

**Anti-pattern**: Docker services in docker-compose or workflow files using `:latest` or no tag. Causes non-deterministic pulls and prevents layer caching.

**Detection heuristic**:

- grep for image names without version pins in `docker-compose*.yml` and workflow files
- Flag any image reference that doesn't include a specific version tag

**Fix**: Pin all Docker images to specific version tags (e.g., `postgres:16.2` not `postgres:latest`).
