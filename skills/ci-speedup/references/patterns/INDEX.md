# ci-speedup pattern index

One line per pattern: id and title (linked to its file), impact, finding class, one sentence.
Find the pattern here, then read `opt<N>-<slug>.md` in full. Do not open
`../optimization-patterns.md`: it is the generated public copy of these files.

This list is also the build order of that compiled file (it is not numeric:
a pattern sits beside the ones it is closest to). Contributors: see `README.md`.

## Category 1 — Caching (`OPT1`–)

tool installs, build/test caches, dynamic cache keys, and the one pattern pointed the other way — a cache measured to cost more than it saves (`OPT79`).

- [OPT1 — Unnecessary Tool Install](opt1-unnecessary-tool-install.md) · MEDIUM · static · Installing tools (e.g., Playwright browsers) in jobs that don't use them.
- [OPT2 — Uncached Large Downloads](opt2-uncached-large-downloads.md) · MEDIUM · static · Big downloads (like Playwright browsers) are re-fetched over the network every run because nothing caches them — cache them once and reuse.
- [OPT3 — Read-Only Turbo Cache (never populated)](opt3-turbo-cache-misconfiguration.md) · MEDIUM · static · A job sets `TURBO_CACHE: remote:ro` — it READS the remote cache but never writes it.
- [OPT4 — Docker Layer Cache Missing](opt4-docker-layer-cache-missing.md) · MEDIUM · static · Building Docker images in CI without layer caching, causing full rebuilds every run.
- [OPT5 — pnpm Store Not Cached (or Wrong Setup Order)](opt5-pnpm-store-not-cached-or-wrong-setup-order.md) · MEDIUM · static · The pnpm package store isn't cached between runs, so every run re-downloads all dependencies from scratch.
- [OPT6 — Cache Key Entropy Too High or Unstable](opt6-cache-key-entropy-too-high-or-unstable.md) · MEDIUM · static · Cache keys that include timestamps, random values, or non-deterministic content, causing cache misses on every run.
- [OPT7 — pnpm Version Drift Across Workflows](opt7-pnpm-version-drift-across-workflows.md) · LOW · static · Different workflows or jobs specifying different pnpm versions, reducing cache compatibility between them.
- [OPT8 — Cache Key Granularity Mismatch](opt8-cache-key-granularity-mismatch.md) · MEDIUM · static · GitHub Actions cache uses a coarse `hashFiles()` key (covering all input files) while the cached directory contains application-level caching with fine-grained per-item keys.
- [OPT9 — Tool-Specific Cache Flag Not Enabled](opt9-tool-specific-cache-flag-not-enabled.md) · HIGH · static · A tool with a built-in cache (ESLint, Jest, tsc, etc.) runs without its cache flag, so it reprocesses every file each run.
- [OPT79 — A Cache That Costs More Than It Saves](opt79-cache-costs-more-than-it-saves.md) · MEDIUM · data-driven · A job restores a dependency cache and then installs dependencies, and restoring the cache takes longer than the install it was meant to shorten.

## Category 2 — Redundancy

duplicate env, repeated setup sequences, redundant build steps, repeated fixed setup across independent small jobs (`OPT77`).

