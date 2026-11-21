---
pattern: OPT107
impact: HIGH
class: data-driven
detector: collect-runs
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml'
fix_strategy: batch-default-branch-full-ci
title_template: 'Default-branch full CI runs too often to stay current'
category: 7
---

# OPT107 — Default-Branch Full CI Runs Too Often to Stay Current

**TL;DR**: A busy default branch starts the full post-merge suite for every
merge even when another merge replaces that commit before, or shortly after,
the run finishes. Keep the pull-request gate unchanged and validate the current
default-branch tip on an hourly schedule instead, with a repository variable
that can restore per-push validation immediately.

**Detection heuristic**: inspect measured `push` runs on the repository's
default branch. A run is batchable when a later default-branch push starts
before it finishes or within one hour afterward. Report only when at least two
runs and at least 25% of the readable sample are batchable, and when at least
three completed push runs provide job timing. Within each dense cluster, reserve
the most conservative number of hourly replacement validations, then credit
only removable runs whose own completed-job compute is readable. A truncated
run page is a lower bound, never extrapolated into unreadable or unseen runs.
Sparse pushes and release-, deploy-, publish-, or migration-like workflows stay
quiet.

**Fix recipe (safest first)**:

1. Keep every pull-request check and required gate unchanged.
2. Add an hourly `schedule` (and retain `workflow_dispatch`) that checks out and
   validates the current default-branch tip.
3. Put the per-push versus scheduled mode behind a repository variable so an
   owner can switch back without reverting workflow code.
4. Keep deploys, releases, migrations, cache publication, and other
   commit-specific side effects on their existing event. Only batch repeatable
   validation whose result is meaningful for the current tip.

**Sizing**: runner-minute effect is a saving (negative usage): the sum of each
credited removable run's own completed-job compute, after reserving conservative
hourly replacement slots. Jobs with commit-specific side effects (a deployment
`environment`, a deploy/release/publish name, or a publish, push, deploy or
migration step) stay on their push event and are left out of that compute.
Developer merge wait is unchanged because the pull-request gate stays unchanged.
The report states the signed runner-minute effect for the fetched sample
window, and a monthly estimate when the workflow's 30-day default-branch push-run count is readable: the sample saving
times that count over the sampled push runs, capped at the workflow's measured
30-day validation compute, labelled as an estimate from the sampled runs. When
the 30-day count is unreadable the monthly saving is reported as unsized, with
the reason.

**Public example**: [OpenClaw pull request
156783](https://github.com/openclaw/openclaw/pull/156783) batches full
default-branch validation while preserving pull-request coverage.

