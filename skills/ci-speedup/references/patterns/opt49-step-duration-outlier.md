---
pattern: OPT49
impact: MEDIUM
class: data-driven
detector: manual
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml'
fix_strategy: step-duration-outlier
title_template: 'Slow or High-Variance Step'
category: 10
status: cut
---

# OPT49 — Slow Setup Step

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
