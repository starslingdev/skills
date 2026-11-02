---
pattern: OPT43
impact: MEDIUM
class: data-driven
detector: manual
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml'
fix_strategy: excessive-queue-time
title_template: 'Excessive Queue Time'
category: 9
---

# OPT43 — Excessive Queue Time

**Anti-pattern**: Jobs spending significant time in queue before a runner picks them up, indicating runner pool saturation or overly restrictive concurrency groups.

**Detection heuristic**:

- Compute the queue per job across recent runs as **runnable time → job `started_at`**. An entry (un-gated) job is runnable at the **run `created_at`** (the trigger). A `needs:`-gated job is runnable when its **last needed upstream job completes** (the latest `completed_at` among its `needs:` jobs in the same run) — measuring it from the trigger would count the upstream jobs' run time as queue and double-count the upstream job's own long-pole fix. A gated sample whose upstream completion is not in the sampled run is excluded, never credited; skipped jobs are not measured.
- Use **percentile-based baselines by trigger type** (PR runs typically queue differently than release/schedule runs)
- Flag P90 queue time >60s for PR jobs, >120s for release jobs

**Fix**: Depends on root cause — if runner pool saturation: increase runner pool or use larger runners. If concurrency group: relax the group, or cancel superseded runs — but take the **scoped** predicate from [OPT45](#opt45--missing-concurrency-groups) / [OPT46](#opt46--superseded-runs-not-cancelled-missing-concurrency-or-cancel-in-progress-false). **Never a bare `cancel-in-progress: true`**: it kills in-flight runs on the default branch and on release tags (a half-finished deploy/publish/migration), and it must never be reachable from a PR whose head branch is itself the default branch.

---
