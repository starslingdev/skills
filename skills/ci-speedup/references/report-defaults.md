# Report defaults review

Status: **awaiting owner approval**

This document records the choices behind the report's sampling, ordering, and
finding thresholds. A value marked **keep** has a reason we can defend to a
reader. A value marked **change** is a proposal until the owner approves this
document and the named public issue lands. Approval should cover every proposal,
including the values rather than only the direction of change.

## Gate sample

### `pr-gate-sample-size`

**Current value:** up to 20 selected PR commits
(`collect_runs.py::_REPR_PR_TARGET`). They may still have queued or in-progress
runs, as the eligibility entry below describes.
The unrelated `--max-runs` option bounds per-workflow timing history and does
not set this population.

**Report statements:** PR-gate P50/P90 durations, gate frequency, pole order,
and every claim that describes a check as typical or rare.

**Reason:** Twenty keeps the API cost modest, but it can represent one short
burst and is too small to support a stable ordering.

**Proposed decision:** **Change to at least 100 completed PR commits** under
#306 and #311. #315 centralizes the provisional value as
`pr_sample_contract.PR_SAMPLE_TARGET`; it remains awaiting owner approval.

### `pr-gate-eligibility`

**Current value:** candidate commits can still have queued or in-progress runs;
their wall time is excluded, but their check rows can feed gate frequency.

**Report statements:** sample completeness, "finishes last," gate frequency,
pole order, and the Bottom line.

**Reason:** A last-finisher cannot be known until every sampled run for the
commit finishes. Partial commits remain useful coverage evidence.

**Proposed decision:** **Change to completed PR commits only** under #306, and
disclose excluded in-flight commits rather than treating them as zero or clean.

**Approval status:** awaiting owner approval.
### `pr-gate-span`

**Current value:** no minimum span; 20 observations can come from one hour.

**Report statements:** every frequency, interval, "typical PR," and pole-order
claim derived from the gate population.

**Reason:** Multiple days reduce sensitivity to one traffic burst, weekday, or
temporary path mix. Five days covers most of a working week.

**Proposed decision:** **Change to at least 5 distinct days** under #306 and
#311, and print the first and last sampled dates. #315 centralizes the
provisional value in `pr_sample_contract.py`; it remains awaiting owner
approval.

**Approval status:** awaiting owner approval.
### `pr-candidate-pages`

**Current value:** 3 pages per workflow (`_PR_CANDIDATE_PAGE_CAP`).

**Report statements:** sample stop reason, candidate exhaustion, sample count,
time span, and every downstream gate-frequency claim.

**Reason:** A fixed cap prevents an unbounded walk, but three pages can be
consumed by incomplete or ineligible commits before reaching the target.

**Proposed decision:** **Change the cap to 12 pages** with an early stop once
100 eligible commits over 5 days are available. #315 centralizes the
provisional cap in `pr_sample_contract.py`. Approve the API cost ceiling
together with the sample target; the value remains awaiting owner approval.

### `config-era-boundary`

**Current value:** a post-change era needs 6 observations to stand alone
(`_RARE_PRESENCE_MIN_PR`); candidate paging is disabled when any config-era
fact exists, including a boundary outside the workflow being measured.

**Report statements:** previous-configuration, current-configuration, and thin
era warnings, plus every post-era P50.

**Reason:** Mixing workflow definitions makes a timing population incoherent.
One workflow's change does not invalidate another workflow's history.

**Proposed decision:** **Change paging** under #306 so an era limits only its
own PR-triggered workflow. **Keep 6** as the minimum sufficient post-change
population pending a separate evidence-led review.

**Approval status:** awaiting owner approval.

## Lookbacks and cohorts

### `rolling-volume-window`

**Current value:** 30 days (`collect_runs.py::_window_30d`).

**Report statements:** monthly run volume, runs per month, runner-minutes per
month, default-branch activity, and modeled monthly savings.

**Reason:** Thirty days is recent enough to reflect current CI and long enough
to include weekly or release-adjacent jobs. It is a rolling window, not a
calendar-month claim.

**Proposed decision:** **Keep 30 days** and print the exact endpoints when
available.

**Approval status:** awaiting owner approval.
### `run-list-depth`

**Current value:** 100 all-status runs per workflow (`_COST_RUNLIST_MAX`).

**Report statements:** run-volume sampling, cost-spine inputs, push/PR lane
classification, retry evidence, and duplicate-run evidence.

**Reason:** One full GitHub API page is a bounded, cheap first view and avoids
silently accepting the API's smaller default page.

**Proposed decision:** **Keep 100.** Deeper walks need their own explicit cap
and coverage disclosure.

**Approval status:** awaiting owner approval.
### `default-branch-push-cohort`

**Current value:** 10 commits (`_DEFAULT_BRANCH_PUSH_SAMPLE_MAX`).

**Report statements:** after-merge lane P50/P90, slowest jobs, and projected
push-lane savings.

**Reason:** Ten provides a bounded recent cohort without mixing the push lane
into the merge gate.

**Proposed decision:** **Keep 10.** Continue disclosing fetch failures and the
sample cap.

**Approval status:** awaiting owner approval.

## Pole selection and order

### `rendered-pole-count`

