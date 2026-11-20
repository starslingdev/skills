---
pattern: OPT109
impact: MEDIUM
class: static
detector: yaml-job-correlated
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml'
fix_strategy: failure-reporter-on-failure
title_template: 'Failure reporter runs on healthy pull requests'
category: 6
---

# OPT109 — Failure Reporter Runs on Healthy Pull Requests

**Anti-pattern**: A job depends on every independent check, does nothing except
post a failure comment, summary or notification, and uses `if: always()`. GitHub
therefore allocates its runner on healthy pull requests where there is no
failure to report.

**Detection heuristic**: Find a job with at least two `needs` whose condition is
`always()`, alone or with `!cancelled()`; any other term (a result comparison,
an event guard) may already skip healthy runs and stays quiet. Every
independent job must be upstream of it, directly or through another need such
as a shared `setup` job; a later aggregate gate may depend on the reporter.
Beyond checkout, setup and dependency installs, every step must only report:
each shell command is a summary, log, `gh pr|issue comment`, webhook or
report-named script call, or a recognized comment, notification or report
action. The report must be failure-only: the job's id or name says so, or every
reporting step is gated on a failed or cancelled need. Fail closed on any other
command, a non-zero `exit` (that job is a gate), and a step that edits, hides
or clears an earlier report.

**Fix recipe (safest first)**:

1. Confirm the reporter does nothing on a healthy run that you still need, such
   as clearing or updating an earlier failure comment after a green rerun. Keep
   that path before gating the job.
2. Change the reporter condition to `if: failure() || cancelled()` (or
   `if: failure()` when it used `always() && !cancelled()`, to keep excluding
   cancelled runs) so the job is skipped when all of its dependencies succeed.
3. In every final required aggregate gate that `needs` the reporter: keep (or
   add) `if: always()`, because without a status-check function a skipped need
   skips the gate and a skipped job reports success to a required check. Accept
   `needs.<reporter>.result` of `success` or `skipped`, and require every
   workload check to be `success`, for example by failing when
   `contains(needs.*.result, 'failure') || contains(needs.*.result, 'cancelled')`,
   which a skipped reporter passes. Change any comparison that demands
   `success` from the reporter, or rejects `skipped` for every need.
4. Test the truth table: all checks pass (reporter skipped, gate passes); one
   check fails (reporter runs, gate fails); one check is cancelled (gate
   fails); and the reporter itself fails (gate fails).

**Sizing**: Runner-minute effect is a **saving**: the failure reporter's billed
compute on healthy runs. It stays unsized until the reporter job's measured
compute and the healthy-run share are available. When a final gate needs the
reporter, the skip also removes the reporter's queue and run time from the
healthy path to that gate; otherwise there is no merge-wait effect.

**Public evidence**: [OpenClaw pull request #191007](https://github.com/openclaw/openclaw/pull/191007)
is the public example of this shape and of the fix: a failure-or-cancellation
condition on the reporter, with the final required gate treating its skip as
success.

**Risk**: MEDIUM. A blanket change can make the final required gate fail on the
reporter's expected skip, let a skipped gate pass a required check, or conceal a
cancelled workload job. Exercise the full result truth table before rollout.
