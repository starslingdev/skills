#!/usr/bin/env python3
"""Map ci-speedup structured-grader results into dogfood SEED BUGS (maintainers-only).

loop-self-improvement-upgrades.md §2-A (PR-A). The dogfood loop's `Run + audit` agent already
LLM-audits each rendered report; this adds a DETERMINISTIC second source of bugs from the two
structured graders the repo already ships — so a defect a grader catches surfaces even when the
LLM audit misses it. The mapping is a pure, unit-tested helper (validated by fixture-replay, §3),
NOT an LLM judgment: the agent runs this script and merges its `seeds`, it does not eyeball-map
grader text.

Two graders feed it:
  - `verify_report.run_checks` — the ~16 per-report invariants (PASS/FAIL/SKIP).
  - `measure_contradictions._consumer_divergence` — the cross-repo pole-pick divergence probe
    (reused, not re-implemented).

NOT every `verify_report` FAIL is a skill bug (§2-A, review #1/P3) — so every check is classified
by a committed TRIAGE ALLOWLIST:
  - AUTO_SEED — a report-INTERNAL-consistency FAIL ⇒ a skill bug. Emitted into `seeds` (the audit
    bug list → the fix fan-out).
  - TRIAGE   — environment/content-coupled (can fire on audited public-repo content, or on a
    dirty/unmerged-branch checkout). Emitted into `triage` for the AGENT to adjudicate; NEVER
    auto-fanned to a blind fix.
  - EXCLUDE  — a pure run/harness artifact, never a skill bug. Recorded in `excluded`, never seeded.
A SKIP or a PASS never seeds.

Seed signatures use TWO dedup namespaces (§2-A, 4th-pass C1):
  - locus-BEARING (an honest `file:symbol`): `<slug>@<file>:<symbol>` — the SAME namespace as
    LLM-audit bugs, so a grader seed that names a real locus dedups against them.
  - locus-LESS (no honest locus — which is EVERY `verify_report` grader check, since none emits a
    `file:symbol`): `<fixed-slug>@check:<check>` — a DISTINCT namespace. It dedups grader seeds
    against EACH OTHER (two repos tripping the same check → ONE bug) but is NOT expected to collide
    with a file:symbol LLM bug. A locus-less seed is filed needs-triage (its renderer locus is for
    the fix agent to pin).

This is LOCAL maintainer infra (outside the installable skill); it clones/calls nothing. Point it
at an already-rendered report + its findings JSON:

    python3 grader_seeds.py --report report.md --findings findings.json --skill-repo skills/ci-speedup
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

# Same-dir sibling: reuse its by-path verify_report loader (avoids the ci-secure verify_report
# name collision on the shared pytest pythonpath) AND its _consumer_divergence (one definition).
import measure_contradictions as mc

# --- triage dispositions -----------------------------------------------------
AUTO_SEED = "auto-seed"   # report-internal-consistency FAIL ⇒ skill bug → seeds
TRIAGE = "triage"         # env/content-coupled → agent adjudicates, never blind-fixed
EXCLUDE = "exclude"       # run/harness artifact → never a skill bug

# The committed, reviewable TRIAGE ALLOWLIST. Keyed by the live `verify_report.Check.name` string
# (what `run_checks` actually yields) so the classification can be applied at runtime and the
# "every check is classified" invariant is directly testable; the originating `check_*` function
# name is in the trailing comment for cross-reference with spec §2-A. 62 AUTO_SEED / 1 EXCLUDE /
# 2 TRIAGE — a new `verify_report` check makes `test_every_check_is_classified` go red until it's
# classified here (so no check is ever left undisposed).
TRIAGE_ALLOWLIST = {
    "primary section present (Long poles)": AUTO_SEED,                                              # check_primary_section_present
    "static-only banner matches CI shape": AUTO_SEED,                                               # check_static_only_banner_matches_ci_shape
    "headline names the wall-clock (merge-wait) axis": AUTO_SEED,                                   # check_headline_names_wall_clock
    "every #pole-N / #also-noticed / #pre-start-wait / #runner-minute-reductions reference resolves to an anchor": AUTO_SEED,  # check_pole_anchors_resolve
    "RCA hands off via prompts (never prescribes a fix, never dead-ends)": AUTO_SEED,               # check_rca_hands_off_never_prescribes
    "agent-prompt MEASURED CAUSE never asserts timeline steps the pole doesn't render": AUTO_SEED,  # check_measured_cause_matches_rendered_timeline
    "TOC 'Also noticed' count == appendix rows + hidden pointer": AUTO_SEED,                        # check_also_noticed_count_honest
    "data basis disclosed; coverage gaps named, not silent": AUTO_SEED,                             # check_coverage_disclosed
    "workflows missing from the sample are NAMED in the report": AUTO_SEED,                         # check_run_list_gaps_named
    "report uses ASCII hyphens only (no em/en/figure/bar/minus dash)": AUTO_SEED,                   # check_no_typographic_dashes
    "code fences are balanced (no stray ``` breaking out of a fence)": AUTO_SEED,                   # check_fences_balanced
    "every rendered finding pattern exists in the findings JSON": AUTO_SEED,                        # check_rendered_patterns_exist
    "every data-driven finding carries a measured signal (no unsupported claims)": AUTO_SEED,       # check_data_driven_have_signal
    "report drills every gating pole, each with a hand-off prompt": AUTO_SEED,                      # check_speed_poles_complete
    "aggregation-gate poles tell the upstream story, never an optimize-this prompt": AUTO_SEED,    # check_aggregation_gate_poles_never_prescribe
    "each rendered pole's drill belongs to its own job (no cross-job log leak)": AUTO_SEED,         # check_pole_drill_belongs_to_its_job
    "every 🤖 gap-fill evidence line is verbatim from the captured job log (issue #106)": AUTO_SEED,  # check_gap_fill_evidence_grounded
    "no spine-dropped check is also framed on the merge-gating critical path": AUTO_SEED,           # check_dropped_check_not_framed_on_path
    "no spine-demoted pole carries the typical-gate framing (prompt / Contents)": AUTO_SEED,        # check_demoted_pole_not_framed_typical_gate
    "headline 'slowest check' names the data layer's critical_path_check": AUTO_SEED,               # check_headline_slowest_matches_stamp
    "headline reconciles a non-universal slowest check with the population floor": AUTO_SEED,        # check_headline_floor_presence_reconciled
    "presence-causal headline only when the slowest check is minority-present": AUTO_SEED,          # check_headline_presence_causal_only_when_minority
    "chain headline re-derives from the stamped chain facts": AUTO_SEED,                             # check_headline_chain_matches_stamp (ENG-1 PR-N2)
    "rendered chain total is never below the largest member it sums": AUTO_SEED,                      # check_aggregate_total_ge_largest_member (issue #25/#22)
    "headline merge-wait is never above the measured makespan p50": AUTO_SEED,                        # check_headline_wait_within_makespan (issue #25/#24)
    "each finding's runner-minute saving is within its jobs' measured compute": AUTO_SEED,            # check_saving_within_measured_compute (issue #25)
    "every credited runner-minute saving carries a measured-basis stamp (sizing door)": AUTO_SEED,     # check_saving_carries_measured_basis (issues #43/#44/#45)
    "cluster-floor lever's ceiling floors at a NON-sibling check (not its own matrix leg)": AUTO_SEED,  # check_cluster_lever_ceiling_escapes_sibling (issue #44)
    "Bottom-line headline leads with the stamped cluster-floor ceiling (no burial)": AUTO_SEED,  # check_headline_consumes_stamped_cluster_ceiling (issue #49)
    "Bottom-line crowned cluster lever is presence-eligible (no minority-workflow crown)": AUTO_SEED,  # check_headline_lever_is_presence_eligible (issue #56)
    "Bottom-line crowned cluster lever is on the merge-gating spine (not off-spine)": AUTO_SEED,  # check_headline_cluster_lever_on_spine (issue #114)
    "headline leads with the observed wall when the chain sum diverges from the makespan": AUTO_SEED,  # check_headline_wait_is_divergence_correct (issue #115)
    "every file-backed structural lever carries a measured dominant step (no name-inferred OPT75)": AUTO_SEED,  # check_structural_pole_has_measured_step
    "no payload-bearing step is binned as `build` (redundant-work inflation → OPT72 misroute)": AUTO_SEED,  # check_structural_step_category_not_payload_binned_as_build
    "crowned detector leaf agrees with the pole's dominant measured category (no off-category ceiling)": AUTO_SEED,  # check_detector_leaf_agrees_with_dominant_category (issue #16)
    "pole addressable ceiling within the co-occurrence floor": AUTO_SEED,                            # check_pole_ceiling_within_cooccurrence
    "recoverable ceiling above the typical wait carries a worst-case reconciliation": AUTO_SEED,     # check_recoverable_within_wait (issue #66 fix 2)
    "no drilled pole measures a pre-change config era without the era disclosure": AUTO_SEED,        # check_config_era_boundary (issue #66 fix 1)
    "check enumeration is bound to the kept config era (no other-config check leaks in)": AUTO_SEED,  # check_era_enumeration_bound (issue #69)
    "the rendered era disclosure matches the enumerated config (no pre-only caveat over post-era checks)": AUTO_SEED,  # check_era_disclosure_matches_enumeration (issue #74)
    "the per-PR chain/makespan spine is bound to the kept config era (no dropped-era PR blends in)": AUTO_SEED,  # check_era_chain_spine_bound_to_kept_era (issue #80)
    "every drilled pole's binding floor is disclosed on the spine (no silent heavy-check drop)": AUTO_SEED,  # check_spine_heavy_check_disclosed
    "rendered per-workflow gate frequency matches the populations re-derivation": AUTO_SEED,        # check_workflow_gate_frequency_matches
    "no drilled pole's job is also framed as an 'Also noticed' minor-cleanup finding": AUTO_SEED,  # check_pole_not_reframed_as_hygiene
    "TOC 'Also noticed' pointer's on/off-path label matches the appendix": AUTO_SEED,             # check_toc_also_noticed_label_honest
    "every rendered cache claim carries a cross-run cache distribution": AUTO_SEED,               # check_cache_claim_backed_by_distribution
    "cache finding's framing matches its measured hit-rate distribution": AUTO_SEED,             # check_cache_framing_matches_distribution
    "headline critical-path check is an actual recurring gate": AUTO_SEED,                        # check_headline_pole_actually_gates
    "every 'slowest ... waits on' framing phrase is a registered claim": AUTO_SEED,               # check_claims_cover_framing_vocabulary
    "Tier-2 R-rows carry re-derived wall-clock-neutral certificates": AUTO_SEED,                  # check_tier2_neutrality_derived
    "Tier-2 R-rows use measured sizing basis": AUTO_SEED,                                        # check_tier2_measured_basis
    "Tier-2 section-lead totals match de-overlapped findings": AUTO_SEED,                         # check_tier2_total_deoverlapped
    "no claim or prose cites the closing-down /timing endpoints": AUTO_SEED,                     # check_no_timing_endpoint_citation
    "Tier-2 claims carry the jobs-API derivation-basis field": AUTO_SEED,                        # check_tier2_claims_derivation_basis
    "Tier-2 headline claim names the top stamped finding": AUTO_SEED,                             # check_tier2_headline_matches_stamp
    "Tier-2 savings rows are backed by runner-minute cost spine": AUTO_SEED,                      # check_tier2_savings_rows_backed_by_cost_spine
    "runner-minute cost spine source block is re-derivable": AUTO_SEED,                           # check_runner_minute_spine_contract
    "no rate-derived dollars on the minutes surfaces": AUTO_SEED,                                 # check_no_rate_derived_dollars
    "shallow cost-spine sample is disclosed (re-derived from the stamped workflows)": AUTO_SEED,  # check_cost_spine_shallow_disclosed
    "the fetch plan matches its call sites (no unconsumed prefetches)": AUTO_SEED,                # check_prefetch_plan_consumed
    "Skip-family prompts carry the required-check Pending caveat": AUTO_SEED,                     # check_skip_family_prompts_carry_pending_caveat (PR-S3)
    "no fileless/managed status check crowns the headline (disclosed as PR-lifetime latency)": AUTO_SEED,  # check_headline_basis_excludes_fileless (issue #12)
    # EXCLUDE — the harness names the report file, not the skill (a pure run artifact).
    "scanned date matches the date in the filename": EXCLUDE,                                       # check_date_matches_filename
    # TRIAGE — can fire on the AUDITED repo's own content, or on a dirty / unmerged-branch checkout.
    "no ci-secure template leakage (security-domain framing in ci-speedup prose)": TRIAGE,         # check_no_domain_leakage
    "report's skill commit is HEAD or an ancestor of it (provenance)": TRIAGE,                      # check_skill_commit_provenance
}

# --- PR-B: closed-vocab `class` field (loop-self-improvement-upgrades.md §2, Item 1) ---------
# Per the spec's explicit alternative ("add a CLOSED-vocab `class` enum to the audit bug schema (OR
# reuse the transcript summary's `root_cause` enum)"), this REUSES the transcript self-improvement
# loop's `root_cause` enum (`maintainers/ci-speedup/loops/loop-summary.schema.json`) verbatim rather
# than inventing a SECOND closed vocabulary the two loops would have to hand-keep in sync — the
# values already fit a dogfood-audit bug (e.g. `estimated-not-measured`, `mis-ranked-lever`,
# `coverage-gap-dead-end` are exactly the report-faithfulness defect shapes this loop finds).
# Mirrored verbatim in `ci-speedup-dogfood.js`'s `BUG_CLASS_ENUM` — kept in lockstep by
# `test_grader_seeds.py`'s `test_class_enum_matches_the_dogfood_workflows_bug_class_enum`.
CLASS_ENUM = frozenset({
    "missing-never-rule", "ambiguous-phase-instruction", "missing-phase-check", "scope-overreach",
    "coverage-gap-dead-end", "estimated-not-measured", "fabricated-or-unsupported-finding",
    "mis-ranked-lever", "missing-second-pole-or-finding", "skipped-verification-after-regen",
    "prescribed-a-fix", "unscrubbed-or-disclosure-risk", "tooling-or-environment", "other",
})

# Every `verify_report` Check.name (same key set as TRIAGE_ALLOWLIST — total classification is a
# unit-tested invariant, mirroring `test_every_check_is_classified`) mapped to the CLOSEST
# CLASS_ENUM label for the defect shape that check's FAIL represents. This is an editorial judgment
# call (there is no single objectively-correct mapping), documented per entry; it retrofits the
# closed-vocab `class` onto grader-seed bugs the tracker log flagged as owed once PR-B lands the
# enum (§4 / 4th-pass I1). A grader-seed bug is, BY CONSTRUCTION, already "covered" (it trips an
# EXISTING check) — so it can never itself become a 'novel-sketch' candidate in the dogfood
# workflow's class routing; classifying it here is for consistency/clustering with LLM-found
# instances of the same underlying defect shape, and for the audit trail.
CHECK_CLASS = {
    "primary section present (Long poles)": "missing-phase-check",                       # a required phase section is absent
    "static-only banner matches CI shape": "fabricated-or-unsupported-finding",          # dormant-repo banner asserted at a live no-PR-gating repo / "no run timing" beside priced timed runs — the report contradicts its own data
    "headline names the wall-clock (merge-wait) axis": "ambiguous-phase-instruction",     # headline mislabels the axis it renders
    "every #pole-N / #also-noticed / #pre-start-wait / #runner-minute-reductions reference resolves to an anchor": "missing-phase-check",  # broken cross-reference — a rendering-completeness gap
    "RCA hands off via prompts (never prescribes a fix, never dead-ends)": "prescribed-a-fix",  # exact match
    "agent-prompt MEASURED CAUSE never asserts timeline steps the pole doesn't render": "fabricated-or-unsupported-finding",  # a canned cause asserts a timeline shape the report contradicts
    "TOC 'Also noticed' count == appendix rows + hidden pointer": "fabricated-or-unsupported-finding",  # a rendered count not backed by the data
    "data basis disclosed; coverage gaps named, not silent": "coverage-gap-dead-end",     # exact match (the spec's own no-silent-drop framing)
    "workflows missing from the sample are NAMED in the report": "coverage-gap-dead-end",  # a workflow that vanished from the sample rendering as measured
    "report uses ASCII hyphens only (no em/en/figure/bar/minus dash)": "other",           # pure formatting, no semantic root-cause fits
    "code fences are balanced (no stray ``` breaking out of a fence)": "other",           # structural corruption guard, no semantic root-cause fits
    "every rendered finding pattern exists in the findings JSON": "fabricated-or-unsupported-finding",  # rendered claim not backed by findings
    "every data-driven finding carries a measured signal (no unsupported claims)": "estimated-not-measured",  # exact match
    "report drills every gating pole, each with a hand-off prompt": "missing-second-pole-or-finding",  # a required pole/finding is missing
    "aggregation-gate poles tell the upstream story, never an optimize-this prompt": "prescribed-a-fix",  # an "optimize this step" prompt over a `needs:`-only success sink that runs no work
    "each rendered pole's drill belongs to its own job (no cross-job log leak)": "fabricated-or-unsupported-finding",  # a drill misattributed to the wrong job's evidence
    "every 🤖 gap-fill evidence line is verbatim from the captured job log (issue #106)": "fabricated-or-unsupported-finding",  # a quoted evidence line not in the captured log = a fabricated/altered quote
    "no spine-dropped check is also framed on the merge-gating critical path": "mis-ranked-lever",  # spine/critical-path framing error
    "no spine-demoted pole carries the typical-gate framing (prompt / Contents)": "mis-ranked-lever",  # a demoted pole framed as the typical gate = a ranking-framing error
    "headline 'slowest check' names the data layer's critical_path_check": "mis-ranked-lever",  # headline doesn't match the measured critical path
    "headline reconciles a non-universal slowest check with the population floor": "mis-ranked-lever",  # a non-universal check headlined as the typical wait without the presence caveat = a critical-path framing error
    "presence-causal headline only when the slowest check is minority-present": "fabricated-or-unsupported-finding",  # headline blames the floor drop on presence the populations contradict
    "chain headline re-derives from the stamped chain facts": "mis-ranked-lever",  # ENG-1 PR-N2: chain headline drifted from chain_facts
    "rendered chain total is never below the largest member it sums": "fabricated-or-unsupported-finding",  # issue #22: a rendered total below a member it sums is a number the data contradicts
    "headline merge-wait is never above the measured makespan p50": "estimated-not-measured",  # issue #24: a crowned/modeled wait above the MEASURED wall — the estimate overstates the measurement
    "each finding's runner-minute saving is within its jobs' measured compute": "estimated-not-measured",  # issue #25: a modeled saving above the job's measured compute — an estimate exceeding its physical bound
    "every credited runner-minute saving carries a measured-basis stamp (sizing door)": "estimated-not-measured",  # issues #43/#44/#45: a saving that skipped the measured sizing door (no basis / unmeasured path) — an estimate with no measured grounding
    "cluster-floor lever's ceiling floors at a NON-sibling check (not its own matrix leg)": "mis-ranked-lever",  # issue #44: a cluster lever capped by its own sibling leg renders ~15x under its true ceiling and is buried — a ranking/placement error
    "Bottom-line headline leads with the stamped cluster-floor ceiling (no burial)": "mis-ranked-lever",  # issue #49: the bottom line headlines a sibling-capped per-leg win while the larger stamped cluster ceiling is buried in Also noticed — a ranking/placement error
    "Bottom-line crowned cluster lever is presence-eligible (no minority-workflow crown)": "mis-ranked-lever",  # issue #56: a minority-present (2/20 workflow) cluster crowns the typical-PR bottom line over pole 1's honest ceiling — a presence-weighting ranking error
    "Bottom-line crowned cluster lever is on the merge-gating spine (not off-spine)": "mis-ranked-lever",  # issue #114: an off-spine cluster (its jobs dropped from the required-scoped spine, but its workflow hosts a required check) crowns the typical-PR headline over the real gating levers — a gating-path ranking error
    "headline leads with the observed wall when the chain sum diverges from the makespan": "mis-ranked-lever",  # issue #115: the chain-sum leads the "typical PR waits" headline while the measured makespan wall is materially bigger (queue gaps) — the wrong measured figure crowns the wait
    "every file-backed structural lever carries a measured dominant step (no name-inferred OPT75)": "estimated-not-measured",  # a lever asserted without a measured step
    "no payload-bearing step is binned as `build` (redundant-work inflation → OPT72 misroute)": "mis-ranked-lever",  # a payload step mis-binned as build inflates redundant-ratio and routes the pole to the wrong pattern (OPT72 not OPT75)
    "crowned detector leaf agrees with the pole's dominant measured category (no off-category ceiling)": "fabricated-or-unsupported-finding",  # issue #16: a leaf crowning a MEASURED CAUSE the pole's own dominant-step data contradicts (lint fix on a test-dominant pole)
    "pole addressable ceiling within the co-occurrence floor": "estimated-not-measured",  # an overstated ceiling not grounded in measured co-occurrence
    "recoverable ceiling above the typical wait carries a worst-case reconciliation": "fabricated-or-unsupported-finding",  # issue #66 fix 2: a ceiling that exceeds the typical wait, rendered without the caveat that reconciles it, reads as a fix giving back more than a typical PR waits
    "no drilled pole measures a pre-change config era without the era disclosure": "coverage-gap-dead-end",  # issue #66 fix 1: a retired-config measurement presented as current, with no era disclosure — a staleness/honesty gap
    "check enumeration is bound to the kept config era (no other-config check leaks in)": "fabricated-or-unsupported-finding",  # issue #69: a post/pre-era-only check rendered as a pole/bar beside the kept era's timings = a configuration that never ran, presented as measured
    "the rendered era disclosure matches the enumerated config (no pre-only caveat over post-era checks)": "fabricated-or-unsupported-finding",  # issue #74: a "measures the previous configuration" disclosure rendered over an all-post measurement — the report's own caveat contradicts what it enumerates
    "the per-PR chain/makespan spine is bound to the kept config era (no dropped-era PR blends in)": "fabricated-or-unsupported-finding",  # issue #80: a dropped-era PR's timing feeds the "typical PR waits N" makespan headline under a disclosure claiming the kept era — a measurement of a config that isn't the one disclosed
    "every drilled pole's binding floor is disclosed on the spine (no silent heavy-check drop)": "coverage-gap-dead-end",  # an undisclosed gap
    "rendered per-workflow gate frequency matches the populations re-derivation": "fabricated-or-unsupported-finding",  # a rendered frequency not backed by the re-derivation
    "no drilled pole's job is also framed as an 'Also noticed' minor-cleanup finding": "mis-ranked-lever",  # a major pole reframed as minor = a ranking error
    "TOC 'Also noticed' pointer's on/off-path label matches the appendix": "fabricated-or-unsupported-finding",  # a rendered label not backed by the appendix
    "every rendered cache claim carries a cross-run cache distribution": "estimated-not-measured",  # a claim without the backing distribution
    "cache finding's framing matches its measured hit-rate distribution": "estimated-not-measured",  # framing not grounded in the measured distribution
    "headline critical-path check is an actual recurring gate": "mis-ranked-lever",       # headline names a non-recurring (non-)gate
    "every 'slowest ... waits on' framing phrase is a registered claim": "fabricated-or-unsupported-finding",  # a rendered framing sentence not backed by a registered claim
    "Tier-2 R-rows carry re-derived wall-clock-neutral certificates": "fabricated-or-unsupported-finding",  # a promoted row's proof is not backed by the data
    "Tier-2 R-rows use measured sizing basis": "estimated-not-measured",  # exact match: promoted bill rows must be measured, not modeled
    "Tier-2 section-lead totals match de-overlapped findings": "fabricated-or-unsupported-finding",  # rendered totals not backed by findings
    "no claim or prose cites the closing-down /timing endpoints": "fabricated-or-unsupported-finding",  # cites a data source the pipeline does not use
    "Tier-2 claims carry the jobs-API derivation-basis field": "fabricated-or-unsupported-finding",  # a Tier-2 claim without its machine-readable data-source field
    "Tier-2 headline claim names the top stamped finding": "mis-ranked-lever",  # top bill-axis claim names the wrong ranked row
    "Tier-2 savings rows are backed by runner-minute cost spine": "fabricated-or-unsupported-finding",  # rendered savings rows not backed by cost-spine rows
    "runner-minute cost spine source block is re-derivable": "fabricated-or-unsupported-finding",  # cost-spine source rows/totals not backed by rates/stamps
    "no rate-derived dollars on the minutes surfaces": "fabricated-or-unsupported-finding",  # a rate-derived $/USD figure leaked onto a minutes-only surface
    "shallow cost-spine sample is disclosed (re-derived from the stamped workflows)": "coverage-gap-dead-end",  # an undisclosed shallow sample reads as exact coverage
    "the fetch plan matches its call sites (no unconsumed prefetches)": "other",  # a drifted prefetch plan wasted API budget; a collection-orchestration bug, no data-faithfulness enum fits
    "Skip-family prompts carry the required-check Pending caveat": "unscrubbed-or-disclosure-risk",  # a skip lever shipped without the Pending warning can block merges — a missing mandatory disclosure
    "no fileless/managed status check crowns the headline (disclosed as PR-lifetime latency)": "mis-ranked-lever",  # issue #12: a fileless PR-lifetime span crowning the merge-wait headline over the real job-groundable gate = a ranking error
    # EXCLUDE — the harness names the report file, not the skill: a pure environment artifact.
    "scanned date matches the date in the filename": "tooling-or-environment",
    # TRIAGE — real-when-adjudicated defect shapes.
    "no ci-secure template leakage (security-domain framing in ci-speedup prose)": "unscrubbed-or-disclosure-risk",  # exact match
    "report's skill commit is HEAD or an ancestor of it (provenance)": "tooling-or-environment",  # branch/dirty-tree coupled — an environment issue
}
# The consumer-divergence probe (locus-less, no verify_report Check.name) — a ranking mismatch
# between the headline gate and the consumer's pole pick.
_DIVERGENCE_CLASS = "mis-ranked-lever"


# Fixed, repo-INDEPENDENT slug for locus-less seeds: two reports tripping the same check must
# produce the SAME signature so they dedup to one bug (the cross-org `seen[sig]` map then collects
# both repos under it). The `@check:` infix keeps this namespace disjoint from the LLM bugs'
# `<slug>@<file>:<symbol>` form (which has no `check:` after the `@`).
_SEED_SLUG = "grader-seed"

# Renderer module the grader checks ultimately implicate — an HONEST, non-fabricated
# suspected_location for a locus-less seed (the precise function is for the fix agent to pin).
_RENDERER = "skills/ci-speedup/scripts/blocking_path.py"

# A few standalone verifier checks fail before markdown rendering is involved. Keep their
# signatures locus-less for cross-repo dedup, but point triage at the producer/verifier surface that
# is most likely responsible instead of defaulting every seed to `blocking_path.py`.
_CHECK_SUSPECTED_LOCATION = {
    "runner-minute cost spine source block is re-derivable": (
        "skills/ci-speedup/scripts/collect_runs.py "
        "(runner_minute_spine source-block producer; verifier contract in "
        "skills/ci-speedup/tests/verify_report.py)"),
    "no payload-bearing step is binned as `build` (redundant-work inflation → OPT72 misroute)": (
        "skills/ci-speedup/scripts/collect_runs.py "
        "(_step_category / _STEP_CATEGORY_RES fine-grained step classifier; verifier contract in "
        "skills/ci-speedup/tests/verify_report.py)"),
}


def _slug_check(name: str) -> str:
    """A stable token from a Check.name (lowercase, non-alphanumerics → single `-`). Changes only
    when the Check.name itself changes — which `test_every_check_is_classified` would catch."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def classify(check_name: str) -> str:
    """Disposition for a `verify_report` Check.name. Raises KeyError (loud, never a silent default)
    for an unclassified check so a new check can't slip through unseeded AND unexcluded."""
    try:
        return TRIAGE_ALLOWLIST[check_name]
    except KeyError:
        raise KeyError(
            f"unclassified verify_report check {check_name!r} — add it to grader_seeds."
            "TRIAGE_ALLOWLIST (AUTO_SEED / TRIAGE / EXCLUDE) before it can seed")


