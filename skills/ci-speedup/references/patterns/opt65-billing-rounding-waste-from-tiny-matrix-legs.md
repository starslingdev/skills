---
pattern: OPT65
impact: LOW
class: data-driven
detector: actions-job-rounding
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml'
fix_strategy: billing-rounding-waste-from-tiny-matrix-legs
title_template: 'Billing Rounding Waste from Tiny Matrix Legs'
category: 13
---

# OPT65 — Billing Rounding Waste from Tiny Matrix Legs

**Anti-pattern**: A matrix fans out into many sub-minute legs. GitHub bills each
job with per-job minute round-up, so three 20-second legs bill as 3 minutes even
though their combined work is only 1 billable minute if handled inside one
off-spine runner allocation.

**Detection heuristic**:

1. Group sampled jobs by an exact trailing-parenthetical matrix base, e.g.
   `lint (a)`, `lint (b)`, `lint (c)` -> `lint`.
2. For each sampled run, compute the exact billing-rounding delta:
   `sum(ceil(job_seconds / 60)) - ceil(sum(job_seconds) / 60)`.
3. Emit only when the matrix base has at least three observed tiny legs, every
   credited occurrence is sub-minute, all credited occurrences are on the same
   known runner, and the combined credited leg p50 for each credited run is
   strictly below the workflow cluster floor. If the combined legs can reach the
   floor, withhold the finding because consolidation can serialize the merge gate
   and become wall-clock negative.
4. Scale the sampled billing-minute delta by the monthly volume for the sampled
   event scope divided by sampled successful runs. This credits only billable
   rounding waste, not runtime.

**Fix**: Do not blindly "merge matrix jobs." Only consolidate off-spine tiny
legs, or restructure shared setup / runner allocation so the merge-gating
matrix stays parallel. Avoid lowering `max-parallel` or adding an upstream
`needs:` stage for any matrix leg that can sit on the gate.

**Tier-2 render note**: OPT65 can promote only with measured rounding evidence
and a `below_cluster_floor` certificate. The finding must stamp
`wall_clock_p50_s=0`, `sizing_basis=measured`, the exact rounding formula in
`measured_signal`, structured `rounding_waste` samples that let
`verify_report.py` rederive the billable-minute amount, and affected jobs that
are the credited matrix legs rather than an ambiguous matrix base. It never
claims speedup; it credits only billing-minute round-up waste.
