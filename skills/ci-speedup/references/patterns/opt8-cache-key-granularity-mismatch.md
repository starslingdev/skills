---
pattern: OPT8
impact: MEDIUM
class: static
detector: yaml-path
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml'
fix_strategy: cache-key-granularity-mismatch
title_template: 'Cache Key Granularity Mismatch'
category: 1
---

# OPT8 — Cache Key Granularity Mismatch

**Anti-pattern**: GitHub Actions cache uses a coarse `hashFiles()` key (covering all input files) while the cached directory contains application-level caching with fine-grained per-item keys. A single file change invalidates the entire Actions cache, forcing all items to rebuild even though the application cache would correctly skip unchanged items.

**Detection heuristic**:

- Find `actions/cache` with `hashFiles()` covering many input files
- Check if the cached directory contains per-item cache files (`.pkl`, `.json`, etc.) with content-addressed keys
- Flag if the Actions cache key is broader than the application cache's granularity

**Fix**: Change the Actions cache key to hash only infrastructure files (cache implementation, build tools), not input data. Let the application-level cache handle input staleness.

**GUARDRAIL — Cache key narrowing:**

A "broad" cache key (e.g. `hashFiles('**/*.ts', '**/*.tsx')`) does NOT automatically mean "the cache is defeated." Two confounders kill most "narrow the key" findings before they ever produce wall-time savings:

1. **`restore-keys` fallback.** If the `actions/cache` step has `restore-keys` entries (e.g. `${{ runner.os }}-nextjs-${{ hashFiles('yarn.lock') }}-` and `${{ runner.os }}-nextjs-`), a primary-key miss still restores cache _content_ via prefix match against an older entry. The "broad" key just means one cache entry is written per commit instead of one per lockfile change — content is still restored. A primary-key miss with restore-keys fallback is NOT the same thing as "no cache restored."
2. **Ephemeral-runner cache invalidation.** Tool-level caches (ESLint `--cache`, Prettier `--cache`, Babel cache, `tsc` `.tsbuildinfo`, Jest cache, etc.) frequently produce **zero step-level speedup** on ephemeral runners despite `actions/cache` reporting a hit. Cache files often embed absolute paths and stat metadata (mtime, ino, dev) that differ across runner instances, so the tool internally re-validates and re-processes nearly every file. `actions/cache` says "Cache restored successfully"; the tool says "scanning 2,540 files" anyway.

**Required handling for cache-key-narrowing findings:**

1. **Enumerate `restore-keys` before claiming the cache is defeated.** The finding MUST quote the existing `restore-keys` block and explicitly state whether prefix-fallback restoration is happening. Phrasing must distinguish "primary-key miss with restore-keys fallback hit" (cache content present, just not the latest entry) from "no cache restored at all" (no fallback configured, or all fallbacks miss). These have very different perf implications and must not be conflated.
2. **Severity cap.** A "narrow the cache key" finding is capped at **MEDIUM** severity unless the finding includes benchmark data measuring the **step-time delta between cache-hit and cache-miss states on the actual runner type used by this repo** — not just a key-match-rate measurement. Key-match rate is necessary but not sufficient; the tool must demonstrably run faster on a hit.
3. **Justify the ephemeral-runner case.** Before claiming wall-time savings on tool-level caches (ESLint, Prettier, Babel, Jest, `tsc`, etc.), the finding must justify why the specific tool's cache actually works on ephemeral runners for this repo — ideally by citing prior benchmark data on the same repo + same runner type. Default assumption is "tool cache is suspect on ephemeral runners until proven otherwise." Self-hosted persistent runners are a different regime; call out which one applies.
4. **Default motivation: cache-namespace hygiene, not wall-time.** The cleanest legitimate motivation for narrowing a cache key is avoiding LRU eviction on the per-repo cache budget (10 GiB on GitHub-hosted; smaller on some self-hosted setups). Writing one cache entry per commit fills the budget faster than writing one per lockfile change, which can evict _other_ legitimate caches. Frame the finding as namespace hygiene first; only claim wall-time savings after the benchmark in (2) lands.
5. **Scope:** This guardrail applies ONLY to "narrow the existing cache key" findings. It does NOT apply to:
   - "No `actions/cache` step is configured at all" (no cache exists; not a narrowing question)
   - "Wrong path is being cached" (cache exists but covers the wrong directory)
   - "Cache key is non-deterministic" (e.g. uses `${{ github.sha }}` so nothing ever hits)
   - "Cache key has no `restore-keys` and the primary key changes per commit" (genuinely no fallback, content never restored — but the finding must still demonstrate step-time delta to claim wall-time savings)
   - OPT8's original detector case (Actions cache key broader than an _application-level_ per-item content cache it wraps, where the inner cache provably works) — that pattern is unaffected, but the benchmark requirement in (2) still applies before claiming savings.

If the workflow has no `actions/cache` step at all, or caches the wrong path, that is a different finding type and proceeds normally.

---
