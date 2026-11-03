---
pattern: OPT97
impact: MEDIUM
class: data-driven
detector: manual
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml'
fix_strategy: single-slot-queue
title_template: 'Queue Caused by a One-at-a-Time Resource'
category: 9
---

# OPT97 — Queue Caused by a One-at-a-Time Resource

**TL;DR**: When queue time grows with competing jobs but execution time stays
flat, investigate a shared slot before adding faster machines.

**Anti-pattern**: A global concurrency group, unique runner label, emulator,
device or shared database admits one job at a time while other work waits.
This is a capacity finding. The sampled intervals do not measure utilization
or prove which hinted resource causes the wait.

**Detection heuristic**: Per job and exact runner-label set, require at least
15 samples, median runnable-to-start queue of at least 60 seconds and P90 of
at least 300 seconds. An entry job under a workflow-level global group is timed
from the run's creation (first attempt only): GitHub creates that run's jobs only
when the group frees. Evaluate only a self-hosted label set or a population
with a resource hint below. On hosted runners an account's concurrent-job limit
raises queue and in-flight work together, so that queue stays with OPT43.
In-flight counts come from one bounded read of every run, any status, created
in the population's window (three hours before its first sample to its last):
at most 3 run-list pages, then job lists only for runs whose workflow could use
the same runners or group, at most 60. A newest-success sample sees a small
fraction of competing jobs, so it is never used for the count. A failed or
capped read gives "check this: competition not measured", never a finding. At
each job's runnable time, count jobs already in flight with the same label set
or the same global group. Require Spearman rank correlation of queue versus
in-flight count at least 0.6 and absolute run-time correlation below 0.3.
Constant execution times have zero correlation. Exclude skipped jobs,
incomplete intervals and upstream `needs:` execution. Fewer than 15 samples is
disclosed as "too few samples" only when the queue already meets the
thresholds on a pool OPT97 evaluates. Sampled jobs matching no single YAML job (reusable-workflow
children, ambiguous names) are counted per workflow in Data sources.

**Resource hints**: A global concurrency group: the string form or a `group`
with `cancel-in-progress` false or omitted (GitHub's default), whose key does
not reference the ref, head ref, PR number, commit SHA or run id. A job-level
group replaces the workflow-level one; `${{ github.workflow }}` alone is global
within its workflow. A label on exactly one repo-level runner when the
repository's complete runner list is readable (organization runners are not
visible, else unknown). Android-emulator runner actions, `xcrun simctl` or
`adb`. Matching ports or database names in job-local service containers do not
establish a shared resource. Correlation is evidence to investigate, not proof
of causation.

**Fix**, in order:

1. Move the one-at-a-time step into its own job so unrelated work can run.
2. Add slots: per-job database names, per-run emulators, or a group keyed per PR.
3. Only then consider adding machines.

**Guardrail**: Keep a real one-at-a-time limit where correctness needs one,
such as deployment to one environment. Narrow its scope instead of removing it.
Inspect the workflow's history and intent before changing policy.

**Sizing**: Median queue above a baseline: the P10 runnable queue of the
repository's other sampled jobs, never the population being evaluated. With no
other jobs, no wall-clock credit. Only an established merge-gating job is
credited. Cap each sampled reduction against all same-run job completion times,
including queue; an independent later-finishing job removes the credit.
Incomplete completion data receives no credit. The existing workflow and
cross-workflow floors then apply. No runner-minute saving: waiting is not
execution.

**Relations**: Replaces the same workflow's same-job OPT43 finding; a finding
naming other jobs, or another workflow's, is kept. The merge also accepts
OPT44, but no OPT44 detector exists, so only OPT43 merges today. A rise in
execution time with contention, a hosted pool without a hint, or an unmeasured
competition does not fire OPT97; OPT43 remains.
