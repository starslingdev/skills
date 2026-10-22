# ci-speedup pattern index

One line per pattern: `OPT<n> — title (impact, class) — what it is about`. Find the
pattern here, then read `opt<n>-*.md` in this directory in full. Never open
`../optimization-patterns.md`: it is the generated public copy of these files.
This order is also the compiled file's order; contributors see `README.md`.

## Category 1 — Caching
- OPT1 — Unnecessary Tool Install (MEDIUM, static) — e.g. Playwright browsers where unused
- OPT2 — Uncached Large Downloads (MEDIUM, static) — browser or toolchain downloads uncached
- OPT3 — Read-Only Turbo Cache (never populated) (MEDIUM, static) — TURBO_CACHE remote:ro never written
- OPT4 — Docker Layer Cache Missing (MEDIUM, static) — docker build without cache-from
- OPT5 — pnpm Store Not Cached (or Wrong Setup Order) (MEDIUM, static) — pnpm store uncached or setup order
- OPT6 — Cache Key Entropy Too High or Unstable (MEDIUM, static) — run_id, sha or date in cache key
- OPT7 — pnpm Version Drift Across Workflows (LOW, static) — different pnpm versions per workflow
- OPT8 — Cache Key Granularity Mismatch (MEDIUM, static) — coarse hashFiles key over app caches
- OPT9 — Tool-Specific Cache Flag Not Enabled (HIGH, static) — ESLint, Jest, tsc cache flags off
- OPT79 — A Cache That Costs More Than It Saves (MEDIUM, data-driven) — cache restore slower than the install
## Category 2 — Redundancy
- OPT11 — Redundant Environment Variables (LOW, static) — same env var at two levels
- OPT12 — Duplicated Setup Across Jobs (MEDIUM, static) — copy-pasted setup steps
- OPT13 — Build Step in Jobs That Don't Need It (MEDIUM, static) — lint/typecheck job runs a build
- OPT14 — Repeated Checkout/Setup Without Artifact Handoff (MEDIUM, static) — every job rebuilds; no artifact handoff
- OPT15 — Cross-Workflow Build Redundancy (HIGH, static) — same build in several PR workflows
- OPT16 — Within-Job Duplicate Commands (LOW, static) — same command twice in one job
- OPT77 — Repeated Fixed Setup Across Independent Small Jobs (MEDIUM, data-driven) — small checks each pay full setup
## Category 3 — Docker
- OPT17 — Sleep-Based Container Readiness (MEDIUM, static) — sleep instead of healthchecks
- OPT18 — All Containers Started for Single-Service Tests (MEDIUM, static) — compose up starts every service
- OPT19 — Test Source Sleep Dominance (includes Playwright `waitForTimeout`) (HIGH, static) — fixed sleeps / waitForTimeout in tests
- OPT20 — Unpinned Docker Image Tags (LOW, static) — docker images on :latest
## Category 4 — Parallelization
- OPT21 — Unnecessary `needs:` Dependencies (MEDIUM, static) — needs: on a job it does not use
- OPT22 — Sequential Workflows via `workflow_run` (MEDIUM, static) — workflow_run chains instead of jobs
- OPT23 — Single-Threaded Matrix (`max-parallel: 1`) (MEDIUM, static) — matrix max-parallel: 1
- OPT24 — Long Test Job Without Sharding (HIGH, static) — long test job, no shards
- OPT25 — Shard Imbalance (MEDIUM, data-driven) — one shard far slower than the rest
- OPT88 — Test Concurrency Is a Guess, Not a Measurement (MEDIUM, data-driven) — unmeasured test worker count
- OPT89 — The Slowest Platform Compiles Its Own Tests (HIGH, data-driven) — cross-compile once, test on each platform
## Category 5 — Actions and Checkout
- OPT26 — Outdated Action Major Versions (LOW, static) — old actions/* major versions
- OPT27 — Duplicate `setup-node` Calls in Same Job (LOW, static) — setup-node twice in a job
- OPT28 — Full Git History Checkout (MEDIUM, static) — fetch-depth: 0 where not needed
- OPT76 — Submodule / Git LFS Checkout Payload (MEDIUM, static) — unused submodules or LFS objects
- OPT80 — Checkout Stalls on the Tail (MEDIUM, data-driven) — git fetch stalls on slow runs
## Category 6 — Conditional Execution
- OPT29 — Merge Queue Skip at Step Level Only (MEDIUM, static) — merge_group skip at step level
- OPT30 — Matrix Jobs Without Job-Level Conditional (MEDIUM, static) — matrix runners provisioned for nothing
- OPT31 — Conditional Step With Unconditional Setup (MEDIUM, static) — setup runs when its consumer is skipped
## Category 7 — Trigger and Scope
- OPT32 — Missing `paths`/`paths-ignore` on Expensive Workflows (HIGH, static) — no path filters on expensive workflows
- OPT33 — No Draft PR Gating on Expensive Jobs (MEDIUM, static) — expensive jobs run on draft PRs
- OPT34 — No Changed-Package Filtering (MEDIUM, static) — monorepo runs every package
- OPT35 — Missing `fail-fast` on Non-Diagnostic Matrix Dimensions (LOW, static) — fail-fast: false on shard matrices
- OPT36 — Cron Schedule Too Frequent (LOW, static) — cron runs too often
- OPT37 — Workflow Trigger Dependency Gap (Cache Race) (HIGH, static) — cache writer and reader race
- OPT38 — Non-Content Trigger Event Types (PR `edited`, etc.) (MEDIUM, static) — PR edited/labeled events re-run CI
- OPT39 — Multi-Language Matrix Without Path Filter (MEDIUM, static) — scanner matrix without path filters
- OPT40 — Monorepo Job Runs Regardless of Affected App (MEDIUM, static) — app job runs when another app changed
- OPT90 — Every Pull Request Pays for Coverage That Could Run After Merge (HIGH, data-driven) — full matrix on PRs, subset would do
## Category 8 — Release Workflow
- OPT41 — `TURBO_FORCE: true` Disabling All Caching (HIGH, static) — TURBO_FORCE in release
- OPT42 — `TURBO_CACHE: remote:rw` in Release (HIGH, static) — TURBO_CACHE remote:rw in release
## Category 9 — Queue Times and Concurrency
- OPT43 — Excessive Queue Time (MEDIUM, data-driven) — jobs wait long for a runner
- OPT44 — Concurrency Group Too Restrictive (MEDIUM, static) — concurrency group too narrow
- OPT45 — Missing Concurrency Groups (HIGH, static) — no concurrency group
- OPT46 — Superseded Runs Not Cancelled (Missing Concurrency or `cancel-in-progress: false`) (MEDIUM, data-driven) — superseded runs keep running
- OPT47 — Redundant push + pull_request Double-Trigger (MEDIUM, data-driven) — push and pull_request both fire
- OPT48 — High Job-Level Failure Rate (>15% over 30d) (MEDIUM, data-driven) — job fails over 15% of runs
## Category 10 — Timing Anomalies
- OPT49 — Slow Setup Step (MEDIUM, data-driven) — cut; id retired
- OPT50 — Post Steps Taking Too Long (MEDIUM, data-driven) — slow Post steps (cache save)
- OPT51 — Install-to-Test Ratio >50% (MEDIUM, data-driven) — cut; id retired
- OPT81 — The Same Job Is Measurably Faster on Another Runner (MEDIUM, data-driven) — job faster on another runner class
## Category 11 — Stack-Specific
- OPT52 — Turbo Tasks Missing `outputs` in turbo.json (MEDIUM, static) — turbo task without outputs
- OPT53 — Unstable Env Vars Invalidating Turbo Cache (MEDIUM, static) — changing env vars in turbo hash
- OPT54 — Full-Repo `pnpm -r` Where Package Filters Are Possible (MEDIUM, static) — pnpm -r where a filter would do
- OPT55 — vitest Running in Watch/Dev Mode in CI (MEDIUM, static) — vitest watch mode in CI
- OPT56 — Playwright Traces/Videos Uploaded Unconditionally (MEDIUM, static) — traces/videos on passing tests
- OPT57 — Missing `timeout-minutes` on Known-Flaky Integration Jobs (MEDIUM, data-driven) — no timeout on flaky integration jobs
- OPT58 — Turbo Tasks Missing `inputs` in turbo.json (MEDIUM, static) — turbo task without inputs
- OPT59 — Runtime-Only Env Vars in Turbo globalEnv (MEDIUM, static) — runtime-only vars in globalEnv
- OPT60 — Turbo CI Configuration Missing (LOW, static) — turbo CI settings missing
- OPT82 — Lint Builds the Whole Type Graph (MEDIUM, data-driven) — type-aware ESLint on a slow lint job
## Category 12 — Build Caching (language-agnostic)
- OPT61 — Missing Dependency Caching (HIGH, static) — compiled-language deps uncached
- OPT62 — Build Artifacts Destroyed Before Every Run (HIGH, static) — rm -rf of build dirs every run
- OPT63 — Dependency Install with Cache Disabled (MEDIUM, static) — --no-cache installs on persistent runners
- OPT86 — CI Compiles a Dependency That Ships Prebuilt (HIGH, data-driven) — source builds of wheels or native add-ons
- OPT91 — The Compile Is the Long Pole and Nobody Profiled It (HIGH, data-driven) — slow compile, profile it first
## Category 13 — Hidden Failures and Dead Config
- OPT64 — Repeated Workflow Attempts From Same Failing Job (LOW, data-driven) — re-runs of the same failing job
- OPT65 — Billing Rounding Waste from Tiny Matrix Legs (LOW, data-driven) — many sub-minute matrix legs
- OPT66 — SKU Arbitrage Ceiling from Expensive Hosted Runners (LOW, data-driven) — removed; id retired
- OPT68 — Broken Step Masked by `continue-on-error` (MEDIUM, static) — continue-on-error hides a broken step
- OPT69 — Dead Workflow Env Vars / Config (LOW, static) — env vars or config nothing reads
## Category 14 — Structural / Critical-Path Levers
- OPT70 — Scope the Build/Test to Only What Changed (HIGH, structural) — test only what changed
- OPT71 — Expensive Non-Required Check on the Critical Path (HIGH, structural) — non-required check on the critical path
- OPT72 — Redundant-Work Ratio (build/install ≫ payload) (MEDIUM, structural) — build/install dwarfs the tests
- OPT73 — Shared Sub-Step Across Critical-Path Jobs (cluster-floor lever) (HIGH, structural) — same step re-runs across parallel jobs
- OPT74 — Trust-Boundary-Forced Cold Work (producer/consumer split) (MEDIUM, structural) — fork PRs cannot warm the cache
- OPT75 — Long Pole: Optimize or Relocate the Dominant Step (HIGH, structural) — one step dominates the long pole
- OPT78 — Per-File Test Isolation Rebuilding Shared Module State (HIGH, structural) — per-file isolation re-imports the app
- OPT83 — Independent Steps on the Long Pole Run One After Another (MEDIUM, structural) — independent steps run serially
- OPT84 — A Slow Step on the Long Pole Runs a Tool That Has a Native Drop-in (MEDIUM, structural) — Prettier/ESLint/tsc with native drop-ins
- OPT85 — Tests Run in Many Processes Instead of One (HIGH, structural) — a process per test case
- OPT87 — Tests Wait on a Slow Dependency They Do Not Verify (HIGH, structural) — tests call a slow real dependency