- [OPT11 — Redundant Environment Variables](opt11-redundant-environment-variables.md) · LOW · static · Setting the same env var at both the workflow/job level AND the step level, creating noise and maintenance burden.
- [OPT12 — Duplicated Setup Across Jobs](opt12-duplicated-setup-across-jobs.md) · MEDIUM · static · Several jobs copy-paste the same setup steps.
- [OPT13 — Build Step in Jobs That Don't Need It](opt13-build-step-in-jobs-that-dont-need-it.md) · MEDIUM · static · A job runs a full build even though it only lints or type-checks and doesn't need the built output.
- [OPT14 — Repeated Checkout/Setup Without Artifact Handoff](opt14-repeated-checkout-setup-without-artifact-handoff-and-slow-to.md) · MEDIUM · static · Multiple jobs each re-install and re-build from scratch instead of building once and passing the result to the others.
- [OPT15 — Cross-Workflow Build Redundancy](opt15-cross-workflow-build-redundancy.md) · HIGH · static · The same build runs in several separate workflows that all fire on one PR, doing identical work several times over.
- [OPT16 — Within-Job Duplicate Commands](opt16-within-job-duplicate-commands.md) · LOW · static · The same script or command is invoked twice within a single job's steps — once in a dedicated "build" step, then again inside a "run tests" step (often with a comment like "rebuild in case of stale artifacts").
- [OPT77 — Repeated Fixed Setup Across Independent Small Jobs](opt77-repeated-fixed-setup-across-independent-small-jobs.md) · MEDIUM · data-driven · A workflow declares several small, independent checks — lint, typecheck, a licence audit, a formatting check, a spellcheck — and every one of them starts a runner, checks out the repository and installs dependencies before doing work that takes no longer than the setup did.

## Category 3 — Docker

sleep-based readiness, over-broad `compose up`.

- [OPT17 — Sleep-Based Container Readiness](opt17-sleep-based-container-readiness.md) · MEDIUM · static · Using `sleep N` to wait for Docker containers to become ready instead of proper healthchecks.
- [OPT18 — All Containers Started for Single-Service Tests](opt18-all-containers-started-for-single-service-tests.md) · MEDIUM · static · A test job starts every Docker service (Postgres, MySQL, Mongo…) when it only needs one or two of them.
- [OPT19 — Test Source Sleep Dominance (includes Playwright `waitForTimeout`)](opt19-test-source-sleep-dominance-includes-playwright-waitfortimeo.md) · HIGH · static · Tests waste time on fixed sleeps baked into the test code — waiting a flat number of seconds instead of waiting for the thing to actually be ready.
- [OPT20 — Unpinned Docker Image Tags](opt20-unpinned-docker-image-tags.md) · LOW · static · Docker services in docker-compose or workflow files using `:latest` or no tag.

## Category 4 — Parallelization

needless `needs:` serialization, unsharded long jobs, a test worker count nobody measured (`OPT88`), the slowest platform compiling its own tests (`OPT89`).

- [OPT21 — Unnecessary `needs:` Dependencies](opt21-unnecessary-needs-dependencies.md) · MEDIUM · static · A job waits on another job it doesn't actually depend on, delaying it for no reason.
- [OPT22 — Sequential Workflows via `workflow_run`](opt22-sequential-workflows-via-workflow-run.md) · MEDIUM · static · Chaining workflows with `workflow_run` when they could run in parallel as jobs within a single workflow.
- [OPT23 — Single-Threaded Matrix (`max-parallel: 1`)](opt23-single-threaded-matrix-max-parallel-1.md) · MEDIUM · static · Setting `max-parallel: 1` on a matrix strategy, running all variants sequentially.
- [OPT24 — Long Test Job Without Sharding](opt24-long-test-job-without-sharding.md) · HIGH · static · A test job runs for many minutes as one big job; splitting it into parallel shards would finish it far sooner.
- [OPT25 — Shard Imbalance](opt25-shard-imbalance.md) · MEDIUM · data-driven · One slice of a parallel test matrix takes far longer than the others, so the whole job waits on that single slow slice.
- [OPT88 — Test Concurrency Is a Guess, Not a Measurement](opt88-measure-test-concurrency.md) · MEDIUM · data-driven · The slowest test job runs with a worker count nobody measured: one worker on a runner with several cores, a fixed cap carried over from an older runner, or "as many as there are cores" where memory or a shared service is the real limit.
- [OPT89 — The Slowest Platform Compiles Its Own Tests](opt89-build-once-test-natively.md) · HIGH · data-driven · A multi-platform test matrix makes every platform build its own test binaries, so the slowest or scarcest runner (Windows, an ARM machine, an emulated architecture) spends most of its time compiling instead of testing.

## Category 5 — Actions and Checkout

stale action pins, repeated setup, full-history checkout, submodule / Git LFS checkout payload, checkout stalls on the tail (`OPT80`).

- [OPT26 — Outdated Action Major Versions](opt26-outdated-action-major-versions.md) · LOW · static · Using old major versions of actions (e.g., `actions/checkout@v3` when `v4` is available).
- [OPT27 — Duplicate `setup-node` Calls in Same Job](opt27-duplicate-setup-node-calls-in-same-job.md) · LOW · static · Calling `actions/setup-node` more than once in the same job.
- [OPT28 — Full Git History Checkout](opt28-full-git-history-checkout.md) · MEDIUM · static · The checkout downloads the project's entire git history when the job only needs the current code — wasted download time on every run.
- [OPT76 — Submodule / Git LFS Checkout Payload](opt76-submodule-lfs-checkout-payload.md) · MEDIUM · static · The checkout clones every submodule, or downloads every Git LFS object, for a job whose steps never read them — a fixed download paid on every single run.
- [OPT80 — Checkout Stalls on the Tail](opt80-checkout-stalls-on-the-tail.md) · MEDIUM · data-driven · The checkout step is a few seconds on a typical run and minutes on a handful, and the slow runs' own logs show the git fetch standing still.

## Category 6 — Conditional Execution

merge_group step-vs-job conditions, draft-PR gating.

- [OPT29 — Merge Queue Skip at Step Level Only](opt29-merge-queue-skip-at-step-level-only.md) · MEDIUM · static · Using `if:` conditions on steps to skip work in merge queue runs, but the job still provisions a runner.
- [OPT30 — Matrix Jobs Without Job-Level Conditional](opt30-matrix-jobs-without-job-level-conditional.md) · MEDIUM · static · Same as OPT29 but for matrix jobs — N runners are provisioned for nothing.
- [OPT31 — Conditional Step With Unconditional Setup](opt31-conditional-step-with-unconditional-setup.md) · MEDIUM · static · A step (typically a tool install, browser install, or large download) exists ONLY to support a downstream step that has an `if:` condition gating it.

## Category 7 — Trigger and Scope

missing path filters, no `--filter` on PR turbo, cron frequency, every PR paying for coverage that could run after merge (`OPT90`).

- [OPT32 — Missing `paths`/`paths-ignore` on Expensive Workflows](opt32-missing-paths-paths-ignore-on-expensive-workflows.md) · HIGH · static · Expensive workflows run on every PR even when the change can't affect them — e.g. a docs-only edit triggers the full test suite.
- [OPT33 — No Draft PR Gating on Expensive Jobs](opt33-no-draft-pr-gating-on-expensive-jobs.md) · MEDIUM · static · Expensive jobs run on draft PRs that are still works-in-progress, burning CI on code that isn't ready for review yet.
- [OPT34 — No Changed-Package Filtering](opt34-no-changed-package-filtering.md) · MEDIUM · static · In monorepos, running all tests/builds regardless of which packages changed.
- [OPT35 — Missing `fail-fast` on Non-Diagnostic Matrix Dimensions](opt35-missing-fail-fast-on-non-diagnostic-matrix-dimensions.md) · LOW · static · A sharded test matrix sets `fail-fast: false`, so when one shard fails the rest keep running and burning minutes on an already-failing run.
- [OPT36 — Cron Schedule Too Frequent](opt36-cron-schedule-too-frequent.md) · LOW · static · A scheduled (cron) workflow runs more often than it needs to, piling up runs.
- [OPT37 — Workflow Trigger Dependency Gap (Cache Race)](opt37-workflow-trigger-dependency-gap-cache-race.md) · HIGH · static · The workflow that writes a cache and the one that reads it can race, so the reader sometimes misses a cache that should have been there.
- [OPT38 — Non-Content Trigger Event Types (PR `edited`, etc.)](opt38-non-content-trigger-event-types-pr-edited-etc.md) · MEDIUM · static · A workflow's `on.pull_request.types` includes events that don't change the code under test, causing the full workflow to re-run on metadata-only changes.
- [OPT39 — Multi-Language Matrix Without Path Filter](opt39-multi-language-matrix-without-path-filter.md) · MEDIUM · static · A security/static-analysis workflow uses a matrix to run language-specific scanners (CodeQL, Snyk, Semgrep, dependency-scan) but never gates the matrix legs on whether files of THAT language changed.
- [OPT40 — Monorepo Job Runs Regardless of Affected App](opt40-monorepo-job-runs-regardless-of-affected-app.md) · MEDIUM · static · In a monorepo, a job for one app runs on every PR even when only a different app changed.
- [OPT90 — Every Pull Request Pays for Coverage That Could Run After Merge](opt90-fast-subset-on-prs-full-after-merge.md) · HIGH · data-driven · Every pull request waits for the full matrix (every OS, every runtime version, every heavyweight configuration) even though a small subset catches almost every failure.

## Category 8 — Release Workflow

release-path caching + redundancy.

- [OPT41 — `TURBO_FORCE: true` Disabling All Caching](opt41-turbo-force-true-disabling-all-caching.md) · HIGH · static · Setting `TURBO_FORCE: true` in release workflows, which forces all tasks to re-execute and ignores both local and remote cache.
- [OPT42 — `TURBO_CACHE: remote:rw` in Release](opt42-turbo-cache-remote-rw-in-release.md) · HIGH · static · Same as OPT3 but specifically in release workflows.

## Category 9 — Queue Times and Concurrency

missing/!coarse concurrency groups.

- [OPT43 — Excessive Queue Time](opt43-excessive-queue-time.md) · MEDIUM · data-driven · Jobs spending significant time in queue before a runner picks them up, indicating runner pool saturation or overly restrictive concurrency groups.
- [OPT44 — Concurrency Group Too Restrictive](opt44-concurrency-group-too-restrictive.md) · MEDIUM · static · Concurrency groups that are too narrow, causing jobs to queue or get cancelled unnecessarily.
- [OPT45 — Missing Concurrency Groups](opt45-missing-concurrency-groups.md) · HIGH · static · Nothing cancels superseded runs, so a branch can have several runs of the same workflow going at once after each push — whether the runs come from `pull_request` or from `push`.
- [OPT46 — Superseded Runs Not Cancelled (Missing Concurrency or `cancel-in-progress: false`)](opt46-superseded-runs-not-cancelled-missing-concurrency-or-cancel-.md) · MEDIUM · data-driven · A workflow that triggers on `push` (or `pull_request`) for branches where developers commonly push multiple times in quick succession, but the workflow either (a) has **no top-level `concurrency:` block at all**, or (b) has a concurrency group with `cancel-in-progress: false`.
- [OPT47 — Redundant push + pull_request Double-Trigger](opt47-redundant-push-pull_request-double-trigger.md) · MEDIUM · data-driven · A workflow triggers on **both** `push` and `pull_request`, and the `push` trigger is **not restricted to the default branch**.
- [OPT48 — High Job-Level Failure Rate (>15% over 30d)](opt48-high-job-level-failure-rate-15-over-30d.md) · MEDIUM · data-driven · This workflow fails a large share of the time.

## Category 10 — Timing Anomalies

failure-rate / bimodal duration signals (advisory), and the same job measured faster on another runner class (`OPT81`).

- [OPT49 — Slow Setup Step](opt49-step-duration-outlier.md) · MEDIUM · data-driven · CUT: not emitted; kept so the id stays retired. A setup step (checkout/install) consistently takes over a minute — usually an uncached dependency fetch that caching would shrink.
- [OPT50 — Post Steps Taking Too Long](opt50-post-steps-taking-too-long.md) · MEDIUM · data-driven · GitHub Actions "Post" steps (cache save, cleanup) taking excessive time, often due to large cache uploads.
- [OPT51 — Install-to-Test Ratio >50%](opt51-install-to-test-ratio-50.md) · MEDIUM · data-driven · CUT: not emitted; kept so the id stays retired. A job spends more time setting up (checkout + install) than actually running tests or building.
- [OPT81 — The Same Job Is Measurably Faster on Another Runner](opt81-same-job-faster-on-another-runner.md) · MEDIUM · data-driven · Your own run history already shows one job running on two kinds of runner, and it is clearly faster on one of them (A1).

## Category 11 — Stack-Specific

turbo task outputs, unstable turbo env keys, type-aware ESLint on a slow lint job (`OPT82`).

- [OPT52 — Turbo Tasks Missing `outputs` in turbo.json](opt52-turbo-tasks-missing-outputs-in-turbo-json.md) · MEDIUM · static · A Turbo task declares no outputs, so Turbo can never cache its result — it re-runs in full every time.
- [OPT53 — Unstable Env Vars Invalidating Turbo Cache](opt53-unstable-env-vars-invalidating-turbo-cache.md) · MEDIUM · static · Environment variables that change between runs (timestamps, build numbers) included in Turbo's env hash, causing cache misses.
- [OPT54 — Full-Repo `pnpm -r` Where Package Filters Are Possible](opt54-full-repo-pnpm-r-where-package-filters-are-possible.md) · MEDIUM · static · Running `pnpm -r <command>` across all packages when only a subset needs the command.
- [OPT55 — vitest Running in Watch/Dev Mode in CI](opt55-vitest-running-in-watch-dev-mode-in-ci.md) · MEDIUM · static · vitest running in watch mode in CI, which never terminates naturally.
- [OPT56 — Playwright Traces/Videos Uploaded Unconditionally](opt56-playwright-traces-videos-uploaded-unconditionally.md) · MEDIUM · static · Playwright configured to always capture traces and videos, even for passing tests.
- [OPT57 — Missing `timeout-minutes` on Known-Flaky Integration Jobs](opt57-missing-timeout-minutes-on-known-flaky-integration-jobs.md) · MEDIUM · data-driven · Integration test jobs without `timeout-minutes`, which default to 360 minutes (6 hours).
- [OPT58 — Turbo Tasks Missing `inputs` in turbo.json](opt58-turbo-tasks-missing-inputs-in-turbo-json.md) · MEDIUM · static · A Turbo task declares no inputs, so unrelated edits (even a README) needlessly bust its cache.
- [OPT59 — Runtime-Only Env Vars in Turbo globalEnv](opt59-runtime-only-env-vars-in-turbo-globalenv.md) · MEDIUM · static · Environment variables listed in Turbo's `globalEnv` or task-level `env` that are only used at runtime (read from `process.env` at execution time), not at compile time (inlined by a bundler like webpack DefinePlugin, Vite's `import.meta.env`, or Next.js automatic `NEXT_PUBLIC_*` inlining).
- [OPT60 — Turbo CI Configuration Missing](opt60-turbo-ci-configuration-missing.md) · LOW · static · Turbo is missing a couple of CI-tuning settings that cut log noise and rendering overhead.
- [OPT82 — Lint Builds the Whole Type Graph](opt82-lint-builds-the-whole-type-graph.md) · MEDIUM · data-driven · A slow lint job runs ESLint with type-aware rules switched on.

