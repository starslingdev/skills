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
at least 300 seconds. At each job's runnable time, count other sampled jobs
already in flight with the same label set or literal global concurrency group.
Require Spearman rank correlation of queue versus in-flight count at least
0.6 and absolute run-time correlation below 0.3. Constant execution times have
zero correlation. Exclude skipped jobs, incomplete intervals, unresolved job
identities and upstream `needs:` execution. Fewer than 15 samples is disclosed
as "too few samples", never an all-clear.

**Resource hints**: A literal group with `cancel-in-progress: false`; a label
on exactly one registered runner when a complete inventory is supplied (else
unknown); Android-emulator runner actions, `xcrun simctl` or `adb`. Matching
ports or database names in job-local service containers do not establish a
shared resource. Expressions are not assumed global. Other sampled workflows compete only when their job identities
and readiness are known. The sample may undercount competing work outside the
audit window, so correlation is evidence to investigate, not proof of causation.

**Fix**, in order:

1. Move the one-at-a-time step into its own job so unrelated work can run.
2. Add slots: per-job database names, per-run emulators, or a group keyed per PR.
3. Only then consider adding machines.

**Guardrail**: Keep a real one-at-a-time limit where correctness needs one,
such as deployment to one environment. Narrow its scope instead of removing it.
Inspect the workflow's history and intent before changing policy.

**Sizing**: Median queue above the repository's sampled P10 runnable-queue
baseline, only for an established merge-gating job. Cap each sampled reduction
against all same-run job completion times, including queue; an independent
later-finishing job removes the credit. Incomplete completion data receives no
credit. The existing workflow and cross-workflow floors then apply. No runner-minute saving: waiting is not
execution. Unknown merge-gating status receives no wall-clock credit.

**Relations**: Replace same-job OPT44 static hints and OPT43 generic queue
findings with this measured finding; retain unrelated jobs' findings. A rise in
execution time with contention does not fire OPT97; OPT43 remains available.
