---
pattern: OPT111
impact: MEDIUM
class: static
detector: yaml-job-correlated
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml'
fix_strategy: review-once-after-quiet-window
title_template: 'Automated code review runs on every pull request push'
category: 7
---

# OPT111 — Automated Code Review Runs on Every Pull Request Push

**Anti-pattern**: An automated code-review action runs for every pull-request
`synchronize` event. Several fix-up pushes close together can therefore repeat
the entire review before the author has finished changing the pull request.

**Detection heuristic**: Find a recognizable review action or review-posting
command in a job whose `pull_request` trigger includes `synchronize`. Require
the job to run on that activity and require no preceding quiet-window or
debounce step. Fail closed on an unreadable activity guard or step group.
Ordinary test, lint and security jobs on `synchronize` are not findings.

**Fix recipe (safest first)**:

1. Review once when the pull request opens or becomes ready for review, and
   provide an authorized explicit re-review command for later changes.
2. If automatic re-review is required, debounce head changes outside the
   hosted runner and review only the latest settled head. Do not spend runner
   time sleeping merely to create the quiet window.
3. Preserve the current review policy, authorization and required status.
   Exercise open, draft-to-ready, synchronize and explicit re-review paths,
   including a head change while a review is pending.

**Sizing**: Runner-minute effect is a **saving**: the automated review job's
measured billed compute on automatic re-reviews superseded by a nearby push.
This is a static finding, so it reports `runner-minute saving: unsized` with
the reason: the scan reads workflow files only and samples none of the review
job's runs, so neither the count of superseded automatic reviews nor their
billed minutes is measured. Review admission can also reduce external model
usage, but that cost is outside runner-minute sizing and is not estimated here.

**Public evidence**: OpenClaw moved its automated reviewer to review-once with
an explicit re-review trigger.

**Risk**: MEDIUM. Reducing automatic review frequency can leave a changed head
unreviewed if ready-state and explicit re-review admission are incomplete.
Preserve exact-head checks and test every supported review trigger before
rollout.
