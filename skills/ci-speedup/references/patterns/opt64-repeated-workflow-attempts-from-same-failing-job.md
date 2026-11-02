---
pattern: OPT64
impact: LOW
class: data-driven
detector: actions-run-attempts
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml'
fix_strategy: repeated-workflow-attempts-from-same-failing-job
title_template: 'Repeated Workflow Attempts From Same Failing Job'
category: 13
---

# OPT64 — Repeated Workflow Attempts From Same Failing Job

**Anti-pattern**: A workflow is repeatedly re-run (`run_attempt > 1`) because
the same job keeps failing or timing out. The earlier attempts are superseded by
the latest attempt, so their job minutes are bill waste once the retry exists.

**Detection heuristic**:

1. Sample all-status workflow runs and keep only runs whose `run_attempt > 1`.
2. Fetch each candidate run's jobs twice:
   - `GET /actions/runs/{run_id}/jobs?filter=all` to expose jobs from all attempts.
   - `GET /actions/runs/{run_id}/jobs?filter=latest` to identify the current attempt.
3. Compute the prior-attempt job delta as `filter=all - filter=latest`, preferring
   the job payload's `run_attempt` field and falling back to job-id set
   difference only when neither page is at the 100-job cap.
4. Emit a finding only when each credited prior attempt has the same unique
   dominant failed/timed-out job and that exact job name appears in the latest
   attempt. Equal top failures, mixed-cause attempts, missing latest-attempt
   matches, and generic retry volume are withheld.
5. Size runner minutes from the prior-attempt job durations only, scaled by the
   workflow's 30-day all-status run volume divided by the sampled all-status
   denominator. Wall-clock is zero because the credited attempts are superseded.

**Fix**: Stabilize or de-flake the dominant failing job, or narrow the job so it
runs only when its signal is needed. Do not hide the failure or make the workflow
green by weakening required checks; the point is to remove repeated failed
attempts, not suppress the signal.

**Tier-2 render note**: This detector can promote only with measured
`post_completion_waste` evidence: the finding must name `run_attempt > 1`,
`filter=all`, `filter=latest`, the prior-attempt delta, the latest-attempt
match, and the exact dominant failing job name that reappears in every credited
prior attempt.
