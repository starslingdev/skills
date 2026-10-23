---
pattern: OPT2
impact: MEDIUM
class: static
detector: yaml-job-correlated
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml'
fix_strategy: uncached-large-downloads
title_template: 'Uncached Large Downloads'
category: 1
---

# OPT2 — Uncached Large Downloads

**TL;DR**: Big downloads (like Playwright browsers) are re-fetched over the network every run because nothing caches them — cache them once and reuse.

**Anti-pattern**: Downloading large binaries (browsers, SDKs, toolchains) on every run without caching them.

**Detection heuristic**:

- Look for `npx playwright install` without a preceding `actions/cache` step keyed on the Playwright version
- Look for SDK/toolchain download steps without caching

```bash
# Check if playwright install is preceded by a cache restore
# Parse workflow YAML and check step ordering within each job
```

**Fix**: Add `actions/cache` with a version-pinned key before the install step, or use the tool's built-in cache mechanism (e.g., Playwright's `PLAYWRIGHT_BROWSERS_PATH`).

---
