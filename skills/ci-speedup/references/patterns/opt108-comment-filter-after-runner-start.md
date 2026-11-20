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

**Sizing**: Runner-minute effect is a **saving**: the affected job's measured
compute on comments rejected by the lifted condition. Do not credit the whole
workflow or guess a comment rejection rate; withhold a number until matching
short successful runs establish the rejected share. Developer wall-clock
saving is not sized because these jobs do not normally gate a merge.

**Public evidence**: [OpenClaw pull request #149664](https://github.com/openclaw/openclaw/pull/149664)
moved existing comment admission rules to job-level conditions so ignored
comments no longer acquired hosted runners, while preserving accepted commands
and meaningful automation events.

**Risk**: MEDIUM. Moving a condition earlier can suppress a command or actor
that the handler previously accepted. Preserve the handler's exact semantics
and prove both accepted and rejected cases before removing its inner guard.
