---
pattern: OPT88
impact: MEDIUM
class: data-driven
detector: manual
risk: MEDIUM
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml,pyproject.toml,pytest.ini,setup.cfg,.bazelrc,.config/nextest.toml,jest.config.*,vitest.config.*,package.json'
fix_strategy: measure-test-concurrency
title_template: 'Test concurrency is set by guess, not measurement'
category: 4
---

# OPT88 — Test Concurrency Is a Guess, Not a Measurement

**Not auto-detected yet.** Like OPT86, no pass emits OPT88, so the audit never reports it on its own and never claims to have checked it. No audit phase applies it; a reader applies it by hand, reading the slow test step's command and configuration against the heuristic below.

**TL;DR**: The slowest test job runs with a worker count nobody measured: one worker on a runner with several cores, a fixed cap carried over from an older runner, or "as many as there are cores" where memory or a shared service is the real limit. Time two or three settings on the real runner class and keep the fastest. More workers is not always faster.

**Anti-pattern**: The test step runs serially on a multi-core runner (pytest without a parallel plugin, `--test-threads=1`, `--runInBand`), or pins a worker count (`-n 4`, `--maxWorkers=2`, a build tool's local test-jobs cap) with no measurement behind it. Often a handful of tests that cannot share a database, a port or a file is what forced the whole suite serial.

**Relation to other patterns**: [OPT23](#opt23--single-threaded-matrix-max-parallel-1) is `max-parallel: 1` across matrix jobs and [OPT24](#opt24--long-test-job-without-sharding) splits a suite across machines. OPT88 is the worker count inside one job on one machine, and it applies before sharding: a shard that wastes its own cores multiplies the waste.

**Detection heuristic** (manual; read, never inferred):

1. The slow step runs tests, and concurrency is set explicitly (serial, or a fixed number), or not at all on a runner whose test tool defaults to serial. Look everywhere it can be set: the test command, the workflow, and the test tool's own config files (for example `local_test_jobs` and a `test_env` thread count such as `RUST_TEST_THREADS` in `.bazelrc`, or pytest, jest and nextest config files).
2. The setting does not match the runner: compare it with the core count of the job's runner label. If the label is custom and its core count is not documented, measure it first with a one-line probe step (`nproc`, or `[Environment]::ProcessorCount` on Windows).
3. Look for what forced it: tests marked serial or skipped under parallel runs, shared fixed resources (one database name, a fixed port, a temp path), real-time waits (`sleep`, fixed-interval polling, see [OPT19](#opt19--test-source-sleep-dominance-includes-playwright-waitfortimeout)). If nothing forced it, say so: the tests did not force the setting, and only the measurement in fix step 3 justifies changing it.

**Fix recipe**:

1. Separate what cannot share: give each worker its own resource (a per-worker database name, an ephemeral port, a per-worker temp directory), and move tests that truly cannot run concurrently onto a serial lane that still runs in the same command (run the parallel set, then the serial set).
2. Replace real-time waits with deterministic synchronization, so workers are not idle.
3. Measure: on the same runner class and commit, with test-result caching off so the tests really run, time the current setting and two or three others (for example the core count and twice it), several runs each. Keep the fastest; stop at the first setting that regresses.
4. Pin the measured value in configuration, per runner class, with a comment naming the measurement; re-measure when the runner changes.

**Guardrail**: the same tests pass at every setting compared, and the flake rate does not rise. Watch memory: a setting that runs out of memory on some runs is not faster.

**Sizing: uncredited**. No `_SIZING` model. The number comes only from the measurement in step 3.

**Real-world example (OpenAI)**, their numbers, not a forecast for your repo:

- openai/codex [#31352](https://github.com/openai/codex/pull/31352) (2026-07-07): "Controlled forced-test-execution measurements in #31339 found that 8 local jobs reduced Bazel elapsed time on every shard by 19–29%, including the slow shard from 835.5s to 609.3s, while 12 jobs regressed." The PR raised the Windows test shards' local test-jobs cap from 4 to 8, the measured best, not the highest value tried.
- openai/openai-agents-python [#2563](https://github.com/openai/openai-agents-python/pull/2563) (2026-03-01): replaced "real-time waits in several slow tests with deterministic synchronization", moved shard-safe tests onto `pytest-xdist`, and isolated Redis/Dapr-sensitive files onto "a serial lane that still runs automatically". `make tests` "averaged 53.59 seconds before these changes and 14.34 seconds after them", measured "on the same machine" across three warm-cache runs: a local measurement, not a CI run.

**Risk**: **MEDIUM**. More workers can expose tests that share state, which shows up as new flakes; the serial lane and the guardrail contain that.
