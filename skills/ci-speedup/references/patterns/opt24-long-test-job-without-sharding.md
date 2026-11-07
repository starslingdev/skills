---
pattern: OPT24
impact: HIGH
class: static
detector: yaml-path-absent
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml'
fix_strategy: long-test-job-without-sharding
title_template: 'Long Test Job Without Sharding'
category: 4
---

# OPT24 — Long Test Job Without Sharding

**TL;DR**: A test job runs for many minutes as one big job; splitting it into parallel shards would finish it far sooner.

**Anti-pattern**: A single test job running all tests sequentially when the test framework supports sharding.

**Detection heuristic**:

- Identify test jobs with wall-clock time >5 minutes
- Check if the test framework supports sharding (Playwright `--shard`, vitest `--shard`)
- Check if sharding is configured

**Fix**: First measure the worker count inside the existing job with OPT88 and use its idle cores safely. Only then add matrix-based sharding. E.g., Playwright: `--shard=${{ matrix.shard }}/${{ strategy.job-total }}`. When the split pieces are few and each leg would repeat a large setup, running the shards as a `parallel:` step group inside the one job is the alternative: setup is paid once and no new check names appear. But in-job shards share one runner's cores, and most test runners already use every core, so the saving is often near zero; separate jobs or a matrix are what add machines (OPT77's fix covers the syntax and runner requirement; OPT75's covers the independence checklist and why the saving is an upper bound until benchmarked).

**Required-checks caveat**: if the job you're sharding is a **required status check** (a merge gate — which the long pole usually is), the new shard jobs must be added to branch protection as required checks (or the ruleset equivalent), or the sharded-out test work silently stops gating merges — everything stays green while the gate no longer actually runs it. The split isn't complete until the new jobs gate the merge, and re-establishing that gating is usually an admin-only step. If the split routes the old check's work behind a `needs:` edge and an aggregator, see OPT75's [dependency-failure skip caveat](#opt75--long-pole-optimize-or-relocate-the-dominant-step) — a dependent skipped by a failed dependency reports skipped, not failed, so the aggregator must run with `always()` (not `!cancelled()`, which a cancelled run skips) and propagate every `needs.<job>.result`, and keeping the required check name on that aggregator re-gates the merge without an admin.

**Wall-clock vs runner-minutes**: Sharding splits only the measured test execution across **PARALLEL** jobs. First subtract compilation and other pre-test work that every shard repeats. A compiled-language step with no measurable compile/test split is a measure-first opportunity with no credited saving. For `N` shards, repeated pre-test work adds roughly **(N−1) × the pre-test floor** in runner-minutes. Sharding can still lower **wall-clock** when enough test payload remains, but the bill axis must include that repeated floor. Note diminishing returns: sharding floors at the per-job fixed overhead, so it must be **stacked** with cache fixes that attack that overhead (warm build cache, dependency cache, browser-binary cache) — past a certain shard count the setup tax dominates and adding shards stops moving wall-clock. See `wall-clock-methodology.md` §7.

When the existing runner is already saturated, extra shard jobs buy wall clock only by spending runner-minutes: every job repeats checkout, setup, and any in-step compile before its tests. Safe in-job workers come first because they can lower both wall clock and runner-minutes without adding that per-job tax.

**Partition-correctness caveat**: a sharded split is only as safe as its guards, and the failure mode is silent — everything stays green while coverage quietly drops. Whichever shape the fix takes (matrix job, in-job `parallel:` step group, or a hand-rolled background-process split), the agent's own tests must prove, not assume: (1) **the shards partition the input set exactly** — every item (page, test file, spec) is covered by exactly one shard across every valid shard count, not left out or doubled up by an off-by-one in the index math; (2) **the shard count is validated where it is read**, in every step that reads it — an empty, zero, or non-numeric count must fail the job loudly rather than silently running zero shards or one shard that claims the whole sweep; (3) **a documented rollback to a lower shard count (often 1) does not itself defeat the guard tests** — if the tests hardcode or derive their expectations from the *current* shard count instead of exercising the real script/workflow logic at an independent count, dialing the count down for an incident silently turns the new guard tests into no-ops at exactly the moment they're needed; and (4) **a result-collection step that merges per-shard verdicts keeps every failure**, not just the first or last one it sees. A split that passes functionally but fails any of these checks is a finding that found a problem, not a safe win — report it as unverified.

---
