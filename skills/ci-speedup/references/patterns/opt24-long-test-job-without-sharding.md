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

**Fix**: Add matrix-based sharding. E.g., Playwright: `--shard=${{ matrix.shard }}/${{ strategy.job-total }}`. When the split pieces are few and each leg would repeat a large setup, running the shards as a `parallel:` step group inside the one job is the alternative: setup is paid once and no new check names appear. But in-job shards share one runner's cores, and most test runners already use every core, so the saving is often near zero; separate jobs or a matrix are what add machines (OPT77's fix covers the syntax and runner requirement; OPT75's covers the independence checklist and why the saving is an upper bound until benchmarked).

**Required-checks caveat**: if the job you're sharding is a **required status check** (a merge gate — which the long pole usually is), the new shard jobs must be added to branch protection as required checks (or the ruleset equivalent), or the sharded-out test work silently stops gating merges — everything stays green while the gate no longer actually runs it. The split isn't complete until the new jobs gate the merge, and re-establishing that gating is usually an admin-only step. If the split routes the old check's work behind a `needs:` edge and an aggregator, see OPT75's [dependency-failure skip caveat](#opt75--long-pole-optimize-or-relocate-the-dominant-step) — a dependent skipped by a failed dependency reports skipped, not failed, so the aggregator must run with `always()` (not `!cancelled()`, which a cancelled run skips) and propagate every `needs.<job>.result`, and keeping the required check name on that aggregator re-gates the merge without an admin.

**Wall-clock vs runner-minutes**: Sharding splits the long pole's test execution across **PARALLEL** jobs, so it lowers **wall-clock** (the critical path) but does **NOT** save runner-minutes — the same test work still runs, and more jobs add per-job fixed overhead (checkout, setup, dep install), so runner-minutes go **UP**. The two axes therefore point opposite ways: it is a **TOP Tier-1 wall-clock lever** (push it aggressively when cost is not a constraint — it directly parallelizes the long pole), but it **saves no runner-minutes** (it adds billable compute), so the bill axis shows zero. Note diminishing returns: sharding floors at the per-job fixed overhead, so it must be **stacked** with cache fixes that attack that overhead (warm build cache, dependency cache, browser-binary cache) — past a certain shard count the setup tax dominates and adding shards stops moving wall-clock. See `wall-clock-methodology.md` §7.

---
