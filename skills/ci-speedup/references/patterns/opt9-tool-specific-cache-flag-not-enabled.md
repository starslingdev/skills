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

**TL;DR**: A tool's result cache is disabled or left on an ephemeral runner, so it reprocesses files that a later run could reuse.

**Anti-pattern**: A linter, formatter, type-checker, or build tool supports its OWN cache flag (e.g. Prettier `--cache`, ESLint `--cache`, TypeScript `--incremental`, Turbo `--cache-dir`, Vitest cache options, Jest cache), but the CI invocation omits the flag. Every run re-parses and re-processes every file from scratch even when `actions/cache` holds the previous run's cache dir. This is distinct from OPT2 (binary download caching) and OPT5 (pnpm store caching) — it's the tool's INTERNAL work cache, not its install cache.

**Detection heuristic**:

- Look in workflow YAML and npm/package-manager scripts for calls to these tools WITHOUT their cache flags:
  - `prettier --check` WITHOUT `--cache` (and optional `--cache-strategy content`)
  - `eslint` WITHOUT `--cache` (and optional `--cache-location`)
  - `tsc` WITHOUT `--incremental` / `--build`
  - `turbo run` WITHOUT persistent `--cache-dir` and `actions/cache` for it
  - `vitest run` WITHOUT a configured cache directory preserved in `actions/cache`
  - `jest` with its cache disabled, or with its default-enabled cache left in the runner's temporary directory instead of an explicit `cacheDirectory` preserved by `actions/cache`; omitting `--cache` alone is not a defect because Jest enables caching by default
- Cross-check that the relevant cache directory is NOT already wrapped by `actions/cache`
- For lint and format result caches, compare the affected step before and after enabling the cache; a warm hit can avoid a large share of repeated parsing.
- For compiler build state such as `.tsbuildinfo` and generated-client output, a cache hit does not by itself prove a saving: restore, validation, and save overhead can cancel the avoided work. Treat these as uncredited suggestions until a same-workload before/after measurement shows a net reduction.

**Fix**: Enable the tool's cache when it is off, choose an explicit cache directory, and wrap that directory in `actions/cache`. Jest already enables its cache by default, so its fix is to set and persist `cacheDirectory`, not to add a redundant `--cache` flag. Because tool-result caches change on every run, use a rolling primary key such as `<tool>-<os>-<lockfile-hash>-${{ github.sha }}` with a stable `restore-keys` prefix such as `<tool>-<os>-<lockfile-hash>-`. An exact primary-key hit is not saved again; the rolling suffix lets the updated result be saved for the next run. Each rolling entry consumes cache budget, so use this per-commit shape for small tool-result caches. Large dependency or build stores need a separately measured key and retention strategy; a stable exact key can also prevent updated mutable state from being saved. For Prettier: `prettier --check --cache --cache-strategy content --cache-location ./node_modules/.cache/prettier`. For ESLint: `eslint --cache --cache-location ./node_modules/.cache/eslint`. Verify the tool's cache file is gitignored. For compiler state and generated output, benchmark the full restore/run/save path against an uncached run on the same workload and keep the cache only when it reduces total step time.

---
