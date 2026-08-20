# ci-speedup Pattern Catalog

This catalog defines every CI optimization pattern the `ci-speedup` skill can
detect. Each entry has a single METADATA block followed by the pattern's
anti-pattern, detection heuristic, and fix recipe.

**Catalog scope.** Patterns are organized into 14 categories (Caching,
Redundancy, Docker, Parallelization, Actions and Checkout, Conditional
Execution, Trigger and Scope, Release Workflow, Queue Times and Concurrency,
Timing Anomalies, Stack-Specific, Build Caching, Hidden Failures and Dead
Config). Each pattern carries an `impact` tier (HIGH/MEDIUM/LOW) and a
`detector` field naming the scanning strategy used. The scanner consumes
the METADATA blocks directly — adding a new pattern is a matter of writing
its catalog entry and (for novel detector types) extending the scanner.

**OPT-id assignment rule.** Each `OPT<n>` id is permanent and **never reused**,
even after a pattern is cut — a cut pattern's id stays retired so historical
reports, evals, and fix-strategy strings never collide with a different pattern.
Assign a **new** id to every new pattern (the next unused number; there are gaps
from retired/never-assigned ids, e.g. 10, 67). Do not renumber existing
patterns. `fix_strategy` slugs and the `_SIZING` keys are keyed on these ids, so
reuse would silently mis-size or mis-link old data.

## Contents

Patterns are `### OPT<n> — Title` blocks grouped into categories. This file is
large; **navigate by grep** — `OPT33`, `## Category 7`, or a keyword like
`turbo` / `concurrency` jumps straight to the entry. The categories
(grep `## Category N`):

1. Caching · 2. Redundancy · 3. Docker · 4. Parallelization · 5. Actions and
Checkout · 6. Conditional Execution · 7. Trigger and Scope · 8. Release Workflow ·
9. Queue Times and Concurrency · 10. Timing Anomalies · 11. Stack-Specific ·
12. Build Caching (Language-Agnostic) · 13. Hidden Failures and Dead Config ·
14. Structural / Critical-Path Levers

- **Category 1 — Caching** (`OPT1`–): tool installs, build/test caches, dynamic cache keys.
- **Category 2 — Redundancy**: duplicate env, repeated setup sequences, redundant build steps.
- **Category 3 — Docker**: sleep-based readiness, over-broad `compose up`.
- **Category 4 — Parallelization**: needless `needs:` serialization, unsharded long jobs.
- **Category 5 — Actions and Checkout**: stale action pins, repeated setup, full-history checkout, submodule / Git LFS checkout payload.
- **Category 6 — Conditional Execution**: merge_group step-vs-job conditions, draft-PR gating.
- **Category 7 — Trigger and Scope**: missing path filters, no `--filter` on PR turbo, cron frequency.
- **Category 8 — Release Workflow**: release-path caching + redundancy.
- **Category 9 — Queue Times and Concurrency**: missing/!coarse concurrency groups.
- **Category 10 — Timing Anomalies**: failure-rate / bimodal duration signals (advisory).
- **Category 11 — Stack-Specific**: turbo task outputs, unstable turbo env keys.
- **Category 12 — Build Caching (language-agnostic)**: uncached compiled-language builds.
- **Category 13 — Hidden Failures and Dead Config**: dead env vars, misconfigured caches.
- **Category 14 — Structural / Critical-Path Levers** (`OPT70`–`OPT75`): routed from the measured long pole (see ARCHITECTURE §11), not a flat grep.

(Catalog OPT-ids are the static scan; the `blocking_path.py` `_parse_log` **leaf
detectors** — prisma / vitest / turbo / playwright — are a separate render-time
set, documented in ARCHITECTURE §12.3.)

---

### OPT1 — Unnecessary Tool Install

<!-- METADATA
pattern: OPT1
impact: MEDIUM
class: static
detector: yaml-job-correlated
affected_files: ".github/workflows/*.yml,.github/workflows/*.yaml"
fix_strategy: unnecessary-tool-install
title_template: "Unnecessary Tool Install"
-->

**Anti-pattern**: Installing tools (e.g., Playwright browsers) in jobs that don't use them.
A vitest unit-test job that runs `npx playwright install` wastes ~30-90s downloading browsers it never launches.

**Detection heuristic**:

- Search for `playwright install` in jobs whose steps never reference `playwright test` or `@playwright/test`
- Search for tool install steps where the tool binary is never invoked later

```bash
# Find playwright install in non-e2e jobs
gh api repos/{owner}/{repo}/contents/.github/workflows --jq '.[].name' | while read f; do
  content=$(gh api repos/{owner}/{repo}/contents/.github/workflows/$f --jq '.content' | base64 -d)
  if echo "$content" | grep -q 'playwright install' && ! echo "$content" | grep -q 'playwright test'; then
    echo "OPT1 hit: $f installs Playwright but never runs Playwright tests"
  fi
done
```

**Fix**: Remove the install step from jobs that don't need it. If a shared setup action includes it, make it conditional or split the action.

**Real-world example (better-auth)**: Adapter integration jobs installed Playwright browsers despite only running vitest. Removing the install saved ~45s per adapter job (6 matrix variants × 45s = ~270s total).

---

---

### OPT2 — Uncached Large Downloads

<!-- METADATA
pattern: OPT2
impact: MEDIUM
class: static
detector: yaml-job-correlated
affected_files: ".github/workflows/*.yml,.github/workflows/*.yaml"
fix_strategy: uncached-large-downloads
title_template: "Uncached Large Downloads"
-->

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

---

### OPT3 — Read-Only Turbo Cache (never populated)

<!-- METADATA
pattern: OPT3
impact: MEDIUM
class: static
detector: regex
match: "TURBO_CACHE:\s*[\"']?remote:ro\b"
wf_name_exclude: "(release|publish|deploy)"
affected_files: ".github/workflows/*.yml,.github/workflows/*.yaml"
fix_strategy: turbo-cache-misconfiguration
title_template: "Read-Only Turbo Cache"
-->

**TL;DR**: A job sets `TURBO_CACHE: remote:ro` — it READS the remote cache but never writes it. If nothing else writes the cache, it stays cold and every task re-executes.

**Anti-pattern**: `remote:ro` means "remote only, read-only" — local cache is skipped AND this job never populates the remote cache. That's correct ONLY when a separate writer (e.g. the main-branch build with `remote:rw`) keeps it warm; a read-only cache with no writer is always a miss.

> **NOTE — `remote:rw` is NOT flagged.** On ephemeral CI runners (GitHub-hosted and most self-hosted) the local file-system cache does not persist between runs, so `remote:rw` (remote-only, read-write — the cross-run cache that actually helps) is the *correct* config, not a misconfiguration. OPT3 only flags the read-only (`ro`) case. The cross-workflow read-only-reader / read-write-writer *race* is OPT37's territory, not this single-file check.

**Detection heuristic**:

```bash
grep -rn 'TURBO_CACHE' .github/workflows/   # flag only value 'remote:ro'
```

**Fix**: If this job should populate the cache, set `remote:rw`. If it intentionally reads a cache a sibling job/workflow writes, confirm that writer exists (see OPT37) — otherwise the cache is never warm.

---

---

### OPT4 — Docker Layer Cache Missing

<!-- METADATA
pattern: OPT4
impact: MEDIUM
class: static
detector: yaml-path-absent
affected_files: ".github/workflows/*.yml,.github/workflows/*.yaml"
fix_strategy: docker-layer-cache-missing
title_template: "Docker Layer Cache Missing"
-->

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

---

### OPT5 — pnpm Store Not Cached (or Wrong Setup Order)

<!-- METADATA
pattern: OPT5
impact: MEDIUM
class: static
detector: yaml-job-correlated
affected_files: ".github/workflows/*.yml,.github/workflows/*.yaml"
fix_strategy: pnpm-store-not-cached-or-wrong-setup-order
title_template: "pnpm Store Not Cached (or Wrong Setup Order)"
-->

**TL;DR**: The pnpm package store isn't cached between runs, so every run re-downloads all dependencies from scratch.