def seed_signature(check_name: str | None = None, locus: str | None = None,
                   slug: str = _SEED_SLUG) -> str:
    """The dedup signature for a seed. locus-BEARING → `<slug>@<file>:<symbol>` (joins LLM bugs);
    locus-LESS → `<_SEED_SLUG>@check:<slug-of-check>` (joins other grader seeds, repo-independent)."""
    if locus:
        return f"{slug}@{locus}"
    return f"{_SEED_SLUG}@check:{_slug_check(check_name or '')}"


def _seed_bug(title: str, detail: str, signature: str, *, locus: str | None,
              suspected_location: str | None = None,
              bug_class: str | None = None) -> dict:
    """A seed in the FULL audit-bug schema `{title, severity, suspected_location, evidence,
    signature, class}` (+ a `source` tag; the AUDIT schema is open). A locus-less seed is filed
    `needs-triage` (no honest locus to pin); a locus-bearing one carries that locus + `medium`.
    `bug_class` (PR-B) is the closed-vocab CLASS_ENUM label — always provided by both call sites
    below (`CHECK_CLASS` / `_DIVERGENCE_CLASS`), so a bare `_seed_bug(...)` call omitting it is a
    caller bug, not a supported "no class" case; still guarded (never emits an invalid enum value)
    rather than crashing a grade over a missing label."""
    bug = {
        "title": title,
        "severity": "medium" if locus else "needs-triage",
        "suspected_location": (
            locus
            or suspected_location
            or f"{_RENDERER} (renderer; exact locus to triage — grader-sourced)"),
        "evidence": detail,
        "signature": signature,
        "source": "grader-seed",
    }
    if bug_class in CLASS_ENUM:
        bug["class"] = bug_class
    return bug


