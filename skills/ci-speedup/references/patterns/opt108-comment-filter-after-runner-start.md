---
pattern: OPT108
impact: HIGH
class: static
detector: yaml-job-correlated
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml'
fix_strategy: comment-filter-before-runner-start
title_template: 'Comment filter runs after the runner starts'
category: 7
---

# OPT108 — Comment Filter Runs After the Runner Starts

**Anti-pattern**: A workflow triggered by `issue_comment` or
`pull_request_review_comment` provisions a runner for every comment, then a
step-level condition or early return rejects comments whose body or author does
not match. Rejected comments still pay runner startup and job setup.

**Detection heuristic**: Find a comment-triggered job whose steps filter on the
comment body or author but whose job-level `if:` does not carry that admission
condition. Require the step-level filter or explicit early return; a workflow
that intentionally processes every comment is not a finding. Confirm the cost
with the share and compute of short successful runs before sizing it.

**Fix recipe (safest first)**:

1. Copy the handler's existing rejection condition to the job-level `if:` so
   GitHub skips the job before allocating a runner.
2. Preserve every accepted command, custom command override, author class and
   event type. Keep the in-handler check as a defence-in-depth guard until the
   workflow fixture proves the two conditions agree.
3. Exercise accepted and rejected comments against the real job condition,
   including multiline input and automation-authored comments where relevant.

**Sizing**: Runner-minute effect is a **saving**: the measured billed compute
of the workflow's rejected comments. From the sampled run list, a successful
`issue_comment` or `pull_request_review_comment` run that finished in under 30
seconds counts as a rejected comment. The saving is that share of the sampled
runs, times the workflow's 30-day run volume, times those runs' mean billed
minutes (each job rounded up to the minute), capped at the workflow's measured
billed compute over the same volume. At least 3 short runs must be priced.
When runs were not collected, the run list could not be read, or no successful
comment run finished under 30 seconds, the finding stays unsized and says why.
There is no wall-clock effect on the pull-request path: comment runs do not
gate a merge.

**Public evidence**: [OpenClaw pull request #149664](https://github.com/openclaw/openclaw/pull/149664)
moved existing comment admission rules to job-level conditions so ignored
comments no longer acquired hosted runners, while preserving accepted commands
and meaningful automation events.

**Risk**: MEDIUM. Moving a condition earlier can suppress a command or actor
that the handler previously accepted. Preserve the handler's exact semantics
and prove both accepted and rejected cases before removing its inner guard.
