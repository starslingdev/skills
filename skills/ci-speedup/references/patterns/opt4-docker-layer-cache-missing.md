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

Also flag a GHA cache scoped to `github.sha`, `run_id`, or `run_number`; a local
cache exported over the directory it imports without a separate `mode=max`
destination; and hosted-runner Dockerfiles that use `RUN --mount=type=cache`
without persisting the BuildKit cache. A SHA scope cannot serve a later commit,
and a run-ID scope cannot serve a fresh workflow invocation (reruns keep their
original IDs). A remote persistent builder is not an ephemeral mount case.

**Fix**: Use `docker/build-push-action` with `cache-from: type=gha` and
`cache-to: type=gha,mode=min` by default, or use `docker compose build` with
BuildKit cache mounts. Use `mode=max` only when intermediate stages are reused
and a measured export fits the job's time and size budget. If `COPY . .` or
`ADD . .` comes before the slow compile, copy dependency manifests first, use
`RUN --mount=type=cache`, or build natively/cross-compile; exporting that
invalidated layer will not make it reusable. Measure the export step and confirm
the intended layers print `CACHED` on a later run.

**Scope**: In a tags-only workflow, a GitHub Actions cache export is scoped to
that tag ref; no later tag can restore this. Do not add the export there unless
the same ref will run again and consume it.

---