**Current value:** 5 distinct workflow/job matrices (`_TOP_WORKFLOWS`).

**Report statements:** Long pole list, drill-down count, captured logs, and
hidden-pole coverage.

**Reason:** Five keeps the report readable while retaining several competing
paths; the collector captures the same number of drill logs.

**Proposed decision:** **Keep 5** and retain the collector/renderer equality
invariant.

**Approval status:** awaiting owner approval.
### `holds-up-threshold`

**Current value:** a check is recurrent after finishing last on 2 sampled PRs
(`_POLE_RECUR_FLOOR`); current main has no explicit two-group presentation.
#318 provisionally keeps 2 as
`gate_frequency_contract.POLE_HOLDS_UP_MIN_COUNT`, while #317 provisionally
requires 5 as `gate_frequency_contract.GATE_MIN_COUNT_FOR_RANKING`.

**Report statements:** recurring-gate framing and, after #305, membership in
"Checks that hold up most PRs."

**Reason:** Two rejects a single outlier, but it is too weak for a larger
population and can elevate roughly one-percent events.

**Proposed decision:** **Change to 5 last-finisher observations** under #305 and
#311. The interval and span must also support ordering. The fixed 2 in #318 and
minimum 5 in #317 govern one ordering decision with two values; both remain
awaiting owner approval, and the implementations must converge after approval.

### `rare-presence-rule`

**Current value:** at most 50% presence is rare, but presence demotion is inactive
below 6 sampled PRs (`_RARE_PRESENCE_FRAC`, `_RARE_PRESENCE_MIN_PR`). Required
checks are exempt.

**Report statements:** minority/conditional labels, rare-pole caveats, and the
typical-PR floor.

**Reason:** The median PR does not run a check present on half or fewer of the
sample, while six observations guards against a one- or two-run guess.

**Proposed decision:** **Keep 50% and 6** for presence framing. Gate-group
membership uses last-finisher count and uncertainty as a separate rule.

**Approval status:** awaiting owner approval.
### `within-group-order`

**Current value:** legacy selection prioritizes typical/recurrent checks and then
duration; it does not promise two groups sorted by displayed P50.

**Report statements:** Long pole numbering, "longest first," the biggest-lever
link, and the Bottom-line check.

**Reason:** Within one reader-facing group, descending P50 is direct and visible
from the row itself.

**Proposed decision:** **Change to descending displayed P50 within each labelled
group** under #305. Equal P50 uses stable workflow/check identity only as a
deterministic tiebreaker.

**Approval status:** awaiting owner approval.
### `needs-chain-handling`

**Current value:** serial `needs:` members add to a measured chain; the chain
headline is bounded by observed makespan, with a 25% divergence threshold
(`_CHAIN_MAKESPAN_DIVERGENCE_PCT`).

**Report statements:** chain P50, stage numbering, addressable chain headroom,
and the Bottom-line chain explanation.

**Reason:** Serial stages form one path rather than competing parallel poles;
the makespan guard keeps the sum consistent with observed developer wait.

**Proposed decision:** **Keep the chain arithmetic and 25% divergence guard.**
After #305 the Bottom line also identifies Long pole 1, or names a chain that
contains it.

**Approval status:** awaiting owner approval.
### `bookkeeping-cutoff`

**Current value:** several reader-facing floors already shape related output:
triage recovery uses `_TRIAGE_WALLCLOCK_FLOOR_S = 90`; aggregate-gate handling
uses `_AGG_GATE_TRIVIAL_S = 30`; wall-clock saving presentation uses
`_WALL_CLOCK_LONG_POLE_FLOOR_S = 30`; and no-op classification uses
`_NOOP_FLOOR_S = 30`.
They do not yet form one general bookkeeping rule.

**Report statements:** which short jobs appear as developer-wait poles and which
are disclosed separately.

**Reason:** A short last-finisher can be bookkeeping, but duration alone cannot
demote a confirmed gate or a stage on its required chain.

**Proposed decision:** **Add a conditional 60-second cutoff** under #311: list a
short job separately only when it finished last and is outside the confirmed
gate chain; always retain a qualifying gate.

**Approval status:** awaiting owner approval.
### `ranking-minimum-count`

**Current value:** no ranking floor beyond the 2-occurrence recurrence rule.
#317 provisionally adds 5 through
`gate_frequency_contract.GATE_MIN_COUNT_FOR_RANKING`; #318 provisionally uses
2 through `POLE_HOLDS_UP_MIN_COUNT` for its first group.

**Report statements:** pole 1, ranked close guidance, and Bottom-line gate
identity.

**Reason:** A check last only a handful of times has a wide frequency interval;
a precise order overstates the sample.

**Proposed decision:** **Require at least 5 last-finisher observations** under
#311, in addition to the multi-day and interval rules. This is the same
owner decision as `holds-up-threshold`; both provisional values remain
awaiting owner approval.

### `gate-frequency-interval`

**Current value:** none is displayed.

**Report statements:** "finishes last N/M," whether two checks are ranked, and
whether the chat close may name one above another.

**Reason:** A rough binomial interval shows when a small count difference is not
evidence of stable order without pretending to forecast future CI.

**Proposed decision:** **Add a two-sided 95% Wilson score interval** (`z = 1.96`)
under #311. Overlap makes the order unsupported while observations remain shown.