## Category 12 — Build Caching (language-agnostic)

uncached compiled-language builds, a dependency compiled from source when a prebuilt package exists (`OPT86`), a compile nobody profiled (`OPT91`).

- [OPT61 — Missing Dependency Caching](opt61-missing-dependency-caching.md) · HIGH · static · Build jobs compile or install dependencies from scratch on every run without any caching action.
- [OPT62 — Build Artifacts Destroyed Before Every Run](opt62-build-artifacts-destroyed-before-every-run.md) · HIGH · static · Explicit `rm -rf` of build directories in workflow steps or build scripts, preventing incremental builds on self-hosted runners where the workspace persists.
- [OPT63 — Dependency Install with Cache Disabled](opt63-dependency-install-with-cache-disabled.md) · MEDIUM · static · Package manager invoked with explicit no-cache flags on persistent runners, defeating the benefit of workspace persistence.
- [OPT86 — CI Compiles a Dependency That Ships Prebuilt](opt86-use-prebuilt-dependency-packages.md) · HIGH · data-driven · An install step compiles a dependency from source (a C or C++ extension, a native add-on, a large machine-learning framework) when a prebuilt package exists for that interpreter and platform.
- [OPT91 — The Compile Is the Long Pole and Nobody Profiled It](opt91-profile-the-compile.md) · HIGH · data-driven · The slowest job's time goes to compiling, the cache is already doing its job, and nobody has asked the compiler where the time goes.

