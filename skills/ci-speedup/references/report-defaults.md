# Report defaults review

Status: **awaiting owner approval**

This document records the choices behind the report's sampling, ordering, and
finding thresholds. A value marked **keep** has a reason we can defend to a
reader. A value marked **change** is a proposal until the owner approves this
document and the named public issue lands. Approval should cover every proposal,
including the values rather than only the direction of change.

## Gate sample

### `pr-gate-sample-size`

**Current value:** 20 runs per workflow (`collect_runs.py`, `--max-runs`).

**Report statements:** PR-gate P50/P90 durations, gate frequency, pole order,
and every claim that describes a check as typical or rare.

**Reason:** Twenty keeps the API cost modest, but it can represent one short
burst and is too small to support a stable ordering.

**Proposed decision:** **Change to at least 100 completed PR commits** under
#306 and #311.

### `pr-gate-eligibility`

**Current value:** candidate commits can still have queued or in-progress runs;
their wall time is excluded, but their check rows can feed gate frequency.

**Report statements:** sample completeness, "finishes last," gate frequency,
pole order, and the Bottom line.

**Reason:** A last-finisher cannot be known until every sampled run for the
commit finishes. Partial commits remain useful coverage evidence.

**Proposed decision:** **Change to completed PR commits only** under #306, and
disclose excluded in-flight commits rather than treating them as zero or clean.

### `pr-gate-span`

**Current value:** no minimum span; 20 observations can come from one hour.

**Report statements:** every frequency, interval, "typical PR," and pole-order
claim derived from the gate population.

**Reason:** Multiple days reduce sensitivity to one traffic burst, weekday, or
temporary path mix. Five days covers most of a working week.

**Proposed decision:** **Change to at least 5 distinct days** under #306 and
#311, and print the first and last sampled dates.

### `pr-candidate-pages`

**Current value:** 3 pages per workflow (`_PR_CANDIDATE_PAGE_CAP`).

**Report statements:** sample stop reason, candidate exhaustion, sample count,
time span, and every downstream gate-frequency claim.

**Reason:** A fixed cap prevents an unbounded walk, but three pages can be
consumed by incomplete or ineligible commits before reaching the target.

**Proposed decision:** **Change the cap to 12 pages** with an early stop once
100 eligible commits over 5 days are available. Approve the API cost ceiling
together with the sample target.

### `config-era-boundary`

**Current value:** a post-change era needs 6 observations to stand alone
(`_RARE_PRESENCE_MIN_PR`); candidate paging can also be shortened by a boundary
outside the workflow being measured.

**Report statements:** previous-configuration, current-configuration, and thin
era warnings, plus every post-era P50.

**Reason:** Mixing workflow definitions makes a timing population incoherent.
One workflow's change does not invalidate another workflow's history.

**Proposed decision:** **Change paging** under #306 so an era limits only its
own PR-triggered workflow. **Keep 6** as the minimum sufficient post-change
population pending a separate evidence-led review.

## Lookbacks and cohorts

### `rolling-volume-window`

**Current value:** 30 days (`collect_runs.py::_utc_window`).

**Report statements:** monthly run volume, runs per month, runner-minutes per
month, default-branch activity, and modeled monthly savings.

**Reason:** Thirty days is recent enough to reflect current CI and long enough
to include weekly or release-adjacent jobs. It is a rolling window, not a
calendar-month claim.

**Proposed decision:** **Keep 30 days** and print the exact endpoints when
available.

### `run-list-depth`

**Current value:** 100 all-status runs per workflow (`_COST_RUNLIST_MAX`).

**Report statements:** run-volume sampling, cost-spine inputs, push/PR lane
classification, retry evidence, and duplicate-run evidence.

**Reason:** One full GitHub API page is a bounded, cheap first view and avoids
silently accepting the API's smaller default page.

**Proposed decision:** **Keep 100.** Deeper walks need their own explicit cap
and coverage disclosure.

### `default-branch-push-cohort`

**Current value:** 10 commits (`_DEFAULT_BRANCH_PUSH_SAMPLE_MAX`).

**Report statements:** after-merge lane P50/P90, slowest jobs, and projected
push-lane savings.

**Reason:** Ten provides a bounded recent cohort without mixing the push lane
into the merge gate.

**Proposed decision:** **Keep 10.** Continue disclosing fetch failures and the
sample cap.

## Pole selection and order

### `rendered-pole-count`

**Current value:** 5 distinct workflow/job matrices (`_TOP_WORKFLOWS`).

**Report statements:** Long pole list, drill-down count, captured logs, and
hidden-pole coverage.

**Reason:** Five keeps the report readable while retaining several competing
paths; the collector captures the same number of drill logs.

**Proposed decision:** **Keep 5** and retain the collector/renderer equality
invariant.

### `holds-up-threshold`

**Current value:** a check is recurrent after finishing last on 2 sampled PRs
(`_POLE_RECUR_FLOOR`); current main has no explicit two-group presentation.

**Report statements:** recurring-gate framing and, after #305, membership in
"Checks that hold up most PRs."

**Reason:** Two rejects a single outlier, but it is too weak for a larger
population and can elevate roughly one-percent events.

**Proposed decision:** **Change to 5 last-finisher observations** under #305 and
#311. The interval and span must also support ordering.

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

### `bookkeeping-cutoff`

**Current value:** no general reader-facing gate-order cutoff. Detector-local
one-minute thresholds do not define developer-wait bookkeeping.

**Report statements:** which short jobs appear as developer-wait poles and which
are disclosed separately.

**Reason:** A short last-finisher can be bookkeeping, but duration alone cannot
demote a confirmed gate or a stage on its required chain.

**Proposed decision:** **Add a conditional 60-second cutoff** under #311: list a
short job separately only when it finished last and is outside the confirmed
gate chain; always retain a qualifying gate.