**Approval status:** awaiting owner approval.

## Other output-shaping defaults

### `workflow-timing-depth`

**Current value:** job lists are fetched for the first 10 runs of each workflow
(`_SHALLOW_RUNS`), then the owners of at most 12 leading checks are deepened to
the full timing depth (`_DEEPEN_TOP_CHECKS`). Run lists and other measurements
remain at full breadth.

**Report statements:** workflow and check P50/P90 values, long-pole evidence,
and whether a row has enough observations for a measured saving.

**Reason:** A shallow pass bounds API cost while targeted deepening spends reads
on checks most likely to shape the report.

**Proposed decision:** **Keep 10 shallow runs and 12 deepened checks.** Both
values remain awaiting owner approval.

### `required-suite-eligibility`

**Current value:** when the required suite is observable, a representative PR
commit is eligible only when all observable required checks ran; the selection
is implemented by `collect_runs.py::_select_repr_shas`.

**Report statements:** sample size and stop reason, required-suite scope,
gate share, pole order, and the Bottom line.

**Reason:** Mixing docs-only or path-filtered PRs into a full-suite population
changes the denominator and can make required checks look rare.

**Proposed decision:** **Keep full-required-suite eligibility** and disclose
unobservable required checks. This semantic default remains awaiting owner
approval.

**Approval status:** awaiting owner approval.
### `typical-floor-thresholds`

**Current value:** when per-pole co-occurrence data is available, a floor needs
strictly more than 50% co-occurrence (`_FLOOR_COOCCUR_MAJORITY_FRAC`). When it
is unavailable, the fallback needs 80% presence relative to the most-present
sibling (`_FLOOR_NEAR_UNIVERSAL_FRAC`).

**Report statements:** typical-PR floor, recurrent chain framing, and which
check is treated as part of the common wait.

**Reason:** The floor should describe a common path rather than a minority
combination of checks.

**Proposed decision:** **Keep the more-than-50% co-occurrence path and its 80%
presence fallback.** Both values remain awaiting owner approval.

**Approval status:** awaiting owner approval.
### `severity-cutoffs`

**Current value:** 300 seconds is HIGH and 120 seconds is MEDIUM
(`_SEV_HIGH_S`, `_SEV_MED_S`).

**Report statements:** the colored severity dot on Long pole headings and their
Contents rows.

**Reason:** These cutoffs make the measured long-pole wait visually scannable
without changing generic finding severity, section order, or measured duration.

**Proposed decision:** **Keep 300 and 120 seconds.** Both values remain awaiting
owner approval.

**Approval status:** awaiting owner approval.
### `success-only-caveat`

**Current value:** the success-only caveat needs 10 runs and a 20% failure rate
(`_SUCCESS_ONLY_CAVEAT_MIN_RUNS`, `_SUCCESS_ONLY_CAVEAT_FAILURE_RATE`).

**Report statements:** whether a duration is labelled success-only and whether
failed-run behavior is called out beside the estimate.

**Reason:** Sparse failures should not dominate the report, while a sustained
failure share must be visible before a success-only timing is used.

**Proposed decision:** **Keep 10 runs and 20%.** Both values remain awaiting
owner approval.

**Approval status:** awaiting owner approval.
### `minimum-timed-runs`

**Current value:** a per-run compute mean needs 3 timed runs
(`_MIN_TIMED_RUNS`).

**Report statements:** compute means, monthly runner-minute projections, and
whether a saving is measured or withheld.

**Reason:** Fewer than three observations cannot show even basic run-to-run
variation.

**Proposed decision:** **Keep 3.** The value remains awaiting owner approval.

### `rare-push-lane`

**Current value:** fewer than 8 default-branch push runs in 30 days is treated
as rare (`_PUSH_LANE_RARE_PER_30D`).

**Report statements:** push-lane confidence, rarity caveats, and whether the
after-merge lane is promoted as a recurring opportunity.

**Reason:** A very small monthly push cohort should not carry the same weight as
a routinely exercised lane.

**Proposed decision:** **Keep 8 per 30 days.** The value remains awaiting owner
approval.

**Approval status:** awaiting owner approval.
### `lifecycle-minimum`

**Current value:** PR lifecycle analysis needs 10 measured PRs
(`pr_lifecycle.MIN_MEASURED`).

**Report statements:** lifecycle queue and merge-wait claims and their
insufficient-sample caveats.

**Reason:** Lifecycle distributions need a modest population before their
percentiles are presented as representative.

**Proposed decision:** **Keep 10.** The value remains awaiting owner approval.

### `merge-queue-proof-thresholds`

**Current value:** OPT98 requires 30 merges per day, 15 successful runs per
event, a queue/PR median ratio from 0.8 through 1.5, a 30-PR churn sample, and a
30-day window (`_OPT98_*`).

**Report statements:** whether the merge queue is said to repeat the full suite
and whether that result receives measured savings.

**Reason:** The finding should require both high merge pressure and enough
comparable PR and queue runs, while rejecting workflows that do materially
different work.

**Proposed decision:** **Keep 30 / 15 / 0.8 / 1.5 / 30 / 30 days.** Every value
remains awaiting owner approval.

### `timeout-recommendation-thresholds`