## Category 13 — Hidden Failures and Dead Config

dead env vars, misconfigured caches.

- [OPT64 — Repeated Workflow Attempts From Same Failing Job](opt64-repeated-workflow-attempts-from-same-failing-job.md) · LOW · data-driven · A workflow is repeatedly re-run (`run_attempt > 1`) because the same job keeps failing or timing out.
- [OPT65 — Billing Rounding Waste from Tiny Matrix Legs](opt65-billing-rounding-waste-from-tiny-matrix-legs.md) · LOW · data-driven · A matrix fans out into many sub-minute legs.
- [OPT66 — SKU Arbitrage Ceiling from Expensive Hosted Runners](opt66-sku-arbitrage-ceiling.md) · LOW · data-driven · REMOVED: not emitted; kept so the id stays retired. Removed with the catalog's dollar figures (2026-07-20).
- [OPT68 — Broken Step Masked by `continue-on-error`](opt68-broken-step-masked-by-continue-on-error.md) · MEDIUM · static · A step has `continue-on-error: true` (or uses an action with built-in retry/upload semantics that silently fails) AND has been failing on every run for an extended period.
- [OPT69 — Dead Workflow Env Vars / Config](opt69-dead-workflow-env-vars-config.md) · LOW · static · A workflow declares an environment variable, secret, or config option that is not consumed by any code in the repo.