**Anti-pattern**: Running `pnpm install` without caching the pnpm store, or setting up Node before pnpm (so the store path isn't available for cache key computation).

**Detection heuristic**:

```bash
# Check setup order: pnpm/action-setup should come before actions/setup-node
# Check if setup-node has cache: 'pnpm' set
grep -rn 'actions/setup-node' .github/workflows/ | head -20
grep -rn 'pnpm/action-setup' .github/workflows/ | head -20
```

**Fix**: Ensure `pnpm/action-setup` runs before `actions/setup-node`, and `setup-node` has `cache: 'pnpm'`.

---

---

### OPT6 — Cache Key Entropy Too High or Unstable

<!-- METADATA
pattern: OPT6
impact: MEDIUM
class: static
detector: regex
match: "key:\s*.*\$\{\{\s*github\.(sha|run_id|run_number)"
affected_files: ".github/workflows/*.yml,.github/workflows/*.yaml"
fix_strategy: cache-key-entropy-too-high-or-unstable
title_template: "Cache Key Entropy Too High or Unstable"
-->

**Anti-pattern**: Cache keys that include timestamps, random values, or non-deterministic content, causing cache misses on every run.

**Detection heuristic**:

```bash
# Search for cache keys with dynamic values
grep -rn 'actions/cache' .github/workflows/ -A 5 | grep 'key:'
# Flag keys containing: ${{ github.run_id }}, date, timestamp, random
```

**Fix**: Use deterministic cache keys based on lockfile hashes, tool versions, and OS. Use `restore-keys` for fallback matching.

---

---

### OPT7 — pnpm Version Drift Across Workflows

<!-- METADATA
pattern: OPT7
impact: LOW
class: static
detector: yaml-workflow-correlated
affected_files: ".github/workflows/*.yml,.github/workflows/*.yaml"
fix_strategy: pnpm-version-drift-across-workflows
title_template: "pnpm Version Drift Across Workflows"
-->

**Anti-pattern**: Different workflows or jobs specifying different pnpm versions, reducing cache compatibility between them.

**Detection heuristic**:

```bash
# Extract all pnpm versions across workflows
grep -rn 'version:' .github/workflows/ | grep -i pnpm
grep -rn 'packageManager' package.json
```

**Fix**: Pin pnpm version in `package.json` `packageManager` field and reference it in all workflows, or use `pnpm/action-setup` without explicit version to auto-detect from `packageManager`.

---

---

### OPT8 — Cache Key Granularity Mismatch

<!-- METADATA
pattern: OPT8
impact: MEDIUM
class: static
detector: yaml-path
affected_files: ".github/workflows/*.yml,.github/workflows/*.yaml"
fix_strategy: cache-key-granularity-mismatch
title_template: "Cache Key Granularity Mismatch"
-->

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

---

### OPT9 — Tool-Specific Cache Flag Not Enabled

<!-- METADATA
pattern: OPT9
impact: HIGH
class: static
detector: yaml-job-correlated
affected_files: ".github/workflows/*.yml,.github/workflows/*.yaml"
fix_strategy: tool-specific-cache-flag-not-enabled
title_template: "Tool-Specific Cache Flag Not Enabled"
-->

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

---

### OPT11 — Redundant Environment Variables

<!-- METADATA
pattern: OPT11
impact: LOW
class: static
detector: yaml-path
affected_files: ".github/workflows/*.yml,.github/workflows/*.yaml"
fix_strategy: redundant-environment-variables
title_template: "Redundant Environment Variables"
-->

**Anti-pattern**: Setting the same env var at both the workflow/job level AND the step level, creating noise and maintenance burden.

**Detection heuristic**:

```bash
# Parse workflow YAML for env blocks at workflow, job, and step levels
# Compare for duplicates within the same scope chain
```

**Fix**: Set env vars at the highest applicable scope only. Remove step-level overrides that match job/workflow-level values.

**Real-world example (better-auth)**: `TURBO_TOKEN` and `TURBO_TEAM` set globally in ci.yml AND repeated in individual steps.

---

---

### OPT12 — Duplicated Setup Across Jobs

<!-- METADATA
pattern: OPT12
impact: MEDIUM
class: static
detector: yaml-workflow-correlated
affected_files: ".github/workflows/*.yml,.github/workflows/*.yaml"
fix_strategy: duplicated-setup-across-jobs
title_template: "Duplicated Setup Across Jobs"
-->

**TL;DR**: Several jobs copy-paste the same setup steps. Pulling them into one shared action is easier to maintain — but note it does NOT speed anything up (each job still runs them).

**Anti-pattern**: Multiple jobs with identical preamble steps (checkout, setup-node, pnpm install, build) that could be extracted to a composite action or use artifact handoff.

**Detection heuristic**:

```bash
# Hash the first N steps of each job and compare
# Look for identical sequences of: checkout → setup-pnpm → setup-node → install → build
```

**Fix**: Extract shared setup into a composite action, or build once and pass artifacts to downstream jobs.

**GUARDRAIL — "downstream ignores producer's artifact" claims (REQUIRED before any cross-workflow handoff finding):**

If your finding crosses workflow boundaries and frames the fix as
"consumer ignores producer's artifact", "wire up the [producer]
artifact", or "downstream throws away the [producer] output", you
MUST `grep -nE 'actions/upload-artifact'` the producer workflow file
BEFORE accepting the framing. Two valid outcomes:

1. **Producer DOES upload an artifact** → finding is valid as written.
   Cite the upload step's file:line in `evidence`. Fix is "add
   `actions/download-artifact` in the consumer".
2. **Producer does NOT upload an artifact** → reframe the finding.
   The producer's "output" is whatever it actually publishes — most
   commonly a warm Turbo / build-tool remote cache (check for
   `TURBO_CACHE: remote:rw`, `sccache`, `ccache`, `nx affected`,
   etc.) or a PR status update. The fix is then "ADD
   `actions/upload-artifact` to the producer AND add
   `actions/download-artifact` to the consumer" — that is a NEW
   pipeline design, not a re-wiring. Re-size the savings: the
   redundancy is `pnpm install` / `cargo fetch` / equivalent
   install-step cost only, NOT "downstream rebuilds the full
   monorepo from scratch". Use measured P50/P95 of the build step
   to size the cold-tail recovery component; never assume 100% cold.

**Severity cap.** A finding that survives outcome (2) — i.e. the
producer doesn't upload — is capped at HIGH severity, never CRITICAL,
because the saving is bounded by install dedup + measured cold-tail,
not by full rebuild elimination.

**Required `evidence` text:** include one of these two literal lines:
- `Verified <producer-workflow> uploads <artifact-name> at L<N>; consumer <consumer-workflow> does NOT call actions/download-artifact. Wiring fix applies.`
- `Verified <producer-workflow> does NOT upload any artifact (no actions/upload-artifact step). Producer's output is <warm-cache | PR-status | other>. Fix is a NEW pipeline; saving re-sized to <X> min/mo from measured install + cold-tail, NOT 'downstream rebuilds from scratch'.`

A prebuild-dedupe audit that assumed "downstream rebuilds from scratch"
when the producer never actually uploaded an artifact is the empirical
motivation for this guardrail — verify the upload before sizing.

---

---

### OPT13 — Build Step in Jobs That Don't Need It

<!-- METADATA
pattern: OPT13
impact: MEDIUM
class: static
detector: yaml-job-correlated
affected_files: ".github/workflows/*.yml,.github/workflows/*.yaml"
fix_strategy: build-step-in-jobs-that-dont-need-it
title_template: "Build Step in Jobs That Don't Need It"
-->

**TL;DR**: A job runs a full build even though it only lints or type-checks and doesn't need the built output.

**Anti-pattern**: Running a full build in jobs that only need type-checking, linting, or running tests against source (not built output).

**Detection heuristic**:

```bash
# Check turbo.json for task dependencies
# If 'test' depends on 'build' in turbo.json, the build is required
# If not, check if the job explicitly runs build before test
```

**Fix**: Remove unnecessary build steps. If using Turbo, ensure `turbo.json` task graph is accurate so Turbo skips unneeded builds.

**GUARDRAIL — Framework build steps (`next build`, `nuxt build`, `vite build`, `remix build`, `astro build`, `gatsby build`, `ng build`, `nest build`, `expo export`, `react-scripts build`, `webpack`/`rollup`/`turbopack`/`rspack` production builds, `tsc -b`/project-references full builds, `mvn package`, `gradle assemble`, etc.):**

A framework build is NOT just a typecheck. Before recommending deletion, you MUST enumerate what the build does that the proposed replacement does not. For example, `next build` performs:

- Route validation (catch broken `app/`/`pages/` exports, invalid route configs, conflicting routes)
- Page-data collection / static analysis for `getStaticProps`/`generateStaticParams`
- React Server Component bundling and "use client"/"use server" boundary checks
- Code splitting, dead-code elimination, tree-shaking — surfaces unresolved imports a typecheck misses
- Codegen (e.g. `.next/types`, route types, manifests)
- Env-var validation for `NEXT_PUBLIC_*` (compile-time inlining)
- Image / font / asset processing
- Production-only minification and bundler errors
- Runtime config validation (middleware, edge functions, ISR config)

`tsc --noEmit`, `eslint`, `vitest`, `jest`, etc. cover only a strict subset. Treat them as **partial substitutes** — never claim they preserve the same gating signal as the build.

**Required handling for framework-build deletion findings:**

1. **Default to "shrink the build", not "delete the build".** Recommend cheaper variants first:
   - `next build` → `--no-lint` (lint runs separately), disable telemetry (`NEXT_TELEMETRY_DISABLED=1`), strip dev-only env stripping, trim `output: 'standalone'` if unneeded, prune unused locales, cache `.next/cache` across runs.
   - Other frameworks: equivalent flags (e.g. `vite build --minify=false` for non-prod gating, `gradle assemble -x test`, etc.).
2. **Severity cap.** A "delete the framework build" finding is capped at **MEDIUM** severity, regardless of measured wall-time savings, unless the finding includes an explicit "the build does the following N things and the replacement covers all N" justification table. Without that table, the finding stays MEDIUM and is presented as an _option_, not a recommendation.
3. **Never frame `tsc --noEmit` (or any single tool) as equivalent gating.** It is a typecheck. If you suggest it, label it as "type-only gate; will not catch route, bundler, codegen, or env-var regressions that the deploy build catches later."
4. **Cite the deploy path.** If production deploys (Vercel, Netlify, Cloud Run, etc.) re-run the same build, note that pre-merge deletion shifts failure detection from PR time to deploy time and call out the worse signal / slower feedback explicitly as a tradeoff.
5. **Scope:** This guardrail applies ONLY to "remove a framework's production build/compile step" findings. It does NOT apply to:
   - Removing a redundant lint step that another job already runs
   - Removing sleep-based polling
   - Sharing a single build across jobs via artifacts or cache
   - Deleting a build that produces nothing the test/lint job consumes AND has no validation surface beyond what other steps already enforce (e.g., a stray `tsc -b` duplicating a typecheck job that already runs).

If the finding is "share/cache the build" or "build once, fan out artifacts", that is OPT14 / OPT15 territory and the guardrail above does not apply — those are still legitimate.

**GUARDRAIL — Runtime dependency on compiled output (D11 backstop):**

Before recommending removal of any pre-test build step (including the "stray `tsc -b`" carve-out above), the audit MUST grep the affected package's source for these four runtime-load patterns:

1. **Worker-thread spawn sites**: `grep -RnE 'new Worker\(|worker_threads' <pkg>/src` — Node's `worker_threads.Worker` resolves its target file at runtime, typically via `path.join(__dirname, "worker-thread.js")`. If `__dirname` resolves under the compiled `dist/`, the build is load-bearing.
2. **child_process exec/spawn/fork sites**: `grep -RnE 'child_process|\.fork\(|\.spawn\(|\.exec\(|\.execFile\(|\.execSync\('` — same hazard. `fork()` and `spawn()` of a node script under `dist/` need the dist to exist.
3. **Dynamic / relative-path require of `dist/` from non-test source**: `grep -RnE "require\([\"\']\.\.+/+dist|from [\"\']\.\.+/+dist|import\([\"\']\.\.+/+dist" <pkg>/src | grep -v '\.test\.\|\.spec\.'` — direct runtime imports of compiled output.
4. **`__dirname`-relative paths resolving under `dist/` at runtime**: `grep -RnE 'path\.join\(__dirname,.*\.js[\"\']' <pkg>/src` — common when a TS source file constructs a path that points into its compiled-output sibling.

If ANY non-test source contains the above, the build is load-bearing at runtime even if no test imports `dist/` directly. The finding must be **INVALIDATED**, not downgraded to "probe" — a probe that ships and then silently regresses in production (because the test that would exercise the worker path didn't run, or the Worker pool init silently caught the failure) is worse than no finding at all.

**Coverage table addition** (extends the existing framework-build coverage table requirement): for any build-removal finding, the coverage table MUST enumerate Worker-thread spawn sites, `child_process` exec sites, and `require()`/`import()` of `../dist/...` from non-test source. Cite each as either "0 hits" or "N hits at file:line — see Finding N evidence". A missing or empty coverage table is a Phase 6 fail.

**Worked counter-example.** The 2026-05-19 langfuse audit's Finding 15 shipped a "drop `pnpm --filter=worker... run build`" recommendation as a MEDIUM-severity probe, citing ~150 min/mo. The D7 source-code safety check (DB-retry, HTTP-client, service-discovery patterns) returned zero hits, so the probe looked safe. Phase 6 round 2 ran the four-grep set above and found `worker/src/features/tokenisation/worker-thread.ts:4` requires `dist/features/tokenisation/usage.js` via a spawned Node Worker thread (`new Worker(path.join(__dirname, "worker-thread.js"))` inside `async-usage.ts` → `TokenCountWorkerManager`). The finding was INVALIDATED — a build that is load-bearing at runtime (a spawned Worker resolving its target under `dist/`) must never be proposed for removal, even as a probe.

---

---

### OPT14 — Repeated Checkout/Setup Without Artifact Handoff (and Slow Tool Replacement)

<!-- METADATA
pattern: OPT14
impact: MEDIUM
class: static
detector: yaml-job-correlated
affected_files: ".github/workflows/*.yml,.github/workflows/*.yaml"
fix_strategy: repeated-checkout-setup-without-artifact-handoff-and-slow-to
title_template: "Repeated Checkout/Setup Without Artifact Handoff (and Slow Tool Replacement)"
-->

**TL;DR**: Multiple jobs each re-install and re-build from scratch instead of building once and passing the result to the others.

**Anti-pattern**: Every job checks out code and installs dependencies independently, even when a prior job already did the same work. No artifacts are passed between jobs.

**Detection heuristic**:

- Count checkout + install sequences across jobs in same workflow
- Check for `actions/upload-artifact` / `actions/download-artifact` usage

**Fix**: Use artifact handoff for built outputs, or extract the shared setup into a reusable composite action to at least reduce duplication.

**GUARDRAIL — same as OPT12 above.** When the proposed fix is "share artifacts from a prior workflow / job", the artifact-existence check is mandatory. See [OPT12 GUARDRAIL](#opt12--duplicated-setup-across-jobs) for the full procedure and required `evidence` text. Findings whose producer doesn't upload an artifact are capped at HIGH severity, and the saving must be sized from measured install dedup + cold-tail recovery, not from "downstream rebuilds from scratch" assumptions.

---

#### OPT14 (sub) — Slow Tool Replacement: Legacy → Rust-Native Swaps

**Anti-pattern**: A CI step runs a JS-implemented dev tool (Prettier, ESLint, Babel, webpack, tsc, etc.) where a Rust-native (or otherwise compiled) drop-in replacement would do the same job in a fraction of the wall-clock time. The wall-time delta is real and measurable on every run, so it compounds quickly across PR volume.

**Catalog of canonical swaps** (evaluate each when auditing a slow lint/format/build step — speedup multipliers below are honest ranges from real benchmarks, not vendor marketing):

| Source             | Target                | Realistic speedup                                                                                | Drop-in?          | Notes                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| ------------------ | --------------------- | ------------------------------------------------------------------------------------------------ | ----------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Prettier**       | **oxfmt**             | **~4–30×** (depends on tree size + tailwind sort)                                                | Mostly            | Drop-in for JS/TS/CSS/MD. Respects `.prettierignore` in scan mode (no positional args); does NOT respect it in explicit-path mode. `npx oxfmt --migrate prettier` produces a config from `.prettierrc`. Tailwind class sort uses oxfmt's own algorithm — order differs from `prettier-plugin-tailwindcss`. `embeddedLanguageFormatting` for JS/TS template literals (CSS-in-JS, `gql\`...\``) is not yet supported.                                                                                                                         |
| **ESLint**         | **oxlint**            | **~8–20×** realistic (NOT the 50–100× advertised when type-aware rules and custom plugins exist) | Partial fit only  | Recommend a **dual-run scoped ESLint pattern** when ANY of the following are present: custom local rules, `tailwindcss` plugin, type-aware rules requiring `tsgolint`, framework plugins (`convex/*`, `next/*`, `@typescript-eslint/*` type-aware rules), or a `react-hooks/exhaustive-deps` configuration the team relies on. In dual-run, oxlint runs first across the tree; ESLint runs second with a config restricted to the rules oxlint can't replicate. Do NOT recommend a wholesale oxlint-only swap when these gates are present. |
| **Babel**          | **SWC**               | **~10×** compile                                                                                 | Yes for most      | Drop-in via `next/babel`, framework integration, or `@swc/jest`. Verify any custom Babel plugins have SWC equivalents before recommending.                                                                                                                                                                                                                                                                                                                                                                                                  |
| **Webpack**        | **Turbopack**         | **~2–5×** build                                                                                  | Yes (Next.js 16+) | Default in Next.js 16+. Check for `webpack:` overrides in `next.config.js` that force fallback to webpack — those often need to be ported or removed before Turbopack is actually active.                                                                                                                                                                                                                                                                                                                                                   |
| **`tsc --noEmit`** | esbuild/swc typecheck | **DO NOT recommend**                                                                             | **Anti-pattern**  | esbuild and swc do not typecheck. They strip types. `tsc --noEmit` is the only real type-check in the TypeScript ecosystem. Recommending this swap is a coverage regression masquerading as a speedup — the "speed without coverage is a regression" principle explicitly forbids it. Skip it.                                                                                                                                                                                                                                              |

**Detection heuristic**:

- Inspect lint/format/typecheck/build steps from the per-job `steps[]` array from `gh api repos/{owner}/{repo}/actions/runs/{run_id}/jobs`
- Sort steps by P50 duration. For each slow step, check the command: `npx prettier`, `eslint`, `babel`, `webpack`, etc.
- If a Rust-native target from the catalog above applies, run a sandbox benchmark before shipping a finding

**Migration cost callout (REQUIRED for any tool-swap finding)** — every OPT14 tool-swap finding MUST surface, before being shipped, the concrete migration costs measured against a sandbox clone (not estimated):

1. **Bulk reformat / rule-coverage gap**: how many files / what fraction of the source tree will diff after the swap? Run the new tool against a clean sandbox clone and `git status | wc -l`. Cite the absolute count and percentage (e.g. "27 files, ~1% of the source tree, net −21 LOC"). Estimating this without measurement is not acceptable.
2. **Ignore-file behavior differences**: confirm the new tool respects `.prettierignore` / `.eslintignore` / `.gitignore` in the relevant invocation mode the workflow uses. Test empirically (e.g. for oxfmt: run `--check` with and without the ignore file present and diff the file counts — the delta should match the ignore-file entries).
3. **Implicit-default parity gaps**: list every config option that is _unset_ in the source tool's config and therefore relies on the source tool's defaults (Prettier has ~15 such options: `arrowParens`, `bracketSpacing`, `endOfLine`, `htmlWhitespaceSensitivity`, `jsxSingleQuote`, `quoteProps`, `singleAttributePerLine`, `tabWidth`, `trailingComma`, `useTabs`, etc.). Confirm or test parity with the target tool for each. Don't assume defaults match.
4. **Coverage caveats**: list features in the source tool that the target tool doesn't support (oxfmt's missing `embeddedLanguageFormatting`; oxlint's missing type-aware rules; Tailwind class-sort algorithm differences; etc.). Confirm whether the codebase relies on any of them — read the actual source files / configs, don't assume.

**Two-commit shipping pattern (REQUIRED for tool-swap findings that require bulk reformat)**:

When a tool swap requires reformatting source files (oxfmt finding 27 files, an ESLint→oxlint swap auto-fixing N files, etc.), structure the patch as **two commits**, never one:

1. **Commit 1 — Recipe**: workflow change + new tool config file. This is the durable, reviewable change.
2. **Commit 2 — Bulk reformat**: the result of running the new tool against the source tree.

The recipe (commit 1) is what ships in the patch artifact. The bulk reformat (commit 2) is **regenerated against upstream HEAD at submission time** by the applying agent — bundling a stale reformat into the patch artifact creates drift the moment any other PR lands. The recipe is what ships; the bulk reformat is regenerated at apply-time against current upstream.

**Real-world example (orgorgtheorg/orgorg, 2026-05)**: `Check Prettier formatting on /code` step ran `npx prettier --check` at 24s P50 over 257 runs. Swapped to `npx --yes oxfmt@0.47.0 --check` with a migrated `.oxfmtrc.json`. Benchmark: 24.3s avg → 5.7s avg = 4.27× speedup, ~80 min/mo saved. Migration cost: 27 files reformatted (~1% of tree, net −21 LOC), shipped as two commits (recipe + bulk reformat).

---

---

### OPT15 — Cross-Workflow Build Redundancy

<!-- METADATA
pattern: OPT15
impact: HIGH
class: static
detector: yaml-workflow-correlated
affected_files: ".github/workflows/*.yml,.github/workflows/*.yaml"
fix_strategy: cross-workflow-build-redundancy
title_template: "Cross-Workflow Build Redundancy"
-->

**TL;DR**: The same build runs in several separate workflows that all fire on one PR, doing identical work several times over.

**Anti-pattern**: The same build command (producing identical artifacts) appears in multiple independently-triggered workflows that all fire on the same PR push event. Each workflow rebuilds from scratch, multiplying build time by the number of workflows.

**Detection heuristic**:

1. List all workflows triggered by `pull_request` on the same branch.
2. For each, extract build step commands (normalized — strip env vars, paths).
3. Hash the commands. Count how many times the same build runs per PR push across all workflows.
4. Flag if >3 identical builds exist across workflows.

```bash
# Example: count cmake/build.sh invocations across all PR workflows
grep -rn 'build\.sh\|cmake\|cargo build\|cargo test\|maturin\|pip install' /tmp/workflows/*.yml | \
  grep -v '#' | sort
```

**Fix**: Extract the shared build into a dedicated workflow or composite action. Use `actions/cache` or `actions/upload-artifact` to share build outputs across workflows.

**Wall-clock warning (serial-gate check)**: The "build once, share artifact" fix (a dedicated prebuild job + downstream consumers that `needs:` it) removes parallel-overlapped compute — a real **runner-minute** win — but it inserts a **SERIAL stage** ahead of the fan-out that every consumer must wait on, so it **ADDS wall-clock**. In the baseline the N copies of the build run in parallel, so wall-clock pays for ONE; the patch collapses them behind one gated build plus an artifact download. Before recommending this pattern, run the critical-path check:

```
Δ wall-clock = + (new serial prebuild stage) + (artifact upload/download/extract on the critical consumer) − (build work removed from the long pole)
```

If `Δ wall-clock > 0` the finding is **wall-clock-NEGATIVE** → demote it to the runner-minute appendix and flag "do NOT ship for wall-clock"; never place it in Tier 1. **Worked proof**: the dedupe-pnpm-build parity benchmark measured **−1,950 runner-min/mo** but **+70–90s wall-clock/run** (build-job ~95s − ~40s removed from the critical consumer + ~29s download/extract ≈ +84s; net-negative on ~20% of runs from 190 MB concurrent download contention).

**ALWAYS pair this with the wall-clock-correct alternative**: warm the build cache (fix the Turbo/Next.js cache key) so each parallel build becomes a ~8s cache restore — the **same** redundancy removed (−33s/job), but with **NO serial gate**. This removes the *cost* of the redundancy instead of the redundancy itself, so the parallel copies stay cheap and wall-clock improves. See `wall-clock-methodology.md` §4 (serial-gate findings can be wall-clock-NEGATIVE) for the critical-path math.

---

---

### OPT16 — Within-Job Duplicate Commands

<!-- METADATA
pattern: OPT16
impact: LOW
class: static
detector: yaml-job-correlated
affected_files: ".github/workflows/*.yml,.github/workflows/*.yaml"
fix_strategy: within-job-duplicate-commands
title_template: "Within-Job Duplicate Commands"
-->

**Anti-pattern**: The same script or command is invoked twice within a single job's steps — once in a dedicated "build" step, then again inside a "run tests" step (often with a comment like "rebuild in case of stale artifacts").

**Detection heuristic**:

```bash
# For each job in each workflow, check if any command appears in multiple steps
for f in /tmp/workflows/*.yml; do
  # Extract run: commands per job and check for duplicates
  grep -oP '(?<=run: ).*' "$f" | sort | uniq -d
done
```

**Fix**: Remove the duplicate invocation. If the rebuild is a workaround for stale artifacts, fix the root cause (e.g., cache key, build system incremental support) instead.

---

## Category 3: Docker

---

### OPT17 — Sleep-Based Container Readiness

<!-- METADATA
pattern: OPT17
impact: MEDIUM
class: static
detector: regex
affected_files: ".github/workflows/*.yml,.github/workflows/*.yaml"
fix_strategy: sleep-based-container-readiness
title_template: "Sleep-Based Container Readiness"
-->

**Anti-pattern**: Using `sleep N` to wait for Docker containers to become ready instead of proper healthchecks.

**Detection heuristic**:

```bash
# Search for sleep in workflow files and docker compose files
grep -rn 'sleep [0-9]' .github/workflows/
grep -rn 'sleep [0-9]' docker-compose*.yml
```

**Fix**: Add healthchecks to `docker-compose.yml` services and use `--wait` flag with `docker compose up`.

**Real-world example (better-auth)**: PR #8010 replaced `sleep 10` with Docker healthchecks across all adapter integration jobs.

---

---

### OPT18 — All Containers Started for Single-Service Tests

<!-- METADATA
pattern: OPT18
impact: MEDIUM
class: static
detector: yaml-job-correlated
affected_files: ".github/workflows/*.yml,.github/workflows/*.yaml"
fix_strategy: all-containers-started-for-single-service-tests
title_template: "All Containers Started for Single-Service Tests"
-->

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

---

### OPT19 — Test Source Sleep Dominance (includes Playwright `waitForTimeout`)

<!-- METADATA
pattern: OPT19
impact: HIGH
class: static
detector: repo-file-check
affected_files: ".github/workflows/*.yml,.github/workflows/*.yaml"
fix_strategy: test-source-sleep-dominance-includes-playwright-waitfortimeo
title_template: "Test Source Sleep Dominance (includes Playwright `waitForTimeout`)"
-->

**TL;DR**: Tests waste time on fixed sleeps baked into the test code — waiting a flat number of seconds instead of waiting for the thing to actually be ready.

**Anti-pattern**: Test steps spend significant time sleeping inside the test source (fixed-ms `setTimeout`, `page.waitForTimeout`, `cy.wait`, raw `sleep()`, polling loops) — not in workflow YAML. OPT17 only detects sleep in workflow files; this pattern catches the much larger category of sleep embedded in test source.

**Subtypes** (all count; use the BROADEST scope — do not pick just one):

- **Playwright hardcoded waits** — `page.waitForTimeout(N)` or bare `waitForTimeout(N)`. These are the most common offender in e2e test suites.
- **Cypress** — `cy.wait(N)` (note: `cy.wait('@alias')` waiting on a network intercept is OK; numeric ms is the anti-pattern).
- **Node/Jest/Vitest** — `await new Promise(r => setTimeout(r, N))`, `await sleep(N)`, `await delay(N)` in `*.test.ts` / `*.spec.ts`.
- **Integration-test fixtures** — `sleep N` in shell wrappers, `time.sleep(N)` in Python tests, fixed waits before assertion retries.

**Detection heuristic (run ALL greps; aggregate matches; sum `sleep_ms` across every file)**:

```bash
# Playwright (the dominant case in monorepos with an e2e package)
grep -rn 'page\.waitForTimeout(\|waitForTimeout([0-9]' \
  packages/ e2e/ playwright/ tests/ integration-tests/ \
  --include='*.ts' --include='*.spec.ts' --include='*.test.ts' 2>/dev/null

# Cypress
grep -rn 'cy\.wait([0-9]' cypress/ e2e/ --include='*.ts' --include='*.js' 2>/dev/null

# Raw setTimeout / sleep / delay inside test files (large N only — 1000ms+)
grep -rn 'setTimeout(.*,\s*[1-9][0-9]\{3,\}\|await sleep(\|await delay(' \
  packages/ e2e/ tests/ --include='*.test.ts' --include='*.spec.ts' 2>/dev/null

# Python test sleeps (when repo has Python tests)
grep -rn 'time\.sleep([0-9]' tests/ --include='*.py' 2>/dev/null
```

**Canonical search paths for monorepos**: `packages/*/e2e/tests/**`, `packages/*/tests/**`, `packages/*/src/**/*.test.ts`, `packages/playground/**`, `packages/memory/integration-tests/**`, top-level `e2e/**`, `playwright/**`, `cypress/**`, `integration-tests/**`. Do NOT stop after the first directory — iterate through each of these before finalizing the finding.

**Aggregation**: Sum `sleep_ms` across EVERY matched file. Compute total sleep per full test run (if test is a single script, sum is the per-run cost; if test is sharded, divide by shard count only if each shard runs a non-overlapping subset). Report the per-run total and the source-of-truth file list in `evidence`.

**Fix**: Replace with event-driven waits. For Playwright: `page.waitForSelector`, `page.waitForLoadState`, `expect(locator).toBeVisible({ timeout })`. For Cypress: `cy.wait('@alias')` on route aliases. For DB/service readiness: ping the service in a bounded retry loop (e.g. `@testcontainers` readiness probes).

**Real-world example (mastra golden 2026-04-09)**: 37 `page.waitForTimeout()` calls across `packages/playground/e2e/tests/` totaling 65,500ms per full run. On E2E kitchen-sink (2,368 runs/mo × 65.5s/run) this is 2,585 min/mo. Initial detection required grepping `page.waitForTimeout` in the `packages/playground/e2e/` scope — looking only at `stores/*.test.ts` misses it entirely.

---

---

### OPT20 — Unpinned Docker Image Tags

<!-- METADATA
pattern: OPT20
impact: LOW
class: static
detector: regex
match: "image:\s*[\"']?[\w./-]+:latest\b"
affected_files: ".github/workflows/*.yml,.github/workflows/*.yaml"
fix_strategy: unpinned-docker-image-tags
title_template: "Unpinned Docker Image Tags"
-->

**Anti-pattern**: Docker services in docker-compose or workflow files using `:latest` or no tag. Causes non-deterministic pulls and prevents layer caching.

**Detection heuristic**:

- grep for image names without version pins in `docker-compose*.yml` and workflow files
- Flag any image reference that doesn't include a specific version tag

**Fix**: Pin all Docker images to specific version tags (e.g., `postgres:16.2` not `postgres:latest`).

---

## Category 4: Parallelization

---

### OPT21 — Unnecessary `needs:` Dependencies

<!-- METADATA
pattern: OPT21
impact: MEDIUM
class: static
detector: yaml-job-correlated
affected_files: ".github/workflows/*.yml,.github/workflows/*.yaml"
fix_strategy: unnecessary-needs-dependencies
title_template: "Unnecessary `needs:` Dependencies"
-->

**TL;DR**: A job waits on another job it doesn't actually depend on, delaying it for no reason.

**Anti-pattern**: Jobs declaring `needs:` on another job when they don't actually consume its outputs, artificially serializing the workflow.

**Detection heuristic**:

```bash
# Parse workflow YAML for needs: declarations
# Check if the dependent job uses any outputs/artifacts from the dependency
```

**Fix**: Remove `needs:` unless the job genuinely requires outputs from the dependency, or the dependency is a gate (e.g., lint must pass before deploy).

---

---

### OPT22 — Sequential Workflows via `workflow_run`

<!-- METADATA
pattern: OPT22
impact: MEDIUM
class: static
detector: yaml-on-trigger
affected_files: ".github/workflows/*.yml,.github/workflows/*.yaml"
fix_strategy: sequential-workflows-via-workflow-run
title_template: "Sequential Workflows via `workflow_run`"
-->

**Anti-pattern**: Chaining workflows with `workflow_run` when they could run in parallel as jobs within a single workflow.

**Detection heuristic**:

```bash
grep -rn 'workflow_run' .github/workflows/
```

**Fix**: Consolidate into a single workflow with parallel jobs, or use `workflow_call` for reusable workflows that can run concurrently.

**Required-checks caveat**: consolidating workflows renames the checks (the old `workflow_run` check name disappears). If the old check was a required status check, add the new job's check name to branch protection as a required check (or the ruleset equivalent), or the consolidated work silently stops gating merges until that admin-only step is done.

---

---

### OPT23 — Single-Threaded Matrix (`max-parallel: 1`)

<!-- METADATA
pattern: OPT23
impact: MEDIUM
class: static
detector: yaml-path
affected_files: ".github/workflows/*.yml,.github/workflows/*.yaml"
fix_strategy: single-threaded-matrix-max-parallel-1
title_template: "Single-Threaded Matrix (`max-parallel: 1`)"
-->

**Anti-pattern**: Setting `max-parallel: 1` on a matrix strategy, running all variants sequentially.

**Detection heuristic**:

```bash
grep -rn 'max-parallel' .github/workflows/
```

**Fix**: Remove `max-parallel` or increase it. If sequential execution is needed for resource constraints, document why.

---

---

### OPT24 — Long Test Job Without Sharding

<!-- METADATA
pattern: OPT24
impact: HIGH
class: static
detector: yaml-path-absent
affected_files: ".github/workflows/*.yml,.github/workflows/*.yaml"
fix_strategy: long-test-job-without-sharding
title_template: "Long Test Job Without Sharding"
-->

**TL;DR**: A test job runs for many minutes as one big job; splitting it into parallel shards would finish it far sooner.

**Anti-pattern**: A single test job running all tests sequentially when the test framework supports sharding.

**Detection heuristic**:

- Identify test jobs with wall-clock time >5 minutes
- Check if the test framework supports sharding (Playwright `--shard`, vitest `--shard`)
- Check if sharding is configured

**Fix**: Add matrix-based sharding. E.g., Playwright: `--shard=${{ matrix.shard }}/${{ strategy.job-total }}`.

**Required-checks caveat**: if the job you're sharding is a **required status check** (a merge gate — which the long pole usually is), the new shard jobs must be added to branch protection as required checks (or the ruleset equivalent), or the sharded-out test work silently stops gating merges — everything stays green while the gate no longer actually runs it. The split isn't complete until the new jobs gate the merge, and re-establishing that gating is usually an admin-only step.

**Wall-clock vs runner-minutes**: Sharding splits the long pole's test execution across **PARALLEL** jobs, so it lowers **wall-clock** (the critical path) but does **NOT** save runner-minutes — the same test work still runs, and more jobs add per-job fixed overhead (checkout, setup, dep install), so runner-minutes go **UP**. The two axes therefore point opposite ways: it is a **TOP Tier-1 wall-clock lever** (push it aggressively when cost is not a constraint — it directly parallelizes the long pole), but it **saves no runner-minutes** (it adds billable compute), so the bill axis shows zero. Note diminishing returns: sharding floors at the per-job fixed overhead, so it must be **stacked** with cache fixes that attack that overhead (warm build cache, dependency cache, browser-binary cache) — past a certain shard count the setup tax dominates and adding shards stops moving wall-clock. See `wall-clock-methodology.md` §7.

---

---

### OPT25 — Shard Imbalance

<!-- METADATA
pattern: OPT25
impact: MEDIUM
class: data-driven
detector: manual
affected_files: ".github/workflows/*.yml,.github/workflows/*.yaml"
fix_strategy: shard-imbalance
title_template: "Shard Imbalance"
-->

**TL;DR**: One slice of a parallel test matrix takes far longer than the others, so the whole job waits on that single slow slice.

**Anti-pattern**: A matrix job where the slowest leg takes >3x longer than the fastest. The workflow's wall clock is bounded by the slowest leg, so the imbalance negates the parallelism benefit.

**TWO DISTINCT CASES — the fix differs, so the detector must classify which it is:**

1. **Homogeneous sharded suite** — the matrix axis is an explicit `shard` / `partition` index (`test (shard 1/4)`, `(partition 2)`). The legs run *interchangeable slices of one test suite*, so a slow shard is a **distribution** problem. Fix = rebalance.
2. **Heterogeneous matrix of distinct legs** — the matrix axis is a set of *different packages / configs / backends* (`@org/prisma-adapter …` vs `@org/memory-adapter …`; `(postgres)` vs `(mysql)`). The legs do **genuinely different work** and are **NOT interchangeable** — you cannot move tests between them. A slow leg here is the **long pole**, not a distribution skew. Fix = split the slow leg itself, NOT rebalance.

**Fix recipe**: The remedy depends on which case the detector classified (the finding's evidence says which). For a **homogeneous sharded suite**, rebalance the distribution — enable timing-based splitting (pytest-split, nextest timing data) or raise the shard count so the hot shard drops toward the mean leg. For a **heterogeneous matrix of distinct legs**, you cannot rebalance non-interchangeable legs — split the slowest leg itself (sub-shard that package's suite, or split its backends into parallel jobs); the saving floors at the next-slowest leg.

**Detection heuristic**:

- Collect per-leg median duration of a matrix base across sampled runs; flag if `max_leg_median / min_leg_median > 3` (**lower to 2x** when the slow leg is the workflow's long pole).
- **Classify the case**: the matrix is *sharded* (case 1) only when the varying axis token is an explicit `shard`/`partition` marker. A prefix-varying / named-package / named-config axis is *heterogeneous* (case 2). Bare-number axes (node versions, etc.) are treated as heterogeneous (they are not interchangeable shards).

**Fix — case 1 (sharded suite), achievable by redistribution**:

- Hash-based partitioning (nextest): increase shard count to dilute hot shards.
- Explicit test lists: rebalance based on measured per-test runtimes.
- Timing-based sharding (pytest-split, nextest timing data): enable it.
- Sizing: the slow shard can drop toward the mean leg duration → `Δwc ≈ slow − mean(legs)`.

**Fix — case 2 (heterogeneous legs), NOT rebalanceable**:

- **Split the slowest leg itself** — sub-shard that package's own test suite (add a `shard` axis *within* the slow package), or split its work into parallel jobs (e.g. run a multi-backend leg's Postgres and MySQL as separate matrix entries).
- Do **NOT** describe this as "rebalance shard distribution" — the legs are not fungible; that advice is inapplicable and misleading.
- Sizing is bounded by the **next-slowest leg**, which becomes the new long pole: splitting the slow leg in two gives `Δwc ≈ slow − max(slow/2, second_slowest)`, not `slow − mean`. To go lower, split the next leg too (stack across the cluster).
- **Required-checks caveat**: splitting a required leg into new matrix entries / parallel jobs adds new check names. If the original leg was a required status check, add the new jobs to branch protection as required checks (or the ruleset equivalent) — otherwise the split-out work silently stops gating merges (everything stays green) until that admin-only gating step is done.

This applies to any framework with sharding (pytest `--shard`, cargo-nextest `--partition`, Jest/Playwright `--shard`) for case 1, and to any package/backend matrix for case 2.

---

## Category 5: Actions and Checkout

---

### OPT26 — Outdated Action Major Versions

<!-- METADATA
pattern: OPT26
impact: LOW
class: static
detector: regex
match: "uses:\s*actions/(checkout|setup-node|setup-python|cache|upload-artifact|download-artifact)@v[123]\b"
affected_files: ".github/workflows/*.yml,.github/workflows/*.yaml"
fix_strategy: outdated-action-major-versions
title_template: "Outdated Action Major Versions"
-->

**Anti-pattern**: Using old major versions of actions (e.g., `actions/checkout@v3` when `v4` is available).

**Detection heuristic**:

```bash
# Extract all action references and compare to latest versions
grep -rn 'uses:' .github/workflows/ | grep -oP 'uses: \K[^@]+@[^ ]+'
```

**Fix**: Update to the latest major version. Check changelogs for breaking changes — the `upload-artifact`/`download-artifact` v3→v4 bump in particular is NOT drop-in: v4 artifacts are immutable and name-unique (a matrix where every leg uploads to the same artifact name fails on v4 — give each leg a unique name and merge on download), and `download-artifact@v3` cannot read v4 uploads, so bump both sides together.

---

---

### OPT27 — Duplicate `setup-node` Calls in Same Job

<!-- METADATA
pattern: OPT27
impact: LOW
class: static
detector: yaml-job-correlated
affected_files: ".github/workflows/*.yml,.github/workflows/*.yaml"
fix_strategy: duplicate-setup-node-calls-in-same-job
title_template: "Duplicate `setup-node` Calls in Same Job"
-->

**Anti-pattern**: Calling `actions/setup-node` more than once in the same job.

**Detection heuristic**:

```bash
# Count setup-node occurrences per job
# Parse YAML and count within each job's steps
```

**Fix**: Remove duplicate calls. If different Node versions are needed, use a matrix instead.

**Real-world example (better-auth)**: release.yml calls `setup-node` twice in the same job.

---

---

### OPT28 — Full Git History Checkout

<!-- METADATA
pattern: OPT28
impact: MEDIUM
class: static
detector: yaml-path
affected_files: ".github/workflows/*.yml,.github/workflows/*.yaml"
fix_strategy: full-git-history-checkout
title_template: "Full Git History Checkout"
-->

**TL;DR**: The checkout downloads the project's entire git history when the job only needs the current code — wasted download time on every run.

**Anti-pattern**: Using `fetch-depth: 0` (full history) when only the latest commit is needed. Wastes time downloading the full git history.

**Detection heuristic**:

```bash
# Check for fetch-depth: 0 or missing fetch-depth (default is 1, which is fine)
grep -rn 'fetch-depth' .github/workflows/
```

**Fix**: Use `fetch-depth: 1` (default) unless the job needs git history (e.g., changelogs, blame). For PR diff detection against the merge commit's parents, `fetch-depth: 2` suffices — but change-scoped runners that diff against the BASE BRANCH (`turbo --filter=...[origin/main]`, `nx affected`, `vitest --changed` — see OPT34/OPT70) need the base ref fetched (`fetch-depth: 0` or a targeted base-ref fetch); do not shallow those jobs.

---

---

### OPT76 — Submodule / Git LFS Checkout Payload

<!-- METADATA
pattern: OPT76
impact: MEDIUM
class: static
detector: yaml-job-correlated
affected_files: ".github/workflows/*.yml,.github/workflows/*.yaml"
fix_strategy: submodule-lfs-checkout-payload
title_template: "Submodule / Git LFS Checkout Payload"
-->

**TL;DR**: The checkout clones every submodule, or downloads every Git LFS
object, for a job whose steps never read them — a fixed download paid on every
single run.

**Anti-pattern**: `actions/checkout` with `submodules: true` / `submodules:
recursive`, or `lfs: true` (equivalently a `git lfs pull` / `git lfs fetch` step
in a run block), in a job that never touches the submodule paths declared in
`.gitmodules` or the paths `.gitattributes` marks `filter=lfs`. The payload is
usually copied wholesale from one job that genuinely needs it (a release build,
a docs render) into every job in the workflow, so the lint job clones the vendor
tree and the unit-test job downloads the design assets. This is the sibling of
OPT28 (`fetch-depth: 0`): the same checkout step, a different payload — and it
is ranked and fixed separately because removing `fetch-depth: 0` does
nothing about a submodule or LFS clone, and vice versa.

**Detection heuristic**:

```bash
# 1. What payload does the repo actually declare?
cat .gitmodules                       # submodule paths
grep -n 'filter=lfs' .gitattributes   # LFS-tracked path patterns

# 2. Which jobs pull it?
grep -rn -e 'submodules:' -e 'lfs:' -e 'git lfs \(pull\|fetch\)' .github/workflows/

# 3. Which of those jobs never reference a declared path (the finding)?
```

A finding requires all three: a declared payload (step 1), a job that pulls it
(step 2), and **no** reference to any declared path anywhere in that job — its
`run` blocks, step and job-level `working-directory`, `strategy.matrix` values,
step `if:` and `name:`, job- and step-level `env:`, `with:` values, `uses:` refs,
and the body of every local composite action the job invokes, followed
transitively into the local actions those invoke (step 3). With no `.gitmodules` (or no
`filter=lfs` line), there is no declared path to prove unread, so nothing is
flagged. When a local composite action the job invokes cannot be read — at any depth in
that chain — the pattern fails **closed** and stays silent, the same conservative
stance OPT28 takes: the cost of a miss is a lost finding, never a fix that breaks
a job. A checkout that names a `repository:` other than this one is skipped
outright: it pulls that repo's submodules and LFS objects, about which this
repo's `.gitmodules` and `.gitattributes` say nothing.

Scoped, like OPT28, to workflows that run on `pull_request` / `push` /
`workflow_call`. A `workflow_dispatch`- or `schedule`-only helper runs ~0×/mo,
so its checkout payload is noise rather than a ranked optimization.

**Fix**: Pull the payload only in the jobs that read it. Drop `submodules:` /
`lfs:` from the other jobs' checkout, or scope the payload down where part of it
is genuinely needed — `submodules: false` plus a targeted `git submodule update
--init --depth 1 <path>` for the one submodule that is read, or `lfs: false`
plus `git lfs pull --include='<path>'` for the assets that are read. Where every
job in a cluster needs the same payload, the lever is OPT73 (a shared sub-step
across the critical-path cluster), not this pattern.

**Core evidence, and its honest limit**: the recipe rests on "this job does not
reference anything under the submodule / does not read the LFS-tracked paths."
The workflow YAML **cannot settle that on its own.** The detector proves only
that no path declared in `.gitmodules` / `.gitattributes` appears in the job's
own YAML or in the local composite actions it invokes. A build script, a
Makefile target, a test fixture, or a config file the job invokes can read the
payload without ever naming the path in CI config — and a `git submodule` /
`git lfs` consumer inside a container image is invisible here too. So the
finding is a **candidate, not a verdict**: before removing the payload, grep the
scripts the job actually runs for the declared paths, and confirm on one run
(the fix is trivially revertible — restore the `with:` key). If the submodule
carries build inputs resolved by path at build time, treat the payload as
load-bearing and skip the finding.

**Sizing**: no static default seconds. The cost is the repo's own payload — the
submodule tree's size and the LFS objects' bytes — which the workflow YAML never
reveals, so the catalog gives this pattern no `_SIZING` model and no modeled
saving; it renders **qualitatively** rather than carrying an invented number
(the same honest path any un-modeled pattern takes — the `_SIZING`
preamble in `collect_runs.py` states the rule: a pattern that isn't in the table
is sized as `None`/`None` and rendered qualitatively, because the scanner never
invents a number). Both axes therefore render empty. Where a reader wants the
number, the measurement is the job's own checkout step duration before and after
the change, per the rollout below — this pattern does not estimate it for them.

**Risk**: **MEDIUM**. For a submodule, removing a payload the job depends on
fails it loudly — a missing path, a missing file — which is why the rollout below
is cheap. LFS is the dangerous variant, and it fails **quietly**: with `lfs:`
dropped, a tracked file is still present as its ~130-byte pointer text, so a tool
reads the pointer and produces a wrong output instead of an error. Only drop
`lfs:` for jobs that read no tracked path at all, and check the job's output, not
just its exit code.

**Guardrail**: Never recommend dropping the payload for a job whose steps, or
whose invoked local actions, reference a declared path. Never recommend it at all
when the declared payload can't be read (no `.gitmodules` / `.gitattributes`) —
absence of a declaration is not evidence of unread payload.

**Rollout**: Change one job, re-run the workflow once, and compare that job's
checkout step duration before and after. Revert by restoring the single `with:`
key.

---

### OPT29 — Merge Queue Skip at Step Level Only

<!-- METADATA
pattern: OPT29
impact: MEDIUM
class: static
detector: yaml-job-correlated
affected_files: ".github/workflows/*.yml,.github/workflows/*.yaml"
fix_strategy: merge-queue-skip-at-step-level-only
title_template: "Merge Queue Skip at Step Level Only"
-->

**Anti-pattern**: Using `if:` conditions on steps to skip work in merge queue runs, but the job still provisions a runner. The runner startup time (~15-30s) is wasted.

**Detection heuristic**:

```bash
# Find jobs triggered by merge_group that have step-level conditions but no job-level if:
# Parse workflow YAML for on: merge_group triggers
# Check each job for job-level if: conditions
```

**Fix**: Add a job-level `if:` condition to skip the entire job for merge_group events when appropriate.

**Sizing (runner-minutes only, physically bounded)**: The waste is confined to the ONE flagged job — never the whole workflow. The saving is `hit_rate (the merge_group-run share) × that job's MEASURED monthly billable compute` from the cost spine (`cost_basis: affected_jobs`, re-grounded by the same machinery OPT45 uses), so it can never exceed what the job burns. The credited figure is a **ceiling**: today only the runner's provisioning is wasted (the steps already skip on merge_group), so the true reclaim is smaller — disclosed as a ceiling in the size note. Pricing this off the workflow long pole × full volume (the pre-#113 model) credited the whole run's compute to a step-skip on a tiny gate job — a physically-impossible saving `check_saving_within_measured_compute` rejects.

**Real-world example (better-auth)**: The `test` job in ci.yml has no job-level `if:` for merge_group — provisions a runner that skips all steps.

**Real-world example (biomejs/biome, #113)**: the `changes` gate in benchmark.yml skips steps on merge_group but still provisions a runner. Its saving is `0.1 × 823 min/mo (the gate's measured billable) = 82.3 min/mo` — NOT `0.1 × the 941s workflow long pole × 823 runs = 1290.7 min/mo`, which exceeded the gate's entire measured compute. (The two `823`s are different units that happen to coincide: the gate is so light it bills the 1-minute minimum on each of its 823 monthly runs, so its measured billable is also ≈823 min/mo.)

---

---

### OPT30 — Matrix Jobs Without Job-Level Conditional

<!-- METADATA
pattern: OPT30
impact: MEDIUM
class: static
detector: yaml-path-absent
affected_files: ".github/workflows/*.yml,.github/workflows/*.yaml"
fix_strategy: matrix-jobs-without-job-level-conditional
title_template: "Matrix Jobs Without Job-Level Conditional"
-->

**Anti-pattern**: Same as OPT29 but for matrix jobs — N runners are provisioned for nothing.

**Detection heuristic**:

- Identify matrix jobs triggered by merge_group
- Check for job-level `if:` conditions

**Fix**: Add job-level `if:` to skip the entire matrix for irrelevant triggers.

**Real-world example (better-auth)**: `adapter-integration` (6 matrix variants) provisions 6 runners in merge queue that all skip.

---

---

### OPT31 — Conditional Step With Unconditional Setup

<!-- METADATA
pattern: OPT31
impact: MEDIUM
class: static
detector: yaml-job-correlated
affected_files: ".github/workflows/*.yml,.github/workflows/*.yaml"
fix_strategy: conditional-step-with-unconditional-setup
title_template: "Conditional Step With Unconditional Setup"
-->

**Anti-pattern**: A step (typically a tool install, browser install, or large download) exists ONLY to support a downstream step that has an `if:` condition gating it. The downstream step is conditionally skipped (e.g., when a secret is missing, when a feature is disabled, on certain branches), but the upstream setup step runs unconditionally — paying the full setup cost for runs where the consumer never executes.

**Why this is missed by OPT1 (Unnecessary Tool Install)**: OPT1 catches the case where the tool is NEVER invoked in the job. OPT31 catches the case where the tool IS invoked, but only conditionally — and the install step is missing the same condition.

**Detection heuristic**:

1. For each job, walk steps in order. For every step that has an `if:` condition (especially conditions referencing `env.*`, `secrets.*`, branch names, or labels):
2. Look at the immediately preceding setup steps (within the same job, no `needs:` boundary). Identify the setup steps whose only consumer is this conditional step.
3. Examples of "setup-and-consumer" pairings:
   - `bunx playwright install` (setup) → `playwright test` or `e2e` (consumer)
   - `apt-get install <pkg>` → `<pkg> --version` or invocation
   - `pip install <test-only-package>` → only used in test invocation
   - `gh auth setup-git` → only used in conditional `gh` calls
4. Flag if the setup is unconditional but the consumer's `if:` would skip in some fraction of runs.
5. The savings = `setup_step_p50_seconds × P(consumer skips)`.

**Fix**: Copy the `if:` condition from the consumer step onto the setup step (or wrap both in a guard step that exits early). Example:

```yaml
# Before
- name: Install Playwright Chromium
  run: cd apps/web && bunx playwright install --with-deps chromium # always runs (~16s)

- name: Web smoke e2e
  if: env.CLERK_SECRET_KEY != ''
  run: cd apps/web && bunx playwright test smoke

# After
- name: Install Playwright Chromium
  if: env.CLERK_SECRET_KEY != '' # add same condition
  run: cd apps/web && bunx playwright install --with-deps chromium

- name: Web smoke e2e
  if: env.CLERK_SECRET_KEY != ''
  run: cd apps/web && bunx playwright test smoke
```

**Real-world example (blen-starter-kit)**: `ci.yml` `web-quality` job has `Install Playwright Chromium` (line 92, ~16s) followed by `Web smoke e2e` (line 95) gated on `if: env.CLERK_SECRET_KEY != '' && env.NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY != ''`. PRs from forks (no Clerk secrets) pay 16s for setup that's never consumed.

**Risk**: LOW. The fix is purely defensive — add the same `if:` to the upstream step. If the conditions diverge later, both steps still execute together (consumer just runs without setup, which would surface as a clear error).

---

## Category 7: Trigger and Scope

---

### OPT32 — Missing `paths`/`paths-ignore` on Expensive Workflows

<!-- METADATA
pattern: OPT32
impact: HIGH
class: static
detector: yaml-on-trigger
affected_files: ".github/workflows/*.yml,.github/workflows/*.yaml"
fix_strategy: missing-paths-paths-ignore-on-expensive-workflows
title_template: "Missing `paths`/`paths-ignore` on Expensive Workflows"
-->

**TL;DR**: Expensive workflows run on every PR even when the change can't affect them — e.g. a docs-only edit triggers the full test suite.

**Anti-pattern**: Expensive CI workflows (E2E tests, full integration suites) run on every push/PR regardless of what changed. A docs-only or README change triggers the full CI suite.

**Detection heuristic**:

```bash
# Check on: block for paths/paths-ignore filters
# Flag workflows with >3 jobs and no path filtering
```

**Fix**: Add `paths-ignore` for documentation, markdown files, and other non-code changes. Or use `paths` to restrict to relevant source directories.

**Required-status-check caveat (the "Pending" landmine)**: if any check this workflow produces is a required status check, do NOT skip it with `paths`/`paths-ignore` (or `branches:` filters / `[skip ci]`) — a workflow skipped by filtering leaves its required check "Pending" and the PR can never merge; official guidance says not to use path or branch filtering on required workflows. The documented-safe shape is a job-level `if:` restating the filter (a skipped job reports Success and satisfies the gate). The no-op twin-workflow (same workflow AND job name, inverse filter) is a community-known workaround, NOT in any current GitHub docs edition. Treat required-status UNKNOWN as required: when branch protection/rulesets are unreadable (the common case on repos you don't admin), assume every check this workflow produces may be required.

**Real-world example (better-auth)**: e2e.yml runs all 8 jobs on docs-only PRs.

---

---

### OPT33 — No Draft PR Gating on Expensive Jobs

<!-- METADATA
pattern: OPT33
impact: MEDIUM
class: static
detector: yaml-job-correlated
affected_files: ".github/workflows/*.yml,.github/workflows/*.yaml"
fix_strategy: no-draft-pr-gating-on-expensive-jobs
title_template: "No Draft PR Gating on Expensive Jobs"
-->

**TL;DR**: Expensive jobs run on draft PRs that are still works-in-progress, burning CI on code that isn't ready for review yet.

**Anti-pattern**: Expensive jobs run on draft PRs where the code is still being worked on.

**Detection heuristic**:

```bash
# Check for draft PR condition in job-level if:
grep -rn 'pull_request' .github/workflows/ -A 10
# Flag expensive jobs missing: if: github.event.pull_request.draft == false
```

**Fix**: Add `if: github.event.pull_request.draft == false` to expensive jobs. Job-level gating is the documented-safe shape — a skipped job reports Success and satisfies a required status check, where narrowing the `pull_request` trigger cannot express draft-ness at all (the default `types:` — opened/synchronize/reopened — fires on drafts too) and a mis-narrowed `types:` list leaves required checks "Pending" and blocks merges. When you add the draft `if:`, also ADD `ready_for_review` to `types:` (it is NOT in the default set): without it, no event fires when the draft flips to ready, and the head commit's required check keeps its draft-time "Success (skipped)" — the PR can merge with the expensive job never having run.

**Required-status-check caveat**: treat required-status UNKNOWN as required — when branch protection/rulesets are unreadable, assume every check this workflow produces may be required, and keep the gating at job level (the "Pending" landmine; see OPT32's caveat for the full mechanism and the community-workaround labeling rule).

**Activation-fidelity gate (only flag jobs that actually run on every PR)**: the detector must confirm the job runs on a normal PR open/update before claiming it "runs on every PR including drafts". A job is NOT every-PR — and must NOT be flagged — when the `pull_request:` trigger is gated by `types:` to a non-lifecycle activity (e.g. `types: [labeled]`, which only fires when a label is added), or the job's own `if:` gates on the activity (`github.event.label` / `contains(github.event.pull_request.labels.*.name, …)` / `github.event.action == '<non-lifecycle>'`). GitHub's default `types:` is exactly `{opened, synchronize, reopened}`; a draft `if:` is a separate concern, not an activity gate. (Shared `_pr_trigger_runs_every_pr` / `_job_runs_on_every_pr` in `scan.py`, also used by OPT39/OPT40.)

---

---

### OPT34 — No Changed-Package Filtering

<!-- METADATA
pattern: OPT34
impact: MEDIUM
class: static
detector: yaml-on-trigger
affected_files: ".github/workflows/*.yml,.github/workflows/*.yaml"
fix_strategy: no-changed-package-filtering
title_template: "No Changed-Package Filtering"
-->

**Anti-pattern**: In monorepos, running all tests/builds regardless of which packages changed.

**Detection heuristic**:

```bash
# Check if turbo is used with --filter for PR workflows
grep -rn 'turbo' .github/workflows/ | grep -v 'TURBO_'
# Flag turbo run commands without --filter=...[base]
```

**Fix**: Use `turbo --filter=...[origin/main]` to only run tasks for changed packages and their dependents (`main` is illustrative — substitute the repo's actual base branch; on PR-triggered runs `origin/${{ github.base_ref }}` resolves it dynamically, so this doesn't break on repos whose default branch isn't `main`). The base ref must exist in the clone: the default `actions/checkout` is a shallow, single-branch clone where that base ref does NOT resolve — add `fetch-depth: 0` (or a targeted `git fetch origin <base-branch>` step) to that job's checkout, and treat this as an explicit exception to OPT28's shallow-checkout guidance.

**Required-status-check caveat**: if the per-package jobs are required status checks, gate them with a job-level `if:` (or an in-job filter like turbo's) so skipped packages still report Success — never by narrowing the workflow's `paths:`/triggers, which leaves required checks "Pending" and blocks the merge (see OPT32's caveat for the full mechanism). Treat required-status UNKNOWN as required when branch protection/rulesets are unreadable.

---

---

### OPT35 — Missing `fail-fast` on Non-Diagnostic Matrix Dimensions

<!-- METADATA
pattern: OPT35
impact: LOW
class: static
detector: yaml-path
affected_files: ".github/workflows/*.yml,.github/workflows/*.yaml"
fix_strategy: missing-fail-fast-on-non-diagnostic-matrix-dimensions
title_template: "Missing `fail-fast` on Non-Diagnostic Matrix Dimensions"
-->

**TL;DR**: A sharded test matrix sets `fail-fast: false`, so when one shard fails the rest keep running and burning minutes on an already-failing run.

**Anti-pattern**: An explicit `fail-fast: false` on a shard-indexed (non-diagnostic) matrix. An ABSENT `fail-fast` is NOT a finding — GitHub Actions defaults to `fail-fast: true`, so only the explicit opt-out wastes compute.

**Detection heuristic**:

```bash
grep -rn 'fail-fast: false' .github/workflows/
# Flag only explicit fail-fast: false on shard-indexed (non-diagnostic) matrices;
# an absent fail-fast already fail-fasts (GHA default: true)
```

**Fix**: Remove the `fail-fast: false` (or set `fail-fast: true`) unless you need all matrix variants to complete for diagnostic purposes (e.g., cross-platform compatibility testing — per-OS/per-version matrices, where `fail-fast: false` is correct).

**Tier-2 render note**: When run history shows an explicit `strategy.fail-fast:
false` shard/partition/chunk matrix where a failed or timed-out shard completed
before sibling shards, OPT35 can promote as measured post-completion waste. The
finding must credit only sibling runtime after the first failed shard, carry
`sizing_basis=measured`, and stamp a `post_completion_waste` certificate whose
evidence names the first failed shard, post-failure minutes, and the diagnostic
matrix carve-out. Static OPT35 hits without failed-run post-failure evidence
remain modeled residual findings in "Also noticed"; a matching static row on a
measured workflow/job is superseded by the measured finding.

---

---

### OPT36 — Cron Schedule Too Frequent

<!-- METADATA
pattern: OPT36
impact: LOW
class: static
detector: yaml-on-trigger
affected_files: ".github/workflows/*.yml,.github/workflows/*.yaml"
fix_strategy: cron-schedule-too-frequent
title_template: "Cron Schedule Too Frequent"
-->

**TL;DR**: A scheduled (cron) workflow runs more often than it needs to, piling up runs.

**Anti-pattern**: Cron-triggered workflows running more frequently than necessary for their purpose. Common examples: cleanup/triage jobs running every 5 minutes when every 15-60 minutes would suffice, or scheduled builds running hourly when daily is adequate.

**Detection heuristic**:

```bash
# Find cron schedules and classify frequency
grep -rn 'cron:' .github/workflows/ | while read line; do
  file=$(echo "$line" | cut -d: -f1)
  cron=$(echo "$line" | grep -oP "'[^']+'" | tr -d "'")
  min_field=$(echo "$cron" | awk '{print $1}')
  # Flag schedules running more than 4x/hour
  if echo "$min_field" | grep -qP '^\*/[1-9]$|^\*/1[0-4]$'; then
    echo "OPT36 hit: $file runs every $(echo $min_field | tr -d '*/')min — verify frequency is justified"
  fi
done
```

**Fix**: Increase the cron interval to match the actual operational need. For issue cleanup/triage bots: `*/15` or `*/30` is typically sufficient. For scheduled builds: daily or every 6 hours. Document the rationale for the chosen frequency.

**Real-world example (mastra)**: PR #14432 changed spam issue cleanup from every 5 minutes (`*/5 * * * *`) to every 15 minutes — the job rarely finds new spam within a 5-minute window.

**Tier-2 render note**: When run history shows consecutive `event=schedule`
runs on the same `head_sha`, OPT36 can promote as measured schedule burn. The
finding must size only the schedule-event subset, carry `sizing_basis=measured`,
price from successful schedule-event job timings only, stamp
`tier2_run_subset_events: ["schedule"]`, and carry a `non_pr_event`
certificate. Static cron-frequency hits without same-`head_sha` run evidence
remain modeled residual findings in "Also noticed"; a matching static row on a
measured workflow is superseded by the measured finding.

---

---

### OPT37 — Workflow Trigger Dependency Gap (Cache Race)

<!-- METADATA
pattern: OPT37
impact: HIGH
class: static
detector: yaml-workflow-correlated
affected_files: ".github/workflows/*.yml,.github/workflows/*.yaml"
fix_strategy: workflow-trigger-dependency-gap-cache-race
title_template: "Workflow Trigger Dependency Gap (Cache Race)"
-->

**TL;DR**: The workflow that writes a cache and the one that reads it can race, so the reader sometimes misses a cache that should have been there.

**Anti-pattern**: A read-only downstream workflow/job depends on a cache that a sibling writer workflow produces, but BOTH workflows trigger on the same event (e.g. `pull_request`). The reader races the writer and usually loses — a job that should hit a warm cache (~6s) instead runs the full build (~300-500s) because the writer hasn't finished yet. Distinct from OPT22 (sequential `workflow_run` adding round-trip latency) — here the problem is the OPPOSITE: the reader is missing the `workflow_run` (or `needs`) dependency it should have.

**Detection heuristic**:

- Find workflows/jobs with `TURBO_CACHE: remote:r`, `TURBO_CACHE: remote:ro`, `pnpm install --prefer-offline`, `actions/cache` with `restore-keys:` but no `key:` write, or similar read-only cache modes
- For each, check whether the writer (e.g., a sibling workflow with `TURBO_CACHE: remote:rw`, or a job that populates the cache key) uses the same `on:` trigger
- Flag when reader and writer share the same webhook event (both on `pull_request`, both on `push`, etc.) and there is no `workflow_run` / `needs` linking them
- Cross-check step-duration distribution from sampled successful runs (per-job `steps[]` timing): reader's cache-dependent step shows bimodal timing (fast when warm, slow when cold) with the slow mode dominating — signal of repeated race loss

**Fix**: Option A — move the reader to `on: workflow_run: {workflows: [<writer>], types: [completed]}` so it runs AFTER the writer populates the cache. Option B — if reader and writer are jobs within the same workflow, add a `needs: <writer-job>` dependency. Option C — if the architectural separation is load-bearing, make the reader seed its own cache instead of reading the sibling's.

**Real-world example (mastra golden 2026-04-09)**: `Validate build outputs` job in `lint.yml` (line 73-102) runs on `pull_request` with `TURBO_CACHE: remote:r` while `Prebuild` runs on the SAME `pull_request` event with `TURBO_CACHE: remote:rw`. The validate-build-outputs Build step measures P50=389s (cold) and ~6s (warm) — cold dominates because Prebuild writes after Validate reads. Fix: move Validate to `workflow_run: [Prebuild]`. Saving: 383s/run × 2,555 runs/mo ≈ 16,301 min/mo.

**GUARDRAIL — log-anchored cache-miss evidence is MANDATORY. YAML inspection alone is INSUFFICIENT to emit a OPT37 finding.**

A OPT37 finding may NOT be emitted (at any severity above "review") from
YAML inspection or step-timing bimodality alone. The detector MUST cite at
least one `actions/runs/<id>` log entry showing the reader job's build
step produced a `Tasks: N cached, M total` (or equivalent build-tool
summary) line below some plausible-race threshold (rule of thumb:
**< 70% task hit rate**, or 0% full-run hit rate, on the reader job's
specific build step). Acceptable forms of the cited line:

- Turbo: `Tasks: N successful, M total` + `Cached: K cached, M total`
  (compute hit rate = K/M).
- sccache: `Compile requests executed` / `Cache hits` block.
- Gradle: count of `FROM-CACHE` vs `EXECUTED` task markers.
- pnpm/npm: lockfile-cache restored vs not-restored block.

If the parsed cache_hit / cache_miss line counts from `gh api repos/{owner}/{repo}/actions/jobs/{job_id}/logs` (downloaded per-job by `collect_runs.py --with-logs`
Step 2b from actual build logs) shows the writer AND reader BOTH
routinely hit cache at ≥ 70% task hit rate, **the race is not actually
happening — downgrade the finding to "review" or invalidate it**. The
genuine cost is then elsewhere (cold-tail outliers, retry storms,
codegen-not-committed, failure-tail in job duration) and the finding
must be reframed against that real cost, not the race framing.

**Authoritative re-derivation lesson — mastra 2026-05-06 → 2026-05-11
(re-derived bug).** The 2026-05-06 audit (Finding 2) initially fired
OPT37 against the `check-bundle` job in `Quality assurance`, claiming
a Turbo cache race. Log inspection of actual runs proved cache hit was
**128/130 tasks (98.5%)** and the 13s build step was followed by a
failure tail driven by `check-clean-worktree.bash` failing on
uncommitted regenerated files (Finding 31, codegen-not-committed) — a
**reliability** finding, not a race. The 2026-05-06 report logged
this as a detector bug ("Detector bugs to file"). Five days later, the
2026-05-11 audit re-derived the same OPT37 finding because the detector
guardrail did not require log-anchored evidence. The fix landed in
a prior worked-example report's Phase 6, but only as
a downgrade — the detector still emitted it. **This guardrail closes
that loop: no log line cited → no OPT37 finding.**

Also acceptable as a STRONGER signal when log-anchored evidence is
sparse: a corroborating step-timing bimodality at the reader's
cache-dependent step (P50 < 50% of P95). This is corroboration, NOT a
substitute for the log evidence. A finding that lacks the log line
must be tagged `severity: review` and routed to Phase 4.5 for log
inspection before it can be promoted.

**Cancel-claim countercheck.** Before shipping, also verify the
**failure mode being attributed to the race is actually the
race**. The mastra-2026-05-06 audit's finding #2 originally claimed
the QA job's 64% failure rate was caused by check-bundle losing the
race; log inspection of two failed runs showed cache hit was 128/130
(98.5%) and the failure was at a different step (`check-clean-worktree.bash`
on uncommitted regenerated files — finding #31). A OPT37 finding that
lacks log-anchored cache-miss evidence AND attributes specific
failures to the race must be downgraded to a draft until the race is
independently confirmed.

**Failure of this guardrail** re-derives a cache-race finding that
log inspection later disproves (cache hit was actually ≥98%, and the
real cost was a reliability/codegen tail). Cite the log line; don't
re-derive it.

---

---

### OPT38 — Non-Content Trigger Event Types (PR `edited`, etc.)

<!-- METADATA
pattern: OPT38
impact: MEDIUM
class: static
detector: yaml-on-trigger
affected_files: ".github/workflows/*.yml,.github/workflows/*.yaml"
fix_strategy: non-content-trigger-event-types-pr-edited-etc
title_template: "Non-Content Trigger Event Types (PR `edited`, etc.)"
-->

**Anti-pattern**: A workflow's `on.pull_request.types` includes events that don't change the code under test, causing the full workflow to re-run on metadata-only changes. The most common offenders:

- `edited` — fires when the PR title, description, or base branch is edited. Title typo fix → full CI re-run.
- `labeled` / `unlabeled` — fires when any label is added/removed. Most workflows don't gate on labels and re-run for nothing.
- `assigned` / `unassigned`, `review_requested`, `review_request_removed` — same story.
- `ready_for_review` — legitimate (draft → ready transition); KEEP this one if you want CI to start when a draft becomes ready.

The default `pull_request` types are `[opened, synchronize, reopened]` — these are the content-change events. Anything beyond that needs justification.

**Detection heuristic**:

```bash
# Find workflows that override pull_request.types
grep -rA3 'pull_request:' .github/workflows/ | grep -E 'types:.*edited|types:.*labeled'
```

For each hit:

1. Confirm the `types:` array includes one of the non-content events.
2. Cross-reference the workflow body for any `if: github.event.action == 'labeled'` (or similar) — if present, the trigger is intentional.
3. If no such guard exists, the metadata events trigger a full no-op re-run.
4. Cost = full workflow P50 × estimated frequency of metadata edits (typically 5-20% of PR activity).

**Fix**: Restrict `types:` to content-change events:

```yaml
on:
  pull_request:
    types: [opened, synchronize, reopened, ready_for_review]
```

Or simply omit `types:` to use the default (which already excludes `edited`).

**Real-world example (blen-starter-kit deep-scan)**: `ci.yml` and several other workflows would re-run on PR title/body edits because of an inherited `types:` array including `edited`. Fix is a one-line YAML change.

**Risk**: LOW. The change reduces noise; legitimate use cases (e.g., a workflow that posts comments based on label) need the trigger and would already have an `if:` guard.

---

---

### OPT39 — Multi-Language Matrix Without Path Filter

<!-- METADATA
pattern: OPT39
impact: MEDIUM
class: static
detector: yaml-job-correlated
affected_files: ".github/workflows/*.yml,.github/workflows/*.yaml"
fix_strategy: multi-language-matrix-without-path-filter
title_template: "Multi-Language Matrix Without Path Filter"
-->

**Anti-pattern**: A security/static-analysis workflow uses a matrix to run language-specific scanners (CodeQL, Snyk, Semgrep, dependency-scan) but never gates the matrix legs on whether files of THAT language changed. CodeQL Python runs on JS-only PRs, CodeQL JavaScript runs on Python-only PRs — both consume runner time + analysis time for zero signal.

**Detection heuristic**:

- Workflow declares a matrix with `language: [javascript, python, ...]` (or similar)
- Uses `github/codeql-action/init` (or `snyk/actions`, `returntocorp/semgrep-action`, etc.) with `languages: ${{ matrix.language }}`
- The workflow has NO preceding `dorny/paths-filter` job or per-leg `if:` checking changed file extensions
- **Activation fidelity**: only flag when the workflow actually runs on every PR — suppress when the `pull_request:` trigger is `types:`-gated to a non-lifecycle activity (e.g. `types: [labeled]`), since the legs then don't run on a normal PR. (Shared `_pr_trigger_runs_every_pr` in `scan.py`; see OPT33.)

**Fix**: Add a pre-job that uses `dorny/paths-filter@v3` to detect which languages changed, then gate each matrix leg with `if: needs.changes.outputs.<lang> == 'true'`. Example:

```yaml
jobs:
  changes:
    runs-on: ubuntu-latest
    outputs:
      js: ${{ steps.filter.outputs.js }}
      python: ${{ steps.filter.outputs.python }}
    steps:
      - uses: actions/checkout@<sha>
      - uses: dorny/paths-filter@v3
        id: filter
        with:
          filters: |
            js: ['**/*.js', '**/*.ts', '**/*.tsx', 'package.json', 'package-lock.json']
            python: ['**/*.py', 'pyproject.toml', 'requirements*.txt']

  codeql:
    needs: changes
    strategy:
      matrix:
        language: [javascript, python]
    if: |
      (matrix.language == 'javascript' && needs.changes.outputs.js == 'true') ||
      (matrix.language == 'python'     && needs.changes.outputs.python == 'true')
```

Caveat: GitHub renders skipped matrix legs as "skipped" (not "passed"). If you have a required-status-check rule on `codeql (javascript)`, change it to `codeql` without the matrix-leg suffix, or make the leg's terminal step a no-op success rather than `if:`-skipping the whole leg. Document the chosen approach in the workflow.

**Real-world example (blen-starter-kit deep-scan)**: `security.yml` runs CodeQL Python and JavaScript on every PR regardless of which app changed. JS-only PRs paid Python init+analyze (~30-60s) for nothing, and vice versa.

**Risk**: MEDIUM. If a polyglot file (a Python/JS bridge, or a config that affects both) changes, both legs should still run. The `paths-filter` rules need to be inclusive enough to catch shared config files.

---

---

### OPT40 — Monorepo Job Runs Regardless of Affected App

<!-- METADATA
pattern: OPT40
impact: MEDIUM
class: static
detector: yaml-workflow-correlated
affected_files: ".github/workflows/*.yml,.github/workflows/*.yaml"
fix_strategy: monorepo-job-runs-regardless-of-affected-app
title_template: "Monorepo Job Runs Regardless of Affected App"
-->

**TL;DR**: In a monorepo, a job for one app runs on every PR even when only a different app changed.

**Anti-pattern**: Repo is a monorepo (top-level `apps/*`, `packages/*`, or `services/*` directories). Workflow has jobs whose work targets one app (e.g., `web-quality` runs `playwright test` in `apps/web/`), but the workflow's `on.pull_request` doesn't have `paths` filters AND no per-job `dorny/paths-filter` gate exists. Result: every PR runs full Playwright + tests + builds for every app, even when only `apps/api/**` changed.

**Why distinct from OPT32**: OPT32 catches workflows missing `paths`/`paths-ignore` at the WORKFLOW level. That works for single-purpose workflows. OPT40 covers the monorepo case where the workflow is correctly scoped (it should run on every PR — there's at least ONE thing that needs to run) but per-job gating is missing for the apps that weren't touched.

**Detection heuristic**:

1. Detect monorepo layout: presence of `apps/`, `packages/`, `services/`, or `pnpm-workspace.yaml` / `turbo.json` / `nx.json`.
2. For each job in each workflow, identify which app it targets:
   - Steps that `cd apps/<name>`, `pnpm --filter <pkg>`, `turbo run --filter=<pkg>`, `nx run <project>:<target>`, etc.
3. For jobs with a clear single-app target, check whether the workflow `paths` includes ONLY that app's path AND whether a `dorny/paths-filter` precedes the job.
4. Flag if neither gate exists.
5. **Activation fidelity**: only flag when the workflow actually runs on every PR — suppress when the `pull_request:` trigger is `types:`-gated to a non-lifecycle activity (e.g. `types: [labeled]`), since the "targets every PR" claim is then false. (Shared `_pr_trigger_runs_every_pr` in `scan.py`; see OPT33.)

**Fix**: Same shape as OPT39 — add a `changes:` job with `dorny/paths-filter@v3` mapping each app/package to its paths, then gate per-job:

```yaml
jobs:
  changes:
    runs-on: ubuntu-latest
    outputs:
      web: ${{ steps.f.outputs.web }}
      api: ${{ steps.f.outputs.api }}
      mobile: ${{ steps.f.outputs.mobile }}
    steps:
      - uses: actions/checkout@<sha>
      - uses: dorny/paths-filter@v3
        id: f
        with:
          filters: |
            web: ['apps/web/**', 'packages/ui/**', 'package.json', 'bun.lock']
            api: ['apps/api/**', 'packages/db/**', 'pyproject.toml']
            mobile: ['apps/mobile/**']

  web-quality:
    needs: changes
    if: needs.changes.outputs.web == 'true'
    # ...
```

Required-status-check caveat from OPT39 applies here too.

**Real-world example (blen-starter-kit deep-scan)**: `ci.yml` `web-quality` runs `bunx playwright install --with-deps chromium` + smoke e2e on every PR, even API-only or mobile-only PRs. Adding the `changes:` job + `if: needs.changes.outputs.web == 'true'` skips the entire job for ~50-70% of PRs.

**Risk**: MEDIUM. Cross-app changes (e.g., a shared `packages/ui` change that affects both `apps/web` and `apps/mobile`) need to be in the `paths-filter` rules for BOTH apps. List shared packages explicitly in each app's filter to avoid false-skip.

---

## Category 8: Release Workflow

---

### OPT41 — `TURBO_FORCE: true` Disabling All Caching

<!-- METADATA
pattern: OPT41
impact: HIGH
class: static
detector: regex
match: "TURBO_FORCE:\s*[\"']?true"
affected_files: ".github/workflows/*.yml,.github/workflows/*.yaml"
fix_strategy: turbo-force-true-disabling-all-caching
title_template: "`TURBO_FORCE: true` Disabling All Caching"
-->

**Anti-pattern**: Setting `TURBO_FORCE: true` in release workflows, which forces all tasks to re-execute and ignores both local and remote cache.

**Detection heuristic**:

```bash
grep -rn 'TURBO_FORCE' .github/workflows/
```

**Fix**: Remove `TURBO_FORCE: true` unless there's a documented reason for it. If freshness is needed, invalidate specific caches instead.

---

---

### OPT42 — `TURBO_CACHE: remote:rw` in Release

<!-- METADATA
pattern: OPT42
impact: HIGH
class: static
detector: regex
match: "TURBO_CACHE:\s*[\"']?remote:rw"
wf_name_filter: "(release|publish|deploy)"
affected_files: ".github/workflows/*.yml,.github/workflows/*.yaml"
fix_strategy: turbo-cache-remote-rw-in-release
title_template: "`TURBO_CACHE: remote:rw` in Release"
-->

**Anti-pattern**: Same as OPT3 but specifically in release workflows.

**Detection heuristic**:

```bash
grep -rn 'TURBO_CACHE' .github/workflows/release*
```

**Fix**: Remove `TURBO_CACHE` or set to `local:rw,remote:rw`.

**Real-world example (better-auth)**: release.yml still has `TURBO_CACHE: remote:rw` (PR #7950 only fixed ci.yml).

---

---

### OPT43 — Excessive Queue Time

<!-- METADATA
pattern: OPT43
impact: MEDIUM
class: data-driven
detector: manual
affected_files: ".github/workflows/*.yml,.github/workflows/*.yaml"
fix_strategy: excessive-queue-time
title_template: "Excessive Queue Time"
-->

**Anti-pattern**: Jobs spending significant time in queue before a runner picks them up, indicating runner pool saturation or overly restrictive concurrency groups.

**Detection heuristic**:

- Compute the wait-to-start per job across recent runs as **run `created_at` (the trigger) → job `started_at`** — NOT the job's own `created_at`. GitHub stamps a *gated* job's `created_at` when its `needs:` dependency resolves, so `started − job.created` sees only that job's own runner pickup and hides the upstream gating cost (the gating job's queue + run time) the developer also waited on (it can undercount by minutes). Measuring from the run trigger captures the full pre-start wait the developer experiences. **Caveat:** for a gated job this number then *includes* the gating job's run time, so it is wall-clock time-to-start, not pure queue — the savable portion is bounded by the gating job's own fix. Entry (un-gated) jobs are unaffected: their `created_at` ≈ the run trigger.
- Use **percentile-based baselines by trigger type** (PR runs typically queue differently than release/schedule runs)
- Flag P90 queue time >60s for PR jobs, >120s for release jobs

**Fix**: Depends on root cause — if runner pool saturation: increase runner pool or use larger runners. If concurrency group: relax the group, or cancel superseded runs — but take the **scoped** predicate from [OPT45](#opt45--missing-concurrency-groups) / [OPT46](#opt46--superseded-runs-not-cancelled-missing-concurrency-or-cancel-in-progress-false). **Never a bare `cancel-in-progress: true`**: it kills in-flight runs on the default branch and on release tags (a half-finished deploy/publish/migration), and it must never be reachable from a PR whose head branch is itself the default branch.

---

---

### OPT44 — Concurrency Group Too Restrictive

<!-- METADATA
pattern: OPT44
impact: MEDIUM
class: static
detector: yaml-path
affected_files: ".github/workflows/*.yml,.github/workflows/*.yaml"
fix_strategy: concurrency-group-too-restrictive
title_template: "Concurrency Group Too Restrictive"
-->

**Anti-pattern**: Concurrency groups that are too narrow, causing jobs to queue or get cancelled unnecessarily.

**Detection heuristic**:

```bash
grep -rn 'concurrency:' .github/workflows/ -A 3
# Check group key granularity
```

**Fix**: Use broader groups (e.g., per-workflow per-branch instead of per-job per-branch).

---

---

### OPT45 — Missing Concurrency Groups

<!-- METADATA
pattern: OPT45
impact: HIGH
class: static
detector: yaml-path-absent
affected_files: ".github/workflows/*.yml,.github/workflows/*.yaml"
fix_strategy: missing-concurrency-groups
title_template: "Missing Concurrency Groups"
-->

**TL;DR**: Nothing cancels superseded runs, so a branch can have several runs of the same workflow going at once after each push — whether the runs come from `pull_request` or from `push`.

**Anti-pattern**: No concurrency group on a `pull_request`- **or** `push`-triggered workflow, allowing multiple runs for the same branch to pile up.

**Detection heuristic**:

```bash
# Flag workflows triggered by pull_request OR push that declare no
# `concurrency:` block (top-level or per-job).
```

**Fix**: Add a concurrency group and scope the *cancellation* with an expression — a bare `cancel-in-progress: true` also cancels in-flight runs on `main` and on release tags, killing a half-finished deploy, publish, or migration. The detector fires on `push`-triggered workflows too, so the recipe must be safe on them:

```yaml
concurrency:
  group: ${{ github.workflow }}-${{ github.head_ref || github.ref_name }}
  cancel-in-progress: >-
    ${{ github.event_name == 'pull_request'
    && github.head_ref != github.event.repository.default_branch }}
```

`cancel-in-progress` accepts an expression, so the predicate is re-evaluated per event: `push`, `merge_group`, `schedule`, and tag builds all evaluate to `false` and run to completion; only superseded PR runs are cancelled.

**The group key and the cancel predicate pull against each other — this is the whole design tension, state it before you touch either.** The group key uses `github.head_ref || github.ref_name` — both are the **short** branch name (`head_ref` is set only on `pull_request`; `ref_name` is the short form on `push`, where `github.ref` would be the fully-formed `refs/heads/…`). Unifying them is deliberate: it puts a branch's `push` and `pull_request` runs in the **same** group, which is what buys the push+PR double-trigger dedup (OPT47). Writing `github.head_ref || github.ref` instead would produce different strings for the same branch, the runs would never share a group, and that saving would be zero.

But that same unification is what makes the cancel predicate dangerous. GitHub decides cancellation from the **incoming** run's `cancel-in-progress`, and it cancels every in-progress run in the group *regardless of their settings*. A fork contributor who commits on **their fork's `main`** and opens a PR (the most common fork workflow — likewise a gitflow `main → develop` back-merge PR) produces a PR run with `head_ref == "main"`, i.e. group `CI-main` — the **same group** as the upstream repo's own `push: [main]` run (`ref_name == "main"`). Its predicate would be `true`, so it would cancel the in-flight push-to-`main` run: exactly the harm this recipe exists to prevent, through a different door. The `&& github.head_ref != github.event.repository.default_branch` term closes it **for the default branch**. `github.event.repository.default_branch` is present on **both** the `push` and the `pull_request` payloads, so it needs no per-repo substitution. Residual: the same name-collision exists for any *other* long-lived branch that both receives pushes and can appear as a PR head name (`release/1.x`, `develop`) — if pushes to such a branch must never be cancelled, add an explicit `&& github.head_ref != '<branch>'` term per branch (a general protected-branch set-membership test is not expressible in a substitution-free one-liner).

**Routing (mechanical — check the trigger set the finding's evidence line reports)**: if the workflow has **no `pull_request` trigger** (the detector also fires on `push` alone), `${{ github.event_name == 'pull_request' }}` is never true, the block cancels nothing, and the runner-minute saving is **zero**. In that case use OPT46's **widened** predicate instead of this one.

**Folded-scalar discipline**: keep the `>-` continuation line at the **same indent** as `${{` (as above) so YAML folds it into one space-joined line; a more-indented line keeps its newline, and an expression carrying a literal newline is not a documented expression — see OPT46's note below for why a mis-folded predicate degrades to a truthy *string* and cancels on **every** event. Equivalently, write the predicate on one line.

---

---

### OPT46 — Superseded Runs Not Cancelled (Missing Concurrency or `cancel-in-progress: false`)

<!-- METADATA
pattern: OPT46
impact: MEDIUM
class: data-driven
detector: manual
affected_files: ".github/workflows/*.yml,.github/workflows/*.yaml"
fix_strategy: superseded-runs-not-cancelled-missing-concurrency-or-cancel-
title_template: "Superseded Runs Not Cancelled (Missing Concurrency or `cancel-in-progress: false`)"
-->

**Anti-pattern**: A workflow that triggers on `push` (or `pull_request`) for branches where developers commonly push multiple times in quick succession, but the workflow either (a) has **no top-level `concurrency:` block at all**, or (b) has a concurrency group with `cancel-in-progress: false`. In either case, every superseded push run continues to occupy a runner — wasting the entire wall-clock duration of the run for the obsolete commit.

This pattern has two flavors that share the same fix mechanism:

- **Flavor A — no concurrency block**: workflow triggers on `push` (often with `branches-ignore: [main]` or similar) but has no `concurrency:` block. Common because authors use `concurrency:` for `pull_request` workflows but forget that `push` workflows on branches need it just as much.
- **Flavor B — `cancel-in-progress: false`**: concurrency group is set but explicitly does not cancel. Stale runs aren't cancelled when a new push arrives.

**Detection heuristic**:

1. **Structural scan** — for every workflow file in `.github/workflows/`:

   ```bash
   # Workflows that trigger on push without a top-level concurrency block
   for f in .github/workflows/*.yml; do
     if yq '.on' "$f" | grep -q 'push' && ! yq '.concurrency' "$f" | grep -q 'group'; then
       echo "$f: push trigger without concurrency"
     fi
   done

   # Workflows with concurrency but cancel-in-progress: false
   grep -rn 'cancel-in-progress: *false' .github/workflows/
   ```

2. **Quantify wasted compute** — a structural match alone is not enough, and neither is "a branch had ≥2 runs" (sequential commits on a long-lived / default branch each test a distinct commit and were never superseded — cancelling them saves nothing). Measure the runs that ACTUALLY RACED:
   - List runs (all statuses) via `gh api repos/{owner}/{repo}/actions/workflows/{wf}/runs?created=<30d-window>`; group by `head_branch`; within each branch, count a run as **superseded** iff a later-created run **started before it finished** (timestamp overlap of `run_started_at`…`updated_at`). Sequential, non-overlapping runs count 0.

3. **Bounded savings estimate** — size the **cancellable remainder**, not the whole run, and report a range:
   - **Lower (credited)**: `cancel-in-progress` cancels a superseded run the moment its successor **starts**, so only the compute it would have burned AFTER that moment is reclaimable — the *remainder*. For each superseded run *i*, `remainder_i = end_i − (earliest later start < end_i)`; credit `mean-per-run compute × Σ(remainder_i / duration_i)` over the superseded runs. Compute spent *before* supersession is spent either way, so charging a run superseded 30s before its natural finish its whole cost over-states the reclaimable amount (the gap grows the later runs get superseded). Per-second compute is unknowable (a run's jobs run in parallel), so this pro-rates the **mean** per-run compute by each run's wall-clock remainder fraction — say so in the basis note.
   - **Upper**: the naive `Σ(runs - 1)` over multi-run branches priced at the **whole** run (the loose bound if every non-final run were wasted end-to-end). The old "overlap-confirmed × whole-run" figure is now **neither** bound — the whole-run price only survives as this upper bound.
   Per-run compute is the **mean** job-minutes of the sampled successful runs (needs ≥3 timed runs to be stable). Extrapolate the sampled count to the 30-day volume by `monthly_volume / sampled_n` in **both** directions (a low-frequency workflow's recent slice spans >30 days and must scale down); compute the remainder ratio on the sampled window and apply it before scaling. Skip dormant workflows (`monthly_volume` 0); a run missing either timestamp contributes nothing and is disclosed as a skip. Report as a range; the superseded attribution is **inference** (the API marks no run "cancelled-by-concurrency").

**Fix**: Add a top-level concurrency block, and make the *cancellation* conditional — **never** a bare `cancel-in-progress: true`. This pattern fires on workflows that trigger on `push`, and a bare `true` cancels the in-flight run on `main` or on a release tag the moment the next commit lands — a deploy, a publish, or a migration killed halfway. `cancel-in-progress` accepts an expression ([workflow syntax: `concurrency`](https://docs.github.com/en/actions/reference/workflow-syntax-for-github-actions#concurrency)), so scope it to the events where a superseded run is genuinely worthless.

**Default (PR-scoped)** — correct whenever the racing runs are PR runs:

```yaml
concurrency:
  group: ${{ github.workflow }}-${{ github.head_ref || github.ref_name }}
  cancel-in-progress: >-
    ${{ github.event_name == 'pull_request'
    && github.head_ref != github.event.repository.default_branch }}
```

`push`, `merge_group`, `schedule`, and tag builds evaluate to `false` and run to completion. `merge_group` needs no separate carve-out here — cancelling a queued merge-group run can eject the PR from the merge queue, and this predicate already excludes it.

**The tension you must not "simplify" away.** The group key uses `github.head_ref || github.ref_name` — both are the **short** branch name (`head_ref` is set only on `pull_request`; `ref_name` is the short form on `push`, where `github.ref` would be the fully-formed `refs/heads/…`). That is deliberate: it lands a branch's `push` and `pull_request` runs in the **same** group, which is the whole point of the push+PR double-trigger dedup (OPT47). Writing `github.head_ref || github.ref` instead yields two different strings for one branch, the runs never share a group, and that saving is zero.

**And that unification is exactly what makes the cancel predicate dangerous.** Cancellation is decided by the **incoming** run's `cancel-in-progress`, and it kills every in-progress run in the group *regardless of their settings*. A fork contributor commits on **their fork's `main`** and opens a PR (the most common fork workflow; a gitflow `main → develop` back-merge PR does the same): that PR run has `head_ref == "main"` → group `CI-main` — the **same group** the upstream repo's own `push: [main]` run sits in (`ref_name == "main"`). Without a guard its predicate is `true`, so it **cancels the in-flight push-to-`main` run** — the very harm this recipe exists to prevent, through a different door. The `&& github.head_ref != github.event.repository.default_branch` term closes it **for the default branch**: a PR whose head branch *is* the default branch never cancels. `github.event.repository.default_branch` is present on **both** the `push` and the `pull_request` payloads (and on `pull_request` it is the **base**/upstream repo's default branch, which is the one that matters), so it needs no per-repo substitution. Residual: the same name-collision applies to any *other* long-lived branch that both receives pushes and can appear as a PR head name (`release/1.x`, `develop`, `production`) — if pushes to such a branch must never be cancelled, add an explicit `&& github.head_ref != '<branch>'` term per branch, exactly as the widened form's release-branch note below prescribes for `github.ref`.

**Widened (Flavor A — waste is on feature-branch pushes)** — a `push`-triggered workflow (e.g. `branches-ignore: [main]`) gets *zero* benefit from the PR-scoped predicate, because it never sees a `pull_request` event. Cancel on every ref *except* the protected ones:

```yaml
concurrency:
  group: ${{ github.workflow }}-${{ github.head_ref || github.ref_name }}
  cancel-in-progress: >-
    ${{ github.event_name != 'merge_group'
    && github.ref != format('refs/heads/{0}', github.event.repository.default_branch)
    && github.head_ref != github.event.repository.default_branch
    && !startsWith(github.ref, 'refs/tags/') }}
```

Substitution-free by construction — `format()` is available anywhere expressions are, and `github.event.repository.default_branch` is on every one of these payloads, so a repo whose default branch is `master` or `develop` copy-pastes this **unchanged**. (Do **not** hardcode `'refs/heads/main'`: on such a repo the term never matches, and the block degenerates to `cancel-in-progress: true` on its own default branch — the original bug, restored.)

Two terms that look redundant and are not:
- `github.event_name != 'merge_group'` — a merge-group run's `github.ref` is a `gh-readonly-queue/...` ref, so the branch and tag terms alone would **not** exclude it, and cancelling it can eject the PR from the queue.
- `github.head_ref != github.event.repository.default_branch` — if this workflow ever also sees `pull_request` events, a PR's `github.ref` is `refs/pull/N/merge`, which passes the branch test and the tag test. That is the same fork-PR-from-`main` hole described above; this term is its guard here.

If the repo also protects long-lived **release** branches that must never be cancelled, add an explicit term (e.g. `&& !startsWith(github.ref, 'refs/heads/release/')`). `github.ref_protected` looks like a shortcut for that, but it is unreliable in **both** directions and this catalog does not recommend it: an *unprotected* default branch reads `false` (→ it gets cancelled — the bug), and repo **rulesets** increasingly target `~ALL` branches, which makes `ref_protected` `true` on ordinary feature branches (→ the predicate never fires and the saving is **zero**).

Keep the continuation lines at the **same indent** as `${{` (as above). YAML folds a `>-` block into one space-joined line only for lines at the base indent; a *more-indented* line keeps its newline, and an expression carrying literal newlines relies on undocumented lexer behavior — if it were ever read as a plain string, a non-empty string is truthy and the workflow would cancel on **every** event, which is exactly the failure this recipe exists to prevent. (`>-` also chomps the trailing newline; `|` / `>` would leave one, and `"false\n"` is truthy.) Equivalently, write the whole predicate on one line.

**When NOT to cancel at all**: release/deploy/publish workflows where partial completion is unsafe (artifacts uploaded, tags pushed, deployments in flight). Keep `cancel-in-progress: false` (or no concurrency) for those — the wasted-compute cost is justified. The scanner already suppresses OPT45/OPT46 on release-like workflows, but confirm it against the workflow's actual jobs before shipping.

**Real-world example (2026-05)**: a `Pre-merge` workflow triggered on `push` with `branches-ignore: [main]` and had no top-level concurrency block. 62% of branches had ≥2 runs in a 30-day window. Adding the **widened** block above (this is a Flavor A / push-triggered case) saved an estimated 70-125 min/mo of runner compute.

**Tier-2 render note**: When the detector can prove overlap from real run timestamps, OPT46 is eligible for the first-class runner-minute section only if the finding is stamped `sizing_basis=measured` and carries a `tier2_neutrality` certificate. Modeled or uncertified instances stay in the residual "Also noticed" appendix.

---

---

### OPT47 — Redundant push + pull_request Double-Trigger

<!-- METADATA
pattern: OPT47
impact: MEDIUM
class: data-driven
detector: manual
affected_files: ".github/workflows/*.yml,.github/workflows/*.yaml"
fix_strategy: redundant-push-pull_request-double-trigger
title_template: "Redundant push + pull_request Double-Trigger"
-->

**Anti-pattern**: A workflow triggers on **both** `push` and `pull_request`, and the `push` trigger is **not restricted to the default branch**. Every commit on a PR branch then runs the workflow **twice** — once for the `push` event and once for the `pull_request` event — building the same `head_sha` on two runners. The `pull_request` run is the one that gates the merge; the `push` run is redundant compute.

Not a finding on structure alone — a repo may legitimately want push builds. It requires **measured** duplication.

**Detection heuristic**:

1. **Structural scan** — the workflow's `on:` includes both `pull_request` and a `push` whose `branches:` filter is absent (or lists branches beyond the default). A `push` scoped to `branches: [main]` does **not** double-fire on PR branches and is excluded.
2. **Positive instance evidence** — in the run history (all statuses, last 30 days), find commits where the **same `head_sha`** produced both a `pull_request`-family run and a `push` run **on a non-default branch**. Only non-default-branch pushes count: a push on the default branch sharing a PR sha is a post-merge (rebase/FF) validation run, which the fix keeps — counting it would be a false saving.
3. **Bounded savings** — size the redundant (`push`-event) runs' compute: `count(duplicated commits) × mean-per-run job-minutes`, extrapolated to the 30-day volume. Report the sample size (`n` runs) and the duplicated fraction.

**Fix**: Add a `branches:` filter to the `push:` trigger so push builds only run where you actually want them (typically the default branch); PRs continue to run via `pull_request`:

```yaml
on:
  push:
    branches: [main]
  pull_request:
```

**GUARDRAILS before removing push:**
1. **Confirm which check the merge requires.** If branch protection requires the **push**-triggered check (not the `pull_request` one), filtering push out leaves the required check unsatisfied and blocks merges. Verify the required status check is the `pull_request` run first. (This is why ci-speedup emits OPT47 as a measured bill finding but does **not** yet certify it wall-clock-neutral.)
2. **Confirm the push run has no SIDE EFFECT the PR run lacks.** A per-commit preview deploy, a sha-tagged image/artifact, or a cache warm makes the push run **not redundant** — filtering it would break that. The run list can't see side effects, so ci-speedup carves out release/deploy/publish-named workflows and warns; you must verify the rest.

**Tier-2 render note**: OPT47 remains a measured residual bill finding until its required-check and side-effect neutrality can be certified. Do not promote it to the neutral runner-minute section by pattern alone.

**When NOT to apply**: workflows that intentionally build both a branch's push and its PRs for different purposes (e.g. push publishes a preview, PR runs tests) — the two runs aren't redundant.

---

---

### OPT48 — High Job-Level Failure Rate (>15% over 30d)

<!-- METADATA
pattern: OPT48
impact: MEDIUM
class: data-driven
detector: manual
affected_files: ".github/workflows/*.yml,.github/workflows/*.yaml"
fix_strategy: high-job-level-failure-rate-15-over-30d
title_template: "High Job-Level Failure Rate (>15% over 30d)"
-->

**TL;DR**: This workflow fails a large share of the time. If those are real bugs/flakes they waste CI; if it's a deliberate policy gate, the failures are it working as intended.

**Anti-pattern**: A job's `failures / (failures + successes)` rate stays above 15% over a 30-day window. Each failed run wastes the full job duration (the runner still paid for its wall clock) AND usually blocks the PR until retried — doubling or tripling the effective time cost. Distinct from OPT46 (superseded-run cancellations): a _failure_ is a hard error, a _cancel_ is a deliberate stop.

Root causes typically fall in a few buckets:

- **Live external dependency variance** — tests hit real LLM/cloud APIs whose latency/output drifts; no recorder/replay harness.
- **Timing-sensitive tests** — `page.waitForTimeout` or fixed `setTimeout` values picked for median machines; tail runs exceed them.
- **Resource contention** — multiple heavy jobs share a runner pool; one OOMs or times out.
- **Flaky third-party services** — rate-limits, intermittent 5xx, eventual consistency.

**Detection heuristic**:

- For each workflow/job in the workflow's `runs?status=...` totals from `gh api repos/{owner}/{repo}/actions/workflows/{wf}/runs?per_page=1&status=...`, compute `failure / (failure + success)`
- Flag when rate > 15% AND `(failure + success) > 100` (ignore low-volume jobs)
- Cross-reference job duration: high-failure-rate × long-P50 = highest cost; investigate those first
- For each flagged job, investigate root cause: read the job's primary test invocation + its config (vitest.config.ts, jest.config.js, playwright.config.ts) and look for sleeps, tight timeouts, or missing retry/replay wiring

**Fix**: Depends on root cause. For live-LLM variance: install a recorder/replay plugin (Mastra's llmRecorderPlugin, similar in other stacks). For timing-sensitive tests: replace waitForTimeout with event-driven waits, raise timeouts, add targeted retries via vitest's `retry`/playwright's `expect.poll`. For resource contention: increase runner concurrency caps or shard the job. For flaky services: add exponential-backoff retries for specific API calls with a sensible ceiling.

**Real-world example (mastra golden 2026-04-09)**: E2E Tests `E2E kitchen-sink` at 22.5% failure rate (644 failures / 2,864 triggered over 30d), P50=457s. Every failure wastes ~457s × 644 ≈ 4,904 min/mo. Memory Tests `test` at 23.8% failure rate (634/2,660), P50=212s → 2,240 min/mo. Fix: wire in Mastra's `llmRecorderPlugin` for replay mode in CI, bump vitest `testTimeout` / `hookTimeout`, and gate live-API runs to nightly instead of PR.

---

## Category 10: Timing Anomalies

---

### OPT49 — Slow Setup Step

<!-- METADATA
pattern: OPT49
impact: MEDIUM
class: data-driven
detector: manual
affected_files: ".github/workflows/*.yml,.github/workflows/*.yaml"
fix_strategy: step-duration-outlier
title_template: "Slow or High-Variance Step"
-->

> **⚠️ CUT — NOT auto-emitted.** This pattern's detector is retained for
> reference but is **not dispatched** by `collect_runs.py`. "A setup step takes
> >60s" inferred the root cause (*uncached*) from the duration alone, never
> proving a cold/missing cache the way the cache family (OPT3/5/8/9, `--with-logs`)
> does — so it was the "a step is slow" observation the admission gate forbids,
> with a one-size "add a cache" fix mis-applied across heterogeneous steps (a 61s
> `Checkout` is a git fetch, not uncached deps). The **verified** slow-setup
> signal is now carried by the cache family (which proves the cache is cold from
> the log) and by **OPT73** (a shared setup step across the cluster, sized
> honestly). The body below is historical reference only.

**TL;DR**: A setup step (checkout/install) consistently takes over a minute — usually an uncached dependency fetch that caching would shrink.

**Anti-pattern**: This is NOT "a step takes a while" — a long step is an observation, not a defect. It fires on one of two *specific, root-causable* conditions, measured across sampled runs of the **same** step:

1. **Slow setup step** — a *setup* step (checkout / install / cache-restore / toolchain) whose **median** duration stays above 60s. Setup is pure overhead before any test/build work; a consistently slow setup is an uncached / unpinned / un-mirrored dependency fetch. The remedy is a caching/pinning change, and the saving is the setup time above a warm-cache floor.
2. **High run-to-run variance** — a step whose duration swings widely *across runs of the same job* (stddev/mean > 0.5). This is a **reliability** signal, not a fixed cost: the step is fast on most runs and slow on a tail (flaky external API, cold/missing cache on some runners, resource contention, a retry). The evidence shows the run-to-run distribution (P50 vs P95 vs max) so you can see how far the tail is from the typical run — that spread IS the finding. The realizable saving is the *tail excess* (how much the slow runs inflate the average), realized only on the tail, never the full stddev and never more than the job's own slice of the critical path.

**Detection heuristic**:

- For each step, compute median / mean / stddev / P95 across sampled runs.
- Flag a *setup* step with median > 60s (case 1), or any step with stddev/mean > 0.5 and mean > 10s (case 2).
- Size against the critical path: credit wall-clock only when the step's job is on the long pole, and cap at the long-pole headroom (a step in a sub-floor job is runner-minute only).

**Fix recipe**: For a slow **setup** step, cache and pin it — `actions/cache` keyed on the lockfile (or `setup-node`/`setup-python` `cache:`), pin action and toolchain versions, and use a mirror/CDN for large downloads; the warm-cache run is the floor. For a **high-variance** step, this is an *investigation*, not a one-line edit — open the linked slowest runs (the P95/max samples), compare a slow run against a fast one step-by-step, and fix the specific cause: record/replay or retry a flaky external call, warm the cache that missed on the slow runs, raise runner size if it's contention, or split a step that intermittently does extra work. The honest saving is the tail you remove, not the whole step.

**Wall-clock vs runner-minutes**: A variance finding is a reliability lever first. Its wall-clock saving is the tail-inflation (mean − median), capped at the critical-path headroom — it must never be sized as if the full stddev is reclaimed on every run, and never exceed the run's critical path.

---

---

### OPT50 — Post Steps Taking Too Long

<!-- METADATA
pattern: OPT50
impact: MEDIUM
class: data-driven
detector: manual
affected_files: ".github/workflows/*.yml,.github/workflows/*.yaml"
fix_strategy: post-steps-taking-too-long
title_template: "Post Steps Taking Too Long"
-->

**Anti-pattern**: GitHub Actions "Post" steps (cache save, cleanup) taking excessive time, often due to large cache uploads.

**Detection heuristic**:

- Look for "Post" steps in timing data with duration >30s
- Check cache sizes being uploaded

**Fix**: Reduce cache scope, exclude unnecessary files, or use more granular cache keys.

---

---

### OPT51 — Install-to-Test Ratio >50%

<!-- METADATA
pattern: OPT51
impact: MEDIUM
class: data-driven
detector: manual
affected_files: ".github/workflows/*.yml,.github/workflows/*.yaml"
fix_strategy: install-to-test-ratio-50
title_template: "Install-to-Test Ratio >50%"
-->

> **⚠️ CUT — NOT auto-emitted.** This pattern's detector is retained for
> reference but is **not dispatched** by `collect_runs.py` — same flaw as OPT49.
> A high setup/total *ratio* is an OBSERVATION, not a verified lever: the
> detector credited `med_total * (med_ratio - 0.3)` as savings (assuming setup
> is reducible to 30% of the job) without ever proving the setup *is* reducible.
> A high ratio is just as often STRUCTURAL — a peer-dependency validator, a
> docs-lint, or a Docker/Playwright job is mostly install by nature and can't be
> cached away — so it sized large runner-min figures onto unrealizable savings,
> exactly the "a job is slow" observation the admission gate forbids. The
> **verified** setup signal is now carried by the cache family (proves a cold
> cache from the log), by **OPT73** (a shared setup step across the cluster,
> sized honestly), and by the artifact-handoff patterns (a concrete, realizable
> lever). The body below is historical reference only.

**TL;DR**: A job spends more time setting up (checkout + install) than actually running tests or building.

**Anti-pattern**: More than half of a job's runtime is spent on setup/install steps rather than actual test/lint/build work. Indicates caching problems or excessive setup.

**Detection heuristic**:

- Classify each step as "setup" (checkout, install, cache restore, Docker startup) or "work" (test, lint, build, type-check)
- Compute ratio: setup_time / total_time
- Flag if >50%

**Fix**: Improve caching, use artifact handoff from a setup job, or consolidate setup into a composite action.

---

## Category 11: Stack-Specific

> The patterns below apply only when the repo uses the listed tools.
> Skip the entire category if the repo's stack doesn't match.

---

### OPT52 — Turbo Tasks Missing `outputs` in turbo.json

<!-- METADATA
pattern: OPT52
impact: MEDIUM
class: static
detector: repo-file-check
affected_files: ".github/workflows/*.yml,.github/workflows/*.yaml"
fix_strategy: turbo-tasks-missing-outputs-in-turbo-json
title_template: "Turbo Tasks Missing `outputs` in turbo.json"
-->

**TL;DR**: A Turbo task declares no outputs, so Turbo can never cache its result — it re-runs in full every time.

**Anti-pattern**: Turbo tasks without `outputs` configured in `turbo.json`, meaning Turbo can't effectively cache the task results.

**Detection heuristic**:

```bash
# Parse turbo.json and check each task for outputs
cat turbo.json | jq '.tasks // .pipeline | to_entries[] | select(.value.outputs == null or (.value.outputs | length == 0)) | .key'
```

**Fix**: Add appropriate `outputs` globs to each task in `turbo.json`.

---

---

### OPT53 — Unstable Env Vars Invalidating Turbo Cache

<!-- METADATA
pattern: OPT53
impact: MEDIUM
class: static
detector: repo-file-check
affected_files: ".github/workflows/*.yml,.github/workflows/*.yaml"
fix_strategy: unstable-env-vars-invalidating-turbo-cache
title_template: "Unstable Env Vars Invalidating Turbo Cache"
-->

**Anti-pattern**: Environment variables that change between runs (timestamps, build numbers) included in Turbo's env hash, causing cache misses.

**Detection heuristic**:

```bash
# Check turbo.json for globalEnv and task-level env
cat turbo.json | jq '.globalEnv, (.tasks // .pipeline | .[].env)'
# Flag known-unstable vars: GITHUB_RUN_ID, GITHUB_RUN_NUMBER, BUILD_NUMBER
```

**Fix**: Remove unstable env vars from Turbo's env configuration, or use `globalPassThroughEnv` for vars that should be available but not affect caching.

---

---

### OPT54 — Full-Repo `pnpm -r` Where Package Filters Are Possible

<!-- METADATA
pattern: OPT54
impact: MEDIUM
class: static
detector: yaml-path
affected_files: ".github/workflows/*.yml,.github/workflows/*.yaml"
fix_strategy: full-repo-pnpm-r-where-package-filters-are-possible
title_template: "Full-Repo `pnpm -r` Where Package Filters Are Possible"
-->

**Anti-pattern**: Running `pnpm -r <command>` across all packages when only a subset needs the command.

**Detection heuristic**:

```bash
grep -rn 'pnpm -r\|pnpm --recursive\|pnpm run -r' .github/workflows/
```

**Fix**: Use `pnpm --filter <package>` or Turbo's `--filter` to scope commands to relevant packages.

---

---

### OPT55 — vitest Running in Watch/Dev Mode in CI

<!-- METADATA
pattern: OPT55
impact: MEDIUM
class: static
detector: regex
match: "vitest\s+(watch\b|.*--watch)"
affected_files: ".github/workflows/*.yml,.github/workflows/*.yaml"
fix_strategy: vitest-running-in-watch-dev-mode-in-ci
title_template: "vitest Running in Watch/Dev Mode in CI"
-->

**Anti-pattern**: vitest running in watch mode in CI, which never terminates naturally.

**Detection heuristic**:

```bash
# Check for vitest without --run flag or with --watch
grep -rn 'vitest' .github/workflows/ | grep -v '\-\-run'
```

**Fix**: Always use `vitest --run` in CI, or set `CI=true` (vitest auto-detects CI and disables watch mode).

---

---

### OPT56 — Playwright Traces/Videos Uploaded Unconditionally

<!-- METADATA
pattern: OPT56
impact: MEDIUM
class: static
detector: yaml-path
affected_files: ".github/workflows/*.yml,.github/workflows/*.yaml"
fix_strategy: playwright-traces-videos-uploaded-unconditionally
title_template: "Playwright Traces/Videos Uploaded Unconditionally"
-->

**Anti-pattern**: Playwright configured to always capture traces and videos, even for passing tests. This adds storage and upload time.

**Detection heuristic**:

```bash
# Check playwright config for trace/video settings
grep -rn 'trace:\|video:' playwright.config.*
# Flag if set to 'on' instead of 'on-first-retry' or 'retain-on-failure'
```

**Fix**: Set `trace: 'on-first-retry'` and `video: 'retain-on-failure'` in Playwright config.

---

---

### OPT57 — Missing `timeout-minutes` on Known-Flaky Integration Jobs

<!-- METADATA
pattern: OPT57
impact: MEDIUM
class: data-driven
detector: actions-timeout-default-burn
affected_files: ".github/workflows/*.yml,.github/workflows/*.yaml"
fix_strategy: missing-timeout-minutes-on-known-flaky-integration-jobs
title_template: "Missing `timeout-minutes` on Known-Flaky Integration Jobs"
-->

**Anti-pattern**: Integration test jobs without `timeout-minutes`, which default to 360 minutes (6 hours). A hung test can block a runner for hours.

**Detection heuristic**:

1. Parse workflow jobs and use missing `timeout-minutes` only as a structural
   precondition. Candidate workflows come from the scanned workflow graph, not
   from existing findings only.
2. From all-status failed/timed-out workflow runs, find matching jobs whose
   sampled duration reached at least 95% of GitHub's 360 minute default timeout.
3. Require at least three successful timed samples for the same workflow job,
   compute p99, and recommend `timeout-minutes` above
   `max(p99 + 10m, p99 * 1.5, 15m)`, rounded up to minutes. Matrix jobs are
   withheld until the detector can prove the timeout is safe across variants.
4. Emit only if the recommendation remains materially below the 360 minute
   default. Credit only failed-run seconds above that p99-backed timeout,
   scaled by the matching event-scoped all-status workflow volume.

**Fix**: Add `timeout-minutes` to the measured flaky/hung job using the detector's
p99-backed recommendation, then re-run the workflow. Do not apply a blanket
15-30 minute timeout to jobs whose legitimate successful p99 is higher.

**Tier-2 render note**: OPT57 promotes only with measured timeout-default burn
evidence. It stamps `wall_clock_p50_s=0`, `sizing_basis=measured`, structured
`timeout_default_burn` samples, and a detector-specific `post_completion_waste`
certificate that `verify_report.py` re-derives from the p99/default-timeout
evidence, scale, and runner-minute math. Generic missing-timeout YAML remains
reliability guidance, not a credited saving. The successful p99 basis admits
only explicitly successful jobs (`conclusion == "success"`), runtime job
matching is exact for non-matrix candidates, and job-scoped OPT57 samples
participate in Tier-2 de-overlap against whole-run eliminators.

---

---

### OPT58 — Turbo Tasks Missing `inputs` in turbo.json

<!-- METADATA
pattern: OPT58
impact: MEDIUM
class: static
detector: repo-file-check
affected_files: ".github/workflows/*.yml,.github/workflows/*.yaml"
fix_strategy: turbo-tasks-missing-inputs-in-turbo-json
title_template: "Turbo Tasks Missing `inputs` in turbo.json"
-->

**TL;DR**: A Turbo task declares no inputs, so unrelated edits (even a README) needlessly bust its cache.

**Anti-pattern**: Turbo tasks without explicit `inputs` configured in root or package-level `turbo.json`. Without `inputs`, Turbo hashes ALL git-tracked files in the package directory, so changes to test files, READMEs, `CHANGELOG.md`, `.eslintrc`, etc. invalidate the build cache unnecessarily. This is the counterpart to OPT52 (missing `outputs`): `outputs` controls what Turbo stores, `inputs` controls what Turbo hashes to compute cache keys.

**Detection heuristic**:

```bash
# Check root turbo.json for tasks without inputs
cat /tmp/turbo.json | jq '
  .tasks // .pipeline | to_entries[] |
  select(.value.inputs == null) |
  .key
'

# Find packages without turbo.json (inheriting root defaults, no inputs override)
# Use gh API tree endpoint to list all turbo.json locations
gh api "repos/{owner}/{repo}/git/trees/HEAD?recursive=1" \
  --jq '.tree[] | select(.path | test("turbo\\.json$")) | .path'

# Count packages that LACK a turbo.json (and therefore have no inputs override)
# Compare total package count vs packages with turbo.json
```

**Fix**: Two approaches (can be combined):

1. **Exclusion-based** (simpler) — Use `$TURBO_DEFAULT$` to keep default `.gitignore`-aware behavior while excluding non-build files:

```json
{
  "extends": ["//"],
  "tasks": {
    "build": {
      "inputs": [
        "$TURBO_DEFAULT$",
        "!**/*.test.*",
        "!**/*.spec.*",
        "!**/__tests__/**",
        "!**/*.md",
        "!vitest.config.*"
      ]
    }
  }
}
```

2. **Inclusion-based** (more precise) — List only files that affect build output:

```json
{
  "extends": ["//"],
  "tasks": {
    "build": {
      "inputs": ["src/**", "tsup.config.ts", "tsconfig.json", "package.json"]
    }
  }
}
```

Note: When `inputs` is set, Turbo opts out of `.gitignore` default behavior unless `$TURBO_DEFAULT$` is included. `package.json`, `turbo.json`, and lockfiles are always considered inputs regardless of the `inputs` setting.

**Real-world example (mastra)**: PR #14432 added explicit `inputs` to 35 packages that were missing them. Without `inputs`, every README or test file change invalidated build cache across the entire monorepo.

---

---

### OPT59 — Runtime-Only Env Vars in Turbo globalEnv

<!-- METADATA
pattern: OPT59
impact: MEDIUM
class: static
detector: repo-file-check
affected_files: ".github/workflows/*.yml,.github/workflows/*.yaml"
fix_strategy: runtime-only-env-vars-in-turbo-globalenv
title_template: "Runtime-Only Env Vars in Turbo globalEnv"
-->

**Anti-pattern**: Environment variables listed in Turbo's `globalEnv` or task-level `env` that are only used at runtime (read from `process.env` at execution time), not at compile time (inlined by a bundler like webpack DefinePlugin, Vite's `import.meta.env`, or Next.js automatic `NEXT_PUBLIC_*` inlining). When these vars are in `globalEnv`, changing them (e.g., rotating an API key) invalidates the cache for every task in the repo.

**Distinct from OPT53**: OPT53 covers **unstable** env vars whose values change between runs (e.g., `GITHUB_RUN_ID`, `BUILD_NUMBER`). OPT59 covers **stable but build-irrelevant** env vars — API keys and secrets that are constant across runs but don't affect compiled output. The detection heuristic is different: OPT53 matches known-unstable variable names; OPT59 requires checking whether the variable is consumed at compile time.

**Detection heuristic**:

```bash
# Extract all env vars from globalEnv AND task-level env arrays
cat /tmp/turbo.json | jq -r '
  (.globalEnv // [])[] ,
  ((.tasks // .pipeline // {}) | to_entries[] | (.value.env // [])[] )
' 2>/dev/null | sort -u | while read var; do
  # Skip known compile-time vars
  echo "$var" | grep -qE '^(NEXT_PUBLIC_|VITE_|REACT_APP_)' && continue
  # Flag API keys and secrets as likely runtime-only
  echo "$var" | grep -qiE '(API_KEY|SECRET|TOKEN|PASSWORD|CREDENTIAL|AUTH)' && \
    echo "OPT59 candidate: $var — likely runtime-only (API key pattern)"
done
```

To confirm: search the source code for compile-time usage of the variable (webpack `DefinePlugin`, Vite `define`, Next.js automatic inlining via `NEXT_PUBLIC_*` prefix). If the variable is NOT used at compile time, it belongs in `globalPassThroughEnv` (turbo v1.10+), not `globalEnv`.

**Fix**: Remove runtime-only env vars from `globalEnv`/`env`. If the var must be available to tasks at runtime but should not affect caching, use `globalPassThroughEnv` instead. Verify the var is not inlined by a bundler before removing.

**Real-world example (mastra)**: PR #14432 removed `RAPID_API_KEY` and `ANTHROPIC_API_KEY` from turbo's `globalEnv`. Both were runtime string literals — not compile-time dependencies. Secret rotation was busting the cache for 60+ packages.

---

---

### OPT60 — Turbo CI Configuration Missing

<!-- METADATA
pattern: OPT60
impact: LOW
class: static
detector: repo-file-check
affected_files: ".github/workflows/*.yml,.github/workflows/*.yaml"
fix_strategy: turbo-ci-configuration-missing
title_template: "Turbo CI Configuration Missing"
-->

**TL;DR**: Turbo is missing a couple of CI-tuning settings that cut log noise and rendering overhead. Minor.

**Anti-pattern**: Turbo used in CI without CI-specific configuration flags, causing unnecessary overhead or noisy logs.

Missing settings include:

- `"ui": "stream"` (root-level) — Avoids interactive TUI rendering overhead in non-interactive CI environments. Note: `"stream"` is the default in recent Turbo versions — check via Context7 whether the project's Turbo version already defaults to stream before flagging.
- `"outputLogs": "new-only"` (**task-level**, not root-level) — Suppresses replayed cache-hit logs, reducing log noise and storage. Valid values: `full` (default), `hash-only`, `new-only`, `errors-only`, `none`. Applied per-task in the `tasks` block.
- `"futureFlags": { "affectedUsingTaskInputs": true }` (root-level) — Enables more precise `--affected` filtering using task-level `inputs` rather than package-level change detection.

**Detection heuristic**:

```bash
# Check root turbo.json for CI-relevant settings
if [ -f /tmp/turbo.json ]; then
  ui=$(cat /tmp/turbo.json | jq -r '.ui // "not set"')
  futureFlags=$(cat /tmp/turbo.json | jq -r '.futureFlags // "not set"')
  [ "$ui" = "not set" ] && echo "OPT60: turbo.json missing ui (check if Turbo version defaults to stream)"
  [ "$futureFlags" = "not set" ] && echo "OPT60: turbo.json missing futureFlags (affectedUsingTaskInputs)"

  # Check tasks for outputLogs (task-level setting, not root-level)
  cat /tmp/turbo.json | jq -r '
    .tasks // .pipeline | to_entries[] |
    select(.value.cache != false) |
    select(.value.outputLogs == null) |
    .key
  ' | while read task; do
    echo "OPT60: task '$task' missing outputLogs (defaults to full — consider new-only for CI)"
  done
fi
```

**Fix**: Add root-level settings and per-task `outputLogs`:

```json
{
  "ui": "stream",
  "futureFlags": { "affectedUsingTaskInputs": true },
  "tasks": {
    "build": {
      "outputLogs": "new-only"
    }
  }
}
```

Note: `ui` can also be set via `TURBO_UI=stream` env var in CI workflows. `outputLogs` can be overridden per-run with `--output-logs` CLI flag. Verify `futureFlags` compatibility with the project's turbo version via Context7.

**Real-world example (mastra)**: PR #14432 added `ui: "stream"` and `futureFlags` at root level, and `outputLogs: "new-only"` on the `build` task.

---

## Category 12: Build Caching (Language-Agnostic)

---

### OPT61 — Missing Dependency Caching

<!-- METADATA
pattern: OPT61
impact: HIGH
class: static
detector: yaml-path-absent
affected_files: ".github/workflows/*.yml,.github/workflows/*.yaml"
fix_strategy: missing-dependency-caching
title_template: "Missing Dependency Caching"
-->

**Anti-pattern**: Build jobs compile or install dependencies from scratch on every run without any caching action. This wastes minutes per job and multiplies across matrix shards.

**Savings estimation**: Build cache savings depend on cache hit rate — the fraction of runs where the cached task's inputs haven't changed. Do NOT assume 100% hit rate. Measure the actual hit rate from sampled job-duration bimodality (runs < 50% of baseline P50 = cache hits) or by parsing cache-restore / cache-miss lines from sampled job logs (`collect_runs.py --with-logs`). Cache typically only helps runs that don't change the ecosystem's source files: frontend-only PRs, dependabot PRs, re-runs, and CI config changes.

**Detection heuristic**:

1. Identify the ecosystem from workflow steps:
   - Rust: `cargo build`, `cargo test`, `cargo clippy` → check for `Swatinem/rust-cache`, `sccache`, or `actions/cache` targeting `target/` or `~/.cargo`
   - Python: `pip install`, `uv sync`, `poetry install` → check for `actions/cache` targeting pip/uv cache or venv
   - Go: `go build`, `go test` → check for `actions/setup-go` with `cache: true` or `actions/cache` targeting `GOMODCACHE`
   - Java: `mvn`, `gradle` → check for `actions/cache` targeting `~/.m2` or `~/.gradle`
   - JS: `npm ci`, `pnpm install`, `yarn install` → check for `actions/setup-node` with `cache:` or `actions/cache`
   - C++: `cmake`, `make`, `ninja` → check for `actions/cache` targeting build dir, or `ccache`/`sccache`
2. Flag if the ecosystem's build/install commands are present but no corresponding cache action exists.
3. Count total jobs affected — in sharded/matrix workflows, the waste multiplies.

```bash
# Check for any caching across all workflows
grep -rn 'actions/cache\|rust-cache\|sccache\|setup-node.*cache\|setup-go.*cache\|setup-python.*cache' /tmp/workflows/
# If empty, check what build tools are used
grep -rn 'cargo \|pip install\|uv sync\|go build\|mvn \|gradle\|cmake\|make ' /tmp/workflows/
```

**Fix**: Add the ecosystem-appropriate caching action. For multi-job workflows, use `shared-key` or equivalent to avoid N separate caches.

---

---

### OPT62 — Build Artifacts Destroyed Before Every Run

<!-- METADATA
pattern: OPT62
impact: HIGH
class: static
detector: yaml-job-correlated
affected_files: ".github/workflows/*.yml,.github/workflows/*.yaml"
fix_strategy: build-artifacts-destroyed-before-every-run
title_template: "Build Artifacts Destroyed Before Every Run"
-->

**Anti-pattern**: Explicit `rm -rf` of build directories in workflow steps or build scripts, preventing incremental builds on self-hosted runners where the workspace persists.

**Detection heuristic**:

1. Search workflow steps for clean commands:

```bash
grep -rn 'rm -rf build\|rm -rf target\|rm -rf dist\|rm -rf node_modules\|cargo clean\|make clean\|gradle clean' /tmp/workflows/
```

2. **CRITICAL**: Also read the build scripts invoked by workflow steps. A workflow step may call `./scripts/build.sh` which internally does `rm -rf build`. The detection heuristic must trace through to the actual script, not stop at the workflow YAML.
3. Flag when the job runs on `self-hosted` runners (where workspace persists between runs). On GitHub-hosted runners, the workspace is always fresh, so `rm -rf` has no effect.

**Fix**: Make the clean step conditional on cache miss, or add an `--incremental` flag to the build script. For cmake: check for `CMakeCache.txt` existence before cleaning. For cargo: incremental compilation is the default — don't `cargo clean`.

---

---

### OPT63 — Dependency Install with Cache Disabled

<!-- METADATA
pattern: OPT63
impact: MEDIUM
class: static
detector: yaml-job-correlated
affected_files: ".github/workflows/*.yml,.github/workflows/*.yaml"
fix_strategy: dependency-install-with-cache-disabled
title_template: "Dependency Install with Cache Disabled"
-->

**Anti-pattern**: Package manager invoked with explicit no-cache flags on persistent runners, defeating the benefit of workspace persistence.

**Detection heuristic**:

```bash
grep -rn '\-\-no-cache\|--no-cache-dir\|--force-reinstall\|--cache /dev/null' /tmp/workflows/
```

Flag only when the job runs on `self-hosted` runners. On GitHub-hosted runners, there's no persistent cache to defeat.

**Fix**: Remove the no-cache flag. Package managers (uv, pip, npm) handle cache invalidation correctly — the flag is unnecessarily conservative on persistent runners.

---

### OPT64 — Repeated Workflow Attempts From Same Failing Job

<!-- METADATA
pattern: OPT64
impact: LOW
class: data-driven
detector: actions-run-attempts
affected_files: ".github/workflows/*.yml,.github/workflows/*.yaml"
fix_strategy: repeated-workflow-attempts-from-same-failing-job
title_template: "Repeated Workflow Attempts From Same Failing Job"
-->

**Anti-pattern**: A workflow is repeatedly re-run (`run_attempt > 1`) because
the same job keeps failing or timing out. The earlier attempts are superseded by
the latest attempt, so their job minutes are bill waste once the retry exists.

**Detection heuristic**:

1. Sample all-status workflow runs and keep only runs whose `run_attempt > 1`.
2. Fetch each candidate run's jobs twice:
   - `GET /actions/runs/{run_id}/jobs?filter=all` to expose jobs from all attempts.
   - `GET /actions/runs/{run_id}/jobs?filter=latest` to identify the current attempt.
3. Compute the prior-attempt job delta as `filter=all - filter=latest`, preferring
   the job payload's `run_attempt` field and falling back to job-id set
   difference only when neither page is at the 100-job cap.
4. Emit a finding only when each credited prior attempt has the same unique
   dominant failed/timed-out job and that exact job name appears in the latest
   attempt. Equal top failures, mixed-cause attempts, missing latest-attempt
   matches, and generic retry volume are withheld.
5. Size runner minutes from the prior-attempt job durations only, scaled by the
   workflow's 30-day all-status run volume divided by the sampled all-status
   denominator. Wall-clock is zero because the credited attempts are superseded.

**Fix**: Stabilize or de-flake the dominant failing job, or narrow the job so it
runs only when its signal is needed. Do not hide the failure or make the workflow
green by weakening required checks; the point is to remove repeated failed
attempts, not suppress the signal.

**Tier-2 render note**: This detector can promote only with measured
`post_completion_waste` evidence: the finding must name `run_attempt > 1`,
`filter=all`, `filter=latest`, the prior-attempt delta, the latest-attempt
match, and the exact dominant failing job name that reappears in every credited
prior attempt.

---

### OPT65 — Billing Rounding Waste from Tiny Matrix Legs

<!-- METADATA
pattern: OPT65
impact: LOW
class: data-driven
detector: actions-job-rounding
affected_files: ".github/workflows/*.yml,.github/workflows/*.yaml"
fix_strategy: billing-rounding-waste-from-tiny-matrix-legs
title_template: "Billing Rounding Waste from Tiny Matrix Legs"
-->

**Anti-pattern**: A matrix fans out into many sub-minute legs. GitHub bills each
job with per-job minute round-up, so three 20-second legs bill as 3 minutes even
though their combined work is only 1 billable minute if handled inside one
off-spine runner allocation.

**Detection heuristic**:

1. Group sampled jobs by an exact trailing-parenthetical matrix base, e.g.
   `lint (a)`, `lint (b)`, `lint (c)` -> `lint`.
2. For each sampled run, compute the exact billing-rounding delta:
   `sum(ceil(job_seconds / 60)) - ceil(sum(job_seconds) / 60)`.
3. Emit only when the matrix base has at least three observed tiny legs, every
   credited occurrence is sub-minute, all credited occurrences are on the same
   known runner, and the combined credited leg p50 for each credited run is
   strictly below the workflow cluster floor. If the combined legs can reach the
   floor, withhold the finding because consolidation can serialize the merge gate
   and become wall-clock negative.
4. Scale the sampled billing-minute delta by the monthly volume for the sampled
   event scope divided by sampled successful runs. This credits only billable
   rounding waste, not runtime.

**Fix**: Do not blindly "merge matrix jobs." Only consolidate off-spine tiny
legs, or restructure shared setup / runner allocation so the merge-gating
matrix stays parallel. Avoid lowering `max-parallel` or adding an upstream
`needs:` stage for any matrix leg that can sit on the gate.

**Tier-2 render note**: OPT65 can promote only with measured rounding evidence
and a `below_cluster_floor` certificate. The finding must stamp
`wall_clock_p50_s=0`, `sizing_basis=measured`, the exact rounding formula in
`measured_signal`, structured `rounding_waste` samples that let
`verify_report.py` rederive the billable-minute amount, and affected jobs that
are the credited matrix legs rather than an ambiguous matrix base. It never
claims speedup; it credits only billing-minute round-up waste.

---

### OPT66 — SKU Arbitrage Ceiling from Expensive Hosted Runners

<!-- METADATA
pattern: OPT66
impact: LOW
class: data-driven
detector: manual
affected_files: ".github/workflows/*.yml,.github/workflows/*.yaml"
title_template: "SKU Arbitrage Ceiling from Expensive Hosted Runners"
-->

> **⚠️ REMOVED — pricing excision (2026-07-20).** OPT66 was a **dollar-only**
> pattern: it derived a published-rate *ceiling* (the $/mo you could avoid by
> moving a job to a cheaper same-core SKU) — never a credited saving. The
> 2026-07-20 pricing punt stripped every rate-derived surface from the skill, so
> the `actions-sku-arbitrage-ceiling` detector was deleted along with
> `scripts/billing.py` and `references/runner-rates.json`. Unlike the OPT49 /
> OPT51 CUTs, **no detector is retained** — the pattern has no meaning in
> a runner-minutes-only world and cannot be emitted. Per the retired-id rule (top
> of this file) the **id stays retired and is never reused**, so historical
> reports, evals, and fix-strategy strings never collide; the maintainers'
> pre-public development archive (#98 / #100) preserves the original detector
> and fix recipe for any future re-introduction.
> See CHANGELOG `[Unreleased] › Removed`.

---

### OPT68 — Broken Step Masked by `continue-on-error`

<!-- METADATA
pattern: OPT68
impact: MEDIUM
class: static
detector: yaml-path
affected_files: ".github/workflows/*.yml,.github/workflows/*.yaml"
fix_strategy: broken-step-masked-by-continue-on-error
title_template: "Broken Step Masked by `continue-on-error`"
-->

**Anti-pattern**: A step has `continue-on-error: true` (or uses an action with built-in retry/upload semantics that silently fails) AND has been failing on every run for an extended period. The job stays green, the failure never surfaces in the dashboard, and the step continues consuming runner time for zero value. Three flavors are common:

1. **`continue-on-error` covering a real bug**: the step's command exits non-zero on every run (wrong arg, missing tool, deprecated API), but `continue-on-error: true` masks it.
2. **`codecov/codecov-action@v4` without a token**: emits `Token required - not valid tokenless upload` and fails, but `fail_ci_if_error` defaults to false. Coverage uploads simply don't happen.
3. **`github/codeql-action/upload-sarif` (or `dependency-review-action`) without GHAS enabled**: API returns `Code Security must be enabled for this repository`. The action exits 1, but workflow doesn't fail (continue-on-error or upload built into analyze step).

**Detection heuristic**:

1. Read per-job logs from `gh api repos/{owner}/{repo}/actions/jobs/{job_id}/logs` (downloaded by `collect_runs.py --with-logs`) for recent successful job runs.
2. For each step in each job, scan the log section for that step looking for error tokens: `Error:`, `error:`, `FAILED`, `failed`, `Token required`, `Code Security must be enabled`, `not valid`, `Permission denied`, `404`, `Unauthorized`, `command not found`.
3. Cross-reference the step's `continue-on-error` setting in the workflow YAML. If the step both has `continue-on-error: true` AND the log shows an error token, flag it.
4. ALSO flag actions with built-in silent-failure modes when the prerequisite is missing:
   - `codecov/codecov-action@v4` step + no `secrets.CODECOV_TOKEN` reference in the workflow → likely silent failure
   - `github/codeql-action/upload-sarif` (or any action that uploads to GHAS) + repo doesn't have `security_and_analysis.advanced_security` enabled
   - `actions/dependency-review-action` on a non-GHAS repo

**Recommendation pattern**: "Step `<name>` has been silently failing in the last N runs (cite log lines). Either fix the underlying issue or remove the step. Currently consuming `<seconds>`s/run for zero value."

**Fix strategies**:

- If the step is genuinely useful: fix the root cause (add the missing token, enable GHAS, fix the broken command).
- If the step is dead weight (token won't be added, GHAS not on the roadmap): delete the step.
- Never recommend "just turn off `continue-on-error`" without addressing the failure — that just turns silent failure into loud failure on every run.

**Real-world example (blen-starter-kit deep-scan)**: `ci.yml` has 3 separate `codecov/codecov-action@v4` steps. None have `secrets.CODECOV_TOKEN` referenced anywhere. Logs show `Token required - not valid tokenless upload` on every run. Each step costs ~3-5s × 3 jobs = ~12s/run × workflow run frequency. Easy delete.

**Risk**: LOW (deletion) or MEDIUM (rewrite). Always check whether the failing step is the only thing producing a downstream artifact (e.g., a coverage badge that the README uses). Most of the time, "silently failing for months" means nobody downstream noticed — safe to remove.

---

---

### OPT69 — Dead Workflow Env Vars / Config

<!-- METADATA
pattern: OPT69
impact: LOW
class: static
detector: yaml-path
affected_files: ".github/workflows/*.yml,.github/workflows/*.yaml"
fix_strategy: dead-workflow-env-vars-config
title_template: "Dead Workflow Env Vars / Config"
-->

**Anti-pattern**: A workflow declares an environment variable, secret, or config option that is not consumed by any code in the repo. Common causes: a feature was removed but the env var wasn't cleaned up, a test was rewritten and the skip-flag was forgotten, a stale env var was inherited from a template repo.

**Detection heuristic**:

1. Enumerate every `env:` block in workflow YAML — both workflow-level and step-level.
2. For each env var name (e.g., `SKIP_DB_TESTS`, `MOCK_PAYMENTS`, `DISABLE_TELEMETRY`, `LEGACY_AUTH`), grep the repo for case-sensitive use:
   - Code: `os.environ.get('SKIP_DB_TESTS')`, `process.env.SKIP_DB_TESTS`, `std::env::var("SKIP_DB_TESTS")`
   - Config: `${SKIP_DB_TESTS}` in `.env*`, `docker-compose*.yml`, `Makefile`, shell scripts
   - Tests: `pytest.mark.skipif(os.getenv('SKIP_DB_TESTS'))`
3. If grep returns zero hits AND the var is set to a non-secret literal (so it's not a deploy-time config), flag as dead.
4. Skip secrets passed via `${{ secrets.X }}` — those may be consumed by external services and aren't grep-able locally.

**Fix**: Delete the env var. Add a follow-up audit suggestion if the deletion uncovers further dead config (e.g., the workflow step that sets the var is now itself dead).

**Saving math**: Per-deletion saving is small (~0ms), but the cumulative readability benefit + reduced confusion for future contributors makes it worth flagging at LOW severity. Aggregate across all dead vars in the report (e.g., "5 dead env vars across 3 workflows — delete in one PR").

**Real-world example (blen-starter-kit deep-scan)**: An env var like `SKIP_DB_TESTS: "true"` was found in `ci.yml` but no Python or shell code in `apps/api/` reads it — the test runner uses a different mechanism. Safe to delete.

**Risk**: LOW. The grep should be case-sensitive and include both `${VAR}` and `$VAR` syntax variants. Verify the var isn't used as a deploy-time secret being passed to a downstream system (Vercel env, CloudWatch dashboard variable, etc.) — those uses won't grep locally.

---

---

## Category 14: Structural / Critical-Path Levers

These patterns are a **different class** from everything above. The catalog
patterns OPT1–OPT69 are *hygiene*: each is a named, locally-checkable defect with
a mechanical, low-risk fix, detected by matching workflow YAML against the
catalog. On real repos almost every hygiene hit moves **~0 developer
wall-clock** — the true bottleneck is usually a check that is *working as
intended* but is simply the slowest thing on the critical path, with no catalog
match.

Structural patterns attack exactly that gap. They are **not** detected by
declarative YAML matching; they are **routed** from the measured critical path
(`collect_runs.py` decomposes the long-pole job, cross-references required
checks, and finds shared/redundant work), and the final lever framing is written
by a per-candidate reasoning step. They carry an OPT-id so the report and tests
stay catalog-keyed, but the catalog is no longer the only thing that can produce
a finding.

**The cost of the higher leverage is higher risk.** A hygiene fix at worst does
nothing. A structural change can **degrade correctness** — drop coverage, turn a
real failure into a false green, diverge from the shipped artifact. So every
structural pattern declares a **risk** rating, a **mandatory guardrail**, and a
**conservative rollout** in its METADATA, and the report ranks on savings AND
risk as **separate axes**: a high-savings/high-risk candidate can sit *below* a
boring safe one. A structural finding is NEVER presented as a safe quick win.

`risk` values: `LOW` (mechanical, reversible, no correctness exposure) <
`MEDIUM` (changes a signal/trigger; reversible but can drop a check developers
rely on) < `HIGH` (can silently change what is built/tested/shipped —
correctness exposure). The report's ranking demotes by risk so the rule is
visible and a future edit can't quietly re-promote a HIGH-risk lever into the
quick-wins list.

### OPT70 — Scope the Build/Test to Only What Changed

<!-- METADATA
pattern: OPT70
impact: HIGH
class: structural
detector: critical-path
risk: HIGH
affected_files: ".github/workflows/*.yml,.github/workflows/*.yaml"
fix_strategy: scope-build-test-to-changed
title_template: "Scope the long-pole build/test to changed targets"
-->

**TL;DR**: Your slowest job rebuilds and re-tests the whole project on every PR, even when the change touched only a small part — it could run just the parts that changed instead.

**Anti-pattern**: A build or test job that always processes every package / module / test file regardless of the PR diff, in a repo whose tooling supports change-scoped execution (a Turborepo/Nx/Bazel/Gradle workspace, or a test runner with a `--changed`/`--onlyChanged`/affected mode). The redundant-work ratio (build+install time ÷ the work actually exercised by the diff) is high — see OPT72.

**Detection heuristic** (routed, not YAML-matched):

1. The job is on the measured critical path (top of the PR check-runs list).
2. Its dominant step is a `build` or `test` category step (from the step-duration decomposition), and the redundant-work ratio is not high enough to route to OPT72.

The deterministic router stops there — it routes on **dominant category alone**, it does **not** inspect the repo's build config. Confirming a scoping mechanism exists is a per-candidate **reasoning step**, not part of detection:

3. *(reasoning step, not auto-detected)* Before recommending the scope, verify the repo's tooling supports change-scoped execution (`turbo.json` / `nx.json` / `WORKSPACE` / `settings.gradle` present, or a `--changed`/`--onlyChanged`-capable test runner). If no such mechanism exists, this candidate is not actionable as written.

**Why this is the most dangerous lever in the catalog.** Scoping trades correctness headroom for speed. Concretely it can:

- **Miss an undeclared / transitive dependency.** If the dependency graph the scoper reads (`turbo` task graph, `nx` project graph, `package.json` deps) is incomplete — a runtime `import`, a generated file, a path alias the graph doesn't know about — a change can affect a target the scoper marks "unaffected", and the gate passes without testing it.
- **Silently drop coverage.** A "speedup" that runs fewer tests is indistinguishable, in green-CI terms, from a real speedup — until a bug ships. The coverage regression is invisible in the metric you're optimizing.
- **Turn a build/import error into a false pass.** In any exit-code-driven gate, if the scoper resolves "nothing affected" it exits 0 — so a broken import or a build error in an "unaffected" area reads as a green check.
- **Diverge from the shipped artifact.** Testing unbuilt source while production ships `dist/` (subpath exports, build-time defines, custom transforms, `tsconfig` path remapping) means the gate validates something the user never runs.

**Fix recipe**: Adopt change-scoped execution for the dominant step, with the mandatory guardrail below. E.g. `turbo run build test --filter='...[origin/${{ github.base_ref }}]'`, `nx affected -t build test --base=origin/${{ github.base_ref }}`, or `vitest --changed origin/${{ github.base_ref }}`. The base must be the **merge base**, not `HEAD~1` — and it must EXIST in the clone: the default `actions/checkout` is a shallow, single-branch clone where `origin/${{ github.base_ref }}` does not resolve. Add `fetch-depth: 0` (or a targeted `git fetch origin ${{ github.base_ref }}` step) to the scoped job's checkout (an explicit exception to OPT28's shallow-checkout guidance). turbo/nx fail loudly on an unresolvable base, but `vitest --changed` against a missing ref can resolve to "no changed files" and exit 0 green — exactly the false pass Mandatory Guardrail #1 exists to prevent.

**Mandatory guardrail (this pattern is invalid without it)**:

1. **Full fallback on resolution error.** If the scoper errors, can't resolve the graph, or returns an empty set on a non-trivial diff, run the **full** build/suite. Never let "couldn't figure out what changed" become "ran nothing → green".
2. **Distinguish a build error from a test failure.** A non-zero exit from graph resolution / compilation must fail the gate, separately from "the scoped tests ran and failed". Don't collapse both into one exit code the scoper can zero out.
3. **Output-diff before adoption.** Before cutting over, run scoped and full in parallel and diff the artifact/coverage set; adopt only when they match on the dimensions that matter (built outputs, covered files).

**Conservative rollout (REQUIRED)**: Run the scoped job **in parallel with** the existing full job for **N runs** (≥1–2 weeks of PR traffic), comparing pass/fail and coverage on every PR. Cut over only after the scoped job has matched the full job across the diff distribution — including at least one PR that touches a shared/base package. Keep the full job on the merge queue / `main` even after cutover, so the trunk is always validated end-to-end.

**Sizing**: population-weighted Δ wall-clock = (dominant-step p50 − the scoped-run floor) × the share of PRs whose diff is narrow enough to scope, capped at the cross-workflow critical-path floor (the next-slowest check still gates the PR). Never size this off the single best-case PR.

**Risk**: **HIGH** — correctness exposure. NEVER list as a quick win.

---

---

### OPT71 — Expensive Non-Required Check on the Critical Path

<!-- METADATA
pattern: OPT71
impact: HIGH
class: structural
detector: critical-path
risk: MEDIUM
affected_files: ".github/workflows/*.yml,.github/workflows/*.yaml"
fix_strategy: expensive-non-required-check
title_template: "Expensive non-required critical-path check (de-scope, gate, or speed up)"
-->

**TL;DR**: One of the slowest checks holding up your PRs isn't even required to merge. If it's just advisory (a comment or preview), stop running it on every PR; if it's a real test, speed it up instead — don't turn it off.

**Anti-pattern (the de-scope case)**: A workflow that runs on every `pull_request` activity type (including `synchronize` — every push) and sits at/near the top of the measured critical path, is absent from the required-status-check list, AND whose output is genuinely advisory (a size-diff comment, a preview deploy, a non-blocking lint annotation) - so the developer's wait on it is pure friction. **This case alone is safe to de-scope.** A non-required check that actually runs tests or validates a build is NOT this anti-pattern: it is load-bearing developer signal even when branch protection doesn't list it, and the lever is to make it faster, not to stop running it.

**Detection heuristic** (routed):

1. The check is on the measured critical path (high p50 in the PR check-runs).
2. Cross-reference the repo's required checks: `gh api repos/{owner}/{repo}/rulesets` and `gh api repos/{owner}/{repo}/branches/{branch}/protection/required_status_checks`. The check name is **not** in that set. (When the required-status data is missing OR only partially readable — common when auditing a repo you don't own, branch protection returns 404 — required-status is **unknown**; the router does NOT assert "non-required" and does NOT emit OPT71. It surfaces "required status unknown" rather than recommend de-scoping a check that might gate the merge.)

The router stops at (1)-(2). Consumer enumeration is a mandatory **reasoning-step precondition**, not part of detection:

3. *(reasoning step, not auto-detected)* **Enumerate the result's consumers** before recommending anything: does anything `needs:` this job, does a later step read its output, is there a downstream comment/label/deploy? A check that *looks* advisory but feeds a required aggregator is required-in-effect.

**Fix recipe**: Once consumers are enumerated and the check is genuinely advisory, the options in increasing aggressiveness are: (a) **narrow the trigger** — drop `synchronize` so it runs once per PR open/ready, not on every push; (b) **gate it** behind a `paths:` filter or a label so it only runs when relevant; (c) **make it advisory-async** — move it to run post-merge / on a schedule and post its result without blocking the PR. Pick the least aggressive option that removes the wait.

**Risk**: **MEDIUM** — reversible, but narrowing a trigger can drop a signal developers actually use (a preview URL they click, a size comment they read). Confirm with the consumers enumeration; if anyone relies on it per-push, prefer making it async over removing it.

**Guardrail**: Never de-scope a check whose required-status is **unknown** (branch-protection 404). Keep the advisory output reachable (async comment) rather than deleting it outright.

**Rollout**: Change the trigger on a branch, watch one week of PRs, confirm no one re-requests the dropped signal, then keep.

**Reporting contract — the Δ wall-clock is a DE-SCOPE CEILING.** The sized
`wall_clock_p50_s` is the wait removed *only if the check is dropped from the PR
path* — the maximum, realized only in the (safe) advisory case. Because the
detector can't classify advisory-vs-real, the report treats this saving as a
**ceiling by default**: the finding still RANKS by it (it IS the bottleneck, so
it leads the report), but the Δ wall-clock renders as a ceiling and is **NOT
counted in the saving total**. The fix step sets **`descope_recommended: true`**
on the finding ONLY when it confirms the check is genuinely advisory and
recommends de-scoping — that promotes the ceiling to a credited win. For a real
gate ("speed it up instead"), leave it unset: the realized saving is the
speed-up amount, which OPT72 / the cache family size separately, not this
de-scope ceiling.

---

---

### OPT72 — Redundant-Work Ratio (build/install ≫ payload)

<!-- METADATA
pattern: OPT72
impact: MEDIUM
class: structural
detector: critical-path
risk: MEDIUM
affected_files: ".github/workflows/*.yml,.github/workflows/*.yaml"
fix_strategy: redundant-work-ratio
title_template: "Long pole spends most of its time on setup, not the actual work"
-->

**TL;DR**: Your slowest job spends most of its time building and installing, and only a little on the tests it actually exists to run.

**Anti-pattern**: A critical-path job whose `dominant_setup_or_build_step_time ÷ payload_step_time` ratio is high — the install/build steps dwarf the test/lint/scan step that is the job's actual purpose. Common shape: a monorepo job that `pnpm install && turbo run build` the whole tree, then runs one package's tests.

**Detection heuristic** (routed): from the step-duration decomposition of a critical-path job, compute `(sum of checkout+install+build+setup step p50) ÷ (sum of test+scan+package step p50)`. The router flags when this ratio exceeds 2× and the dominant step is itself a build or test step. The redundant build/install is the candidate to scope (OPT70) or warm-cache.

**Fix recipe**: Two paths, different risk. (a) **Scope the setup** to what the payload needs — only build the packages the tested package depends on (`turbo run build --filter=<pkg>...`). This inherits OPT70's HIGH-risk correctness guardrails (a missed transitive dep means testing against a stale build). (b) **Warm-cache the setup** — make the redundant build a cache restore (dependency cache, build cache keyed on inputs) so each run pays seconds, not minutes. Path (b) is the **safe default**: it removes the *cost* of the redundancy without removing the redundancy itself, so correctness is unchanged.

**Risk**: **MEDIUM** as written (warm-cache path is LOW; scope-the-setup path escalates to OPT70's HIGH). The report should prefer the cache path unless the user explicitly accepts OPT70's correctness rollout.

**Guardrail**: If recommending the scope path, carry OPT70's full guardrail (full-build fallback, output diff). If recommending the cache path, verify on ephemeral runners that the cache actually restores warm (an `actions/cache` hit that the tool re-validates from scratch is not a saving — see OPT8).

**Rollout**: Cache path — ship and measure warm-vs-cold step time over 5 PRs. Scope path — OPT70's parallel-run rollout.

---

---

### OPT73 — Shared Sub-Step Across Critical-Path Jobs (cluster-floor lever)

<!-- METADATA
pattern: OPT73
impact: HIGH
class: structural
detector: critical-path
risk: LOW
affected_files: ".github/workflows/*.yml,.github/workflows/*.yaml"
fix_strategy: shared-substep-floor
title_template: "A shared step recurs across the whole cluster — fix it once, lower the floor"
-->

**TL;DR**: The same named step re-runs in several jobs that execute at the same time (the evidence names the step and its cost in each job) — speeding up that one step lowers all of those jobs at once.

**Anti-pattern**: A normalized step (same name/category — e.g. `pnpm install`, `setup toolchain`, `build base image`, `restore deps`) that appears in ≥2 of the jobs in the critical-path cluster, each paying its full cost independently. Cutting one job's copy leaves the others gating the run.

**Detection heuristic** (routed): across the cluster jobs (the long pole plus every job within striking distance of it — the floor band), normalize step names (strip matrix args, lowercase, category-classify) and find a step category that recurs in ≥2 cluster jobs with material p50 in each. That step is a floor-lowering candidate.

**Fix recipe**: Make the shared step cheap **in every job that runs it** — a warm dependency/build cache keyed so all cluster jobs hit it, a prebuilt base image they all pull, or a `setup-*` `cache:` shared across jobs. The saving is credited across **every** cluster job containing the step (the floor drops by the per-job saving), not just the long pole — that's what makes it beat the floor. (Avoid the serial-gate trap: do NOT consolidate the shared step into one upstream job the others `needs:` — that adds wall-clock behind a serial gate, see OPT14/§4. Lower the floor by making each parallel copy cheap, not by serializing.)

**Risk**: **LOW** — caching a shared setup step is mechanical and reversible, and changes no test/build semantics. (Escalates only if the "shared step" is itself a build whose caching could serve stale outputs — then carry a cache-key-correctness check.)

**Guardrail**: Verify the cache key captures the step's real inputs (lockfile, toolchain version, source the build reads) so a warm hit never serves stale artifacts. On ephemeral runners, confirm the cache restores warm (OPT8).

**Rollout**: Ship the shared cache, measure the floor (second-tallest job p50) before/after across 5 PRs — the wall-clock win shows only when the whole cluster comes down.

---

---

### OPT74 — Trust-Boundary-Forced Cold Work (producer/consumer split)

<!-- METADATA
pattern: OPT74
impact: MEDIUM
class: structural
detector: critical-path
risk: MEDIUM
affected_files: ".github/workflows/*.yml,.github/workflows/*.yaml"
fix_strategy: trust-boundary-cache-split
title_template: "Untrusted fork-PR job redoes cold work it can't cache securely"
-->

**TL;DR**: Jobs triggered by PRs from forks can't use the shared cache, so they redo the full install/build from scratch every time.

**Anti-pattern**: A fork-PR-triggered job on the critical path whose setup can't be warm because the trust boundary denies it: `pull_request` from a fork runs with a read-only `GITHUB_TOKEN` and no repo secrets, so it can't restore a cache the trusted side wrote (or the cache scope isolates fork branches), and every fork PR pays cold install/build. Trust-boundary-forced cold work is structural, not a missing-cache hygiene bug.

**Detection heuristic** (routed): the job runs on `pull_request` (fork-reachable, not `pull_request_target`), has a high setup/build floor, and references no secrets / uses a cache the fork can't populate. Its cold setup is the addressable cost, but the naive fix (let the fork write the shared cache) is a **cache-poisoning** vector.

**Fix recipe**: **Trusted-producer + read-only-consumer split.** A trusted workflow (`push` to the base branch, or `schedule`) builds the shared dependencies/base image and publishes them **keyed by a ref the consumer can compute** (base-branch SHA, lockfile hash). The untrusted fork job **restores read-only** with a **local fallback**: on a cache miss it does the cold work rather than failing, so a fork PR is never blocked on the producer. The producer's output must be **content-addressed and validated** (the consumer recomputes/verifies the key from its own inputs) so a poisoned cache entry can't be served to the trusted side.

**Encode the cache-poisoning guardrails generally**: never let an untrusted job **write** a cache/artifact the trusted side reads; key shared artifacts by an input the consumer independently derives (not an attacker-controlled branch name); validate restored content before use; and keep the fallback path (cold build) always available so availability doesn't depend on the producer.

**Risk**: **MEDIUM** — security-sensitive. A careless split (untrusted job writing the shared cache, or trusted job consuming fork-produced artifacts) introduces a supply-chain hole worse than the slow CI it fixes.

**Guardrail**: The poisoning guardrails above are mandatory, not optional. If the split can't be made read-only-for-untrusted, do NOT recommend it — keep the cold work.

**Rollout**: Stand up the trusted producer first, confirm the consumer restores warm on same-base PRs and falls back cold on a forced miss, then measure.

---

---

### OPT75 — Long Pole: Optimize or Relocate the Dominant Step

<!-- METADATA
pattern: OPT75
impact: HIGH
class: structural
detector: critical-path
risk: MEDIUM
affected_files: ".github/workflows/*.yml,.github/workflows/*.yaml"
fix_strategy: decompose-inherent-cost-pole
title_template: "The long pole's time is one addressable step — speed it up or move it off the PR path"
-->

**TL;DR**: The slowest check holding up your PRs has no off-the-shelf hygiene fix, but almost all its time goes to a single step you can target directly — shard/parallelize a test, cache an install, scope a scan, or (for a fileless check you can't edit) move it off the PR trigger. The fix is whatever the measured dominant step calls for; this lever just refuses to dead-end.

**Anti-pattern**: The report's old dead-end — "the gating check has no matching optimization pattern, so cutting wall-clock here is outside this catalog." That stops exactly where the leverage is. A long-pole job is almost never uniformly slow; it's checkout + install + build + the actual test/scan, and usually **one** of those is the bulk.

**Detection heuristic** (routed): decompose the long-pole job into steps (`_step_durations`), classify each by category (checkout / install / build / test / scan / package / setup), and find the **dominant** step and its share of the job. OPT75 is the NEUTRAL catch-all — it carries no presupposed remedy (never "decompose/split" or "scope/drop your tests"), so the fix step is free to land on the right lever (parallelize, cache, scope, relocate) from the measured behaviour. A **build**-dominant pole routes to OPT72 (cache) or OPT70 (scope) instead; everything else — **test** (shard/parallelize, NOT the HIGH-risk "drop tests"), install, setup, scan, format, package, and fileless checks — routes here.

**Fix recipe** (reasoning step — picks the concrete remedy for *this* repo's tooling; these are NOT emitted as distinct findings by the detector): route the dominant category to a concrete lever —

- dominant = **install / checkout / setup** → a caching / shallow-fetch / pin lever (often LOW risk; see OPT73 if shared across the cluster).
- dominant = **build** → warm the build cache, or scope the build (OPT70/OPT72).
- dominant = **test** → shard it (OPT24), or scope it to changed targets (OPT70).
- dominant = **scan / package** → cache the scan DB / incremental scan, or move it advisory-async if non-required (OPT71).

Report the dominant step, its category, and its share so the reader sees *why* the inherent-cost pole is actually addressable.

**Risk**: **MEDIUM** by default — the dominant-step remedy ranges from LOW (cache an install) to HIGH (scope a test/build, inheriting OPT70). The emitted candidate carries the risk of whichever specific lever its dominant category routes to.

**Guardrail**: Carry the guardrail of the routed lever (e.g. OPT70's full-suite fallback if the dominant step is a test being scoped). Never present the decomposition as free.

**Rollout**: The routed lever's rollout. Re-measure the pole's p50 after the dominant step is attacked; the next-largest step (or the cluster floor) becomes the new target.

---

---