**Current value:** OPT57 treats 95% of GitHub's 360-minute default as near the
ceiling, needs 3 successful samples, recommends at least 15 minutes, adds a
10-minute buffer or 1.5x the observed P99, and accepts 15 minutes of kill grace
(`_OPT57_*`).

**Report statements:** timeout-risk findings, the recommended timeout, and
whether a failed job is described as reaching the platform ceiling.

**Reason:** Recommendations need successful duration evidence and headroom,
while remaining below the platform's hard stop.

**Proposed decision:** **Keep 95% / 3 samples / 15 minutes / 10 minutes / 1.5x
/ 15 minutes.** Every value remains awaiting owner approval.

### `dependency-cache-floor`

**Current value:** dependency-cache evidence needs a median miss rate of at
least 50% before it can support the corresponding cache claim
(`_DEPENDENCY_CACHE_FLOOR_PCT`).

**Report statements:** dependency-cache confidence, saving credit, and
withheld-evidence wording.

**Reason:** A cache that misses on fewer than half of the relevant runs is
mostly warm and should not receive cold-cache saving credit.

**Proposed decision:** **Keep 50%.** The value remains awaiting owner approval.

### `cold-cache-miss-threshold`

**Current value:** 99.5% is the cold-miss threshold
(`_CACHE_COLD_MISS_PCT`).

**Report statements:** cold-cache labels, cache saving credit, and the warning
that a cache is effectively never warm.

**Reason:** The label should be reserved for cohorts with virtually no useful
hits while allowing small measurement noise.

**Proposed decision:** **Keep 99.5%.** The value remains awaiting owner
approval.

**Approval status:** awaiting owner approval.
### `cache-partial-miss-floor`

**Current value:** 40% is the general partial-miss floor
(`blocking_path.py::_PARTIAL_MISS_FLOOR_PCT`).

**Report statements:** whether a cache is described as mostly warm rather than
churning, and whether its remediation can become the report's BIGGEST LEVER.

**Reason:** A cache whose miss share stays below two fifths is predominantly
warm; treating that cohort as churn would overstate both the problem and its
available saving.

**Proposed decision:** **Keep 40%.** The value remains awaiting owner approval.

**Approval status:** awaiting owner approval.
### `ceiling-minimum-gates`

**Current value:** the verifier needs 5 gating PRs to re-derive a stable saving
ceiling (`verify_report.py::_CEILING_MIN_GATING_PR`).

**Report statements:** whether a wall-clock saving ceiling is verified or
reported as thin-sample evidence.

**Reason:** A ceiling from only a few gating occurrences is too sensitive to
one commit.

**Proposed decision:** **Keep 5.** The value remains awaiting owner approval.

### `partial-majority-rule`

**Current value:** main has no centralized majority rule for the representative
sample. #315 provisionally uses more than one half through
`pr_sample_contract.PR_SAMPLE_PARTIAL_MAJORITY_NUMERATOR` and
`PR_SAMPLE_PARTIAL_MAJORITY_DENOMINATOR`.

**Report statements:** whether the sample is labelled mostly partial and
whether gate frequency may be headlined.

**Reason:** A population with more incomplete than complete commits cannot
support a confident last-finisher ranking.

**Proposed decision:** **Use strictly more than 1/2.** The #315 value remains
awaiting owner approval.

## Finding thresholds and caps

### `magnitude-noise-band`

**Current value:** a magnitude range wider than 25% of its median triggers a
wider cross-run probe (`_MAG_WIDE_REL`).

**Report statements:** whether a drilled log's magnitude is stable enough for a
short probe and whether more runs are sampled.

**Reason:** A quarter-of-median spread is large enough that three observations
cannot distinguish ordinary variation from warm/cold or other modes.

**Proposed decision:** **Keep 25%.** It widens evidence collection and does not
claim statistical significance.

**Approval status:** awaiting owner approval.
### `bimodal-noise-band`

**Current value:** at least 8 observations; each mode at least 30%; slow/fast
median ratio at least 1.6; slow median at least 60 seconds
(`_bimodal_split` defaults).

**Report statements:** fast/slow disclosure, minority-mode pole selection, and
worst-case reconciliation.

**Reason:** These gates reject lone outliers and sub-minute fluctuations while
retaining a substantial, visibly separated slow mode.

**Proposed decision:** **Keep 8 / 30% / 1.6x / 60 seconds.** A later change needs
corpus evidence because it changes which checks enter the spine.

**Approval status:** awaiting owner approval.
### `ten-percent-sizing-rules`

**Current value:** 10% modeled occurrence for OPT29, OPT35, OPT38, and OPT44 in
`collect_runs.py::_SIZING`.

**Report statements:** modeled runner-minute savings for merge-queue step skips,
failed-shard fallback, PR-edit triggers, and restrictive concurrency.

**Reason:** These are conservative placeholders for qualitative static findings,
not measured rates; the report labels them modeled and caps them to affected
compute.

**Proposed decision:** **Keep the four 10% rules for now**, but never describe
them as measured frequency. Replace each with measured or uncredited evidence
when a bounded observable cohort exists.

**Approval status:** awaiting owner approval.
### `per-pattern-minimums`