## Category 14 — Structural / Critical-Path Levers (`OPT70`–`OPT75`, `OPT78`, `OPT83`–`OPT85`, `OPT87`)

routed from the measured long pole (see ARCHITECTURE §11), not a flat grep. `OPT85` (tests run in many processes instead of one) and `OPT87` (tests wait on a slow dependency they do not verify) have no router yet: like `OPT74`, they are catalogued for application from the drilled pole's log and are reported as without a detector. `OPT78` is routed by the drill-time leaf detector rather than the structural router; `OPT83` and `OPT84` are routed by structural passes over the drilled poles' step decomposition plus the workflow YAML, not by the log leaves (`OPT84` also follows the step's `package.json` scripts).

- [OPT70 — Scope the Build/Test to Only What Changed](opt70-scope-build-test-to-changed.md) · HIGH · structural · Your slowest job rebuilds and re-tests the whole project on every PR, even when the change touched only a small part — it could run just the parts that changed instead.
- [OPT71 — Expensive Non-Required Check on the Critical Path](opt71-expensive-non-required-check.md) · HIGH · structural · One of the slowest checks holding up your PRs isn't even required to merge.
- [OPT72 — Redundant-Work Ratio (build/install ≫ payload)](opt72-redundant-work-ratio.md) · MEDIUM · structural · Your slowest job spends most of its time building and installing, and only a little on the tests it actually exists to run.
- [OPT73 — Shared Sub-Step Across Critical-Path Jobs (cluster-floor lever)](opt73-shared-substep-floor.md) · HIGH · structural · The same named step re-runs in several jobs that execute at the same time (the evidence names the step and its cost in each job) — speeding up that one step lowers all of those jobs at once.
- [OPT74 — Trust-Boundary-Forced Cold Work (producer/consumer split)](opt74-trust-boundary-cache-split.md) · MEDIUM · structural · A fork PR can only *restore* what the base branch already published — anything it saves is scoped to its own pull request — so where the warm asset is behind repo secrets, or nothing on the base branch publishes it under a key the fork can compute, every fork PR redoes the full install/build from scratch.
- [OPT75 — Long Pole: Optimize or Relocate the Dominant Step](opt75-decompose-inherent-cost-pole.md) · HIGH · structural · The slowest check holding up your PRs has no off-the-shelf hygiene fix, but almost all its time goes to a single step you can target directly — shard/parallelize a test, cache an install, scope a scan, or (for a fileless check you can't edit) move it off the PR trigger.
- [OPT78 — Per-File Test Isolation Rebuilding Shared Module State](opt78-test-isolation-shared-module-registry.md) · HIGH · structural · Your slowest test job spends more time re-importing the app for each test file than running the tests — because the runner isolates every file and each one rebuilds the same expensive module graph from scratch.
- [OPT83 — Independent Steps on the Long Pole Run One After Another](opt83-run-independent-pole-steps-in-parallel.md) · MEDIUM · structural · The slowest job gating the merge runs several independent compute steps (say lint, typecheck and unit tests) one after another on one runner.
- [OPT84 — A Slow Step on the Long Pole Runs a Tool That Has a Native Drop-in](opt84-slow-tool-on-pole-has-native-drop-in.md) · MEDIUM · structural · A heavy step on the slowest job gating the merge runs a JavaScript dev tool (Prettier, ESLint or `tsc --noEmit`) that has a native, much faster drop-in (oxfmt, oxlint, TypeScript 7).
- [OPT85 — Tests Run in Many Processes Instead of One](opt85-run-tests-in-fewer-processes.md) · HIGH · structural · The slowest test job spends most of its time starting processes rather than running tests: every test case launches a fresh interpreter or script, or every test file is linked into its own test binary.
- [OPT87 — Tests Wait on a Slow Dependency They Do Not Verify](opt87-stub-slow-external-dependency-in-tests.md) · HIGH · structural · Tests on the slowest job call a real, slow dependency (a network API, a model endpoint, a package registry, a remote service) for behaviour the tests are not about, so they spend their time waiting on calls, timeouts and retries.