def seed_from_check(check, *, locus: str | None = None, slug: str = _SEED_SLUG) -> tuple[str, dict | None]:
    """(disposition, seed-or-None) for one `verify_report` Check. None unless it is a non-skipped
    FAIL of an AUTO_SEED or TRIAGE check; EXCLUDE / SKIP / PASS never produce a seed dict.

    `slug` is threaded to `seed_signature` so a future locus-BEARING caller (one passing a real
    `file:symbol` `locus`) gets `<slug>@<file>:<symbol>` — the LLM-audit-bug namespace it must
    dedup against. It is inert on the locus-LESS path every current callsite uses (that form is
    always `grader-seed@check:<…>`, repo-independent by design)."""
    if check.skipped or check.ok:          # SKIP and PASS never seed
        return ("skip" if check.skipped else "pass", None)
    disp = classify(check.name)
    if disp == EXCLUDE:
        return (EXCLUDE, None)
    bug = _seed_bug(
        title=f"[grader] {check.name}",
        detail=f"verify_report FAIL: {check.detail}",
        signature=seed_signature(check_name=check.name, locus=locus, slug=slug),
        locus=locus,
        suspected_location=_CHECK_SUSPECTED_LOCATION.get(check.name) if not locus else None,
        bug_class=CHECK_CLASS.get(check.name))
    return (disp, bug)