**Current value:** representative report-bearing floors are OPT64: 3 retried
runs; OPT79: 3 hits, 3 misses, 5 seconds and 20% waste, a 25% tail share, and
1 second of long-pole headroom; OPT80: 6 occurrences, a tail threshold equal to
`max(3 × P50, P50 + 30 seconds)`, 2 tail runs, 2 proven tail runs, a 20-second
gap, and at most 4 log probes;
OPT81: 8 samples per label, a 30-second and 25% gap, 50% covered time, and a
30-second cache-lever floor; OPT82: 60 seconds; OPT83: 15% and 20 seconds;
OPT84: 0% minimum share and 20 seconds.

**Report statements:** whether each named pattern is promoted, withheld, or
uncredited, plus its evidence and saving.

**Reason:** Each floor prevents a sparse observation from becoming a finding or
ranked saving while keeping the withheld reason visible.

**Proposed decision:** **Keep these current per-pattern minimums.** Each future
pattern must add its own named minimum and contract entry rather than borrowing
an unrelated global threshold.

**Approval status:** awaiting owner approval.
### `saving-caps`

**Current value:** modeled cache savings use at most 90% of the available cache
budget (`_CACHE_BUDGET_FULL_FRACTION`); cache-tail framing starts at 25% of
qualifying runs (`_CACHE_TAIL_MIN_FRAC`). Every saving is also bounded by
measured affected compute and wall-clock headroom.

**Report statements:** wall-clock and runner-minute totals, "up to" ceilings,
cache-tail wording, biggest-lever ranking, and the Bottom line.

**Reason:** A model must leave room for irreducible work and cannot exceed the
compute or critical-path wait observed in the same population.

**Proposed decision:** **Keep 90%, 25%, and the physical compute/headroom caps.**
Measured detectors may use stamped exact evidence but cannot cross physical
bounds.

**Approval status:** awaiting owner approval.
### `rendered-row-caps`

**Current value:** 12 rows each for Also noticed, promoted runner-minute findings,
and the cost spine (`_ALSO_NOTICED_CAP`, `_TIER2_CAP`,
`_RUNNER_MINUTE_SPINE_CAP`), plus 5 displayed withheld job names
(`_WITHHELD_JOBS_SHOWN`).

**Report statements:** visible row counts and every "+N more" disclosure.

**Reason:** Twelve keeps each secondary section scannable while the hidden-row
count preserves coverage honesty.

**Proposed decision:** **Keep 12 / 12 / 12 / 5.** A cap may hide rows, never erase
them from findings or omit the hidden count.

**Approval status:** awaiting owner approval.

## Machine-readable contract

The suite compares this block with named engine constants. Proposed values that
do not exist in current behavior remain in the decision sections until their
implementation lands.

