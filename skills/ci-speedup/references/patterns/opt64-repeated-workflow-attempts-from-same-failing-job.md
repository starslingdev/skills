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
6. Rank (credit) a saving only when at least 3 sampled retried runs share the
   dominant failing job and that job failed again on every retry. A retry
   re-runs the same run, so the same commit: a job that failed and then passed
   is a flaky-test signal whose remedy is de-flaking, not a CI-config change
   (SKILL.md admission rule 3); a retry still running, cancelled or skipped may
   yet pass. Fewer runs, or any retry that did not fail again, emits the
   finding **advisory** as OPT48 is: no saving, not ranked, kept in
   the findings JSON with a `rerun_sample` stamp and wording that names how many
   retried runs it rests on. The renderer and `verify_report.py` hold findings
   written by older engines to the same minimum; missing or malformed retry
   outcomes remain advisory until a fresh collection establishes them. A
   carried-forward job from an older attempt never proves that the retry failed
   and its unchanged compute is not counted as retry savings.

**Fix**: Stabilize or de-flake the dominant failing job, or narrow the job so it
runs only when its signal is needed. Do not hide the failure or make the workflow
green by weakening required checks; the point is to remove repeated failed
attempts, not suppress the signal.

**Tier-2 render note**: This detector can promote only with measured
`post_completion_waste` evidence: the finding must name `run_attempt > 1`,
`filter=all`, `filter=latest`, the prior-attempt delta, the latest-attempt
match, and the exact dominant failing job name that reappears in every credited
prior attempt.