### `ranking-minimum-count`

**Current value:** no ranking floor beyond the 2-occurrence recurrence rule.

**Report statements:** pole 1, ranked close guidance, and Bottom-line gate
identity.

**Reason:** A check last only a handful of times has a wide frequency interval;
a precise order overstates the sample.

**Proposed decision:** **Require at least 5 last-finisher observations** under
#311, in addition to the multi-day and interval rules.

### `gate-frequency-interval`

**Current value:** none is displayed.

**Report statements:** "finishes last N/M," whether two checks are ranked, and
whether the chat close may name one above another.

**Reason:** A rough binomial interval shows when a small count difference is not
evidence of stable order without pretending to forecast future CI.

**Proposed decision:** **Add a two-sided 95% Wilson score interval** (`z = 1.96`)
under #311. Overlap makes the order unsupported while observations remain shown.

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

### `per-pattern-minimums`

**Current value:** representative report-bearing floors are OPT64: 3 retried
runs; OPT80: 6 occurrences and a 3x tail; OPT81: 8 samples per label and a 25%
gap; OPT82: 60 seconds; OPT83: 15% and 20 seconds; OPT84: 20 seconds; OPT79:
3 hits, 3 misses, and 20% waste.

**Report statements:** whether each named pattern is promoted, withheld, or
uncredited, plus its evidence and saving.

**Reason:** Each floor prevents a sparse observation from becoming a finding or
ranked saving while keeping the withheld reason visible.

**Proposed decision:** **Keep these current per-pattern minimums.** Each future
pattern must add its own named minimum and contract entry rather than borrowing
an unrelated global threshold.

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

### `rendered-row-caps`

**Current value:** 12 rows each for Also noticed, promoted runner-minute findings,
and the cost spine (`_ALSO_NOTICED_CAP`, `_TIER2_CAP`,
`_RUNNER_MINUTE_SPINE_CAP`).

**Report statements:** visible row counts and every "+N more" disclosure.

**Reason:** Twelve keeps each secondary section scannable while the hidden-row
count preserves coverage honesty.

**Proposed decision:** **Keep 12 / 12 / 12.** A cap may hide rows, never erase
them from findings or omit the hidden count.

## Machine-readable contract

The suite compares this block with named engine constants. Proposed values that
do not exist in current behavior remain in the decision sections until their
implementation lands.

```json report-defaults-contract
{
  "pr-candidate-pages": {"source": "collect_runs.py:_PR_CANDIDATE_PAGE_CAP", "value": 3},
  "config-era-minimum": {"source": "collect_runs.py:_RARE_PRESENCE_MIN_PR", "value": 6},
  "run-list-depth": {"source": "collect_runs.py:_COST_RUNLIST_MAX", "value": 100},
  "push-cohort": {"source": "collect_runs.py:_DEFAULT_BRANCH_PUSH_SAMPLE_MAX", "value": 10},
  "rare-presence-fraction": {"source": "collect_runs.py:_RARE_PRESENCE_FRAC", "value": 0.5},
  "pole-recurrence-floor": {"source": "collect_runs.py:_POLE_RECUR_FLOOR", "value": 2},
  "magnitude-wide-band": {"source": "collect_runs.py:_MAG_WIDE_REL", "value": 0.25},
  "cache-budget-cap": {"source": "collect_runs.py:_CACHE_BUDGET_FULL_FRACTION", "value": 0.9},
  "cache-tail-floor": {"source": "collect_runs.py:_CACHE_TAIL_MIN_FRAC", "value": 0.25},
  "opt64-retried-runs": {"source": "collect_runs.py:_OPT64_MIN_RETRIED_RUNS", "value": 3},
  "opt80-occurrences": {"source": "collect_runs.py:_OPT80_MIN_SAMPLED_OCCURRENCES", "value": 6},
  "opt80-tail-multiple": {"source": "collect_runs.py:_OPT80_TAIL_P95_MULTIPLE", "value": 3.0},
  "opt81-samples-per-label": {"source": "collect_runs.py:_OPT81_MIN_SAMPLES_PER_LABEL", "value": 8},
  "opt81-gap-fraction": {"source": "collect_runs.py:_OPT81_MIN_GAP_FRAC", "value": 0.25},
  "opt82-minimum-seconds": {"source": "collect_runs.py:_OPT82_MIN_LINT_P50_S", "value": 60.0},
  "opt83-minimum-share": {"source": "collect_runs.py:_OPT83_MIN_SHARE", "value": 0.15},
  "opt83-minimum-seconds": {"source": "collect_runs.py:_OPT83_MIN_STEP_S", "value": 20.0},
  "opt84-minimum-seconds": {"source": "collect_runs.py:_OPT84_MIN_STEP_S", "value": 20.0},
  "opt79-minimum-hits": {"source": "collect_runs.py:_OPT79_MIN_HITS", "value": 3},
  "opt79-minimum-misses": {"source": "collect_runs.py:_OPT79_MIN_MISSES", "value": 3},
  "opt79-minimum-waste": {"source": "collect_runs.py:_OPT79_MIN_WASTE_FRAC", "value": 0.2},
  "rendered-poles": {"source": "blocking_path.py:_TOP_WORKFLOWS", "value": 5},
  "chain-divergence-percent": {"source": "blocking_path.py:_CHAIN_MAKESPAN_DIVERGENCE_PCT", "value": 25.0},
  "also-noticed-rows": {"source": "blocking_path.py:_ALSO_NOTICED_CAP", "value": 12},
  "runner-minute-rows": {"source": "blocking_path.py:_TIER2_CAP", "value": 12},
  "cost-spine-rows": {"source": "blocking_path.py:_RUNNER_MINUTE_SPINE_CAP", "value": 12}
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