def seed_from_divergence(diverges: bool, detail: str) -> dict | None:
    """A consumer-divergence verdict → a TRIAGE candidate (NOT an auto-seed), or None when the
    report does not diverge. Routed to `triage` (the agent adjudicates) rather than `seeds` because
    `_consumer_divergence` is a deliberately CRUDE proxy that over-counts: the seal effort measured
    ~44% job-base proxy divergence vs only ~13% real `ci-harness`-pick divergence (below its 15%
    materiality gate), and concluded most of it is immaterial frequency-vs-p50 ordering or a
    harness-side bot mis-pick — NOT a skill bug. Auto-seeding every proxy hit would fan a fix agent
    at ~half the fleet for mostly non-issues, so the agent must judge a divergence real before it
    becomes a bug. Locus-less (the renderer's headline gate vs the stamped `critical_path_check`)."""
    if not diverges:
        return None
    return _seed_bug(
        title="[grader] consumer-divergence: headline gate != consumer pole pick",
        detail=f"measure_contradictions consumer-divergence (PROXY — over-counts; adjudicate before "
               f"treating as a skill bug): {detail}",
        signature=seed_signature(check_name="consumer-divergence"),
        locus=None,
        bug_class=_DIVERGENCE_CLASS)


def collect_seeds(checks, divergence: tuple[bool, str] | None = None) -> dict:
    """Drive the mapping over a `run_checks` list (+ an optional `_consumer_divergence` verdict)
    into `{seeds, triage, excluded, skipped, divergence}`:
      - seeds   — AUTO_SEED FAILs the agent appends to its `bugs` verbatim.
      - triage  — TRIAGE FAILs AND a consumer-divergence the agent ADJUDICATES (include only if a
                  real skill bug); these are never auto-fanned to a blind fix. The divergence rides
                  the crude proxy (see `seed_from_divergence`), so it belongs here, not in seeds.
      - excluded — names of EXCLUDE checks that FAILed, recorded for transparency, never seeded.
      - skipped  — `[{name, disposition}]` for every check that SKIPPED (no-silent-drops: a check
                   that SKIPPED is "I couldn't check", NOT "clean" — an AUTO_SEED check that skipped
                   on `findings unreadable` is a COVERAGE GAP, not a pass. Recording it keeps that
                   visible instead of vanishing).
      - divergence — the probe's status: `{ran: false, reason}` when it did not run (findings
                   unavailable/unreadable → `divergence=None`), else `{ran: true, diverges, detail}`.
                   So "probe ran and the report is clean" is distinguishable from "probe was skipped"
                   (both previously showed as simply no divergence entry — a silent coverage loss).
    Pure + deterministic (fixture-replay-tested)."""
    seeds: list[dict] = []
    triage: list[dict] = []
    excluded: list[str] = []
    skipped: list[dict] = []
    for c in checks:
        disp, bug = seed_from_check(c)
        if bug is None:
            if disp == EXCLUDE:
                excluded.append(c.name)
            elif c.skipped:
                # Record a SKIPPED check as a coverage gap ONLY if it COULD have produced a finding
                # (AUTO_SEED / TRIAGE / unclassified). An EXCLUDE check that skips never seeds either
                # way, so its skip is NOT a coverage gap — omit it (else skipped[] carries noise the
                # agent would chase for nothing). `.get` (not `classify`) so a hypothetical
                # unclassified skipped check is still recorded as "unclassified" (visible) rather than
                # crashing — `test_every_check_is_classified` is the real guard against that.
                disp_name = TRIAGE_ALLOWLIST.get(c.name, "unclassified")
                if disp_name != EXCLUDE:
                    skipped.append({"name": c.name, "disposition": disp_name})
            continue
        (triage if disp == TRIAGE else seeds).append(bug)
    if divergence is None:
        div_status = {"ran": False,
                      "reason": "divergence probe not run (findings unavailable/unreadable)"}
    else:
        diverges, detail = divergence
        div_status = {"ran": True, "diverges": bool(diverges), "detail": detail}
        d = seed_from_divergence(diverges, detail)
        if d is not None:
            triage.append(d)   # adjudicated, not auto-seeded — the proxy over-counts
    return {"seeds": seeds, "triage": triage, "excluded": excluded,
            "skipped": skipped, "divergence": div_status}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Map ci-speedup grader results into dogfood seed bugs.")
    p.add_argument("--report", required=True, type=Path)
    p.add_argument("--findings", required=True, type=Path,
                   help="findings JSON the report came from (REQUIRED — 5 checks SKIP without it)")
    p.add_argument("--skill-repo", type=Path,
                   help="skill git checkout, for the (TRIAGE) provenance check")
    args = p.parse_args(argv)

    # The dogfood agent parses THIS script's stdout JSON, so any grading crash (a bad report read,
    # a run_checks bug, a verify_report import failure) must STILL emit structured output — a bare
    # traceback would give the agent nothing and silently drop every seed. Wrap the whole grade in
    # one guard that, on an unexpected error, prints an empty result + a loud `error` and exits 1.
    try:
        vr = mc._load_verify_report()
        report = args.report.read_text(encoding="utf-8")
        checks = vr.run_checks(report, args.report, args.findings, skill_repo=args.skill_repo)
        try:
            findings = json.loads(args.findings.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            findings = None   # genuinely unreadable/unparseable → the probe did NOT run
        # A readable findings DID let the probe run — even an empty `{}` or a wrong-shape list, which
        # `_consumer_divergence` guards and reports as "not measurable". That is distinct from "not
        # run" (an unreadable file → `findings is None`). Keying on `is None` (not truthiness) keeps a
        # readable-but-empty `{}` from being mislabeled a non-run, mirroring the --single-report CLI.
        divergence = None if findings is None else mc._consumer_divergence(findings)
        result = collect_seeds(checks, divergence)
    except Exception as e:  # noqa: BLE001 — a crash must still be machine-readable, never a silent drop
        print(json.dumps({"seeds": [], "triage": [], "excluded": [], "skipped": [],
                          "divergence": {"ran": False,
                                         "reason": "divergence probe not run (grader crashed)"},
                          "error": f"grader_seeds crashed: {type(e).__name__}: {e}"}, indent=2))
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