```json report-defaults-contract
{
  "bimodal-noise-band": {
    "decision": "bimodal-noise-band",
    "symbols": [
      "collect_runs.py:_bimodal_split"
    ]
  },
  "bookkeeping-cutoffs": {
    "decision": "bookkeeping-cutoff",
    "planned_sources": {
      "gate_frequency_contract.py:GATE_BOOKKEEPING_CUTOFF_S": 60.0
    },
    "sources": {
      "blocking_path.py:_AGG_GATE_TRIVIAL_S": 30.0,
      "blocking_path.py:_WALL_CLOCK_LONG_POLE_FLOOR_S": 30.0,
      "collect_runs.py:_AGG_GATE_TRIVIAL_S": 30.0,
      "collect_runs.py:_NOOP_FLOOR_S": 30.0,
      "collect_runs.py:_TRIAGE_WALLCLOCK_FLOOR_S": 90.0,
      "verify_report.py:_VR_AGG_GATE_TRIVIAL_S": 30.0,
      "verify_report.py:_WALL_CLOCK_LONG_POLE_FLOOR_S": 30.0
    }
  },
  "ceiling-minimum-gates": {
    "decision": "ceiling-minimum-gates",
    "sources": {
      "verify_report.py:_CEILING_MIN_GATING_PR": 5
    }
  },
  "chain-divergence": {
    "decision": "needs-chain-handling",
    "sources": {
      "blocking_path.py:_CHAIN_MAKESPAN_DIVERGENCE_PCT": 25.0,
      "verify_report.py:_VR_CHAIN_DIVERGENCE_PCT": 25.0
    }
  },
  "cold-cache-miss-threshold": {
    "decision": "cold-cache-miss-threshold",
    "sources": {
      "collect_runs.py:_CACHE_COLD_MISS_PCT": 99.5,
      "verify_report.py:_VR_CACHE_COLD_MISS_PCT": 99.5
    }
  },
  "cache-partial-miss-floor": {
    "decision": "cache-partial-miss-floor",
    "sources": {
      "blocking_path.py:_PARTIAL_MISS_FLOOR_PCT": 40.0,
      "verify_report.py:_VR_PARTIAL_MISS_FLOOR_PCT": 40.0
    }
  },
  "config-era-minimum": {
    "decision": "config-era-boundary",
    "sources": {
      "blocking_path.py:_RARE_PRESENCE_MIN_PR": 6,
      "collect_runs.py:_RARE_PRESENCE_MIN_PR": 6,
      "verify_report.py:_VR_RARE_PRESENCE_MIN_PR": 6
    }
  },
  "dependency-cache-floor": {
    "decision": "dependency-cache-floor",
    "sources": {
      "collect_runs.py:_DEPENDENCY_CACHE_FLOOR_PCT": 50.0,
      "verify_report.py:_VR_CACHE_FLOOR_BY_FIX_KEY": {
        "dependency-cache": 50.0
      }
    }
  },
  "gate-frequency-interval": {
    "decision": "gate-frequency-interval",
    "planned_sources": {
      "gate_frequency_contract.py:GATE_INTERVAL_Z": 1.96
    }
  },
  "holds-up-threshold": {
    "decision": "holds-up-threshold",
    "planned_sources": {
      "gate_frequency_contract.py:GATE_MIN_COUNT_FOR_RANKING": 5,
      "gate_frequency_contract.py:POLE_HOLDS_UP_MIN_COUNT": 2
    },
    "sources": {
      "blocking_path.py:_POLE_RECUR_FLOOR": 2,
      "collect_runs.py:_POLE_RECUR_FLOOR": 2,
      "verify_report.py:_VR_POLE_RECUR_FLOOR": 2
    }
  },
  "lifecycle-minimum": {
    "decision": "lifecycle-minimum",
    "sources": {
      "pr_lifecycle.py:MIN_MEASURED": 10
    }
  },
  "magnitude-wide-band": {
    "decision": "magnitude-noise-band",
    "sources": {
      "blocking_path.py:_MAG_WIDE_REL": 0.25,
      "collect_runs.py:_MAG_WIDE_REL": 0.25
    }
  },
  "merge-queue-proof-thresholds": {
    "decision": "merge-queue-proof-thresholds",
    "sources": {
      "collect_runs.py:_OPT98_CHURN_SAMPLE": 30,
      "collect_runs.py:_OPT98_DIFFERENT_RATIO": 1.5,
      "collect_runs.py:_OPT98_MIN_MERGES_PER_DAY": 30,
      "collect_runs.py:_OPT98_MIN_RUNS": 15,
      "collect_runs.py:_OPT98_REPROVE_RATIO": 0.8,
      "collect_runs.py:_OPT98_WINDOW_DAYS": 30
    }
  },
  "minimum-timed-runs": {
    "decision": "minimum-timed-runs",
    "sources": {
      "collect_runs.py:_MIN_TIMED_RUNS": 3
    }
  },
  "opt64-retried-runs": {
    "decision": "per-pattern-minimums",
    "sources": {
      "blocking_path.py:_OPT64_MIN_RETRIED_RUNS": 3,
      "collect_runs.py:_OPT64_MIN_RETRIED_RUNS": 3,
      "verify_report.py:_VR_OPT64_MIN_RETRIED_RUNS": 3
    }
  },
  "opt79-minimums": {
    "decision": "per-pattern-minimums",
    "sources": {
      "collect_runs.py:_OPT79_MIN_HITS": 3,
      "collect_runs.py:_OPT79_MIN_MISSES": 3,
      "collect_runs.py:_OPT79_MIN_WASTE_S": 5.0,
      "collect_runs.py:_OPT79_MIN_WASTE_FRAC": 0.2,
      "collect_runs.py:_CACHE_TAIL_MIN_FRAC": 0.25,
      "collect_runs.py:_OPT79_POLE_MIN_HEADROOM_S": 1.0,
      "verify_report.py:_VR_OPT79_MIN_HITS": 3,
      "verify_report.py:_VR_OPT79_MIN_MISSES": 3,
      "verify_report.py:_VR_OPT79_MIN_WASTE_S": 5.0,
      "verify_report.py:_VR_OPT79_MIN_WASTE_FRAC": 0.2,
      "verify_report.py:_VR_OPT79_TAIL_MIN_FRAC": 0.25,
      "verify_report.py:_VR_OPT79_POLE_MIN_HEADROOM_S": 1.0
    }
  },
  "opt80-occurrences": {
    "decision": "per-pattern-minimums",
    "sources": {
      "collect_runs.py:_OPT80_MIN_SAMPLED_OCCURRENCES": 6,
      "collect_runs.py:_OPT80_MIN_TAIL_RUNS": 2,
      "collect_runs.py:_OPT80_MIN_PROVEN_TAIL_RUNS": 2,
      "collect_runs.py:_OPT80_MIN_GAP_S": 20.0,
      "collect_runs.py:_OPT80_LOG_PROBE_MAX": 4,
      "verify_report.py:_VR_OPT80_MIN_SAMPLED_OCCURRENCES": 6,
      "verify_report.py:_VR_OPT80_MIN_TAIL_RUNS": 2,
      "verify_report.py:_VR_OPT80_MIN_PROVEN_TAIL_RUNS": 2,
      "verify_report.py:_VR_OPT80_MIN_GAP_S": 20.0,
      "verify_report.py:_VR_OPT80_LOG_PROBE_MAX": 4
    }
  },
  "opt80-tail-multiple": {
    "decision": "per-pattern-minimums",
    "sources": {
      "collect_runs.py:_OPT80_TAIL_P95_MULTIPLE": 3.0,
      "collect_runs.py:_OPT80_TAIL_P95_ABS_S": 30.0,
      "verify_report.py:_VR_OPT80_TAIL_P95_MULTIPLE": 3.0,
      "verify_report.py:_VR_OPT80_TAIL_P95_ABS_S": 30.0
    }
  },
  "opt81-samples-and-gap": {
    "decision": "per-pattern-minimums",
    "sources": {
      "collect_runs.py:_OPT81_MIN_GAP_FRAC": 0.25,
      "collect_runs.py:_OPT81_MIN_GAP_S": 30.0,
      "collect_runs.py:_OPT81_MIN_SAMPLES_PER_LABEL": 8,
      "collect_runs.py:_OPT81_COVERED_FRAC": 0.5,
      "collect_runs.py:_OPT81_CACHE_LEVER_MIN_S": 30.0,
      "verify_report.py:_VR_OPT81_MIN_GAP_FRAC": 0.25,
      "verify_report.py:_VR_OPT81_MIN_GAP_S": 30.0,
      "verify_report.py:_VR_OPT81_MIN_SAMPLES_PER_LABEL": 8,
      "verify_report.py:_VR_OPT81_COVERED_FRAC": 0.5,
      "verify_report.py:_VR_OPT81_CACHE_LEVER_MIN_S": 30.0
    }
  },
  "opt82-minimum-seconds": {
    "decision": "per-pattern-minimums",
    "sources": {
      "collect_runs.py:_OPT82_MIN_LINT_P50_S": 60.0
    }
  },
  "opt83-minimums": {
    "decision": "per-pattern-minimums",
    "sources": {
      "collect_runs.py:_OPT83_MIN_SHARE": 0.15,
      "collect_runs.py:_OPT83_MIN_STEP_S": 20.0,
      "verify_report.py:_VR_OPT83_MIN_SHARE": 0.15,
      "verify_report.py:_VR_OPT83_MIN_STEP_S": 20.0
    }
  },
  "opt84-minimum-seconds": {
    "decision": "per-pattern-minimums",
    "sources": {
      "collect_runs.py:_OPT84_MIN_SHARE": 0.0,
      "collect_runs.py:_OPT84_MIN_STEP_S": 20.0,
      "verify_report.py:_VR_OPT84_MIN_SHARE": 0.0,
      "verify_report.py:_VR_OPT84_MIN_STEP_S": 20.0
    }
  },
  "partial-majority-rule": {
    "decision": "partial-majority-rule",
    "planned_sources": {
      "pr_sample_contract.py:PR_SAMPLE_PARTIAL_MAJORITY_DENOMINATOR": 2,
      "pr_sample_contract.py:PR_SAMPLE_PARTIAL_MAJORITY_NUMERATOR": 1
    }
  },
  "pr-candidate-pages": {
    "decision": "pr-candidate-pages",
    "planned_sources": {
      "pr_sample_contract.py:PR_SAMPLE_CANDIDATE_PAGE_CAP": 12
    },
    "sources": {
      "collect_runs.py:_PR_CANDIDATE_PAGE_CAP": 3
    }
  },
  "pr-sample-eligibility": {
    "decision": "pr-gate-eligibility",
    "symbols": [
      "collect_runs.py:_select_repr_shas"
    ]
  },
  "pr-sample-span": {
    "decision": "pr-gate-span",
    "planned_sources": {
      "pr_sample_contract.py:PR_SAMPLE_MIN_DISTINCT_DAYS": 5
    }
  },
  "pr-sample-target": {
    "decision": "pr-gate-sample-size",
    "planned_sources": {
      "pr_sample_contract.py:PR_SAMPLE_TARGET": 100
    },
    "sources": {
      "collect_runs.py:_REPR_PR_TARGET": 20
    }
  },
  "push-cohort": {
    "decision": "default-branch-push-cohort",
    "sources": {
      "collect_runs.py:_DEFAULT_BRANCH_PUSH_SAMPLE_MAX": 10,
      "verify_report.py:_VR_DEFAULT_BRANCH_PUSH_SAMPLE_MAX": 10
    }
  },
  "ranking-minimum-count": {
    "decision": "ranking-minimum-count",
    "planned_sources": {
      "gate_frequency_contract.py:GATE_MIN_COUNT_FOR_RANKING": 5,
      "gate_frequency_contract.py:POLE_HOLDS_UP_MIN_COUNT": 2
    },
    "sources": {
      "collect_runs.py:_POLE_RECUR_FLOOR": 2,
      "verify_report.py:_VR_POLE_RECUR_FLOOR": 2
    }
  },
  "rare-presence-rule": {
    "decision": "rare-presence-rule",
    "sources": {
      "blocking_path.py:_RARE_PRESENCE_FRAC": 0.5,
      "blocking_path.py:_RARE_PRESENCE_MIN_PR": 6,
      "collect_runs.py:_RARE_PRESENCE_FRAC": 0.5,
      "collect_runs.py:_RARE_PRESENCE_MIN_PR": 6,
      "verify_report.py:_VR_RARE_PRESENCE_FRAC": 0.5,
      "verify_report.py:_VR_RARE_PRESENCE_MIN_PR": 6
    }
  },
  "rare-push-lane": {
    "decision": "rare-push-lane",
    "sources": {
      "collect_runs.py:_PUSH_LANE_RARE_PER_30D": 8
    }
  },
  "rendered-poles": {
    "decision": "rendered-pole-count",
    "sources": {
      "blocking_path.py:_TOP_WORKFLOWS": 5
    }
  },
  "rendered-row-caps": {
    "decision": "rendered-row-caps",
    "sources": {
      "blocking_path.py:_ALSO_NOTICED_CAP": 12,
      "blocking_path.py:_RUNNER_MINUTE_SPINE_CAP": 12,
      "blocking_path.py:_TIER2_CAP": 12,
      "blocking_path.py:_WITHHELD_JOBS_SHOWN": 5,
      "verify_report.py:_VR_RUNNER_MINUTE_SPINE_CAP": 12,
      "verify_report.py:_VR_TIER2_CAP": 12,
      "verify_report.py:_VR_WITHHELD_JOBS_SHOWN": 5
    }
  },
  "required-suite-eligibility": {
    "decision": "required-suite-eligibility",
    "symbols": [
      "collect_runs.py:_select_repr_shas"
    ]
  },
  "rolling-volume-window": {
    "decision": "rolling-volume-window",
    "symbols": [
      "collect_runs.py:_window_30d"
    ]
  },
  "run-list-depth": {
    "decision": "run-list-depth",
    "sources": {
      "collect_runs.py:_COST_RUNLIST_MAX": 100
    }
  },
  "saving-caps": {
    "decision": "saving-caps",
    "sources": {
      "collect_runs.py:_CACHE_BUDGET_FULL_FRACTION": 0.9,
      "collect_runs.py:_CACHE_TAIL_MIN_FRAC": 0.25,
      "verify_report.py:_VR_CACHE_TAIL_MIN_FRAC": 0.25
    }
  },
  "severity-cutoffs": {
    "decision": "severity-cutoffs",
    "sources": {
      "blocking_path.py:_SEV_HIGH_S": 300.0,
      "blocking_path.py:_SEV_MED_S": 120.0
    }
  },
  "success-only-caveat": {
    "decision": "success-only-caveat",
    "sources": {
      "collect_runs.py:_SUCCESS_ONLY_CAVEAT_FAILURE_RATE": 0.2,
      "collect_runs.py:_SUCCESS_ONLY_CAVEAT_MIN_RUNS": 10
    }
  },
  "ten-percent-sizing-rules": {
    "decision": "ten-percent-sizing-rules",
    "sources": {}
  },
  "timeout-recommendation-thresholds": {
    "decision": "timeout-recommendation-thresholds",
    "sources": {
      "collect_runs.py:_GHA_DEFAULT_JOB_TIMEOUT_S": 21600.0,
      "collect_runs.py:_OPT57_DEFAULT_KILL_CEILING_S": 22500.0,
      "collect_runs.py:_OPT57_KILL_GRACE_S": 900.0,
      "collect_runs.py:_OPT57_MIN_TIMEOUT_S": 900.0,
      "collect_runs.py:_OPT57_NEAR_DEFAULT_TIMEOUT_S": 20520.0,
      "collect_runs.py:_OPT57_TIMEOUT_BUFFER_S": 600.0,
      "collect_runs.py:_OPT57_TIMEOUT_MULTIPLIER": 1.5,
      "verify_report.py:_VR_OPT57_DEFAULT_KILL_CEILING_S": 22500.0,
      "verify_report.py:_VR_OPT57_DEFAULT_TIMEOUT_MIN": 360.0,
      "verify_report.py:_VR_OPT57_MIN_SUCCESS_SAMPLES": 3,
      "verify_report.py:_VR_OPT57_MIN_TIMEOUT_S": 900.0,
      "verify_report.py:_VR_OPT57_NEAR_DEFAULT_TIMEOUT_S": 20520.0,
      "verify_report.py:_VR_OPT57_TIMEOUT_BUFFER_S": 600.0,
      "verify_report.py:_VR_OPT57_TIMEOUT_MULTIPLIER": 1.5
    },
    "symbols": [
      "collect_runs.py:_opt57_recommended_timeout_s"
    ]
  },
  "typical-floor-thresholds": {
    "decision": "typical-floor-thresholds",
    "sources": {
      "blocking_path.py:_FLOOR_COOCCUR_MAJORITY_FRAC": 0.5,
      "blocking_path.py:_FLOOR_NEAR_UNIVERSAL_FRAC": 0.8
    }
  },
  "within-group-order": {
    "decision": "within-group-order",
    "symbols": [
      "blocking_path.py:_render_core"
    ]
  },
  "workflow-timing-depth": {
    "decision": "workflow-timing-depth",
    "sources": {
      "collect_runs.py:_DEEPEN_TOP_CHECKS": 12,
      "collect_runs.py:_SHALLOW_RUNS": 10
    }
  }
}
```

## Approval checklist

- [ ] Approve or edit every keep/change decision above.
- [ ] Approve 100 completed PR commits over at least 5 days.
- [ ] Approve the 12-page cap with early stopping.
- [ ] Approve the conditional 60-second bookkeeping rule.
- [ ] Approve 5 last-finisher observations for supported ranking/grouping.
- [ ] Approve two-sided 95% Wilson intervals and overlap as unsupported order.
- [ ] Confirm #305, #306, and #311 may merge after their implementations match
      the approved values.
