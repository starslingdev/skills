---
pattern: OPT57
impact: MEDIUM
class: data-driven
detector: actions-timeout-default-burn
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml'
fix_strategy: missing-timeout-minutes-on-known-flaky-integration-jobs
title_template: 'Missing `timeout-minutes` on Known-Flaky Integration Jobs'
category: 11
---

# OPT57 — Missing `timeout-minutes` on Known-Flaky Integration Jobs

**Anti-pattern**: Integration test jobs without `timeout-minutes`, which default to 360 minutes (6 hours). A hung test can block a runner for hours.

**Detection heuristic**:

1. Parse workflow jobs and use missing `timeout-minutes` only as a structural
   precondition. Candidate workflows come from the scanned workflow graph, not
   from existing findings only.
2. From all-status failed/timed-out workflow runs, find matching jobs whose
   sampled duration reached at least 95% of GitHub's 360 minute default timeout
   and ended no more than 15 minutes after it. A longer failed span was not a
   default-timeout kill (the job was waiting, e.g. on a deployment approval that
   expires after 30 days), so an explicit timeout could not cut it. Only the 360
   minute default is GitHub's, and it applies on GitHub-hosted and self-hosted
   runners alike (the 5-day self-hosted limit caps an explicit timeout, not the
   default); the 95% floor and the 15-minute grace are this detector's own
   tolerances.
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
