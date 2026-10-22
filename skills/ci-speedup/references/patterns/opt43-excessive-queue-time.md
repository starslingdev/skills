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

- Compute the wait-to-start per job across recent runs as **run `created_at` (the trigger) → job `started_at`** — NOT the job's own `created_at`. GitHub stamps a *gated* job's `created_at` when its `needs:` dependency resolves, so `started − job.created` sees only that job's own runner pickup and hides the upstream gating cost (the gating job's queue + run time) the developer also waited on (it can undercount by minutes). Measuring from the run trigger captures the full pre-start wait the developer experiences. **Caveat:** for a gated job this number then *includes* the gating job's run time, so it is wall-clock time-to-start, not pure queue — the savable portion is bounded by the gating job's own fix. Entry (un-gated) jobs are unaffected: their `created_at` ≈ the run trigger.
- Use **percentile-based baselines by trigger type** (PR runs typically queue differently than release/schedule runs)
- Flag P90 queue time >60s for PR jobs, >120s for release jobs

**Fix**: Depends on root cause — if runner pool saturation: increase runner pool or use larger runners. If concurrency group: relax the group, or cancel superseded runs — but take the **scoped** predicate from [OPT45](#opt45--missing-concurrency-groups) / [OPT46](#opt46--superseded-runs-not-cancelled-missing-concurrency-or-cancel-in-progress-false). **Never a bare `cancel-in-progress: true`**: it kills in-flight runs on the default branch and on release tags (a half-finished deploy/publish/migration), and it must never be reachable from a PR whose head branch is itself the default branch.

---
