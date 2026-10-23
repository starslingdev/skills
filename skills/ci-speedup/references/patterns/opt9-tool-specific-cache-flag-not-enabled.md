---
pattern: OPT9
impact: HIGH
class: static
detector: yaml-job-correlated
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml'
fix_strategy: tool-specific-cache-flag-not-enabled
title_template: 'Tool-Specific Cache Flag Not Enabled'
category: 1
---

# OPT9 — Tool-Specific Cache Flag Not Enabled

**TL;DR**: A tool with a built-in cache (ESLint, Jest, tsc, etc.) runs without its cache flag, so it reprocesses every file each run.

**Anti-pattern**: A linter, formatter, type-checker, or build tool supports its OWN cache flag (e.g. Prettier `--cache`, ESLint `--cache`, TypeScript `--incremental`, Turbo `--cache-dir`, Vitest cache options, Jest cache), but the CI invocation omits the flag. Every run re-parses and re-processes every file from scratch even when `actions/cache` holds the previous run's cache dir. This is distinct from OPT2 (binary download caching) and OPT5 (pnpm store caching) — it's the tool's INTERNAL work cache, not its install cache.

**Detection heuristic**:

- Look in workflow YAML and npm/package-manager scripts for calls to these tools WITHOUT their cache flags:
  - `prettier --check` WITHOUT `--cache` (and optional `--cache-strategy content`)
  - `eslint` WITHOUT `--cache` (and optional `--cache-location`)
  - `tsc` WITHOUT `--incremental` / `--build`
  - `turbo run` WITHOUT persistent `--cache-dir` and `actions/cache` for it
  - `vitest run` WITHOUT a configured cache directory preserved in `actions/cache`
  - `jest` WITHOUT `--cache --cacheDirectory=<persisted path>`
- Cross-check that the relevant cache directory is NOT already wrapped by `actions/cache`
- Per-run saving is usually a large fraction of the tool's runtime on unchanged files (50-95% reduction on full incremental hit)

**Fix**: Add the tool's cache flag AND wrap the cache directory in `actions/cache` with a stable key (hash of lockfile + tool-version). For Prettier: `prettier --check --cache --cache-strategy content --cache-location ./node_modules/.cache/prettier`. For ESLint: `eslint --cache --cache-location ./node_modules/.cache/eslint`. Verify the tool's cache file is gitignored.

---
