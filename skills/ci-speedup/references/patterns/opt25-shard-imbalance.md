---
pattern: OPT25
impact: MEDIUM
class: data-driven
detector: manual
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml'
fix_strategy: shard-imbalance
title_template: 'Shard Imbalance'
category: 4
---

# OPT25 — Shard Imbalance

**TL;DR**: One slice of a parallel test matrix takes far longer than the others, so the whole job waits on that single slow slice.

**Anti-pattern**: A matrix job where the slowest leg takes >3x longer than the fastest. The workflow's wall clock is bounded by the slowest leg, so the imbalance negates the parallelism benefit.

**Classify within the slowest leg's family before choosing a remedy:**

Strip trailing digits and separators from named variants (`slow-0` → `large`), preserving other matrix axes. Families exist only on an explicit `shard` / `partition` axis (`Shard slow-0`); unmarked numbered variants such as `postgres-14` or `Node.js 20` are distinct configurations. Rank and average only that family; a slower leg of another family in the same matrix still floors the saving. Without a named family structure, preserve the existing classification below. Recognizable platform/runtime version labels remain heterogeneous. Named shard families need no particular starting index: one-based and partially sampled families are supported. A singleton named family has no peer evidence and is not pooled with other families.

**Atomic-unit case:** when the family's slowest median is at least **1.6×** its second-slowest, use the oversized-test/file hypothesis even with an explicit shard marker. Inspect test timings to confirm an indivisible unit, then split the slow test itself (subtests, a table-driven partition, or a smaller file). Adding shards or timing data cannot spread that unit. Model `slow − max(slow/2, second)` and apply the existing workflow and cross-workflow bounds. Evidence names the family and the rule. Otherwise retain the plain-imbalance remedy, using the family mean.

**Other cases:**

1. **Homogeneous sharded suite** — the matrix axis is an explicit `shard` / `partition` index (`test (shard 1/4)`, `(partition 2)`). The legs run *interchangeable slices of one test suite*, so a slow shard is a **distribution** problem. Fix = rebalance.
2. **Heterogeneous matrix of distinct legs** — the matrix axis is a set of *different packages / configs / backends* (`@org/prisma-adapter …` vs `@org/memory-adapter …`; `(postgres)` vs `(mysql)`). The legs do **genuinely different work** and are **NOT interchangeable** — you cannot move tests between them. A slow leg here is the **long pole**, not a distribution skew. Fix = split the slow leg itself, NOT rebalance.

**Fix recipe**: First apply the family-scoped atomic-unit rule above: split the oversized test or file and inspect the planner knobs below for the next step. For findings outside that case, the remedy depends on which case the detector classified (the finding's evidence says which). For a **homogeneous sharded suite**, rebalance the distribution — enable timing-based splitting (pytest-split, nextest timing data) or raise the shard count so the hot shard drops toward the mean leg. For a **heterogeneous matrix of distinct legs**, you cannot rebalance non-interchangeable legs — split the slowest leg itself (sub-shard that package's suite, or split its backends into parallel jobs); the saving floors at the next-slowest leg.

**Next step / planner knobs:** when the atomic split floors at the second leg and at least two remaining legs lie within ±15% of their median, first use OPT88 to measure and fill idle cores inside each existing job, then raise this family's shard count. The plateau is the planner target; keep the combined ceiling separate from the first fix and cap it through `wall_clock.py`'s existing cross-workflow bounds. Without a measured cross-workflow floor, offer only qualitative guidance. For a `fromJSON(needs.<job>.outputs.<x>)` matrix, follow that output to its producing step and surface numeric `--shards`, `--split-shards`, `--*-target-seconds`, or `--shard-count` flags from its command. Otherwise name "the repo's matrix planner". Inspect which knob controls the named family before changing it.

**Runner-minute effect:** rebalancing the same existing jobs is roughly neutral. Raising the shard count adds jobs that each repeat checkout and setup and are billed separately, so it usually raises runner-minutes; on a runner that already uses all of its cores, the extra shards buy wall clock by spending those minutes.

**Detection heuristic**:

- Collect per-leg median duration of a matrix base across sampled runs; flag an atomic-unit ratio of at least 1.6×, or a plain imbalance if `max_leg_median / min_leg_median > 3` (**lower to 2x** when the slow leg is the workflow's long pole).
- **Classify the case**: without a named family, the matrix is *sharded* (case 1) only when the varying axis token is an explicit `shard`/`partition` marker. A prefix-varying / named-package / named-config axis is *heterogeneous* (case 2). Bare-number axes (node versions, etc.) are treated as heterogeneous (they are not interchangeable shards).

**Fix — case 1 (sharded suite), achievable by redistribution**:

- Hash-based partitioning (nextest): increase shard count to dilute hot shards.
- Explicit test lists: rebalance based on measured per-test runtimes.
- Timing-based sharding (pytest-split, nextest timing data): enable it.
- Runner distributes by whole file (Jest `--shard`, Playwright `--shard` without `fullyParallel: true`, file-granular splitters): the imbalance is one oversized file, so find it from measured per-file runtime and split it before adding shards; more shards cannot spread one file. Playwright with `fullyParallel: true` shards individual tests, so this does not apply there.
- Sizing: the slow shard can drop toward the mean leg duration → `Δwc ≈ slow − mean(legs)`.

**Fix — case 2 (heterogeneous legs), NOT rebalanceable**:

- **Split the slowest leg itself** — sub-shard that package's own test suite (add a `shard` axis *within* the slow package), or split its work into parallel jobs (e.g. run a multi-backend leg's Postgres and MySQL as separate matrix entries).
- Do **NOT** describe this as "rebalance shard distribution" — the legs are not fungible; that advice is inapplicable and misleading.
- Sizing is bounded by the **next-slowest leg**, which becomes the new long pole: splitting the slow leg in two gives `Δwc ≈ slow − max(slow/2, second_slowest)`, not `slow − mean`. To go lower, split the next leg too (stack across the cluster).
- **Required-checks caveat**: splitting a required leg into new matrix entries / parallel jobs adds new check names. If the original leg was a required status check, add the new jobs to branch protection as required checks (or the ruleset equivalent) — otherwise the split-out work silently stops gating merges (everything stays green) until that admin-only gating step is done. If the split routes the old check's work behind a `needs:` edge and an aggregator, see OPT75's [dependency-failure skip caveat](#opt75--long-pole-optimize-or-relocate-the-dominant-step) — a dependent skipped by a failed dependency reports skipped, not failed, so the aggregator must run with `always()` (not `!cancelled()`, which a cancelled run skips) and propagate every `needs.<job>.result`, and keeping the required check name on that aggregator re-gates the merge without an admin.

This applies to any framework with sharding (pytest `--shard`, cargo-nextest `--partition`, Jest/Playwright `--shard`) for case 1, and to any package/backend matrix for case 2.
