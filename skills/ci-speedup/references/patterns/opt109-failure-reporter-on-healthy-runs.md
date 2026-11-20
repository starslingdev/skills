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

**Detection heuristic**: Find a job with at least two `needs`, `if: always()`,
and only recognizable reporting steps. Require its dependencies to include
every independent job; allow a later aggregate gate to depend on the reporter.
Fail closed when any step does ordinary build, test, cleanup or deployment work.

**Fix recipe (safest first)**:

1. Change the reporter condition to `if: failure() || cancelled()` so the job
   is skipped when all of its dependencies succeed.
2. Update the final required aggregate gate so that this intentional reporter
   skip is accepted as success, while a failed or cancelled workload check is
   still rejected.
3. Test the truth table: all checks pass; one check fails; one check is
   cancelled; and the reporter itself fails.

**Sizing**: Runner-minute effect is a **saving**: the failure reporter's billed
compute on healthy runs. Do not state a number until the reporter job's measured
compute and the healthy-run share are available. This normally does not shorten
the merge wait because successful workload checks, rather than an empty
reporter, determine the required gate.

**Public evidence**: [OpenClaw pull request #191007](https://github.com/openclaw/openclaw/pull/191007)
added the failure-or-cancellation predicate to a failure-report job and to the
final gate's selection logic, so the reporter is skipped on healthy runs without
making the required gate reject that intentional skip.

**Risk**: MEDIUM. A blanket change can accidentally make the final required
gate fail on the reporter's expected skip or conceal a cancelled workload job.
Exercise the full result truth table before rollout.
