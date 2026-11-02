---
pattern: OPT29
impact: MEDIUM
class: static
detector: yaml-job-correlated
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml'
fix_strategy: merge-queue-skip-at-step-level-only
title_template: 'Merge Queue Skip at Step Level Only'
category: 6
---

# OPT29 — Merge Queue Skip at Step Level Only

**Anti-pattern**: Using `if:` conditions on steps to skip work in merge queue runs, but the job still provisions a runner. The runner startup time (~15-30s) is wasted.

**Detection heuristic**:

```bash
# Find jobs triggered by merge_group that have step-level conditions but no job-level if:
# Parse workflow YAML for on: merge_group triggers
# Check each job for job-level if: conditions
```

**Fix**: Add a job-level `if:` condition to skip the entire job for merge_group events when appropriate.

**Sizing (runner-minutes only, physically bounded)**: The waste is confined to the ONE flagged job — never the whole workflow. The saving is `hit_rate (the merge_group-run share) × that job's MEASURED monthly billable compute` from the cost spine (`cost_basis: affected_jobs`, re-grounded by the same machinery OPT45 uses), so it can never exceed what the job burns. The credited figure is a **ceiling**: today only the runner's provisioning is wasted (the steps already skip on merge_group), so the true reclaim is smaller — disclosed as a ceiling in the size note. Pricing this off the workflow long pole × full volume (the pre-#113 model) credited the whole run's compute to a step-skip on a tiny gate job — a physically-impossible saving `check_saving_within_measured_compute` rejects.

**Real-world example (better-auth)**: The `test` job in ci.yml has no job-level `if:` for merge_group — provisions a runner that skips all steps.

**Real-world example (biomejs/biome, #113)**: the `changes` gate in benchmark.yml skips steps on merge_group but still provisions a runner. Its saving is `0.1 × 823 min/mo (the gate's measured billable) = 82.3 min/mo` — NOT `0.1 × the 941s workflow long pole × 823 runs = 1290.7 min/mo`, which exceeded the gate's entire measured compute. (The two `823`s are different units that happen to coincide: the gate is so light it bills the 1-minute minimum on each of its 823 monthly runs, so its measured billable is also ≈823 min/mo.)

---
