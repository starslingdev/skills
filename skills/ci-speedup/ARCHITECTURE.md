# ci-speedup - Architecture

Contributor/maintainer-facing guide to how the `ci-speedup` skill is built
and why. Read `SKILL.md` first (the canonical contract); this doc explains the
implementation behind it so you can extend the skill safely.

The conceptual heart of the skill is the **wall-clock lever model** - a
cascade of physical bounds that decides whether a finding actually shortens a
developer's wait. It gets its own prominent section below.

## Contents

1. Purpose & scope
2. Pipeline overview (scan → collect_runs → render → gap-fill → verify) — incl. §2.1 adaptive run sampling, §2.1a config-era boundary, §2.2 fetch orchestration
3. Data model — findings.json
4. The two metric axes
5. The wall-clock lever model — a cascade of physical bounds
6. Admission gate & advisory routing
7. Evidence, provenance & verification
8. Reproducibility & determinism — incl. §8.1 what the data pass does NOT re-fetch
9. Testing strategy
10. Status of the planned bounds
11. The structural / critical-path track
12. The blocking-path report — incl. §12.0 the Long pole map cascade, §12.3 leaf detectors, §12.7 the coverage-gap fallback
13. Cross-references

## 1. Purpose & scope

ci-speedup audits a repository's GitHub Actions workflows against a 73-pattern
catalog — 67 **hygiene/data-driven** patterns (OPT1–OPT69, with gaps 10 and 67) plus 6 **structural /
critical-path** patterns (OPT70–OPT75, routed from the measured long pole; see
§11) — and produces a **root-cause-analysis** markdown report with **measured**
impact on two axes: developer wall-clock wait (the ranking axis) and
runner-minutes (the cloud bill). Detection, ranking, and every measured number
are deterministic — no LLM in detection, scoring, the critical-path spine, or
the cross-run checks. The one exception is the **gap-fill** (SKILL.md phase 4a):
when a drilled pole's log matches no catalog detector, the agent running the
skill writes a labelled, log-grounded root-cause reading so the pole still
yields a breakdown + prompt instead of a dead-end — it never touches detection,
ranking, or the magnitudes. ci-speedup does **not** prescribe fixes:
each finding ships a ready-to-paste agent prompt the user hands to their own
coding agent, which sees the repo and reasons out the fix. Everything here is
self-contained in this skill directory; the report links out only to the public
catalog and to GitHub job-log permalinks.

## 2. Pipeline overview

`findings.json` is the single artifact threaded through every stage. Each stage
reads it, enriches it in place, and atomically rewrites it (`.partial` →
rename), so a crash mid-write never replaces a good file.

```
 repo clone (.github/workflows/*.yml)
      │
      ▼
 ┌──────────────┐   static detectors (per-file / declarative / cross-workflow /
 │  scan.py     │   repo-file / source-grep). Parses the catalog, emits a
 │  (Phase 2)   │   finding per hit. Reports catalog_patterns_without_detector.
 └──────┬───────┘
        │ findings.json  (findings[], scanned_workflows, scan_incomplete, …)
        ▼
 ┌──────────────┐   gh sampling (_sample_runs, _monthly_volume) + data-driven
 │ collect_runs │   detectors (OPT24/25/43/48/51) + STRUCTURAL routing
 │  (Phase 3)   │   (OPT70-75: decompose long pole, required-checks, §11) +
 └──────┬───────┘   two-axis sizing (_SIZING / _size_finding) + the wall-clock
        │           bound cascade. --with-logs anchors cache findings in logs;
        │           --data-dir also captures the blocking-path drill bundle (§12).
        │           ── run.py orchestrates Phases 2-3 in ONE process ──
        ▼
 ┌──────────────┐   renders the measured-spine RCA report: a metadata header + a
 │ blocking_    │   Contents TOC, then the critical-path spine — per gating pole a
 │ path.py      │   gate → step-timeline → root-cause drill (ASCII), each ending in
 │  (Phase 4)   │   a ready-to-paste AGENT PROMPT (no prescribed fix). Queue
 └──────┬───────┘   time gets its own "Pre-start wait" wall-clock section;
        │           measured+certified, source-backed runner-minute findings
        │           render in "Runner-minute reductions (wall-clock-neutral)"; residual
        │           hygiene demotes to "Also noticed". Strips em-dashes at the
        │           render boundary.
        ▼
 ┌──────────────┐   LLM gap-fill (§12.7): for any drilled pole whose log matched
 │ agent +      │   NO catalog detector, the agent running the skill reads the
 │ --analysis   │   captured log and writes a grounded, LABELLED root-cause analysis
 │  (Phase 4a)  │   + tailored prompt, passed back via blocking_path.py --analysis.
 └──────┬───────┘   The ONLY non-deterministic step; never touches the measured
        │           numbers. No pole ships as a "no pattern matched" dead-end.
        ▼
 ┌──────────────┐   PASS/FAIL invariant checks on the rendered report + JSON
 │ verify_report│   (Phase 5): header arithmetic, anchors, no-emdash, provenance,
 └──────────────┘   every finding carries a prompt, diagnosis never dead-ends.
```

`run.py` collapses the deterministic phases (scan → collect_runs) into a single
process and one findings JSON, then prints a `summary.py` digest to stdout (the
gating resolution + addressable poles + the exact `blocking_path.py` render
command, so the agent acts on it instead of re-spelunking the JSON or re-probing
gh), and the agent calls `blocking_path.py` to render.

The report renders to an **internal/session path** beside the scratch
`findings.json` (`run.py`'s `--report-out` default) and is **rendered + verify-gated
there on every run**. Issue #18 made the artifact opt-in: it is surfaced into the
user's working tree (`./ci-speedup-findings-report.md`) only when they pick "save
the full report" at the phase-6 close — the honesty gate (verify_report) fires
regardless, so opting in only copies an already-verified `.md`.

**There is no fix-generation phase.** ci-speedup stops
at the measured root cause; the report's per-pole agent prompts are the
hand-off — the user pastes one into their own coding agent, which sees the repo
and reasons out the fix. Detection + the measured report are deterministic and
reproducible from the findings JSON; the only non-deterministic step is the
phase-4a LLM gap-fill for poles the catalog can't analyse (labelled + log-
grounded, never touching the measured numbers). `record_timing.py` lets the orchestrator
merge a measured duration for any non-scripted phase into the JSON's `timings`
block, so "where did the time go?" is answerable from the artifact alone.
`blocking_path.py` (§12) is the **single, data-first renderer**: it reads the
findings JSON plus an optional drill bundle (logs/timelines/magnitude samples)
and answers *"why is the merge slow?"* as a gate → step-timeline → root-cause
drill-down. (An earlier catalog-spine renderer, `report.py`, was retired when
the measured critical path became the report's spine; `blocking_path.py` is now
the only renderer.)

### 2.0 The gh choke point (`GhClient`) — no silent drops

Every GitHub byte the skill reads passes through `GhClient.json()` / `.text()`
in `collect_runs.py`, so the correctness rules that protect the *sample* are
stated once, there:

The one rule underneath all of them: **a truncated or incomplete sample must never
be presented as clean.** A failed fetch is a *coverage gap* (disclosed); an empty
result is a *fact* (rendered). Collapsing the first into the second is the skill's
cardinal bug, and it has a dozen possible sites — this is the list of them.

- **Full pagination, or a loud gap** (`_paginate`). The list endpoints
  (`…/actions/workflows`, `…/runs/{id}/jobs`, `…/commits/{sha}/check-runs`) are
  walked to completion. Requesting `per_page=100` and keeping page 1 silently
  truncated the sample: measured on better-auth/better-auth @ 6f20f44, check-runs
  reported `total_count = 103` and returned 100, so **3 checks vanished from the
  critical-path sample** — any of which could have been the merge pole, which would
  have made the computed gate wrong. The walk stops on a **short page** — one GitHub
  could not fill, which by definition ends the collection. `total_count` is a
  *warn-only cross-check*, never a stop: it is a field we don't control, and if an
  endpoint ever under-reports it (the `filter=all` jobs view is the one to worry
  about), stopping on `len(items) >= total_count` would silently drop pages 2+ —
  reintroducing the exact truncation this exists to prevent. Cost: one extra call on
  an endpoint whose item count lands exactly on a page boundary. Everything else
  FAILS the whole fetch (`None`, a disclosed gap), never a short-but-plausible list:
  a page that errors, a page that is malformed, and the `_GH_MAX_PAGES` ceiling —
  reaching that means the stop condition is broken, so the accumulated items are of
  unknown completeness and the honest answer is a gap, not a list. The one exception
  is a page 1 that is **explicitly** empty (`total_count == 0`, or the list key
  present and empty) — a genuinely empty collection, which every caller already reads
  as "nothing ran". A body that merely *lacks* the key is not evidence of emptiness
  and fails like any other malformed page: `_fixture_name` is lossy, so in replay a
  check-runs fixture can be served some other endpoint's body (`{"default_branch":
  "main"}` — no `check_runs` key, no `total_count`), and the old "no key and no
  total" guard turned that into `[]` with `errors == 0`: a clean critical path built
  on nothing. (Record mode now **raises** on a fixture-name collision rather than
  overwriting, and the one collision the pipeline actually issued — `created=>=` vs
  `created=<=`, whose operators both transliterated to `_` — is spelled out (`gte` /
  `lte`) so the two opposite windows can no longer share a file.)
  Page 1's endpoint string is byte-identical to the pre-pagination spelling, so the
  record/replay fixture corpus (`_fixture_name`) still resolves; page 2+ appends
  `&page=<n>` (a re-recorded corpus must therefore carry those pages).
- **A failed fetch never becomes an empty result** (`_list_workflows`, `_run_list`
  and the four run-list samplers over it). These returned `[]` on failure, which
  reads downstream as "this workflow has no runs" — so a rate-limited fetch made a
  workflow *vanish* from the audit while the report still rendered as though it had
  been measured, and a failed workflow-list made the audit believe the repo runs
  **nothing at all**. They now return `None`. A failed workflow list aborts the gh
  pass with a disclosed reason; a failed run list marks that workflow `unavailable`,
  skips the detectors that would otherwise clear it off an empty sample, and is
  disclosed **by name** (see the disclosure path below). A body that comes back
  MALFORMED — a dict with no (or a non-list) `workflow_runs` — is a failure too, not
  an empty workflow: `_run_list` used to launder that shape into `[]`, with `errors`
  unbumped and no gap noted, which is the same silent deletion by a different door.

- **The disclosure path: data → renderer → verify invariant.** A gap the collector
  records but the ARTIFACT doesn't show is not a disclosure. All three links are
  load-bearing, and the middle one is where this broke:
  1. **Data.** `collect()` stamps the two ways a workflow can leave the MEASURED
     sample — `data_sources.run_list_fetch_failures` (its run-list fetch died: no runs)
     and `data_sources.job_fetch_failures` (its per-run job fetches ALL died: runs, but
     no job timing) — plus `partial_reason` (the human sentence, which NAMES them) and
     `partial_kind` (the SEVERITY, as a machine-readable key: `sample_thinned` |
     `workflow_missing` | `collection_failed` | `gh_unavailable` | `static_only`). Both
     sentences come from ONE helper (`_partial_reason` / `_partial_kind`, fed the UNION
     of the two gap lists) — `collect()` re-stamps coverage at the end (more gh calls
     happen after the first stamp), and an earlier re-stamp that re-derived the sentence
     inline **clobbered** the by-name disclosure back down to a bare "N gh API call(s)
     failed during collection". Never re-derive a `data_sources` value a helper already
     owns. **All four keys are stamped on EVERY exit from `collect()`, even when empty**
     — an early return that omitted them made the verify invariant SKIP (see link 3).
     **`gh_unavailable` means gh is genuinely absent — NOT an API refusal.** `available()`
     is a `gh auth status` probe, and that probe makes a live API call to verify the
     token, so it ALSO returns non-zero when gh is installed and a token is stored but the
     API does not accept the credential — a secondary-rate-limit 403, a transport / 5xx
     error, or an invalid (expired / revoked / too-narrowly-scoped) token. Stamping that
     case `gh_unavailable` (a NOT-MEASURED kind) shipped a silent static-only report that
     read as a complete audit of a quiet repo while the collection had actually been
     refused. The up-front branch now asks `GhClient.diagnose_unavailability()` (OFFLINE
     `gh --version` + `gh auth token` probes, so the diagnosis can't itself be
     rate-limited): a present binary + present token STRING means `api_blocked` → the LOUD
     `collection_failed` kind (with `gh_available` True, since gh IS available — the API
     refused it), the same severity the mid-collection `gave_up` abort already uses; only
     a truly absent binary/token keeps the quiet `gh_unavailable`. The offline probes
     cannot tell a rate limit from a dead token (`gh auth token` proves a token is stored,
     never that the API would accept it), so the disclosure names both remedies — wait out
     a rate limit, or re-authenticate — rather than asserting one; and an ambiguous probe
     failure (a hung `gh auth token`, `TimeoutExpired`) also routes LOUD, never back to
     the quiet path that would re-open the silent drop.
  2. **Renderer.** `blocking_path._coverage_note` renders it at the severity the DATA
     says, branching on `partial_kind` and never string-matching the prose. Only
     `sample_thinned` may carry the minimizing suffix ("a few runs/jobs are absent
     … marginally fewer runs"); stapling that onto a `workflow_missing` /
     `collection_failed` reason ("NO workflow could be measured") had the report
     contradicting its own disclosure. The note fires whenever a `partial_reason` is set
     (not only when `gh_error_count` is non-zero — a gap is not always a failed CALL),
     and it NAMES every workflow in EITHER gap list, re-derived from the stamp. The
     static-only banner is the load-bearing one: "an archived, brand-new, or
     low-activity repo whose run history aged out" is a confident WRONG diagnosis of a
     broken fetch that invites the reader to conclude their CI is quiet. It is now
     gated on the **INVERTED** predicate `_measurement_is_broken` — the quiet-repo
     banner may render ONLY when the gh pass never ran (`static_only` / `gh_unavailable`,
     which the report already announces up top); EVERY other kind (including a merely
     thinned sample with no spine, and any severity key the renderer has never heard of)
     defaults to the loud "Collection FAILED — this is not a quiet repo" banner. Gating
     on `partial_kind == collection_failed` alone (as an earlier round did) closed the
     hole for ONE of the five kinds and left a total run-list wipeout — which stamps
     `workflow_missing` — rendering the dormant-repo lie under a passing verify gate.
     The no-static-findings sub-case (which returned "" → the bare one-line note) now
     renders the banner too.
  3. **Verifier.** `verify_report.check_run_list_gaps_named` fails any report that does
     not name every workflow in a non-empty gap list (both `run_list_fetch_failures`
     and `job_fetch_failures`). That is the invariant that makes the clobber
     uncatchable-again: the pre-existing gh-error guard only required the error COUNT to
     appear, which the clobbered sentence satisfied. It fails **CLOSED on a missing
     stamp** whenever the gh tier ran — it used to skip, and since the stamps were only
     written on the main path, the invariant SKIPPED on all six committed worked
     examples and ran only in its own unit tests (a guard that skips corpus-wide is a
     guard that isn't there). `workflow_activity: {"status": "unavailable"}` is a
     machine-readable stamp on findings.json for the same gap; no renderer reads it, and
     it is NOT the disclosure.

- **A job-fetch WIPEOUT cannot headline off a queue-inflated span.** When every per-run
  job fetch for a workflow fails, its `crit` is empty, so its checks would fall back to
  CHECK-RUN SPAN timing — which is queue-inflated (an 80s job whose check-run reads
  1871s, because the span starts at check CREATION). That inflated number can outrank
  the true gate and headline the report — off a value we failed to measure. So while any
  `job_fetch_failures` entry stands, a check with no sampled job timing is DROPPED from
  the critical-path spine (`_bar_queue_inflated_fallback`) rather than timed by its span.
  The bar is spine-wide because a check-run carries no workflow path to attribute it
  back to the wiped workflow; every wiped workflow is NAMED and `partial_kind` is severe,
  so the spine reads as incomplete instead of quietly headlining the wrong gate. With no
  wipeout, this is exactly the old fallback behaviour.

- **A sustained FAILURE ENDS the run; it never hangs it** (the global breaker). The
  attempt budget is per CALL, so on its own it bounds nothing in aggregate: with a
  bucket exhausted (or the API 5xx-ing, or hung) and no terminal condition, every
  remaining call burns its attempts, gives up — and the next starts fresh at attempt 1.
  Several hundred queued calls is HOURS of wall-clock, i.e. a fast wrong answer traded
  for a hang. The timeout case is the worst: each attempt costs the full `timeout` (60s
  json / 90s text), so a hung API is a multi-hour grind. So the "better to fail loudly
  than wait" rule is applied at the CLIENT: after `_GH_MAX_GIVEUPS` consecutive
  exhaustions of ANY retryable class (rate limit, 5xx, OR timeout — an earlier round fed
  the breaker rate limits only, which left the 5xx/timeout outage completely unbounded),
  or once cumulative backoff across BOTH the rate-limit and transient paths passes
  `_GH_TOTAL_BACKOFF_BUDGET_S`, the client is terminally `gave_up` — every later call
  short-circuits to `None` + a counted gap with no sleep and no subprocess, and
  `collect()` aborts through the SAME disclosed-coverage-gap path the workflow-list
  failure uses (`tiers_run: []`, `partial_kind: collection_failed`, scan.py's static
  findings intact). A success resets the consecutive count, so the breaker fires on a
  sustained failure, not on two unlucky calls. Measured over 200 endpoints: every class
  now trips in ≤ 6 subprocess calls.
- **An EXPECTED absence is not an error.** `allow_missing` (both `json()` and
  `text()`) means "a 404 here is expected" — an admin-only rulesets endpoint, or a
  job log GitHub has deleted (`retention-days`, default 90; a pinned
  `--created-before` window can sit entirely outside it). Before this, 4 guaranteed
  404s on better-auth's skipped jobs became `gh_error_count: 4` and a **phantom
  partial-coverage banner on a fully-successful run**, which devalues the banner
  exactly when it's real.
- **The log skip gates on whether the job STARTED, not on what it concluded**
  (`_job_has_log`). No log: a `skipped` job, a job still `queued`/`in_progress`
  (`conclusion: null` — in-flight runs ARE sampled), and a job cancelled *before it
  started*. A job cancelled MID-RUN **does** have a partial log that GitHub serves
  200 — and since the drill picks its representative job by DURATION and never
  filters on conclusion, a long cancelled job can BE the drilled pole. Skipping it
  on conclusion alone would delete that pole's drill-down: a coverage regression
  inside a coverage fix.
- **A rate limit is loud, retried, shared, and counted** (`_classify_gh_failure`,
  `_invoke`). A 429 / secondary-limit 403 used to be indistinguishable from a 404:
  no retry, no backoff, `None` returned, `logger.debug` (invisible at the default
  INFO level). A throttled run therefore shrank its own sample and still rendered a
  confident, plausible, **wrong** report. Now: every live call runs `gh api -i`, so
  the failed response's OWN `retry-after` / `x-ratelimit-reset` arrives with the
  failure — one request, no extra probe fired at the endpoint that just blocked us.
  Retried up to `_GH_MAX_ATTEMPTS`, floored at GitHub's documented one minute (an
  `x-ratelimit-reset` describes the PRIMARY bucket; a secondary block needs ≥60s
  regardless, or the whole attempt budget burns in seconds), jittered, capped at
  `_GH_MAX_BACKOFF_S`, and **published to every worker** (`_blocked_until`) so one
  thread's discovery pauses the shared fetch pool instead of eight threads hammering
  a blocked endpoint in lockstep. The rate-limit KEYWORD alone is decisive — it does
  not wait for the HTTP status to parse, since a false positive costs one retry and
  a false negative costs a silently truncated report. **`_classify_gh_failure` also
  reads the response HEADERS** `-i` already returned: a 403/429 carrying
  `x-ratelimit-remaining: 0` or a `retry-after` is a rate limit regardless of gh's
  prose. Without that, a 403 whose message gh renders without the keywords classified
  as `forbidden` — no retry, and on an `allow_missing` endpoint (every job log) not
  even counted: measured, 200 rate-limited job logs, 0 errors, silent. 5xx **and
  timeouts** (the data exists; we didn't get it) retried at 1/2/4s + jitter, and their
  waits are charged to the same run-wide backoff budget the breaker reads. Everything
  is logged at
  **WARNING**, and exhausting the retries counts toward `errors` **even on an
  `allow_missing` endpoint** — being blocked from data that exists is a real
  coverage gap, not an expected absence. This legibility is the precondition for
  raising the fetch concurrency: a wider pool is only safe if throttling can't hide.

### 2.1 Adaptive run sampling (shallow → deepen-to-convergence)

The dominant gh cost is one `GET /runs/{id}/jobs` per sampled run. Sampling every
workflow at full `--max-runs` (default 20) is wasteful: a check's *identity* is
stable within ~10 runs while only its exact p50 needs the full sample. So
`collect_runs` runs two passes (`--shallow-runs`, default 10, clamped ≥1):

1. **Shallow** — fetch jobs for the first `--shallow-runs` runs of every workflow
   (concurrent), and stash each workflow's un-fetched deeper runs.
2. **Deepen, iterating to convergence** — rank every candidate's *concurrent-check
   keys* (each job's p50, plus the long-pole p95 so a fast-median/high-tail bimodal
   gate surfaces) across the **eligible** workflows, deepen the owners of the top
   `_DEEPEN_TOP_CHECKS` (12 ≥ the renderer's `src[:9]` chart) to `--max-runs`,
   recompute, and re-rank — repeating until the top region is entirely full-depth
   (`_deepen_candidates`, `_deepen_check_keys`, the loop in `collect()`).

**Eligibility is full-breadth** (`events_by_wf ∩ _DEVELOPER_EVENTS`), NOT the
shallow `event_scope` — so a `[push, pull_request]` workflow whose newest runs are
all push can't be hidden from deepening and measured against the wrong run
population. Only the per-run JOB fetch is depth-gated; the run-list (1 call/wf),
`_monthly_volume`, events, and the PR-sha pool stay at full breadth.

When the PR check-run sample proves a declared-PR check ran on sampled PRs but
the workflow-job sample for that workflow still has no developer-event run,
`collect_runs` keeps the check in the PR spine using the sampled PR check-run
duration. It stamps the pole `timing_source=pr_check_runs` and withholds the
workflow-job step drill, because the only available job steps came from
`all-events` push/schedule timing and would fabricate a PR root cause. The
structural router follows the same rule: no OPT70/72/75 root cause is inferred
from those all-events job steps.

**Guarantee + the one honest limit.** Convergence makes the **gate, drilled poles,
the cross-workflow floor, and bimodal flags depth-invariant (== a full pass)** —
validated by Monte-Carlo subsampling of depth-20 ground truth on five repos
(gate/drill/floor/bimodal = 100%) plus end-to-end diff vs a forced full pass.
PR33/#174 adds the cost-axis equivalent after the PR19/PR32 panel gate passed:
`collect_runs` first builds the shallow `runner_minute_spine`, then
`_cost_deepen_candidates_from_spine` ranks workflows by summed
billable-equivalent minutes/month, fetches the selected workflows' remaining
sampled runs to `--max-runs`, and rebuilds the runner-minute source block from
that deeper sample. This deepens the cost-spine clone only; it does not mutate
the already-produced wall-clock timing, queue, or finding-level evidence.
**Off-path hygiene findings (OPT24/33/43/45) aggregate over ALL workflows**, so
for capped workflows outside the wall-clock-deepened set they still rest on the
shallow sample and are **approximate** (queue-time p90 can swing run-to-run).
The runner-minute source block tracks its own cost-spine full-depth/shallow
sets separately. This is *disclosed, never silent*: the provenance cost line
states the shallow/deepened split, and the report carries one ⚠️ "re-run with
`--shallow-runs <max>` to confirm" note whenever a capped workflow was left
shallow on either axis — normally inside the Pre-start-wait and/or Also-noticed
sections, and emitted standalone when neither of those host sections renders
(PR-P2/#186 closed that corner, where an honest render silently dropped the
note). The cost-spine half is verifier-enforced:
`verify_report.check_cost_spine_shallow_disclosed` re-derives the disclosure's
workflow count from `data_sources.cost_spine_shallow_workflows` (the names
list, never the count stamp), so a renderer regression that drops it is a
verify FAIL, not a style drift. `--shallow-runs >= --max-runs` forces a single
full pass. Provenance fields: `shallow_runs`,
`max_runs`, `shallow_capped`, `capped_workflows`, `pole_candidates`,
`deepened_workflows`, `deepen_converged`, `cost_deepen_candidate_workflows`,
`cost_deepened_workflows`, `full_depth_workflows`,
`shallow_remaining_workflows`, `cost_spine_full_depth_workflows`, and
`cost_spine_shallow_workflows`.

PR19 makes that threshold reproducible without changing shipped behavior:
`maintainers/ci-speedup/scripts/measure_bill_pole_convergence.py` compares an
adaptive findings JSON against a forced full-depth findings JSON, applies the
PR18 selector to the adaptive spine, and simulates replacing selected workflows
with full-depth rows. The default gate requires the selected workflows to cover
at least 95% of full-depth billable-equivalent minutes and the simulated
deepened total to sit within 5% absolute error of the full-depth total. It also
fail-closes unless the paired artifacts have the expected adaptive/full
`max_runs`/`shallow_runs` provenance, the same repo, the same non-empty pinned
sampling window, complete cost-spine coverage, and zero cost-spine job-fetch
failures. Missing or malformed panel artifacts, wrong-side filenames, and empty
panels are measurement failures, not clean passes.

PR35 adds the local discovery counterpart for bill-only gaps. When the
`collect_runs.py` CLI runs from a tracked maintainer source checkout and the
final `runner_minute_spine` is render-ready, it writes the top billable-minute
workflows that have no source-backed Tier-2 finding to
`.ci-speedup-gaps/bill-workflows/`. The ranking is the same cost-spine axis
used for bill-pole deepening: summed billable-equivalent minutes/month, with
top job rows and repo/commit/window provenance copied into `bill-gap.json`.
This does not render a report section and does not promote a detector. The
namespace is deliberately separate from log-backed phase-4b captures so
`draft_detector.py` ignores it until a later human-reviewed detector/test PR is
grounded in real logs or equivalent deterministic evidence.

PR20 adds the maintainer-local producer for those paired artifacts:
`maintainers/ci-speedup/scripts/produce_bill_pole_panel_artifacts.py`. It scans
each local panel checkout once, then runs `collect_runs.py` twice from the same
scan output with the shared pinned window: adaptive (`--max-runs 20
--shallow-runs 10`) and forced full-depth (`--max-runs 20 --shallow-runs 20`).
It verifies the checkout's GitHub origin/clean git root and writes a manifest
after a complete producer run; panel measurement requires that manifest and
checks the exact adaptive/full paths it declares, so a smoke subset or stale
retry cannot masquerade as a full-panel pass. Generated findings live under the
gitignored `.ci-speedup-bill-pole/`; the script is an input producer for the
PR19 gate, not a collection-behavior change.

**Run-list triage + relative recovery (skip the job fetch for workflows that can't hold the pole).** Before the
shallow pass spends a `GET /runs/{id}/jobs` on a workflow, its run-LIST (1 call/workflow,
already fetched) gives each run's wall-time for free (`run_started_at`|`created_at` →
`updated_at`). `_TRIAGE_WALLCLOCK_FLOOR_S` (90s) is a coarse, fetch-cheap **pre-filter**: a
workflow whose **slowest** sampled run finishes under it is provisionally triaged (its per-run
job fetch skipped). It **can** still be a concurrent sibling on the PR, so the triage stub
carries `concurrent_wall_p50` (its run-list wall) and the cross-workflow floor
(`wall_clock._concurrent_workflows`) keeps counting it — the floor is **preserved**, not
dropped, so a saving can't overstate by up to ~90s when the binding sibling is itself sub-90s.

The 90s constant is absolute, but the merge pole is relative — so on a **seconds-scale** repo
(measured gate at/under the floor) a sub-90s workflow CAN own a near-gate secondary pole. Once the
shallow pass has **measured the gate**, a RECOVERY pass (before `_deepen_candidates`) job-fetches
any triaged workflow whose run-list wall reaches `_TRIAGE_RECOVER_GATE_FRAC` (0.5) of the gate's
long pole, so it is ranked + drilled like any pole rather than dismissed — triage relative to the
measured pole rank, not the absolute constant. Disclosed via
`recovered_fast_workflows`/`recovered_fast_count`. A minutes-scale gate sits far above the floor,
so nothing under it qualifies and triage is fully preserved there (zero behavior change, zero extra
gh). If a recovery fetch yields no job timing (total fetch failure), the workflow stays triaged
with its stub floor intact, surfaced via `jobs_fetch_failures`, rather than mislabeled recovered
with empty data.

**Headline-crown recovery.** The relative-recovery pass references the *gate* long pole, which
is the rare-giant's p50 when a heavy pole is present-but-minority. So on a repo where required
checks are unreadable AND every heavier pole is minority-present, the crown (`critical_path_check`
= the slowest **typical** check) can fall to a sub-floor lint that is still triaged — its wall
never reaches 0.5 of the giant gate. The report would then HEADLINE a workflow it also discloses
as triaged-fast, and that headline pole dead-ends ("no captured log" / "NO CATALOG PATTERN
MATCHED"). So after the spine is ranked, `_crown_recovery_wf` resolves the crown to its workflow
(via the scanned job graph, since the timing mapper misses a triaged workflow) and, if that
workflow is triaged with retained runs, job-fetches it once — un-triaging the headline so it is
drillable and dropping it out of `triaged_fast_workflows`/into `recovered_fast_workflows`. Bounded
and single-shot (one workflow, no re-rank: a fast-lint crown's job p50 ~= its check-run p50, so
its rank is unchanged — only its per-step drill was missing). No-op on a healthy repo (the crown
is never triaged) and on a fileless/external crown (no workflow to recover — a legitimate no-drill
headline).

Conservative on purpose: gated on the **max** wall-time over the sampled window (a workflow
that ran long anywhere in that window is fetched), the floor is far below any plausible pole,
and a workflow with **any** sampled run of unknown duration (missing/unparseable timestamps)
is fetched rather than triaged — so a fast run can't mask an unmeasured long one — so the
gate/pole/floor/bimodal stay exact (validated by measured before/after: on a
dogfood repo this cut **total gh calls ~23%, 408 → 314**, with an unchanged pole). Only the
skipped workflow's
job-level hygiene/queue degrade to run-list-only, disclosed in
`data_sources.triaged_fast_workflows` (+ a provenance footnote) — never silent. PR-sha pool,
events, and volume still come from the run metadata, so the sample population is unaffected.

A triaged workflow's check-run still rides along on the sampled PRs, so its check-run p50 can
land in `pr_checks_tuple` and even rank into the structural top-N — but it has **no sampled job
to decompose**, so it is excluded from the drilled `pr_critical_path.poles`
(`_structural_pole_candidates` resolves each candidate's workflow with the same `_pole_mapping`
the decomposer uses and drops it when that workflow is triaged). Without this it would render as
a **bare** long pole (no `dominant_step`/`steps` → "no captured log" + "NO CATALOG PATTERN
MATCHED"), contradicting the very triage coverage note above. The dropped check stays on the
`checks` spine (its honest fast-check disclosure); it is simply never a drilled pole. The class
guard `verify_report.check_speed_poles_complete` re-derives this from the findings JSON in two
places: the poles-keyed `_triaged_pole_offenders` (a triaged `workflow_file` appearing among the
poles) AND the crown-keyed `_crown_triaged_offender` (the `critical_path_check` mapping — via its
`checks[*].workflow_file` — to a triaged workflow), so both a bare drilled pole and a triaged
HEADLINE (which never enters `poles`) are caught on every report.

**Pole-log fetches are concurrent.** The cross-run magnitude check (`_magnitude_sample`)
validates the drilled run's load-bearing number against a few other runs' logs; those logs
(often multi-MB) are fetched per batch **concurrently** (bounded by the batch; the adaptive
probe→escalate decision stays sequential between batches), so the downloads overlap instead
of running back-to-back.

**Why per-run REST job timing, not a GraphQL `CheckRun` projection.** A recurring
instinct is to collapse the per-run `GET /runs/{id}/jobs` cost by reading check
state from GraphQL instead. We don't: the `CheckRun` projection is lossy for
*timing* — it doesn't expose the per-run job fields the drill and queue-time
analysis depend on (runner label, queued/started timestamps, per-step timing), and
its check-run span folds in queue + re-run boundaries, re-introducing the
queue-inflated-duration bug the REST path was written to avoid. The per-run REST
call is the price of an accurate, queue-clean p50 and a step-level drill. The
adaptive two-pass above is the *sanctioned* way to cut that cost, because it was
proven not to move the answer. The durable rule: **any change that trades gh calls
for an approximation — a smaller sample depth, a cheaper endpoint, a coarser
projection — must be justified by *measured convergence* against full-depth ground
truth (the Monte-Carlo subsampling / forced-full-pass diff above), never by the
code's general reasoning that "it should be enough." Reproduce the
gate/drill/floor/bimodal signal first, then adopt the cheaper path.**

### 2.1a Config-era boundary — a sample never blends two CI configurations (issue #66)

The sample window is a *time* window, but a workflow file can CHANGE inside it. When
it does, the runs before the change and the runs after it measure two different CI
configurations, and blending them is unsound: it produces a stale headline, a drill
that mixes the old step layout with the new (the fabricated "the guard runs twice,
once whole and once sharded" cross-era synthesis — no PR ran both), and a recoverable
ceiling drilled from the retired config that exceeds the typical wait under the
current one. This is the universal *second-run journey* (audit → the user's own fix
lands → re-audit), and the skill's own description advertises "re-auditing after
upstream CI changes," so it must handle a mid-window change first-class.

**The boundary.** For each workflow file with ≥2 sampled runs, `_workflow_change_boundary`
issues ONE `commits?path=<wf>&per_page=2` REST call — the two most-recent commits that
touched the file, returned as `(last, prev)` — pinned `&until=<created_before>` so an
edit LATER than the audited window is never mistaken for the boundary (a regen samples
the same window). `last` is the config-era boundary; `prev` is the boundary of the era
immediately before it, used only to keep the disclosed-pre fallback single-era when a
workflow changed TWICE in the window (see below). **API cost:** one call per
straddle-eligible workflow (K calls, K = workflows with ≥2 runs) — `per_page=2` is still
a SINGLE call, so the budget is unchanged. This is the frugal choice, sanctioned by the
adaptive-sampling ethos above: the runs API exposes **no** workflow-content hash, so the
only content-diff alternative is one `/contents/` fetch per run (N ≫ K) — the commit
lookup is O(1) per workflow and strictly cheaper. `allow_missing=True`; any failure/empty
history → `(None, None)` → a byte-identical no-op.

**The partition** (`_partition_config_era`), applied to each workflow's spine + drill
run population (`runs_for_spine`, threaded into the PR-sha pool, the shallow/deepen job
plan, and the triage-recovery stash — NOT `sampled_runs_by_wf`, which the runner-minute
/ relative-recovery consumers keep at full sample; the partition is scoped to the PR
spine/drill, L2):

The straddle state space is **total** — every straddle resolves to exactly one of three
rules, each with stamps that survive and a disclosure that matches what the report
enumerates (issue #74). The blended-while-claiming-purity state is unreachable, not caught
after the fact:

| sample vs. boundary | rule | kept era | rendering |
|---|---|---|---|
| all runs one side (or no boundary) | — | full sample | byte-identical no-op (a workflow that did not change is untouched) |
| straddle, post-change sample ≥ `_RARE_PRESENCE_MIN_PR` | `post_only` | post | measures the CURRENT config on a narrowed window; a light "narrowed to the current configuration" note |
| straddle, post-change sample too thin, pre era **has** a gate-bearing check | `disclosed_pre` | pre | measures the PREVIOUS config; a PROMINENT ⚠️ disclosure near the headline ("`ci.yml` changed N ago — this audit measures the previous configuration; re-run once history accumulates") |
| straddle, post-change sample too thin, pre era has **no** gate-bearing check in the sample (the gate PRs are all post-change) | `post_only_thin` | post | measures the CURRENT config from the thin post sample; a PROMINENT ⚠️ disclosure ("`ci.yml` changed N ago — measures ONLY the new configuration on a thin sample; only N runs … treat these numbers as provisional; re-run as history accumulates") |

In every straddle branch the change's OWN before/after never blend — one side is always
dropped whole — so the fabricated retired-vs-current synthesis (the "runs twice, once whole
and once sharded") is **structurally impossible**, not merely caught after the fact.

**The `disclosed_pre` → `post_only_thin` flip — the timing spine flips WITH the rule (issue #74,
direction (a)).** `_partition_config_era` chooses `disclosed_pre` from RUN counts alone, but only the
PR gate sample (`per_sha_checks`, available after `_select_repr_shas`) reveals whether the kept PRE
era actually produced a gate-bearing check IN THE SAMPLE. When it did not — the live #74 shape, where
the sole gate-bearing sampled PRs were all post-change so the pre era is check-empty — "measure the
old config" is UNAVAILABLE (there is nothing pre-era to show). The original never-empties fallback
skipped the enumeration cut whole: it left the blend intact AND cleared the stamps the enumeration
guard re-derives from, so the report rendered `test` @ 8m58s + `guard shard 3/4` under a "reflect the
configuration BEFORE it" disclosure while the guard went blind.

`_era_resolve_thin_flip` closes it BEFORE the spine is consumed. The decision is a pure function of
the sample; the crucial part is that when it fires it does not merely relabel the rule — it
**re-drills the workflow's whole spine from the POST runs** (the injected `_era_redrill` re-fetches
that one workflow's post-era job listings and rebuilds `crit_by_wf`/`jobs_per_run_by_wf`), so
`pr_check_p50`, the poles, the representative-run links, and the makespans all derive from the new
configuration — never a pre-era run under a "measures the new configuration" claim (the defect a
naive enumeration-only flip left behind: `crit_by_wf` still at the retired 538s). This runs in the
pipeline order (partition → shallow/deepen drill off `runs_for_spine` → `_select_repr_shas` →
**flip + re-drill** → `pr_check_p50` → `_era_scope_enumeration`), so every downstream consumer inherits
the post-era spine. Only then are `rule`/`kept_era` rewritten to `post_only_thin`/`post`; the
disclosure becomes the provisional `_CONFIG_ERA_THIN_MARKER` line rather than the pre-only caveat; and
`_era_scope_enumeration` binds the already-resolved fact (keeping the post checks, `other_era_checks`
empty). The re-drilled post sample is below `_RARE_PRESENCE_MIN_PR`, so the presence-dependent
machinery (minority demotion, populations, the presence-causal headline forms) stays inert on the POST
timings and the thin disclosure carries the reduced confidence — no sub-floor pretend-confident
output. A timing-provenance guard leg (`_stamp_pole_repr_run_era` +
`check_era_disclosure_matches_enumeration`) stamps each pole with its earliest drilled-run timestamp
and FAILs a post-claiming disclosure that carries a pole drilled from a pre-boundary run.

**Era classification is content-keyed (issue #77).** A run is placed pre/post by the workflow-file
CONTENT its `head_sha` actually executed, not by its `created_at`. A `pull_request` run runs the
workflow from the PR's OWN head, so the two PRs that CARRY a CI fix run the NEW config from their own
heads — often minutes BEFORE the fix merges — and a stale branch merged AFTER the boundary runs the
OLD config; timestamp classification misreads both, and on the live internal-dev-repo shape it
rendered the fix-PRs' new-config makespan under a pre-only disclosure and SUPPRESSED the thin-flip
(the fix-PRs looked like kept-side pre gate PRs). The mechanism: `_workflow_change_boundary` returns
the two boundary COMMIT SHAs; `_resolve_content_eras` fetches the workflow BLOB sha on each side
(POST at the boundary commit, PRE at its predecessor) and, for each UNIQUE sampled head_sha, the blob
that head carried — matching POST→post, PRE→pre, NEITHER→timestamp fallback. `_partition_config_era`,
`_era_pr_side` (thin-flip / enumeration / re-drill), and the pole stamps all consult this content era
first and fall back to `created_at` when it does not resolve. **API cost — the worst-case bound:**
≤2 blob calls for the boundary + ≤1 per unique sampled head_sha, and ONLY for a workflow whose sample
TIMESTAMP-straddles the boundary (the cheap `_timestamp_straddles` gate). A workflow that did not
change, or whose whole sample sits one side, fetches **zero** blobs — byte-identical to a
non-straddling repo. When the POST blob fails to resolve, content classification is skipped whole
(every run falls to timestamp) without paying the per-head fetches. Classification bookkeeping
(content vs timestamp sampled-run counts, boundary-blob resolution) is stamped on the era fact as
`classification`, and each straddling pole gains `repr_run_era` / `repr_run_era_basis` /
`repr_run_head_sha` alongside `repr_run_created_at`, which the verify guard's timing-provenance leg
reads basis-aware (a content-post pole with a pre-boundary timestamp is no longer a false FAIL, and
the pole's era stamp is cross-checked against the fact-level content map).
**Multi-boundary:** if a workflow changed TWICE inside the window, the runs before `last`
themselves span two older eras; the `disclosed_pre` fallback narrows the kept set to the
single `[prev, last)` era (using the `prev` boundary from the same one call), so the pre-side
is a single era too — `multi_change`/`kept_count` record when that narrowing fires. (In the
common single-change case `prev` predates the whole window, so the narrow keeps every pre
run — byte-identical to the un-narrowed path.) In the rare ≥3-change corner where `[prev,
last)` holds no sampled run — only the two most-recent boundaries are fetched, not deeper
history — the kept set falls back to a disclosed wider pre set that may span older eras
(best-effort, always disclosed, never silent). The sufficiency floor reuses
`_RARE_PRESENCE_MIN_PR` (the same sample-size floor below which a check's frequency is
already treated as noise). Per-workflow era facts (boundary, prev boundary, kept era,
rule, multi-change flag, pre/post/kept counts) are stamped in
`pr_critical_path.config_eras`; `verify_report.check_config_era_boundary` re-derives that
no drilled pole (nor the spine) bound to a `kept_era == "pre"` workflow ships without the
disclosure. **Bill-scope (L2):** the partition is scoped to the PR spine/drill; the
runner-minute / cost-spine figures deliberately keep the FULL sample (they size total
compute, not the critical path), so a straddle co-renders a caveat that those figures
still include the earlier configuration and a duration- or structure-changing edit blends
both layouts there. A **recoverable-within-wait** coherence guard
(`check_recoverable_within_wait`, a bounds-family sibling of §7's #24/#25/#30) further
requires any rendered "up to ~X" ceiling that exceeds the typical merge wait to
co-render the slow-mode/worst-case reconciliation (the excess is the pole's conditional
figure on the PRs where it IS the pole, not the median).

**Enumeration binding — one report describes one configuration (issue #69).** The partition
above scopes the SPINE TIMING (`runs_for_spine`) to one era, but the enumerated CHECK SET —
the PR-gate check-run sample that seeds pole candidacy, the populations, and the Level-1 chart
— is drawn from the sampled PR heads, which can include *post-boundary* PRs carrying the NEW
config's checks. Left unbound, a `disclosed_pre` report renders the new config's jobs (`guard
shard 1/4..4/4`, observed on a couple of post-change PRs) as poles and Level-1 bars BESIDE the
pre-era `test` timing — a configuration that never ran, and the seed of the same fabricated
cross-era redundancy #66 kills, through a different path. `_era_scope_enumeration` closes it:
for each straddle it splits the sampled PRs at the boundary (`_era_pr_side`, mirroring the
run-partition's kept-side selection incl. the multi-boundary `[prev, last)` window), attributes
each spine check to its workflow (the same timing-then-scanned-graph mappers the collector uses everywhere else to bind a check to its job),
and drops any check bound to the straddling workflow that was observed ONLY on the *dropped*
side. Those dropped checks are the other configuration's adds/removes: stamped on the fact as
`other_era_checks` (with `kept_checks` alongside) and NAMED in the era note ("the new
configuration adds checks not measured here: `guard shard 1/4`…`guard shard 4/4`" for
`disclosed_pre`; the converse "the previous configuration ran checks not measured here" for
`post_only`). Because pole candidacy, presence, populations, and the chart all re-derive from
`pr_check_p50`, dropping there binds every enumeration surface at once. `_era_scope_enumeration` runs
on ALREADY-RESOLVED facts — the disclosed_pre → post_only_thin flip and its spine re-drill happen
earlier, in `_era_resolve_thin_flip` (above), so this binder never sees the empty-enumeration case a
disclosed_pre straddle used to hit (it has been re-drilled to post first). The only residual
empty-spine path — a `post_only` straddle whose CURRENT era is check-empty while the retired one
was not — leaves the enumeration intact but KEEPS its stamps (never the old skip-whole clear, the #74
blindness), so the guard FAILs loudly on the leak rather than skipping blind. It is a pure no-op when
nothing straddles (L2 byte-identity).
`verify_report.check_era_enumeration_bound` re-derives the bind from the stamped `other_era_checks`:
no check the report enumerates (from `checks` / `poles` / `populations`) may be a member of any
era's `other_era_checks` — a check absent from the kept era's runs. A second, independent guard
(`check_era_disclosure_matches_enumeration`, issue #74) keys on the RENDERED disclosure marker: a
pre-only "measures the previous configuration" caveat must not co-exist with an all-post / hollow
(empty-`kept_checks`) measurement — re-derivable now that the flip makes stamps always survive. Both
are a loud narrow SKIP on a straddle artifact predating the enumeration stamps (the cleared-stamp
shape the old guard went blind on — the bind isn't re-derivable, a coverage gap not a clean pass).

**Per-PR spine door — the makespan/chain layer is bound to the kept era too (issue #80).** The
enumeration bind above scopes the CHECK SET (`pr_check_p50`) by NAME, but a check name survives a
config change, so the layer built from the raw per-PR SAMPLE — the `chain_facts` (→ `chain_summary`
→ `makespan_p50_s`, the "a typical PR waits **N**" headline and the §7 #24 physical-bound cap), the
`populations`, and the presence denominators — still blended a *dropped*-era PR's latest-attempt
interval for a kept-NAMED check (`test` is still `test` after it gets 3× faster). On the live
internal-dev-repo run two dropped-era PRs' 166s makespan crowned the headline under a disclosure
whose pre-era `test` p50 was 538s — physically impossible. `_era_scope_pr_spine_sample` closes it:
under a straddle it scopes the per-PR sample to the kept side, **surgically per straddling
workflow** — a PR on workflow W's DROPPED side (`_era_pr_side`, content-first via head_sha) loses
only its W-attributed checks (`check_wf_of`); era-neutral checks from non-straddling siblings
survive, and a row left with no gate-bearing check drops whole. The scoped sample feeds
`_rank_spine_present_first` (presence), `_segment_pr_populations`, and `_stamp_chain_facts`;
`sampled_pr_count` becomes the kept-side count and `era_dropped_pr_count` stamps the drop for the
sampling caveat. It runs AFTER `_era_resolve_thin_flip` (so an emptied kept side has already flipped
to `post_only_thin` and its post PRs are kept here — never an empty-spine render under a pre claim)
and deliberately does NOT touch the flip's decision input, which still sees the full sample. Pure
no-op when nothing straddles. `verify_report.check_era_chain_spine_bound_to_kept_era` re-derives the
bind offline in three legs: `chain_summary.n ≤ sampled_pr_count`; no chain fact for a
content-classified *dropped* head carries a kept-era chain member; and a pre-claiming makespan may
not sit below a kept gate present on EVERY kept sampled PR (the unanimous-presence restriction keeps
it false-positive-free; a non-unanimous kept gate is left to the first two legs + the engine door).

### 2.2 Fetch orchestration (one pool, bounded prefetch waves, a token-wide governor)

§2.1 decides **which** gh calls to make. This decides **when** they are issued — and
nothing else. Every call in the pass is an idempotent, latency-bound GET, so the
whole family is safe to overlap; the constraint is that the *results* must land in
the same order the serial code saw, and the call multiset must not change.

**One shared pool.** `_fetch_pool()` is a single process-wide `ThreadPoolExecutor`
at `_FETCH_CONCURRENCY` (8). Every concurrent site maps over it, so the in-flight
ceiling for the whole run is exactly the width — not (number of live pools × width),
which is what a pool-per-call-site gives you.

**Prefetch waves (`GhClient.prefetch_json` / `prefetch_text`).** The parallelism seam.
A caller about to issue a known set of endpoints hands them over first; they are
fetched through the shared pool and parked in a buffer, and each original
`json()`/`text()` call site then finds its response already there. The consuming code
is untouched — same iteration order, same results — which is what makes the
"identical output" claim checkable rather than hopeful. Two rules keep the call
multiset invariant:

- **Prefetch only what will certainly be requested — never be MORE EAGER than the call
  site.** A parked response nobody consumes is a gh call the serial path never made.
  `drain_prefetch()` counts any leftovers at the end of `collect()` (parked JSON, parked
  logs, **and** the tail of the bounded log window that was never reached), logs them
  loudly, **and writes the count into `data_sources.prefetch_unconsumed`**. That count is
  then both *disclosed* (`blocking_path` renders a prefetch-drift line when it is non-zero)
  and *checked* (`verify_report.py`'s `check_prefetch_plan_consumed` fails any freshly
  collected report whose count is non-zero) — so a drifted plan leaves a mark a human and
  the gate both see, not a lost scrollback. (The guard earns its keep: it caught 31 real
  unconsumed prefetches while this pass was built.) NB the committed worked-example reports
  predate this key and carry no `prefetch_unconsumed` at all; the key is a guarantee about
  future collected reports, and the serial-vs-parallel equivalence test — not the report
  diffs — is what proves the parallel path. This is why the rerun-attempt plan prefetches
  the `filter=all` leg **only** and never the `filter=latest` leg: that leg's call site is
  the one actively changing, and a plan that assumes it would keep paying for N calls per
  workflow that nobody consumes.
- **Consumption is POP-ONCE, not a cache.** A second request for the same endpoint
  re-issues live, exactly as before. So a call site that legitimately fetches an
  endpoint twice still costs two calls, and `gh_query_count` is unchanged. Responses
  queue **FIFO per key** and a wave never skips an already-parked endpoint — skipping is
  how a pop-once buffer quietly turns into a cross-phase cache, serving a later call site
  a value fetched in an earlier phase.
- **The buffer key is `(endpoint, allow_missing)`, not the endpoint alone.**
  `allow_missing` is the *accounting rule* the fetch was made under (does a failure count
  toward `errors`, and so toward the partial-coverage banner?), and it is applied on the
  pool thread at prefetch time. Keyed by endpoint alone, a value fetched under the
  permissive rule could be served to a call site passing the strict one — the failure
  would already have gone uncounted and the consumer would just see `None`. A mismatched
  consumer therefore MISSES the buffer, fetches live under its own rule, and the
  disagreement is logged at WARNING.

The plans and the call sites share their endpoint builders (`_volume_endpoint`,
`_run_list_endpoint`, `_status_count_endpoint`, `_run_jobs_endpoint`,
`_job_log_endpoint`) and their guard predicates (`_opt65_scope_event`,
`_opt57_timeout_job_specs`, `_on_has_event`, `_recovers`) precisely so the two cannot
disagree about which URL, or whether a call happens at all. A predicate re-stated inline
next to the plan that computed it is a drift bug waiting to happen; there is exactly one
copy of each.

The waves, in order: the run-list family for every workflow → the whole shallow job
sample flattened across workflows → one wave per deepen round → the triage-recovery
job fetches → the detector loop's run-list family → the rerun-attempt `filter=all`
listings → the workflow `contents` reads, the cache-evidence logs, the cache push-probe
logs, and the pole drill logs.

**Peak memory: the JSON waves are flat, the LOG waves are a bounded window.** The JSON
responses (run lists, job listings) are retained by the consumer for the rest of the pass
anyway — `jobs_per_run_by_wf` holds the entire sample — so buffering a whole wave of them
adds no new peak. Job logs are the opposite: multi-MB each (a big test job's log runs to
tens of MB), and the serial path they replaced held exactly one at a time — fetch, parse,
discard. A flat `pool.map` over the whole log plan would materialise *every* planned log
before the first is consumed and hold them all live until popped — peak memory O(plan),
which on a large repo (every cache-family finding × `cap` runs, plus the pole drills) is
hundreds of MB to GB. So the three `prefetch_text` plans drain through a **window**
(`_TEXT_WINDOW = 16`): at most 16 logs are parked at once, topped back up as the call site
consumes them, so peak log memory is O(window), independent of plan length, while each
refill is still a full-width wave. The one directly-pooled log map (`_magnitude_sample`)
is chunked to the same window for the same reason. A test asserts the parked log set never
exceeds the window across a plan far longer than it.

**What must stay serial.** The deepen **rounds** (§2.1). Each round's ranking is
computed from the previous round's *corrected* p50s — that feedback IS the convergence
guarantee — so the rounds never overlap. Workflows *within* one round do fan out.
(`test_gh_concurrency.py` pins this: fusing the rounds changes the answer.)
`_select_repr_shas` also keeps its bounded, newest-first, stop-early batching: it is
capped to the PRs still needed so it never over-fetches.

**Width 8 bounds concurrency; the governor bounds the *rate* (they are different things).**
GitHub's *primary* limit (5,000 req/hr) is indifferent to concurrency — the pass issues the
same calls either way. The binding rule is the *secondary* limit. Its clauses, verbatim
from "Rate limits for the REST API → Secondary rate limits":

- *"No more than 100 concurrent requests are allowed. This limit is shared across the REST
  API and GraphQL API."* — width 8 is comfortably inside this.
- *"No more than 900 points per minute are allowed for REST API endpoints"* (a GET = 1
  point). **This is an AGGREGATE budget for the token across the whole REST API, not a
  per-route allowance.** (The giveaway is the sibling clause: GraphQL scores 2,000/min
  against *"the GraphQL API endpoint"* — a single endpoint. There is no per-route allowance
  anywhere in GitHub's rate-limit docs; the only other secondary limits are the
  100-concurrent one above and *"no more than 80 content-generating requests per minute"*,
  both per-token.) An earlier version of this section claimed "900 points per minute per
  REST route" — that was a misreading, and it is corrected here and in the governor below.

Width 8 is deliberately *not* a new number: it is exactly the width the three pre-existing
pooled call sites already used, so **peak concurrency is unchanged** by the
re-orchestration. But peak concurrency is not the whole story: finishing the same ~700
calls in roughly a third of the wall time **triples the sustained request rate**, and the
binding secondary limit *is* a rate. So the honest claim is *"peak concurrency unchanged at
8; sustained request rate up ~3×, paced by the governor"* — not "zero new rate-limit
exposure". The governor below is what makes the higher sustained rate safe; the width is
what bounds the blast radius when it isn't. The value here is parallelising the *right
group* (the run-list family, hoisted out of the per-workflow loops), not driving a wider
pool.

> ⚠️ **Raising `_FETCH_CONCURRENCY` above 8 is unblocked but not free.** Rate-limit
> detection, retry/backoff, `Retry-After` handling and the circuit breaker all live in
> `_invoke` (§2.0) — so a secondary-limit **403** is retried, shared across the pool, and
> counted rather than silently lost. What a wider pool still costs is exposure: more of
> the sample is in flight per incident, and an exhausted retry budget is still lost data.
> The bump is a one-line follow-up that must ship with a fresh before/after measurement;
> a test pins the ceiling at 8 until then.

**The token-wide governor, and why capacity ≠ budget.** `_RestRateLimiter` is a **single**
token bucket for the whole REST API on the token — because that is the thing GitHub scores.
It refills at `_REST_RATE_PER_MIN` (600/min = 67% of the documented 900) and holds at most
`_REST_BURST` (100) tokens. The remaining third is real headroom, not timidity: the budget
is the *token's* (the user's `gh` is usually also driving their shell and editor), GitHub
says some endpoints cost more than 1 point and does not publish which, and a 403 here is
lost data once the retry budget is exhausted — riding at 94% of the ceiling is not a safety
mechanism. It costs nothing measurable because the pass is latency-bound at width 8 (see
the measured numbers in the CHANGELOG): the cap binds only on a repo whose responses come
back fast enough for the pool to *sustain* >600/min, which is exactly where it should bind.

`_route_key` still exists, but only as a DEBUG label ("which family is the governor pausing
on?") — it does not select a bucket, because there is only one. It now collapses every
*parameter* in the path, not just numeric ids: owner/repo, SHA-shaped hex
(`commits/{sha}/check-runs` is one route, not one per commit) and everything under
`/contents/` (one route, not one per workflow file). The old numeric-only version left
those two families keyed per-value, which — under the old per-route bucket — meant they got
a fresh full burst on every call, i.e. no pacing at all; the global bucket fixes that
regardless, but the keying was simply wrong and narrower keying is *not* "more
conservative" (more buckets = more admissions).

The two knobs are separate on purpose. A token bucket admits, in the worst 60-second
window, `capacity + rate × 60` requests — so a bucket whose capacity *is* the per-minute
budget admits **twice** the budget from a cold start and only begins pacing in the *second*
minute. That is precisely the wrong shape: GitHub's secondary limit is burst-sensitive, and
a large repo's job-listing wave (40 workflows × 20 runs) IS a cold-start burst. With the
burst decoupled:

    worst case in any 60s = _REST_BURST + _REST_RATE_PER_MIN = 100 + 600 = 700 < 900 ✅

A test states exactly that property, and — the point the old single-route test missed —
offers the infinite load **across many routes**, because the budget is shared: N routes ×
one bucket is not a partition of anything GitHub scores. On a small repo the burst covers
the whole pass and the governor never blocks; on a large one it — not the width — sets the
pace.

**A failed fetch is never silent.** `allow_missing=True` means *"a **404** here is
expected"* (branch protection / rulesets on a repo you don't own; a workflow file that no
longer exists) — it does **not** mean "any failure here is expected". Every non-404
non-zero exit is counted in `errors` even at an `allow_missing` call site, and logged at
WARNING (visible at the default INFO level). This matters most exactly where the
concurrency work put the most pressure: the workflow `contents` reads are all
`allow_missing=True` and are now one late wave. A swallowed 403 there drops a workflow
from `_wf_docs`, and every consumer (`on:` block, matrix/shard recognition, OPT57 timeout
specs, OPT65 event scope, the schedule block) then evaluates against an empty dict — a
report that renders clean and complete with a workflow's entire file-level signal missing.
The classification lives in one function (`_classify_gh_failure` — 403-vs-429-vs-5xx by
status line + header evidence, rate-limit keywords as a fallback) and every live call
funnels through one choke point (`GhClient._invoke`), which applies the retry/backoff, the
shared rate-limit block (`_blocked_until`), and the global give-up breaker (`gave_up`).
`allow_missing` only ever exempts a genuine 404; a rate-limit / 5xx / timeout exhaustion is
counted even there. The concurrency pass runs `_invoke` on the shared pool's threads, so
that same choke point also carries the token-wide rate governor (`_RestRateLimiter`, acquired
before every live attempt) and catches an `OSError`/`UnicodeDecodeError` so one bad fetch
never discards a whole wave.

## 3. Data model - findings.json

`scan.py` emits the document; later stages add keys. The top-level shape:

| Key | Set by | Meaning |
| --- | --- | --- |
| `findings` | scan + collect_runs | the finding list (see below) |
| `scanned_workflows` | scan | count of workflow files seen |
| `scan_incomplete` | scan | files that could not be read/parsed (coverage gaps) |
| `catalog_patterns_without_detector` | scan | coverage-honesty list |
| `scanned_at` / `skill_commit_sha` / `commit_sha` / `repo` | scan main | provenance |
| `data_sources` | collect_runs | tiers run, gh counts, `sampled_runs_created_before`, `partial_reason`, adaptive-sampling fields (`shallow_runs`, `max_runs`, `shallow_capped`, `capped_workflows`, `pole_candidates`, `deepened_workflows`, `deepen_converged`, `full_depth_workflows`, `shallow_remaining_workflows` — §2.1), run-list-triage fields (`triaged_fast_workflows`, `triaged_fast_count`, `recovered_fast_workflows`, `recovered_fast_count` — §2.1), cost-spine-only triage/fetch fields (`cost_spine_triaged_workflows_included`, counts for workflows/runs/jobs, `cost_spine_job_fetch_failures`, `cost_deepen_candidate_workflows`, `cost_deepened_workflows`, `cost_spine_full_depth_workflows`, and `cost_spine_shallow_workflows`), and `cache_dist_probe` (`{cache_poles, push_logs_fetched, pr_logs_reused}` — the cache-distribution provenance + the schema stamp `verify_report`'s cache checks SKIP on when absent, §12.4) |
| `pr_critical_path` | collect_runs | the merge-wait spine: `critical_path_check`/`critical_path_s` (the headline pole), `checks[]` (`name`, `p50_s`, `present_on`, **`pole_n`** = PRs where this check is the actual pole, the recurrence-ranking signal, §11; `workflow_file`, `bimodal?`), `check_present_n_pr` (presence denominator), `sampled_pr_count` (the kept-side PR count post the §2.1a #80 spine door) + **`era_dropped_pr_count`** (PRs the door removed as dropped-era; stamped only on a straddle that dropped >=1), `poles[]` (decomposed drilled poles; a cache pole also carries **`cache_dist`** = the per-event, fork-aware miss distribution + `verdict`, §12.4), `populations[]` (per-PR check sets, M2), `dropped_non_pr_checks` / `dropped_non_required_checks` (visible exclusions), **`fileless_status_checks[]`** (`name`, `span_s`, `basis` — the fileless/managed status checks EXCLUDED from the crowning basis, disclosed as PR-lifetime status-gating latency; issue #12, §12.2a) + **`all_checks_fileless`** (the degenerate flag), **`config_eras[]`** (`workflow_file`, `boundary`, `kept_era`, `rule`, `pre_count`, `post_count`, plus `prev_boundary` / `multi_change` / `kept_count` on a `disclosed_pre` era — per-workflow record of every sample that straddled its last-change commit; empty when nothing straddled; issue #66, §2.1a), **`chain_facts[]`** (ENG-1: per sampled PR the longest `needs:` chain — spine-scoped members, capped per-member spans, `chain_s`, `co_longest_n`, `runner_up_s`, the attempt-scoped `makespan_s` cross-check) + **`chain_summary`** (modal chain, `chain_p50_s`, `runner_up_p50_s`, signed `divergence_pct`) — when the modal chain has >=2 members the renderer's headline names the CHAIN and its summed p50 instead of the slowest single check (`headline_chain` claim, re-derived by `check_headline_chain_matches_stamp`) |
| `per_workflow_timing` | collect_runs | `{wf: {long_pole_job, long_pole_p50, floor_p50, job_p50, events}}` (`events` = the workflow's OBSERVED trigger events, so `verify_report` can re-derive merge-path eligibility — §12.6a); a run-list-triaged workflow (§2.1) instead carries `{long_pole_p50: 0, …, concurrent_wall_p50}` (its run-list wall, read only by the cross-workflow floor) |
| `per_workflow_monthly_volume` | collect_runs | `{wf: 30-day run count}` for the final cost-spine coverage denominator: finding-backed workflows plus any detector-only probes that emitted cost-spine rows. Detector-only probes that emit no finding and no rows are sampled for evidence but omitted from this map so they do not masquerade as incomplete cost-spine coverage. |
| `runner_minute_spine` | collect_runs | source block for the cost-spine table: stable per-workflow/job/runner rows, raw compute minutes, billable-equivalent minutes, totals, repo visibility, and `render_ready: true` only when complete workflow coverage is proven; PR16 stamps `sampled_workflows_in_play_with_job_data` plus `workflow_coverage` for positive-volume workflows in play, omitted workflows, unknown-volume workflows, triaged workflows included, and source-row fetch failures |
| `dropped_unprovable` | collect_runs (`--with-logs`) | cache findings dropped at the admission gate |
| `data_bundle` | collect_runs (`--with-logs --data-dir`) | `{logs_dir, logs:[{job, file, steps_file, mag_file, sample, selected:"nearest-p50"}]}` — the blocking-path drill bundle (§12): per pole, the nearest-P50 run's log + step timeline + cross-run magnitude sample |
| `timings` | scan/run/report | per-phase durations + `run_start_epoch` |
| `skill_tree_dirty` | orchestrator | injected so the report can stamp `<sha>-dirty` |

A single **finding** dict carries (load-bearing fields in bold):

- `id` (`f1`, …), **`pattern`** (a catalog OPT-id), `pattern_class`
  (`static` | `data-driven` | `structural`), **`severity`**
  (HIGH/MEDIUM/LOW/MANUAL), `title`, **`workflow_file`**, **`line`**,
  `affected_jobs`, `evidence`, `fix_strategy`, `fix_recipe_anchor`.
- **Structural findings only** (`structural: true`, OPT70–OPT75 — see §11):
  **`risk`** (`LOW`/`MEDIUM`/`HIGH`), **`guardrail`**, `rollout`,
  `failure_mode`, `decomposition` (`{dominant_step, dominant_category,
  dominant_share, redundant_ratio, …}`), and `required_status`
  (`required`/`not-required`/`unknown`). `risk` + `guardrail` are
  load-bearing: `_validate_findings` pulls out any structural finding missing
  them, so a high-leverage change can never render as if it were safe hygiene.
- Sizing: **`wall_clock_p50_s`** (the ranking metric; `null` = qualitative,
  `0.0` = sized-as-zero), **`runner_min_saving`**, `tier` (1 direct lever / 2
  bill-only-or-negative / 3 reliability), `realization` (`direct` / `tail` /
  `none`), `size_note`, and `wall_clock_uncapped_p50_s` when a cross-workflow
  cap fired.
- Evidence/provenance: `measured_signal` (required on data-driven findings),
  `measured_evidence` (`{summary, table:{headers,rows}, note}` with links to
  real job logs), `evidence_snippet` (verbatim matched YAML, static findings),
  `workflow_activity` (`{runs_30d, last_run, dormant}`).
- Cross-workflow: `concurrent_workflows` (`[{workflow, long_pole_p50_s}]`) and
  `workflow_long_pole_p50_s`.
- Routing: `advisory` (excluded from the report, kept in JSON), `needs_log_confirmation`.

A pattern absent from the `_SIZING` table is rendered qualitatively (both axes
`null`) - the scanner never invents a number.

## 4. The two metric axes

Every finding is sized on **two different numbers measuring two different
things**:

- **Δ wall-clock** - the per-run developer wait removed from the critical path.
  A finding can have a large runner-min saving and **zero** wall-clock (its job
  sits below the cluster floor and never gated the run).
- **Δ runner-min/mo** - the cloud-bill saving. Unlike wall-clock it **is**
  additive across disjoint step-seconds. For promoted Tier-2 rows the section
  sums raw credited minutes after the Tier-2 de-overlap pass; residual appendix
  rows still group by pattern and do not cross-family de-overlap.

**The report RANKS on Δ wall-clock** (`blocking_path.py`): the findings table
puts the biggest wall-clock lever first, including all non-advisory findings;
advisory findings are excluded from the report but kept in the findings JSON.
Off-path catalog hygiene (a fix that moves ~0 wall-clock) now has two routes:
measured findings with a `tier2_neutrality` certificate promote into
**"Runner-minute reductions (wall-clock-neutral)"** as source-backed candidates,
while modeled, uncertified, advisory, or residual same-pattern members stay in
**"Also noticed"**. Candidate admission and appendix exclusion are per finding
(`_is_tier2_finding`); visible R-rows additionally require matching
`runner_minute_spine` source rows. The appendix groups by pattern, so an OPT-id
can straddle both sections. A finding whose fix is wall-clock-negative
(`_increases_wall_clock`: the explicit `wall_clock_negative` flag, set by
`_size_finding`) is disclosed as such in the residual appendix — a bill saving
that *adds* developer wait, not a speed win.

### 4.1 Runner-minute reductions promotion gate

`blocking_path.py` renders a first-class Tier-2 section after Pre-start wait and
before Also noticed. A finding becomes a Tier-2 candidate only when
`sizing_basis == "measured"` and `tier2_neutrality` is stamped; that predicate is
the routing source for candidate admission. The visible section uses the
stricter source-backed set: a candidate renders only when
matching render-ready `runner_minute_spine` rows exist for the finding's
workflow, relevant job identities, and non-conflicting row dimensions when
the cost spine stamps that detector-specific population. OPT64 must bind to its
stamped `rerun_dominant_job` and `all-status`/`prior` source rows; explicit
finding filters cannot weaken those required dimensions. Non-OPT64 savings
must bind to success/latest/all-status rows, so prior-attempt rows are not
allowed to back ordinary latest-attempt savings. Job matching is exact-first:
matrix-base source rows are used only when no exact source row exists for the
stamped job name. Latest-attempt
schedule/failed-run detectors currently bind to the collector's
all-events/success/latest source envelope while their detector-specific stamps
prove the event or failed-run subset. The matched source rows must also cover
the credited raw minutes; OPT65 rounding waste is the billable-minute exception
because it credits per-run billing-rounding delta, not raw compute time.
The Contents count, section lead, bottom-line Tier-2 sentence, and `summary.py`
handoff line all use that source-backed set. Measured/certified candidates that
fail source binding are suppressed from R-numbered savings cards but fall back
to `Also noticed` with an explicit source-backing note, so they stay visible
without being presented as source-backed.

The section ranks by billable-equivalent runner minutes and displays raw
credited minutes. All figures are runner-minutes; the section lead ends with the
one-sentence pricing story ("multiply by your runner's per-minute rate to get
dollars"). The renderer shows at most 12 R-rows, but the lead totals cover every
source-backed Tier-2 row in `findings.json`; overflow rows are disclosed as not
shown and remain in findings JSON rather than moving back into Also noticed. Each
visible card's `Source block` line shows the current cost-spine raw minutes and
billable minutes for those source rows. The verifier also rejects duplicate
R-row marker IDs, rendered rows without per-finding Tier-2 stamps, non-positive
savings, and visible row savings whose credited raw minutes exceed the matched
source-row totals (billable totals for OPT65).

The verifier re-derives the promoted set, measured basis, certificate class and
margin where applicable, and de-overlapped raw total from
`findings.json`. The checks SKIP, never FAIL, on genuinely
pre-stamp findings JSON so older worked examples keep verifying until their data
is regenerated; if top-level Tier-2 stamps are present with positive
runner-minute candidates but the per-finding stamp surface is missing, they fail
closed instead of treating the artifact as pre-stamp.

Whole-run Tier-2 detectors can stamp `tier2_sample_run_ids`, the sampled
workflow-run IDs behind their credited count. `_reconcile_tier2_overlap` uses
those IDs to move duplicate sampled-run credit into `runner_min_overlap_s`
before totals are rendered, so the section lead can show both the
naive and credited-after-de-overlap minute totals. Event-subset certificates
also carry a machine-readable event stamp: OPT36 schedule burn promotes only
`tier2_run_subset_events: ["schedule"]`, and `verify_report.py` re-derives that
`non_pr_event` proof from the persisted workflow events plus the detector's
schedule-only evidence. The persisted mirror (`events_by_wf`) is first built from
the main-pass SUCCESS slice, so a `[push, schedule]` workflow whose recent
successes are all `push` would omit `schedule` and fail that re-derivation;
`_fold_observed_events` unions the events the dedicated schedule probe actually
observed back into the mirror so it stays a faithful superset of every event the
pipeline saw for the workflow.
When measured OPT36 is emitted, the matching static OPT36 finding for that
workflow is retained in JSON with `tier2_superseded_by` but excluded from Also
noticed, so the report shows the measured upgrade once.
The same supersession path is used by measured OPT35: explicit
`fail-fast: false` shard matrices promote only when failed-run job timings prove
sibling shard compute after the first failed shard, and the certificate remains
`post_completion_waste` because the run result was already decided. OPT35 emits
one measured finding per matrix job and supersedes only the matching static
workflow/job row, leaving unrelated shard matrices in the residual appendix
until they have their own failed-run evidence.
OPT64 uses the same whole-run Tier-2 path for reruns: it compares a
`run_attempt > 1` run's `filter=all` jobs against `filter=latest`, credits only
the prior-attempt job delta, and requires every credited prior attempt to share
the same unique dominant failed/timed-out exact job name that appears in the
latest attempt. It keeps those prior-attempt seconds additive with OPT36/OPT46:
a bare workflow run ID is too coarse an overlap key because OPT36/OPT46 attribute
at the latest-attempt RUN level (OPT46 credits the cancellable remainder of each
superseded run, issue #89 — not its whole compute) while OPT64 credits
earlier-attempt job seconds. If job payloads omit `run_attempt`, the job-id fallback is disabled on
capped 100-job pages rather than treating a truncated page as measured waste.
OPT65 is the billing-rounding exception to the normal raw runner-minute
subtractive sizing: it computes the exact per-run billable-minute delta
`sum(ceil(job_seconds/60)) - ceil(sum(job_seconds)/60)` for tiny matrix legs
from sampled job timestamps. It emits per matrix base only when every credited
occurrence is tiny and on the same runner, and each credited run's combined leg p50 stays
strictly below the workflow cluster floor. It stamps structured
`rounding_waste` evidence, a `below_cluster_floor` certificate, and claims no
speedup (`wall_clock_p50_s=0`, `realization=none`). It deliberately does not
stamp bare run IDs for de-overlap: the credited unit is per-job billing round-up,
not whole-run elimination. The guardrail text must keep consolidation off the
merge gate; lowering matrix parallelism or adding a serial `needs:` stage for an
on-spine matrix is wall-clock-negative.

OPT57 now has a measured timeout-default-burn upgrade. A missing
`timeout-minutes` key is only the structural gate: `collect_runs.py` emits a
finding only when a failed/timed-out sampled job without an explicit timeout
burned at least 95% of GitHub's 360 minute default, and the same workflow job has
at least three explicitly successful timed samples that support a recommended
`timeout-minutes` above p99. Candidate workflows come from the scanned workflow
graph, so this detector can run even when a workflow has no prior static finding.
The recommendation is rounded up from
`max(p99 + 10m, p99 * 1.5, 15m)` and withheld when that approaches the default,
so long legitimate jobs do not become false savings; matrix jobs are withheld
until a per-variant p99 safety proof exists. The credited amount is the observed
failed-run seconds above the p99-backed timeout, scaled by the matching
event-scoped all-status workflow volume, with `wall_clock_p50_s=0`,
`realization=none`, structured `timeout_default_burn` evidence, and a
detector-specific `post_completion_waste` certificate re-derived by
`verify_report.py`. OPT57 uses exact non-matrix runtime job matching and its
job-scoped timeout samples participate in Tier-2 de-overlap against whole-run
eliminators without collapsing different timeout jobs in the same workflow run.

The cost spine is **runner-minutes only**. The pricing layer (`scripts/billing.py`,
per-SKU rate derivation, `references/runner-rates.json`, and the SKU-arbitrage
ceiling detector) was retired 2026-07-20 — the report states runner-minutes and
leaves the per-minute rate to the reader (the maintainers' pre-public
development archive preserves the pricing infra for later re-introduction).

The v2 cost-spine table is intentionally **invariant-first**. It must not be
rendered from ad hoc markdown-side aggregation. The implementation must first
produce a machine-readable findings JSON source block (working name:
`runner_minute_spine`) with stable row identity `(workflow_file, job_name,
runner_label, event_scope, status_filter, attempt_filter,
volume_filter)`. Each row must stamp its sample window, sampled workflow-run
count, sampled job occurrence count, sampled positive-duration occurrence count
(the occurrences GitHub actually bills — zero-span jobs, `started_at ==
completed_at`, bill 0), occurrence fraction or effective monthly job volume, 30d
workflow volume, mean sampled compute seconds per occurrence, mean sampled
billable-equivalent minutes per occurrence (`ceil(duration/60)`), raw compute
runner-min/mo, billable-equivalent min/mo, and share of the all-row derived
total. Because a
bucket can mix a short real run with zero-span occurrences, its MEAN
billable-equivalent minutes can legitimately fall below 1.0 while its MEAN
compute seconds stays positive; the per-occurrence 1-minute floor is therefore
re-derived from the stamped positive-duration count (`round(mean_billed ×
occurrences) ≥ positive-duration occurrences`), never from the aggregate mean
alone. Raw compute minutes are diagnostic; the share-of-total re-derives from the
billable-equivalent basis.
The block must also carry repo visibility, or point to the findings root value.
`verify_report.py` must fail closed when a rendered
cost-spine row has no matching JSON row, when row identity or sample-population
semantics are ambiguous, or when raw minutes, billable minutes,
or percentage of total do not
re-derive exactly within the existing rounding tolerance. Render may sort or
truncate rows, but it must disclose hidden rows and keep the percentage
denominator over every row in findings JSON. Any renderer emitting a cost-spine
surface must stamp the explicit `<!-- ci-speedup:runner-minute-spine -->`
marker, and verification must fail when that marker appears without a valid
`runner_minute_spine` block. Reviewers must also reject visible cost-spine
headings, tables, or totals that omit the marker because the verifier cannot
bind unmarked markdown to the source block. The first cost-spine implementation
PR must be data + verifier fixtures before any visible table, matching the
Tier-2 stamp-first pattern from PR-1/PR-3.

`collect_runs.py` stamps `runner_minute_spine`, grouping
sampled successful latest-attempt jobs into all-events rows and sampled
prior-attempt jobs into separate `attempt_filter: prior`,
`status_filter: all-status` rows. Every row stamps `volume_filter: all-status`
so the sampled job status/attempt population and 30d workflow volume population
are explicit. Still-triaged workflows are fetched into a cost-spine-only sample,
leaving critical-path triage unchanged while allowing sub-floor workflows to
appear in the cost-spine source block. The collector seeds the explicit
workflow-coverage set from paths returned by GitHub's workflow list plus
repo-rooted `.github/workflows/*.yml|*.yaml` paths, so source-file findings such
as OPT19 `tests/conftest.py` keep finding provenance without becoming
workflow-volume coverage gaps while API-missing workflow files still fail closed
as unknown-volume workflows. The block stamps
`coverage_scope: sampled_workflows_in_play_with_job_data` plus
`workflow_coverage` counts/omissions/failures; `complete_repo_coverage` is true
only when every positive-volume workflow in play has rows, every in-play
workflow has known volume, and no cost-spine source-row job fetch failed.

When `complete_repo_coverage` is true, the producer stamps `render_ready: true`
and `blocking_path.py` renders the marked
`<!-- ci-speedup:runner-minute-spine -->` table at the top of the Runner-minute
reductions section. The table is derived only from `runner_minute_spine`,
includes the all-row Total row, sorts by billable minutes, caps visible rows, and
discloses hidden rows next to the table. When
the spine renders without promoted Tier-2 findings, the section heading is
`Runner-minute cost spine` rather than a reductions claim. `verify_report.py`
validates the source block when present, fails any marked table without a valid
source block, rejects unmarked cost-spine headings/tables/totals, parses the
marked markdown table when `render_ready: true`, re-derives every rendered row
and the final blank-metadata Total row from the source block, requires the
rendered rows to match the sorted visible source rows,
requires and cross-checks cost-spine fetch failures against `data_sources`, and
rejects complete coverage that is not render-ready (or render-ready coverage
that is not complete). The Tier-2 verifier also re-derives each visible R-row's
`Source block` line from `runner_minute_spine`, failing promoted savings rows
that lack render-ready matching source rows, whose rendered row
count/minutes drifts from the source block, whose marker IDs are
duplicated, or whose credited savings exceed the matched source-row totals
(billable totals for OPT65).

A one-line **workflow ▸ jobs ▸ steps** hierarchy glossary
(the report's only definition of the three terms) renders once, under the
`## 🗺️ Long pole map` heading, and only when that section renders (§12.0).

**Exception — pre-start WALL-CLOCK wait.** Queue time (OPT43, the `_WAIT_PATTERNS`
set) is NOT hygiene: it is developer wall-clock wait *before* a job starts, which the
critical-path spine (job start → finish) doesn't capture, and it carries no
runner-minute saving. So `_queue_wait_block` renders it in its own **"⏳ Pre-start
wait (queue time)"** section ABOVE the hygiene appendix (with a Contents pointer),
ranked by P90, excluded from `_also_noticed_block`. For a `needs:`-gated job the
metric is wait-to-start — it includes the gating job's run time, so the embedded agent
prompt (`_hygiene_prompt(wait=True)`) frames it as wall-clock wait bounded by the
gating job's own fix, not a bill cut. See the OPT43 note in §3 and `collect_runs`
`_detect_opt43_queue_time` (`wc_p50` holds the floor-capped **savable** wait for
this family; the raw P90 is in `wall_clock_uncapped_p50_s`, present only when a
bound actually capped the P90 — absent when nothing capped, where `wc_p50` is
itself the raw P90).

The sizing rules live in two reference docs - read them before touching
`_size_finding` or the detectors:

- [`references/wall-clock-methodology.md`](references/wall-clock-methodology.md)
  - critical-path / long-pole / cluster-floor model, the serial-gate
  wall-clock-negative case, and the non-additive stacking rule.
- [`references/savings-methodology.md`](references/savings-methodology.md) -
  two-axis sizing, monthly-volume computation, the cache-finding log-evidence
  guardrail, and the bimodal-tail / serial-gate guards.

## 5. The wall-clock lever model - a cascade of physical bounds

This is the model that keeps the report honest about developer wait. The mental
model in one line:

```
developer wall-clock wait
   = max over all workflows triggered on the PR of (that workflow's critical path)

a finding is a real wall-clock lever
   ⟺ it shortens the long pole of the GATING workflow,
      and only down to the next floor
```

A workflow's critical path is **not** the sum of its job-seconds. With fan-out
parallelism, `wall-clock ≈ entry-gate + max(parallel jobs) + joiner +
scheduling overhead`. So shortening a job only helps if that job is the *long
pole*, and only until it drops to the *cluster floor* (the second-tallest job).
Across workflows, shortening one workflow only helps if it is the *slowest*
workflow running on the PR.

### The two measurement sources (and why the model needs both)

The merge-wait side of the model is fed by a deliberate two-source hybrid:

- **Per-commit check-runs** (`commits/{sha}/check-runs` on sampled PRs,
  `_fetch_check_runs`) define **what gates a PR**: the full concurrent set across
  every workflow, including fileless checks (CodeQL default setup, third-party
  apps) no YAML scan or job listing can see. This is why the crown and the
  per-PR `populations` derive from check-runs, never from any single workflow
  run's duration — the merge wait is a max over concurrent checks, not one
  workflow's runtime.
- **Sampled job timings** (`actions/runs/{id}/jobs`) define **how long the work
  measurably takes**. A check-run clock spans creation → completion, so it
  absorbs queue time and re-run inflation; `_pole_caps` builds per-check caps
  from the sampled job p50s (`job_p50_all` / `job_bimodal_all`) and
  `_segment_pr_populations` applies the SAME caps when building `populations`,
  so the engine's argmax and every verifier re-derivation see identically
  de-inflated values. Job data also feeds the step drill-downs and the
  runner-minute spine.

Each source screens the other's failure mode: jobs alone miss fileless checks
and per-PR gating frequency; check-runs alone are inflatable clocks. A check
with **no** sampled job to cap it has nothing grounding its span in measurable
Actions compute — such fileless spans measure how long the gate sat open across
the PR's lifetime and are excluded from the crowning basis entirely (disclosed
separately; see §12, and issue #42 on the disclosure vocabulary for
external-primary-CI repos).

### The cascade (raw estimate → bounds → effective)

CAP 1 is applied during per-finding sizing (it's model-specific - sharding
intentionally skips it). The cross-cutting bounds run afterward in
`wall_clock.size_wall_clock`, which enforces the invariants and records the
derivation.

```
 raw per-pattern wall-clock estimate
   (default_s, or a measured per-run figure)
        │
        ▼
 ┌─── CAP 1 - within-workflow critical path  (in _size_finding / detectors) ─┐
 │   own_job_p50 ≤ floor_p50  →  0  (below the cluster floor: bill only)      │
 │   else  →  min(wc, long_pole_p50 - floor_p50)  (shorten only to next job)  │
 └──────────────────────────────────┬─────────────────────────────────────────┘
                                     ▼   size_wall_clock(raw, ctx) runs CASCADE:
 ┌─── developer-facing gate  (bound_developer_facing) ───────────────────────┐
 │   workflow's sampled events all non-PR (push/schedule/workflow_run)        │
 │     →  0   (post-merge/scheduled time, not developer PR wait)              │
 └──────────────────────────────────┬─────────────────────────────────────────┘
                                     ▼
 ┌─── MEASURED critical-path floor  (bound_measured_critical_path) ──────────┐
 │   from gh check-runs of a representative PR - INCLUDES fileless checks     │
 │   (CodeQL default setup, app checks) the YAML scan can't see.              │
 │   NO sampled job for this workflow (config-file / dormant finding) → 0     │
 │     (a modeled saving on a workflow that isn't even a PR check)            │
 │   slowest OTHER concurrent check ≥ this workflow's slowest check → 0       │
 │   else  →  min(wc, own_max_check - slowest_other_check)                    │
 └──────────────────────────────────┬─────────────────────────────────────────┘
                                     ▼
 ┌─── cross-workflow floor  (bound_cross_workflow) ──── FALLBACK ────────────┐
 │   only when check-runs unavailable: slowest sampled sibling workflow      │
 └──────────────────────────────────┬─────────────────────────────────────────┘
                                     ▼
              final wall_clock_p50_s + wall_clock_derivation on the finding
   (runner_min_saving is NEVER touched by any bound - the bill saving is
    real even when the wall-clock saving floors to 0)
```

The **measured critical-path floor** is the most important screen: a finding
only keeps wall-clock credit if its job is at/near the slowest check that
actually runs on a representative PR (`gh commits/{sha}/check-runs`). This is
the only source that sees checks with NO workflow file - CodeQL default setup,
third-party app checks - which are frequently the true long pole. Without it,
a #3 job (e.g. mastra's Docs E2E at 334s) was crowned the top wall-clock win
while CodeQL (1359s) and changed-tests (788s) gated the merge unseen. The same
floor also zeroes a finding whose workflow has **no sampled job** on the PR at
all - a config-file finding like `turbo.json` (OPT58) carries only a *modeled*
per-run estimate (e.g. 30s) with no measured presence on the critical path, so
crediting it wall-clock would rank a hygiene tweak above the real long pole. It
keeps its runner-minute / cache-correctness value; it just stops claiming
developer wait it was never observed to remove.

The report makes this concrete in the **Long poles** section at the top
(`_long_poles_block`), the report's diagnosis. For each gating check it shows: how
often it is the ACTUAL pole across sampled PRs (from `pr_critical_path.populations`
— so a check that's huge but rare, like mastra's `changed-tests` ~750s on only
3/16 PRs, isn't crowned over `Lint` ~264s on 8/16), the per-step breakdown of its
job (`pr_critical_path.poles`, persisted by collect_runs via `_decompose_job_steps`
for EVERY top pole, not just structural-routed ones), and the **dominant
(root-cause) step** marked. This is pure diagnosis: the block prescribes no fix
and points the reader to the Findings section, where each gating check is a
finding with its own agent prompt. Render-time only (no new gh calls), purely
descriptive of the *current* blocking path.

The **Findings** section then lists the top-N detected inefficiencies by measured
impact (Δ wall-clock on the gating checks first), each as a root-cause card with
its evidence and a ready-to-paste agent prompt — no prescribed fix, no bang-for-
buck fix ranking. Findings past the top-N cap are named in a one-line pointer and
kept in the findings JSON. `verify_report` cross-checks the report
against the findings JSON.

**Run frequency is a core ranking input** (separate from the cascade), applied
RELATIVELY - no absolute cutoff. A per-run wall-clock saving on a workflow that
runs on few PRs removes little developer wait overall, so `collect_runs` records
each workflow's run-share (30d volume / busiest PR workflow) and the wall-clock
ranking weights each finding by it: `Δwc × run-share`. A saving on a workflow
developers hit on most PRs therefore outranks an equal saving on a rarely-run
one, and a docs-only `paths:` gate sinks proportionally - without any hard
demotion threshold. The top-N cap stays the only materiality gate (the same
relative-to-repo principle as wall-clock sizing: no absolute second/minute or
frequency floor). Each finding surfaces its share as a "Run frequency" row.

`size_wall_clock` enforces two invariants on every bound: **monotonic-down** (a
bound may only lower the saving - raises otherwise) and **no-silent-shrink**
(any reduction must carry a reason). It returns the `derivation` chain (bound,
before, after, reason), which collect_runs stamps on the finding and the report
renders as a per-finding audit trail. Adding a new bound = a new `Bound` appended
to `CASCADE` + its test; it then flows through every finding.

### What is measured vs derived vs assumed

- **Measured** (directly from sampled gh runs/jobs/logs):
  - per-job `started_at`/`completed_at` → per-job durations → p50/p95
    (`_critical_path`, `_percentile`);
  - per-step durations (from the job JSON, no extra API calls);
  - trigger events that actually fired each workflow (`run.event`, collected
    into `events_by_wf`);
  - cache hit/miss and install/build log lines (`--with-logs`).
- **Derived** (computed from the measurements):
  - `long_pole_p50` (max job p50) and `floor_p50` (second-tallest) per workflow;
  - the concurrency sets (`_concurrent_workflows`: which workflows share the
    chosen PR/developer-wait event);
  - both caps (CAP 1 long-pole headroom, CAP 2 cross-workflow headroom).
- **Assumed, and flagged in the finding's prose**:
  - sharding splits a suite's work in half (N=2 conservative floor) for
    OPT24/parallel-rebalance sizing;
  - concurrency ≈ a shared trigger event (`_concurrent_workflows` picks one
    primary event - `pull_request` > `merge_group` > `push` - to avoid counting
    a `push`-only sibling as concurrent on the PR path);
  - the ephemeral-runner cache caveat (OPT3/8/9): a "warm cache" wall-clock
    saving is unproven on ephemeral runners until a warm-vs-cold delta is shown.

### Where the cascade lives

The cross-cutting bound cascade lives in **`wall_clock.py`** - a stdlib-only
leaf module (no `config`/`collect_runs` import, so the bounds are unit-testable
via `import wall_clock` without the report/config name collision). It owns
`_resolve_job_p50`, `_wf_basename`, `bound_within_workflow` (CAP 1, applied
during sizing), `bound_developer_facing` + `bound_cross_workflow` + `_concurrent_
workflows` (the cross-cutting bounds), the `Bound` contract, `WallClockContext`/
`WallClockResult`, the `CASCADE` list, and `size_wall_clock`. `collect_runs`
owns the sampling and the `_critical_path` aggregation that PRODUCES the `crit`
dict the bounds consume, and calls `size_wall_clock` once per wall-clock-
positive finding. The report renders the cross-workflow context as a "Concurrent
workflows on the same trigger" table plus the per-finding derivation line.
Remaining bounds are tracked in [§10](#10-status-of-the-planned-bounds).

### 5.1 The measured sizing door — one derivation path for every runner-minute saving

The wall-clock cascade above bounds the **merge-wait** axis. The **runner-minute
(bill)** axis has a symmetric contract, enforced in ONE place: the *measured
sizing door* (`collect_runs._reground_runner_minute_savings`, run once after the
cost spine is final). It exists because each finding pattern historically carried
its own sizing path, so models kept pricing from modeled or single-sample bases
where measured data exists — three instance-fixes in a week (OPT45 #33, OPT73 #43,
`chain_win_s` #45). The door makes that structurally impossible:

**The contract.** Every finding that credits a positive `runner_min_saving` flows
through the door, which either DERIVES the saving from, or CLAMPS it to, the
MEASURED cost-spine billable of its affected jobs (`billable_equiv_min_per_month`,
summed via the shared `_measured_billable_index` / `_measured_billable_for_jobs`
join — at least as strict as `verify_report`'s `_base` join, so a door-derived
figure can only ever match a SUBSET of the rows the verifier bounds against). A
finding whose affected jobs miss the join UNSIZES honestly (`runner_min_saving =
None`, basis `unmeasured_no_spine_match`) rather than render an unbounded modeled
figure. Every sized finding stamps `runner_min_basis`.

**EXACT job identity in the join (issue #52).** The join keys on
`(workflow_file, base-job)` where a base-job is the full job name **modulo its
matrix-leg parenthetical** — `build-image / build-image (linux/amd64)` and
`(linux/arm64)` are two legs of ONE job (`build-image / build-image`), while
`build-image-streaming / build-image` is a DIFFERENT job. The index already SUMS a
base's matrix legs into one figure, so both the door and the guard reduce a
finding's affected jobs to their DISTINCT base identities before summing: a
finding that lists several legs of one job adds that job's compute ONCE, never
once per leg (the mastodon build-push-pr double-count that added the two
build-image legs' 16,642.4 twice → 33,284.8, escaping the clamp). The door and the
guard apply the SAME dedupe principle (L3), each through its own base normalization
— the door's `_whole_run_cancel_base_key` is at least as strict as the guard's
`_base`/`_cmp_name` (it does no scope-stripping) — so they tighten together and the
door still bounds to a subset of what the guard bounds.

**Per-pattern semantics, one basis + join.** A cancel-rate model multiplies
differently than a step-decomposition credit, so per-pattern *semantics* stay —
but the MEASURED BASIS and the JOIN live in one place. The policy is a total
function (`_rm_door_policy`, over `_RM_DOOR_OVERRIDES` + the `_SIZING` model
family):

| policy | patterns | rule | basis stamp |
|---|---|---|---|
| **derive** | OPT45 | `hit_rate × Σ(measured billable)` | `measured_spine_billable` |
| **clamp** | OPT73 | `min(modeled, Σ(measured billable))` | `measured_spine_clamped` (or `measured_spine_billable` when already within) |
| **not_spine_derivable** (the EXPLICIT whitelist) | the measured run-elimination detectors (OPT46/47/64/65 — basis is the eliminated-runs slice, not per-job billable); the modeled-static patterns (`direct` / `runner-min-only`, disclosed as modeled in the report's sized-of-total ratio); the other structural step-decomposition levers (OPT70/71/72/74/75, per-job step basis) | retained, with the reason recorded in `runner_min_door_note` | `not_spine_derivable` |

The whitelist is **visible, not a silent bypass**: a reasoned entry per family,
and tightening the modeled/structural families from whitelist → clamp is tracked
follow-up. A rm-crediting pattern with NO declared policy stamps the loud
`UNCLASSIFIED_door_policy` sentinel, and `check_saving_carries_measured_basis`
(`verify_report`) FAILs on it — so **a new pattern cannot ship its own unmeasured
sizing path**. Two more invariants complete the class cut:
`check_saving_within_measured_compute` (a saving never exceeds the compute it cuts;
PR #30) and `check_cluster_lever_ceiling_escapes_sibling` (§5's cluster-floor lever
must not be capped by its own matrix sibling — issue #44). The door SKIPs (like the
compute guard) when no render-ready spine exists, so a legacy pre-door artifact
never trips it.

### 5.2 Presence-weighting the cluster crown (issue #56)

The cascade above sizes a lever's ceiling by **magnitude**; §5.2 gates whether an OPT73 cluster-floor
lever may claim that ceiling as **wall-clock** at all. A cluster's fix only saves *typical-PR merge
wait* if the cluster is on the typical merge-gating critical path — i.e. its **workflow gates a
majority** of sampled PRs. So `_detect_shared_substep` presence-weights the cluster the same way the
spine ranks poles (§11): a minority-present anchor leg can't lead the Evidence, and a **minority-gate
workflow** demotes its wall-clock to **bill-only** (option **b**), keeping the runner-minute saving.
A majority workflow with one minority leg keeps its wall-clock and re-anchors (option **a**). The full
rule, its two-track `(a)`/`(b)` decision, and the converse guard live in
[§12.1b](#121b-the-cluster-crown-is-presence-weighted--a-minority-workflow-cant-headline-issue-56);
noted here because it is a wall-clock-crediting gate, alongside §5.1's sizing door.

### 5.3 The presence denominator is PR identity, not head-sha (merge-queue dedup, issue #58)

Every presence signal in §5.2 / §11 / §12.1b — `check_present_n_pr`, the per-check
`present_on`, `_gate_counts`, `_workflow_gates_minority`, `_leg_presence_eligible` — is a
count *over sampled PRs*. Those "PRs" are the head-SHAs `collect_runs` walks
(`pr_sha_ts` → `_select_repr_shas`), one population row per SHA. That identification of
**one head-sha = one PR** breaks under a **merge queue**: a `merge_group` run executes on a
GitHub-generated temporary branch `gh-readonly-queue/<base>/pr-<N>-<sha>`, a DISTINCT
head-sha from the PR's own head. A repo that runs its heavy suite in the queue therefore
minted a *second* population row per PR carrying only the queue's checks, so the heavy
suite read as present on a minority of the (now inflated) denominator and §5.2/§12.1b
demoted the real merge gate to bill-only while a lighter `pull_request` check was crowned —
confidently wrong on exactly the large-OSS merge-queue shape.

The denominator rule, applied **at the data layer before stamping** (`_group_dev_shas_by_pr`,
between `pr_sha_ts` accumulation and `_select_repr_shas`):

- **One population row per PR IDENTITY, not per head-sha.** A PR's row UNIONs the
  check-runs across every head-sha it ran on — its `pull_request` head *and* its
  `gh-readonly-queue` commit — so a gate that runs only in the queue is present on that PR.
- **Queue → PR linkage.** The PR number is recovered from the queue branch
  (`_merge_group_pr_number`) and matched to the `pull_request` run's
  `pull_requests[0].number`; runs sharing a PR number collapse onto one `("pr", N)` group.
- **Non-derivable fallback (the stated rule for unlinkable queue data).** A `merge_group`
  run whose branch is off the naming scheme collapses onto a SINGLE orphan class row — N
  such runs add **one** to the denominator, not N, so they **cannot dilute PR presence**.
  Their timing is still measured (the row carries the heavy suite); the wrong answer would
  be to silently drop queue data, so orphans are kept, just never multiplied.
- **No-queue repos are untouched.** With no `merge_group` run in the sample the grouping is
  the identity map (one member per row) — byte-for-byte the pre-#58 per-sha behaviour.

Because the correction lands in `per_sha_checks` before anything is stamped, `populations`,
`check_present_n_pr`, `_gate_counts`, and `chain_facts` all inherit it, and every
`verify_report` mirror — which re-derives presence/floors from the **stamped, already-deduped**
`populations` — needs no change. (Under a config-era straddle this PR-identity sample is scoped
ONE more time before those spine-facing consumers read it: `_era_scope_pr_spine_sample` (§2.1a,
issue #80) drops the dropped-era PR rows so `chain_facts` / `populations` / presence describe the
kept era only — a scoped COPY, so the full deduped sample still seeds the thin-flip decision.) (A verifier guard *for the inflation signature* is not
addable: the stamped artifact carries no run event / queue-branch provenance to re-derive it
from, precisely because the fix removes the signature before stamping.) Red-proofed by
`tests/test_mergequeue_presence_dedup.py`.

## 6. Admission gate & advisory routing

A finding is emitted ONLY when all three of `SKILL.md`'s admission criteria
hold (specific root cause, positive instance evidence, an applicable
tool-producible remedy). The implementation:

- **Config-change-only remedy.** `OPT13` (build step in jobs that don't need it)
  and `OPT15` (cross-workflow build redundancy) need judgment that has produced
  confident-but-wrong findings, so they are never auto-emitted. (The retired
  `report.py` surfaced them plus `OPT18` as a manual-review checklist appendix;
  the spine renderer dropped that appendix — they stay catalogued for human
  application and are not rendered.) Omit rather than fake.
- **Reliability / non-CI-config remedy → advisory.** A "finding" whose only
  remedy lives in a domain ci-speedup can't edit (e.g. "go fix your flaky
  test") is a *signal*, not a ranked optimization. `scan.py`'s
  `_ADVISORY_PATTERNS = {"OPT19"}` (test-source sleeps - the fix is test code,
  not CI config) stamps `advisory: true` at emit time; `collect_runs.py` does
  the same for `OPT48` (high failure rate). `blocking_path.py` excludes advisory
  findings from the "Also noticed" appendix and the headline - they are not
  rendered in the report (they stay in the findings JSON).
- **Unprovable cache → dropped.** With `--with-logs`, a cache-family finding
  whose logs show **no** cache line AND no install/build activity AND no
  measurable cost is unprovable (absence is not evidence). `_attach_cache_log_
  evidence` marks it `_drop` and `collect()` moves it to
  `dropped_unprovable`. When logs show the cache **hitting** on every sampled
  run, the finding is kept but its wall-clock is zeroed and re-flagged as config
  hygiene (the evidence refutes a wall-clock claim).

`scan.py` never emits an OPT-id absent from the catalog, and reports every
catalog pattern lacking a registered detector in
`catalog_patterns_without_detector` rather than faking coverage.

## 7. Evidence, provenance & verification

- **Measured evidence.** Data-driven findings carry a `measured_evidence` table
  whose cells link the **actual GitHub job logs** (`html_url` per sampled job) -
  a timing claim is proven by run timings, not by a workflow `file:line`. The
  report renders this table instead of a code permalink. Cache findings quote
  the **verbatim** cache hit/miss line from the job log.
- **Provenance.** `scan.py` records `skill_commit_sha`, `commit_sha`, and
  `repo` (`run.py` forwards them, deriving each from git HEAD when not passed, so
  a run never records a NULL sha). `blocking_path.py` renders the metadata header
  as `ci-speedup skill commit <sha>`, appending `-dirty` when the orchestrator
  passed `skill_tree_dirty` (the HEAD sha alone can lag uncommitted working-tree
  edits). The catalog is linked at that exact commit (content-addressable
  permalinks).
- **Installed copies have no git HEAD — the lockfile is the fallback (issue #2).**
  The skills CLI installs `skills/ci-speedup/` as a recursive copy with **no
  `.git`**, so the git derivation above returns None. Rather than record a NULL sha
  (which blanks the footer and FAILS `check_skill_commit_provenance`, historically
  forcing the agent to re-run the whole gh data pass just to pass a *guessed*
  `--skill-commit-sha`), `run.py` reads the installer's `.skill-lock.json` (a
  sibling of the installed skill dirs; a lockfile inside the skill root is accepted
  defensively) and stamps a **distinct `installed:<hash12>` provenance form** from
  the entry's `skillFolderHash` — or the terminal `installed:unversioned` when no
  lockfile/entry exists. Never NULL, never a failed run, never a guessed remote sha;
  an explicit `--skill-commit-sha` still wins. The lockfile records a content hash,
  **not a commit**, so `blocking_path.py` renders it as a plain `skill build
  <installed:…>` identity string with **no fabricated commit/catalog URL**, and
  `check_skill_commit_provenance` **accepts** the `installed:` forms for
  live/installed runs (verified without `--skill-repo`) while still **rejecting**
  them for committed worked examples (verified with `--skill-repo`), which must
  carry a real, resolvable git sha.
- **Provenance is squash-proof for committed examples.** The problem, stated
  first: a report is stamped with the commit that rendered it, and a later
  **squash-merge discards that commit**, so a pure ancestry gate turns `main` red
  for a legitimate, unchanged report (it did, once).

  The **original** resolution was to stop enforcing ancestry on the worked
  examples - `test_committed_reports.py` passed no `--skill-repo`, so
  `check_skill_commit_provenance` reported `skipped`. That kept only the weaker
  guarantee that a well-formed, non-null skill commit is recorded. It was a
  deliberate, documented tradeoff, not an oversight - but it meant two committed
  reports (`psf/requests`, `mastra`) carried skill commits unresolvable on `main`
  and nothing said so.

  **Superseded 2026-07-08 (OD10).** A squash-merge discards commits but
  **preserves trees**. `blocking_path.py` now also stamps the render-time git tree
  of `scripts/` (`_skill_scripts_tree_sha`), rendered as
  `(skill commit <sha>, scripts tree <tree>)`. `check_skill_commit_provenance`
  passes when the recorded commit is an ancestor **or** the recorded scripts tree
  equals HEAD's exactly, and refuses a `-dirty` tree (rendered from uncommitted
  code, whose sha names the committed tree rather than the code that ran).
  `scripts/` and not the whole skill dir, because the skill dir can contain
  committed reports (the archive's `reports/` corpus) - stamping the whole dir
  would make committing a report change the very tree that report claims to
  have been produced under.

  So the gate is now **enforced** on the committed examples, against the real
  checkout, and it no longer false-reds on a squash. The cost, accepted
  explicitly: an exact tree match means any change to `scripts/` makes every
  committed report stale until re-rendered (zero gh calls; it re-renders from the
  committed `findings.json`). That is what makes the "ship the refreshed worked
  examples with the change" rule a red test rather than a convention.
  Net: **any merge method - squash or merge-commit - is safe.**
- **What `verify_report.py` enforces** (one PASS/FAIL command, standalone - no
  imports of the skill scripts): the primary section is present (>=1 `## Long
  pole N:`, or the fileless note); the title + `Bottom line` name the wall-clock
  (merge-wait) axis; every `#pole-N` / `#pre-start-wait` /
  `#runner-minute-reductions` / `#also-noticed` reference resolves to an
  anchor; the RCA hands off via per-finding agent prompts and never prescribes a
  fix or dead-ends; the TOC's "Also noticed" count equals the appendix rows +
  hidden pointer; the data basis is disclosed and any coverage gap names its
  file(s); `Date (UTC)` matches the filename date; the report is ASCII-hyphen
  only (no typographic dash); no ci-secure security-domain leakage; (with
  `--skill-repo`) the skill commit is HEAD or an ancestor of it, **or** the
  rendered `scripts tree` token equals HEAD's `scripts/` tree (the squash-proof
  path, OD10); every rendered
  finding pattern exists in the findings JSON; every data-driven finding carries
  a `measured_signal`; Tier-2 rows re-derive from the stamped measured basis,
  neutrality certificates, de-overlapped totals, and billing rates; and the
  report drills **one fully-formed long pole per independent gating check** (>=2
  when >=2 gate), each ending in a hand-off prompt — the sole exception being an
  **aggregation gate** (§12.6b), which carries no drill and no prompt by design
  and is held to the mirror-image invariant instead (it must name its slowest
  upstream member and prescribe nothing).

## 8. Reproducibility & determinism

Detection, ranking, and the measured numbers are deterministic by construction
(no LLM in that loop; the phase-4a gap-fill is the lone, labelled exception and
touches none of them). The remaining
source of drift was live gh re-sampling: a regen would sample whatever runs
exist *now*, producing a different finding set. The **run-sampling pin** closes
this: `collect_runs.py --created-before <ISO>` samples only runs created at or
before the timestamp.

- `_window_30d(created_before)` - unpinned, the 30-day window ends "now"
  (`created>=now-30d`, original behavior); pinned, it ends *at the pin*
  (`created=now-30d..pin`) so the window doesn't drift forward. **"Now" is
  `_unpinned_now()`, resolved ONCE per process.** `datetime.now()` has second
  resolution and a real collection runs for minutes, so re-reading the clock per
  call made the unpinned window *slide mid-run*: two workflows' volume probes
  issued a second apart were counted over two different 30-day windows. One
  window per run.
- `_sample_runs(...)` appends `&created=<=<pin>` so the sampled N successful
  runs are reproducible; `_monthly_volume` and `_detect_opt48_failure_rate` use
  the same pinned window for their `total_count` queries.
- The pin is recorded as `data_sources.sampled_runs_created_before` (null =
  unpinned). Pass the prior audit's scan time to a regen and it reproduces the
  same finding set instead of re-sampling live history.

gh usage is frugal: one workflow-list, one total-count per workflow, one
job-list per sampled run (default 8), and one log per hottest cache job under
`--with-logs` - no per-step API calls (step timings come from the job JSON).
*How* those calls are issued (one shared pool, bounded prefetch waves, a token-wide
rate governor) is §2.2; it changes the wall-clock of the pass, never its contents.

### 8.1 What the data pass does NOT re-fetch

Four things the pass could ask GitHub for twice and gets locally instead.

**The equivalence is CONDITIONAL, and the condition is checked at runtime, not
assumed.** Each derivation below can decide the value from a payload already in
hand *for most inputs* — and for the inputs where it cannot, it says so and pays
for the fetch. That distinction is the whole design: a derivation that returns a
confident answer from a payload that could not support one does not save a call,
it manufactures a wrong number. Two of these carry a "cannot decide -> UNKNOWN,
go ask REST" branch for exactly that reason, and `unknown != empty` is the rule
they enforce (§7).

On the corpora we measure, the conditions hold nearly always: the offline
full-pipeline replay drops 41 gh calls to 35, every sampled value byte-identical,
and the call count is pinned as a golden integer (`_GOLDEN_GH_QUERY_COUNT`) so a
future change to the budget has to be a deliberate edit.

- **One run-list page per workflow.** `_all_status_runs` (all conclusions) is a
  superset of the `status=success` sample, so `_success_runs_from_all_status`
  derives the sample from it and the run-elimination detectors (OPT35/46/47/57/64)
  reuse the same page.
  **Cannot decide when:** the page is FULL (truncated at `_COST_RUNLIST_MAX`) and
  still holds fewer than `--max-runs` successes — it cannot see far enough back.
  Only then does the explicit `status=success` query fire. A *short* page is the
  workflow's whole visible history, so its successes are all the successes there
  are; falling back there would re-fetch the identical runs and make a monorepo
  full of small/rarely-run workflows pay **two** run-list calls where it used to
  pay one — turning the reduction into a regression on the repos with the most
  workflows. If the fallback query itself fails, the derived sample is KEPT
  (`_sample_runs(...) or runs`): a short real sample beats no sample.
  The page is also cached fetch-once — but **only on success**. A failed fetch is
  never cached: `_all_status_runs` returns `None` (not `[]`), because `[]` is a
  real answer ("this workflow has no runs") that would report the entire
  run-elimination family CLEAN over a literal "0 of 0 runs" evidence line on the
  strength of one transient timeout.
- **One job-list per attempt-run.** REST's `filter=latest` is a server-side filter
  on a field the `filter=all` payload already carries (`job.run_attempt`), so
  `_latest_attempt_jobs` derives it. This holds on **partial** re-runs too, which
  is the non-obvious part: GitHub materializes the FULL job graph into every
  attempt, so a job that "Re-run failed jobs" did not re-execute still appears
  under the new attempt — new job id, new `run_attempt`, original timestamps.
  There is no carried-over job left behind at `run_attempt: 1` for the derivation
  to drop. (Pinned against recorded `filter=all` + `filter=latest` payloads for a
  real 3-attempt partial re-run: `tests/fixtures/gh_recorded/`.)
  **Cannot decide when: the payload is TRUNCATED.** The jobs endpoint is fetched
  unpaginated and `filter=all` returns jobs **oldest-attempt-first**, so a
  >100-job run's page 1 can be *entirely prior-attempt jobs* — every one of them
  carrying a `run_attempt`, so a missing-basis guard sails right past it. Left
  unguarded the derivation returns `[]`, `_dominant_prior_failing_job` finds no
  latest-attempt keys, and **OPT64 can never fire on a big-matrix repo** — a
  guaranteed false-negative class on exactly the repos where re-run waste costs
  most. So a truncated payload is UNKNOWN, and `_attempt_job_samples` re-fetches
  `filter=latest` for that run. Cost is bounded by the number of truncated
  attempt-run payloads (zero on the better-auth corpus).

  **Truncation is a fact about the PAYLOAD, and it disables the attempt-scoped
  derivations on BOTH sides.** `_JobsPayload` (a `list` subclass) carries a
  `.truncated` flag set by the fetcher from the endpoint's own `total_count`
  (`len(jobs) < total_count`), falling back to the page-size heuristic when a
  caller hands over a bare list. `_prior_attempt_jobs` checks it **first, before
  any basis** — the explicit `run_attempt` path included:
  - a truncated `filter=all` page yields a prior-attempt set that is silently
    SHORT (recorded: dbt-core run 29121623799 — 100 of attempt 1's 114 jobs), so
    OPT64 would size re-run waste against a partial set and the "unique dominant
    failing job" contest could crown a job that only wins because the true one was
    cut off — a **wrong root cause, asserted confidently**;
  - a truncated `filter=latest` page yields short `latest_keys`, so a prior failing
    job that IS in the latest attempt looks absent and the finding is withheld.

  Deriving truncation from `total_count` rather than from length is what keeps this
  from becoming a permanent OPT64 kill-switch: when the jobs fetchers are
  paginated, a complete 150-job payload reports `truncated=False` and every guard
  keyed on it becomes a no-op, rather than declaring the complete payload UNKNOWN.

  Deriving `filter=latest` also fixed a double-count: the two-fetch path ran the
  fetch-failure accounting once per fetch, charging one unreachable run to the
  cost-spine coverage gap twice.
- **Workflow YAML comes off disk.** `_fetch_workflow_docs` reads
  `<--root>/<wf_path>` (the checkout `scan.py` already walked), falling back to
  `GET /contents/{path}` when the local read yields **no usable doc** — missing,
  empty, conflict-markered, or invalid YAML alike. (All four are the same fact,
  "we learned nothing here". Falling back only on a *missing* file would DROP a
  present-but-broken workflow, silently no-opping every `wf_doc`-gated detector
  for it — OPT35's shard specs, OPT57's timeout specs, OPT24's shard recognizer,
  `_declared_pr_workflows` — and an absent finding reads as clean.)
  This is a **tradeoff**, not a free win, and the two sources genuinely disagree
  whenever the checkout and the default branch do. `/contents/` serves the
  *default branch's HEAD*, which is not necessarily the commit the report stamps
  as audited. The checkout's working tree *is* what the report stamps — so
  parsing it makes the stamp true — but it can also be dirty, or a feature branch
  that produced none of the sampled runs (a workflow that gained a
  `pull_request` trigger last week has PR runs in the sample but a push-only `on:`
  block in an old checkout, and `_declared_pr_workflows` would drop a real PR
  gate). `run.py` passes `--root` on **every** run, so this is the default path,
  not an opt-in. We prefer the checkout — *parse what you claim to have audited* —
  and **disclose the skew** rather than hide it, on three axes:
  - **Right repo?** The local read is used **only after the checkout is verified to
    be a clone of `--repo`** (`_root_is_clone_of`, via the origin remote). A
    mismatched `--root` would otherwise size one repo's workflows against another
    repo's timings, silently. Unverifiable (no remote, not a checkout) falls back
    to the API.
  - **Right commit line?** `_root_branch_skew` compares HEAD against the repo's
    `default_branch` — a feature branch, a detached HEAD, or a `main` that is
    behind/ahead of `origin/main`. This is the half `-dirty` cannot see: a clean
    checkout on a feature branch has no uncommitted change, so the stamped sha is
    perfectly *true* while the detectors parse YAML that produced **none** of the
    sampled runs. Stamped as `data_sources.workflow_yaml_skew` and rendered as a
    NAMED warning in the provenance block. We disclose rather than silently switch
    to the API, because quietly parsing the default branch while stamping the
    checkout's sha breaks *parse what you stamp* in the other direction.
  - **Which source, per workflow?** `data_sources.workflow_yaml_source` records how
    many workflows were read from the checkout vs fetched from the API, and the
    Data-sources footer renders it. (Uncommitted edits are separately stamped: the
    audited commit renders `<sha>-dirty` when `.github/workflows` is dirty,
    `_workflows_are_dirty`.)
- **A run's jobs are fetched once.** `_JobFetchMemo` keys the per-run job fetch
  by (fetch flavour, repo, run id), so the event-scoped detector probes (OPT36's
  schedule sample, the OPT35/OPT57 failure samples) read runs the main pass
  already fetched instead of re-fetching them. Only successful fetches are
  memoized - a failure is re-tried, never inherited - and hits are deep-copied,
  so two call sites never alias the same job dicts. The miss path takes a
  **per-key lock**, so two threads racing the same run id make ONE call (a plain
  check-then-act de-duplicates only sequential accesses — fine for today's
  per-workflow pools, wrong the moment the job pools are flattened cross-workflow).

#### A SKIPPED detector is disclosed, by name

The `unknown != empty` rule (§7) says a detector whose basis run list could not be
fetched must be SKIPPED, never sized against a laundered `[]` — which would render
"0 superseded runs of 0 runs sampled" as CLEAN off one transient gh timeout.

Skipping alone is only half of it. **A finding that never appears reads exactly
like a finding that looked and found nothing.** Left at a stderr warning, the
false negative just relocates: from *reported clean off 0 runs* to *silently not
evaluated*, which is harder to catch because it looks fixed. The generic gh-error
footnote cannot carry the disclosure either — it says a few runs are absent and the
P50s are marginally thinner, which is a **different failure** and simply false here
(no P50 is affected).

So the skip is DATA, and the reader sees it:

- `collect_runs` records `data_sources.detectors_skipped` — `[{workflow, detectors,
  reason}]` — at each skip site (the all-status page for OPT35/46/47/64, OPT57's
  scoped page, OPT36's schedule page). Only detectors that WOULD have been
  dispatched for that workflow are named (OPT35/OPT57 are additionally gated on the
  workflow's own YAML, which is available regardless).
- `blocking_path` renders a NAMED provenance bullet per affected workflow: *"`ci.yml`:
  OPT46/OPT47/OPT64 did not run … their absence from this report is UNKNOWN, not
  clean."*
- `verify_report` enforces it: a non-empty `detectors_skipped` whose workflows and
  detector ids are not named in the rendered report is a **FAIL**.

`gh_error_count` counts distinct unavailable **resources**, not attempts at them:
the run-list page is deliberately re-tried by the detector loop after the shallow
loop's fetch failed, and billing one dead page twice inflates the coverage banner
against the only question its reader is asking.

## 9. Testing strategy

Tests live under `tests/` and are picked up by the repo-root `pyproject.toml`'s
`testpaths`; CI runs `pytest -v`.

- **Detector unit tests** (`test_scan_detectors.py`, ~40 cases) - positive and
  **negative** (suppression) cases for the static detectors: OPT1/2/5/9/14/16/
  18/21/27/28/29/31/33/36/39/62/63, including load-bearing carve-outs (OPT28
  git-history jobs and local composite actions, OPT33 aggregator/change-detection
  suppression, and the shared **activation-fidelity** gate — a `pull_request:
  types:`-gated or job-`if:` label/activity-gated job is not "every PR", so
  OPT33/39/40 suppress it: `_pr_trigger_runs_every_pr`/`_job_runs_on_every_pr`).
- **Sizing & bounds unit tests** (`test_collect_runs_sizing.py`, ~25 cases) -
  the cascade and the sizing models: cross-workflow concurrency (`_concurrent_
  workflows`, both caps, the non-gate zeroing and no-op cases), the
  `--created-before` window math, serial-gate negativity, below-floor demotion,
  matrix-name → display-name resolution, job-scoped sizing (OPT33/40) vs the
  long pole, qualitative degradation, and the ephemeral-cache caveat.
- **Measured-evidence & render tests** (`test_measured_evidence.py`) - OPT24
  long-pole context, OPT25 heterogeneous-split vs sharded-rebalance framing,
  verbatim cache miss/hit extraction, the unprovable-cache drop, OPT48 advisory
  shape, and that the report renders the evidence table (not a YAML fence).
  Several assert against **committed worked-example** reports under
  `reports/` (a corpus maintained in the pre-public development archive — this
  repository ships its public worked examples under `examples/` instead, and
  these guards skip loudly when the corpus is absent).
- **Committed-report guard** (`test_committed_reports.py`) - the committed
  `reports/<repo>/` (archive corpus; skips when absent) are **documentation**
  (illustrative worked examples), NOT the
  test input. The guard renders each committed `findings.json` **fresh** with the
  current renderer and runs the invariants (no typographic dash, no coverage-gap
  dead-end, and the full `verify_report.py` check set) against that fresh render,
  so a real renderer↔verifier drift SURFACES instead of hiding behind an old `.md`.
  A pre-existing per-check drift is tolerated only via `_KNOWN_DRIFT` (with a
  self-expiring tripwire), never a whole-repo skip; a non-empty-render +
  min-checks-fired floor blocks a vacuous pass. The committed `findings.json`
  **data** shape is separately gated by `test_measured_evidence.py`.

  **Amended 2026-07-08 (OD10).** This bullet used to add that the examples are
  "free to lag the renderer" and that "a renderer change can't force a report
  regen". Both are withdrawn. Unenforced in either direction, that licence let
  `langfuse` and `mastra` drift silently from the renderer for weeks. A committed
  report must now equal a fresh render and carry a `scripts tree` token resolving
  in this checkout, so **a change to `scripts/` does force a re-render** - one
  command, zero gh calls, since it renders from the committed `findings.json`.
  Rendering also writes `<report>.md.claims.json`; that sidecar is part of the
  committed artifact (it switches `verify_report` to manifest-first comparison and
  binds each claim's rendered sentence to the report), so it is committed too and
  guarded for tracked-ness, not merely presence.
- **Blocking-path report tests** (`test_blocking_path.py`, §12) - the five leaf
  detectors + the unrecognized-log fallthrough; the step **timeline** Gantt (offset
  bars, dominant-step marking, tiny-step collapse, P50 fallback, the check-vs-job
  reconciliation); and the **cross-run check** (bracket-vs-median labelling by n,
  the IQR verdict that reads a tight-cluster-plus-outlier as *stable*, n<2
  suppression). The bundle capture — `_persist_pole_logs` nearest-P50 selection,
  `_step_timeline`, and `_magnitude_sample`'s probe→escalate + categorical-skip — is
  covered in `test_structural_findings.py`.
- **Artifact guard** - `verify_report.py` is the one-command report-invariant
  checker (see §7), runnable on any landed report and wired into the e2e flow.

### Em-dash sanitizer

The **rendered RCA reports** must use a plain ASCII hyphen. Em-dashes leak in
from many sources (hardcoded prose, detector notes, catalog TL;DRs), so
`blocking_path.py`'s `render()` strips them **once at the
render boundary** via `_strip_emdashes` (replacing the glyph with `-`, preserving
spacing) rather than chasing every source. The committed-report guard
(`test_committed_reports.py`) renders each committed `findings.json` **fresh** and
fails CI on any typographic dash in that render (catching a renderer regression,
not a frozen snapshot), and `verify_report.py` re-checks no-typographic-dashes
per landed report.

### Fence sanitizer (repo-text can't break out of a fence)

Repo-controlled free text — GitHub check/job/step **names** and **verbatim**
captured job-log / workflow-YAML evidence lines — is dropped into the rendered
report, both inside `` ```text `` fences and inline code spans / headings. Left
raw, a run of >=3 backticks would **close a fence early**: the rest of the report
renders as broken Markdown on GitHub, *and* `verify_report.py` desyncs (it splits
the same text with `re.findall(r"```text\n(.*?)```")`, so the identical stray
fence fools the safety net). `_fence_safe(s)` neutralizes it — defuse any >=3
backtick run to an equal-length apostrophe run, collapse embedded newlines/CRs to
a space (a name/step/log line is ONE line), drop dangerous control chars — and is
**byte-identical on clean single-line input**. It is folded into `_clean_label`
(the canonical NAME normalizer every check/job/step name flows through) so the
pole heading, the `` ```text `` waterfall labels, and the agent-prompt fences are
all neutralized at **one chokepoint** rather than per-sink; the heading also wraps
the check as an inline code span (`_safe_span`) so a `*`/`_`/backtick can't render
as formatting. Verbatim **evidence** lines (which bypass `_clean_label`) are
fence-safed at each emission site, prompt bodies per-line (`_fence_body`), and
prose/table cells via `_flatten_cell`. `verify_report._strip_scope` mirrors the
`_clean_label` transform (kept verbatim-coupled by `test_s1a`) so the name
comparators stay aligned, and a defense-in-depth `check_fences_balanced` FAILS if
a CommonMark fence walk ever ends still inside a fence.

## 10. Status of the planned bounds

The cascade extraction (`wall_clock.py`, the `Bound` contract, the monotonic-
down + no-silent-shrink invariants, and `size_wall_clock` wired into production
with a rendered derivation) is **done** (§5). Disposition of the four physical
bounds the gaps list originally named - three implemented, one deliberately not:

- ✅ **Developer-facing gate (`bound_developer_facing`)** - implemented. A
  workflow whose sampled triggers are ALL non-developer-facing (push-to-main,
  schedule, `workflow_run` after merge) is not on the PR critical path, so its
  wall-clock saving is zeroed (runner-minutes untouched). Uses the measured
  `run.event` set; removes the over-claim of crediting post-merge/scheduled
  time as developer PR wait.
- ✅ **Per-event critical paths** - implemented. `_critical_path` is computed
  from the developer-facing event's runs (`_developer_event`: `pull_request`,
  else `pull_request_target` (the fork-PR / triage variant a PR also waits on),
  else `merge_group`) when the workflow fired on one, not blended across
  triggers - a workflow's PR runs can have a different long pole than its
  push-to-main runs (e.g. better-auth `ci.yml`: 493s on PR vs 439s blended).
  `pull_request_target` is a developer-wait event for this scoping: a workflow
  on `push` + `pull_request_target` with no plain `pull_request` once found NO
  developer event and fell back to `all-events`, blending its post-merge push
  runs into the PR wait (roboflow/supervision `pr-conflict-labeler`: a 120s
  push-mode retry that gates zero merges became a false "~2m on ~30% of PRs"
  bimodal gate). The crit's `event_scope` is recorded and surfaced in the
  concurrent-workflows table; `verify_report`'s `check_speed_poles_complete`
  re-derives it and FAILS any PR-critical-path pole still scoped `all-events`,
  except the narrow `timing_source=pr_check_runs` case where the pole p50 comes
  from sampled PR check-runs and no workflow-job step drill is present.
  Detector *evidence* p50 stays over all sampled runs (more robust); only the
  sizing critical path is PR-scoped, and the table notes the scope so the two
  are never confused.
- ✅ **Matrix post-shard residual floor** - already handled in the OPT25
  detector's sizing (the saving is floored at the next-slowest leg). Lives in
  the detector, not the loop cascade, because it's pattern-specific.
- ❌ **Required-status-check gating** - deliberately NOT built. Two blockers,
  both confirmed: (1) **data** - the required-checks list lives behind branch
  protection, which returns **404 without admin** on the repo; ci-speedup's
  normal case is auditing repos you don't own, so this would be "unknown" on
  almost every real audit. (2) **semantics** - only *required* checks block the
  merge, but developers wait on non-required checks in practice, so discounting
  a non-required workflow's saving is genuinely ambiguous (merge-gate vs.
  attention). The developer-facing gate already captures the defensible,
  always-available core ("is this on the developer's critical path") via
  `run.event`; required-check gating would add a usually-blank, contentious cap
  on top. Left out on purpose rather than shipped hollow.

## 11. The structural / critical-path track

The hygiene/data-driven catalog (OPT1–OPT69) is mostly **declarative** -
static findings are locally-checkable YAML defects, while measured Tier-2 rows
come from run history. Its blind spot: on real repos the merge is
gated by a check that is *working as intended* and simply slow, with no
catalog match. The old catalog-spine report used to dead-end there ("inherent
cost, outside this catalog"). The **structural track** (catalog category 14, OPT70–OPT75,
`class: structural`) is a **second finding class that is not catalog-bound** —
it is routed from the measured critical path instead of matched against YAML.

### Where it lives

- **`scan.py`** — unchanged. `class: structural` entries are excluded from
  `static_entries`, so they never enter the per-file dispatch and never appear
  in `catalog_patterns_without_detector` (same treatment as `data-driven`).
- **`collect_runs.py`** owns detection (`_detect_structural_candidates`, after
  the hygiene sizing loop so an already-shortened check isn't double-surfaced):
  - `_decompose_job_steps` breaks the long-pole job into per-step p50s and
    classifies each via `_step_category` (checkout / install / build / test /
    scan / package / setup) to find the **dominant** step + its share + the
    redundant-work ratio (setup+build ÷ payload). It first reconciles the sampled
    instances to a **single workflow version** (`_current_version_steps`): the
    sampled window can span a workflow MIGRATION, and aggregating per-step p50s by
    name across versions would inject phantom steps absent from the audited commit
    (e.g. a since-removed `Run actions-rs/cargo@v1`) and crown a non-existent
    dominant lever. Only step names present in the MOST RECENTLY-started instance
    (the version the audited tip runs) are aggregated.
  - `_fetch_required_checks` reads `…/rulesets` + `…/branches/{b}/protection`
    (via `GhClient.json(..., allow_missing=True)`, so an admin-only 404 is
    "unknown", not a coverage error). Required-status is `required` /
    `not-required` / `unknown` — a 404 is **never** asserted non-required.
  - The router picks the best-fit pattern per top critical-path check:
    not-required → **OPT71** (de-trigger); dominant build/test with high
    redundant ratio → **OPT72** (prefer the safe cache path); dominant
    build/test otherwise → **OPT70** (scope — HIGH risk); else → **OPT75**
    (decompose, route by dominant category). `_detect_shared_substep` adds
    **OPT73** when a step category recurs across ≥2 cluster jobs.
- **`wall_clock.py`** adds two structural sizing helpers (leaf, unit-tested):
  `credit_detrigger` (de-triggering a non-required pole drops the wait to the
  next concurrent check) and `credit_shared_substep` (a shared step lowers the
  whole cluster floor — the one lever that beats the long_pole−floor cap;
  wall-clock = per-job step time, runner-min = × the jobs). Both feed their RAW
  estimate into the **same** `size_wall_clock` cascade as every other finding,
  so structural savings are population-weighted and floor-capped — never a
  single-PR best case.
- **`blocking_path.py`** renders the measured critical path **as the report's
  spine** — so a structural lever (the slowest gating check, OPT70–OPT75) is the
  headline, drilled gate → step-timeline → root cause, rather than a row in a
  ranked table that a trivial hygiene tweak could top. The structural signals
  (dominant step/category, redundancy ratio, required-status, shared substep)
  annotate the pole they came from; the catalog OPT70–OPT75 findings are
  therefore **excluded** from the off-path "Also noticed" appendix
  (`_also_noticed_block`, which is hygiene OPT1–OPT69 only) since the pole already
  represents them. Like every pole, a structural lever carries an agent prompt
  rather than a prescribed fix; for a HIGH-risk lever (e.g. OPT70 scope-to-
  changed) the prompt's failure-mode/guard section tells the agent to state the
  failure mode + full-build/full-suite fallback + parallel-run rollout before
  shipping. The fuller failure-mode/guardrail/rollout profile lives in the
  catalog body + the findings JSON.
  - **Matrix sibling legs (`_collapsed_sibling_structural`).** `by_matrix`
    renders only the slowest leg of a matrix as the pole; because
    `_structural_for_pole` does NOT fold a distinct sibling leg AND the appendix
    excludes per-pole structural levers, a faster leg's own lever would otherwise
    render nowhere (a silent drop). So the representative pole SURFACES each
    collapsed leg's lever, carrying that leg's own measured numbers. Two forms
    (issue #53): a sibling whose lever is **identical** to the pole's own — same
    routed pattern id + dominant-step BASE name (the `+ N more <category> step`
    aggregation suffix normalized away by `_dominant_step_base`) + dominant
    category (`_struct_identity`) — folds into ONE compact per-leg measurement
    line (`_collapsed_sibling_line`, boilerplate rendered once by the pole's own
    block); a sibling carrying a **different** lever keeps its full annotation
    block (`_sibling_structural_annotation`). Either way every leg's name +
    measured p50/share still render, so the anti-drop rule holds — only the
    duplicated guardrail/rollout/failure-mode boilerplate is dropped. This is
    presentation-only: the stamped findings are untouched, so `verify_report`'s
    structural checks (which read findings.json) are unaffected.

### Required-scoped spine (the merge-blocking pole)

The report answers *"why is the merge slow?"*, so the spine — and therefore the
headline pole, which is just the slowest thing on it — must be the **merge-blocking**
checks. The spine is built in `collect()` from `pr_check_p50`; immediately after the
non-PR-gating drop (`_dropped_non_pr`) and **before** `pr_checks_tuple` is sorted,
`_scope_spine_to_required` restricts it to the checks reachable from the required set by
**`needs:`-reachability** over the workflow job graph.

The graph itself is repo-agnostic: `scan.py`'s `_build_workflow_job_graph` emits
`workflow_job_graph = {wf_file: {job_id: {name, needs:[job_id], reusable, timeout_minutes}}}`
straight from the YAML (matrix `${{...}}` placeholders left in `name` for the consumer
to regex-match; `reusable` flags a job with a `uses:` reusable-workflow call;
`timeout_minutes` records whether the job declares `timeout-minutes`). `collect_runs`
consumes it:

- **`_required_reachable_checks(candidates, req_names, job_graph, crit_by_wf)`** — returns
  the candidates that are required **or** a job the required work transitively `needs:`.
  Each required check maps to a job *node* (`_check_to_job_node`): a `<caller> / <child>`
  name resolves to the **reusable caller** job (so a required reusable child anchors the
  whole invocation — the merge-reports-rollup pattern), and a plain check anchors its file
  via `_map_check_to_job` then matches the job `name` template (exact, then matrix-regex).
  The **anchors** are those required job nodes; the **reachable** set is the anchors plus
  their downward `needs:` closure (everything an anchor depends on). A candidate is kept
  iff required, or its node is reachable, or it's file-backed-but-unpinnable (kept, never
  silently dropped); a fileless/external non-required check is dropped.
- **A match-anything matrix `name:` binds nothing (no-timing binders).** A job whose `name:`
  is ENTIRELY a matrix placeholder (`${{ matrix.target }}`) expands to `^.+?$` — it matches
  *every* check-run name, so it carries no discriminating signal. The no-sampled-timing binders
  (`_check_to_job_node_scanned` / `_check_to_workflow_file_static`, the scanned-graph fallback
  used when a workflow was triaged out of job-fetching) refuse such a **degenerate** template
  (`_name_template_is_degenerate`): otherwise it grabs a managed/external check-run that appears
  in *no* workflow YAML (a Netlify `Redirect rules` / CLA / app check) and mis-anchors it to a
  workflow file, fabricating a file-backed long pole with a wrong-file agent prompt (live:
  `tokio-rs/tokio`). A degenerate-named job's own legs still resolve via the **sampled-timing**
  anchor (`_map_check_to_job`), which a foreign external check — having no sampled job — never
  reaches; so the refusal costs nothing for real legs and keeps a genuinely fileless check
  fileless. `verify_report._external_check_misbound_offenders` re-derives this from
  `workflow_job_graph` and fails any no-sampled-job pole bound to a workflow that produces no
  matching job.
- **Cross-workflow same-name ambiguity bails, never guesses (`_map_check_to_job`).** A
  monorepo can declare the SAME job name (`Build`, `test`) in several package workflows, so
  the sampled-timing binder's match tier lands in more than one file. There is no evidence to
  pick one — the check-runs endpoint carries no workflow path, and a check-run's own
  started→completed span is queue-inflated so it can't select the "closest" job p50. The
  binder used to keep the SLOWEST match, mis-attributing the pole's `workflow_file` / steps /
  fix to a **different** workflow (issue #59). It now returns **None** on genuine
  cross-workflow ambiguity — the same refuse-don't-guess stance as
  `_check_to_job_node_scanned` — and routes to the honest unmapped path. The
  `require_developer_timing` filter is applied FIRST as disambiguating evidence: when only one
  candidate workflow is PR/merge-timed, the ambiguity is resolved and the binder pins that
  file. Same-name jobs *within* one workflow (matrix legs) are not a file-attribution
  ambiguity and still resolve to the slowest, so every unambiguous repo is byte-identical.
  Downstream stays honest: `_is_pr_gate_check` decides PR-gating from the FULL matching-workflow
  set (`_workflows_matching_check`), never the single slowest pick, and the structural
  disclosure distinguishes an ambiguous file-backed check (`_check_producing_workflows` > 1 →
  "produced by a same-named job in more than one workflow; give them distinct names to drill")
  from a genuinely fileless bot/app check — a real in-repo check is never called "third-party".
  The bail is a FILE-attribution bail only: an ambiguous check that is the real merge gate must
  still crown the headline. Its crowning MAGNITUDE is unambiguous (a PR waits on the slowest
  same-named job), so the spine grounds it on that job p50 (`_check_grounded_job_p50`, never the
  queue-inflated check-run span) and stamps it `workflow_jobs`, keeping it in the crowning basis;
  `_partition_fileless_checks` therefore never mis-drops a file-backed ambiguous gate into the
  fileless / PR-lifetime-latency bucket. Only its per-file step drill/fix stays withheld.
- **Reach, not file co-residence.** `needs:`-reachability handles both real shapes a
  name-prefix or file matcher gets wrong: a **bare aggregator** (better-auth's `ci`/`e2e`
  @~3s, which `needs:` the real slow jobs that share no name prefix — kept), and an
  **independent sibling** (a slow non-required job sharing a required workflow file that no
  required job `needs:` — dropped, where file-membership would wrongly keep it).
- **Gated hard.** The filter only fires when `required_checks` is non-None, `complete`,
  non-empty, **and** the required suite was satisfiable on the sample. A partial read
  leaves an absent check **UNKNOWN** (not not-required) — dropping it could discard a
  check an unread ruleset gates; an unsatisfiable suite means the required names aren't in
  the sample at all (the PR-floor fallback renders), so filtering would empty the spine.
  It also **never empties the spine** on an over-tight match (returns the input unchanged).
  With no usable graph it degrades to literal required membership (the conservative subset).
- **Satisfiability is computed over OBSERVABLE required checks.** A required check that
  never appears as a check-run anywhere in the sampled window is a status-only/external
  gate (a `Devin Review` GitHub-App commit *status*, an enterprise status check) that
  *can't* be sampled — so the per-PR suite test (`req_names ⊆ checks`) excludes it and
  re-selects on the observed required subset (`_select_repr_shas`), recording the excluded
  names in `required_checks_unobservable`. Without this, one unobservable required status
  empties the suite and tips an otherwise-normal repo into the PR-floor fallback (live:
  `triggerdotdev/trigger.dev`, whose required `All PR Checks` aggregator gates every PR but
  whose required `Devin Review` status is never a check-run). A fully-external suite
  (nothing observable) still falls back to the PR-floor.
- **No silent drop.** Excluded checks are recorded on `cp["dropped_non_required_checks"]`
  (sibling to `dropped_non_pr_checks`) AND surfaced as a report footnote ("Narrowed to
  merge-blocking checks: …"); a dropped check loses its structural pole/findings but the
  exclusion is visible in both the JSON and the rendered report.
- **Stamp agreement.** The `required_status` stamp in `_detect_structural_candidates` uses
  the **same** reachability (`_required_reachable_checks` over the scoped checks), so a kept
  leg (e.g. a sharded `Test` job feeding a required `Merge Test Reports` rollup) is stamped
  `required`, never offered an OPT71 de-trigger that would tell the user to drop a job that
  gates the merge.

Filtering `pr_check_p50` before the tuple propagates consistently to
`critical_path_check`, `checks`, `poles`, the drilled matrices, populations, structural
routing, and the renderer — no renderer change needed. Live-confirmed on `mastra-ai/mastra`:
without it, the non-required `changed-tests` (its own `changed-test-gate.yml`) headlined the
report even though it gates no merge; an early file-membership matcher then mis-kept the
non-required `Validate build outputs` (a `lint.yml` sibling that required `Lint` doesn't
`needs:`), which `needs:`-reachability correctly drops.

**Rare-pole demotion — by actual-critical-path FREQUENCY, not presence (when no required set
scopes the spine).** The required filter is inert on a repo whose required set is
unreadable/None (no admin) — there the spine is the raw PR-floor, and a raw-p50 sort lets a
check that a typical PR never actually waits on headline. So the `pr_checks_tuple` sort is
**two-tier by POLE FREQUENCY**: a check that is the ACTUAL critical path (the slowest job a PR
ran) on fewer than `_POLE_RECUR_FLOOR` (2) sampled PRs is a one-path outlier and ranks below
the genuine recurring gates, each tier by p50 desc. Pole frequency is
`_pole_frequencies(per_sha_checks, set(pr_check_p50), caps)` — the per-PR argmax over the SAME
job-p50-capped spans `populations` uses (`_pole_caps`), so an inflatable check-run span (queue
/ re-run time) can't crown a light check and the headline can never disagree with the
`populations` `verify_report` re-derives from; co-slowest ties credit every tied check. Each
check carries its count as `pole_n` (and `present_on` for disclosure). A required check is
exempt (gates by definition); inert below `_RARE_PRESENCE_MIN_PR` (6) PRs.

Why frequency and NOT presence (the expo/expo class this replaced): the old `present > half`
cutoff demoted every check present on ≤ 50% of PRs. On a path-partitioned monorepo each heavy
suite runs on a MINORITY of PRs (a PR touches only part of the tree), so that cutoff demoted
the entire heavy-job set at once and crowned a lightweight always-present `check-packages`
that — by the findings' own `populations` — was the actual slowest job on **0 of 20** sampled
PRs, burying the genuine 20-minute native/CLI suites. Ranking by how often a check is the real
critical path surfaces the recurring gates a developer actually waits on. Because it reorders
the same `pr_checks_tuple`, the headline, poles, structural routing (`_is_typical_check` uses
the same `pole_n >= _POLE_RECUR_FLOOR`), **and** the `run.py` data-pass summary all agree.
`verify_report.check_headline_pole_actually_gates` re-derives the per-PR pole from
`populations` and FAILs a headline that gates < floor PRs while a genuine gate was passed over
— the class-detector that makes this a class fix (it independently flagged a second victim,
`Photoroom`, across the dogfood corpus). The renderer applies the matching split and labels the
demoted pole by why it was demoted — reading `pole_n`, *"Rarely the merge gate — the actual
slowest check a PR waits on, on only N/npop sampled PRs"* — falling back to the legacy
`present_on` presence wording (*"opt-in / rare — ran on only N/M sampled PRs"*) only for
pre-`pole_n` findings. Live-confirmed on `expo/expo`
(headline `check-packages` 2.9m/pole-on-0 → the real `ios-test-e2e` 26m + `playwright-windows`
23m) and `paradedb/paradedb` (a label-gated benchmark ~153m on 1/20 PRs demoted below the real
~23m test gate).

**Demoted-pole framing depends on whether the spine is required-scoped.** The rank demotion
above is pole-frequency-based and applies on both a PR-floor spine and a required-scoped one —
but the *label* must not. On a PR-floor / recency spine a demoted minority check really can be a
non-required opt-in job, so the renderer keeps the *"opt-in / throughput, not merge-wait"*
framing. On a **required-scoped** spine the data layer has filtered to required-reachable
checks, so a demoted minority leg is a *required* gate that's merely **path-conditional** (it
gates the PRs whose paths trigger it, where it IS the merge pole) — the renderer reframes all
three label sites (headline, "Also slower" note, per-pole role) as *"required ·
path-conditional — ran on N/M PRs"*. The flag it keys off is **`spine_required_scoped`**, set
only when `_scope_spine_to_required` ACTUALLY narrowed the spine — *not* the sampling-level
`required_suite_scoped`, which is True whenever a required set was satisfiable even on a
partial / anchorless read where the spine was left unscoped and still holds non-required
checks (relabeling there would re-introduce the very mislabel this guards against). Where a
required-scoped spine mixes a path-conditional required leg with an external review check, the
"Also slower" tail addresses each on its own terms. Live-confirmed on
`triggerdotdev/trigger.dev`: `internal` unit tests (the slowest leg, 6m51s, 9/20 PRs) is
path-filtered to `internal-packages/**` changes — required, not opt-in.

### The risk axis (surfaced in the pole's prompt, never demoting the rank)

Structural changes can degrade **correctness**, not just performance, so every
structural finding carries `risk` + a mandatory `guardrail` + `rollout` +
`failure_mode` — built **by construction** from `_STRUCTURAL_META` (in
`collect_runs.py`), so the risk axis can never be silently dropped. Risk does
**NOT** demote the rank (that would bury the biggest speed win — the whole reason
the measured critical path is the spine); instead it is made loud where the pole
renders: the per-pole agent prompt's failure-mode/guard section spells out the
failure mode + full-build/full-suite fallback + parallel-run rollout, so a
HIGH-risk lever (e.g. OPT70 scope-to-changed) is never handed over as a safe
quick win. (The pole's 🔴/🟠/🟡 dot is a wall-clock duration tier, not the risk
rating.) The catalog body for each structural pattern is the long-form risk
profile; `_STRUCTURAL_META` is its structured form.

## 12. The blocking-path report (`blocking_path.py`)

`blocking_path.py` is the skill's **single, data-first renderer** (§2) — answering
one question, *"why is the merge slow?"*, as a drill-down from the gating check
down to the root cause. (It replaced an earlier catalog-spine renderer, `report.py`,
when the measured critical path became the report's spine.) Like the rest of the skill
(§2) it **stops at the measured root cause and does not prescribe the fix**: each
pole ends with a **prompt** that restates the root cause and points the user's
coding agent at the tool's official docs (`_PROMPTS`, keyed by `fix_key`) — no
recommendation, no code diff. It reads the **same** `findings.json` plus an optional
**data bundle** (logs + timelines + magnitude samples) that `collect_runs` captures
under `--with-logs --data-dir`. CLI: `--in findings.json` + per-pole
`--log/--steps/--mag KEY=PATH` (KEY = a substring of the workflow filename) +
`--captured-at`. With no bundle it still renders (level-1 + P50 step bars).

### 12.0 The Long pole map — the cascade up top (presentation-only)

Immediately after the Contents the report renders a **`## 🗺️ Long pole map`**: the whole
blocker cascade in one `text` fence, so the reader sees the shape of the wait before the
per-pole drills. It is **checks-first** (the physically true model — GitHub gates on
CHECKS, which all race concurrently; a workflows-first grouping would hide a runner-up
blocker living in a different file):

- **Level 1** — the flat race of merge-gating checks (`src[:9]`, the **same** typical-PR
  set the Contents draws — no new data source), each bar labelled `{check} · {workflow file}`
  (the file from the check's own `workflow_file`, else a same-named pole's; a fileless /
  external check renders with no suffix). The **◀┐** hangs from the row of the check the
  cascade descends into — the check of **pole 1** (`pole_wfs[0]`, "Long pole 1" below).
- **Level 2** — that check's **stamped steps** (`pole["steps"]`, p50-desc, top 6, **no**
  roll-up row — the map is orientation; the pole section keeps the full accounting). Each
  step's % is its share of the **check's own p50** (the job wall level 1 just showed), via
  `_emit_level`'s `pct_denom`. The ◀ lands on the dominant-category lead (`_dom_lead_idx`,
  agreeing with the crown), falling back to row 0.
- **Level 3** — the dominant step's internals, rendered **only** when the descent pole's
  parsed-log leaf has a `deeper` first level of ≥2 rows (the same `_parse_log →
  _apply_cache_dist → _demote_offcategory_leaf` pipeline the per-pole loop runs — hoisted
  into `_derive_pole_leaf` and shared via `_pole_leaves` so the map's L3 and the pole's L3
  can never disagree). Only `deeper[0]` is drawn (its `blocker_note` when `deeper` is a
  single level).

The whole cascade reuses the pole-drill `_emit_level` machinery (`mark_idx` + `header_below`
connector idiom). **Degenerate levels collapse** — never a one-bar level; level numbering
stays semantic (checks=1, steps=2, internals=3, matching the pole sections). If level 1 is
too thin (<2 rows) but level 2 qualifies, the fence opens at level 2; if the descent pole's
check isn't among the shown rows, `pole_wfs` is empty, or the pole has no usable steps, it
falls back to the **level-1-only** render (the pre-cascade #75 form); if nothing qualifies
(one check, one step), the section is skipped entirely (exactly where the `if src:` guard
skips it). The map is **presentation-only**: no claims-layer `cs.add(...)`, no `verify_report`
check binds to any map line — the Contents list + the stamped fields stay the single source
of truth.

### 12.1 Three levels, three provenance scopes

| Level | What | Source | Aggregation |
| --- | --- | --- | --- |
| **L1 — concurrent checks (jobs)** | the checks a PR waits on, slowest first; top = the gate | `pr_critical_path` in findings.json | **P50 across sampled PRs** |
| **L2 — the gating job's steps** | a **sequential timeline** of the steps | one representative run's real per-step start/end (`--steps`) | **one run** (absolute) |
| **L3+ — the dominant step's internals** | per-file / per-suite / cache split → the root cause | that **same** run's raw log, parsed by the leaf detectors (`--log`) | **one run** (absolute) |

The split is deliberate: **L1 jobs run concurrently** (bars are left-aligned —
they all start together; you wait for the slowest), whereas **a job's steps run in
succession**, so L2 is an **offset Gantt** (`░` = elapsed-before-start, `█` = the
step running) rather than left-aligned bars. Drawing steps left-aligned implied
they overlap, which is backwards — the timeline fixes that and the steps sum exactly
to the one run's wall time.

### 12.1a The render-layer single door — the headline consumes the stamped ceilings (issue #49)

The Bottom-line / headline lever is **selected and sized from the stamped per-finding
wall-clock ceilings** (`findings[].wall_clock_p50_s`), never re-derived. This is the
render-layer analogue of §5.1's sizing door: the data layer already ran the whole
cascade (§5) and stamped one authoritative ceiling per finding, so the renderer must
**consume** it rather than compute a second, divergent number.

The subtle case is the **cluster-floor lever** (OPT73). Its fix cuts a step shared
across concurrent matrix sibling legs, so it drops the WHOLE cluster in lockstep and
saves its full stamped `wall_clock_p50_s` of merge wait. The per-pole
`_pole_addressable` / chain-headroom arithmetic can't see that — it floors at the next
**sibling** leg, which descends *with* the fix — so before #49 it headlined a tiny
per-leg number (mastodon: ~36s over a stamped 627s; electron: ~5m37s over 2635s) while
the real lever sat in "Also noticed". The renderer now, at the Bottom-line site,
compares the win the branch cascade would show against the biggest **credited** cluster
ceiling (`_headline_cluster_lever`); when the ceiling wins, the Bottom line LEADS with
it (`_cluster_headline_bottom_line`, naming the shared step + cluster and pointing at
the OPT73 appendix entry). The contract:

- **Keyed on the PERSISTED marker.** `collect_runs` stamps `cluster_floor_lever=True`
  on every OPT73 cluster construction; the renderer selects only findings carrying that
  marker AND a positive `wall_clock_p50_s` (a credited developer-facing win, not a
  bill-only / off-path zero). A legacy artifact predating the stamp has no marker, so
  the branch never fires and the report renders **byte-identically** — the chain "up to
  X" sentence keeps its legitimate uses (a real chain with no dominating cluster lever).
- **Re-derives the SAME selection.** The renderer never recomputes headroom; it reads
  the ceiling the data layer sized. The two layers cannot disagree because there is one
  number.
- **Honest about concurrency.** The "drops the whole cluster in lockstep" framing is
  true only for a **concurrent** matrix cluster. A `needs:`-chained cluster runs
  SEQUENTIALLY, so `collect_runs` also persists `cluster_legs_concurrent`, and the
  Bottom line phrases a sequential cluster as "N sequential (`needs:`-chained) stages …
  the per-stage savings compound on the critical path" — never "concurrent … in
  lockstep" (the same mislabel §12.1a's appendix already avoids). Missing marker
  defaults to concurrent (the matrix-leg case).
- **Guarded both ways.** `verify_report.check_headline_consumes_stamped_cluster_ceiling`
  re-derives the max credited cluster ceiling from findings.json and FAILs when the
  rendered Bottom-line lever is strictly smaller (the burial). With no persisted marker
  it SKIPs LOUDLY (a legacy artifact it cannot verify) rather than reading clean — the
  L8 fail-open that let this defect survive PR #47.

### 12.1b The cluster crown is presence-weighted — a minority workflow can't headline (issue #56)

§12.1a stops a real cluster ceiling from being **under**-credited; this stops the converse — a
**minority-present** cluster **over**-crowning the typical-PR headline. The OPT73 detector originally
chose its anchor (the leg whose p50 sizes the win and leads the Evidence) by **absolute slowest p50**,
while the merge-wait spine is **presence-weighted** (§11): a check that is the actual pole on `<
_POLE_RECUR_FLOOR` sampled PRs is a one-path outlier the ranking demotes. The two disagreed on
microsoft/playwright: OPT73 anchored on `Test msedge-dev on macos-latest` (the pole on ~0/20 PRs, in
NO Level-1 pole) and let its `tests_secondary.yml` cluster — a workflow that gates only **2/20** PRs —
crown the Bottom line's "biggest single measured win (~3m 15s)" and self-label "sits ON the merge-
gating critical path", above the honest ceiling (pole 1's own ~2m 37s on the 13/20 majority workflow).

The rule composes with the existing rare-demotion (§12.2 `_ms_freq_demoted`) and re-uses the spine's
**own** predicate (never a parallel notion — the L3/L5 mirror-the-engine discipline):

- **Anchor (both cases).** `collect_runs._detect_shared_substep` orders the cluster legs
  **presence-eligible first** (`_leg_presence_eligible`, the inverse of the spine's `is_rare` —
  the exact complement for any spine-ranked leg, with an unknown-to-the-map leg treated as eligible
  rather than rare, so we never exclude on partial info: required, or the actual pole on
  `>= _POLE_RECUR_FLOOR` PRs, or unknown/small-sample), then by p50. So `affected_jobs[0]` — the
  Evidence lead and the appendix `**Where:**` lead — is never a minority-present leg. When every leg
  is eligible (the common matrix case, every majority cluster with no rare leg — mastodon
  `test-ruby`, the electron `build` chain) the order is **byte-identical** to before.
- **Crown / on-path label — follow the data.** A **majority** workflow with one minority leg (option
  **a**) keeps its wall-clock and simply re-anchors — its cluster genuinely gates the typical PR. A
  **minority** workflow (option **b**, `_workflow_gates_minority`: its summed gate count is `<= half`
  the sampled PRs — a workflow-level majority test on the same count `_toc_block` renders, coarser
  than the spine's per-check `_typical_check` split) has its wall-clock **demoted to bill-only**
  (`wall_clock_p50_s=0`, `realization=none`, tier 2) — exactly the existing all-legs-rare demotion
  path — so it never becomes a `_is_credited_cluster_lever`, never crowns the headline, and never
  carries the on-path label. That gate count is summed in the check-name domain via `_map_check_to_job`
  (`_workflow_gate_freq`), not by a raw job-name lookup, so a name-divergent majority workflow is not
  spuriously demoted. The runner-minute (bill) saving is **untouched** on either axis: all the legs
  still pay the shared step whenever the workflow runs.
- **Guarded (the converse of §12.1a).** `verify_report.check_headline_lever_is_presence_eligible`
  re-derives the crowned lever's workflow gate frequency from `checks[].pole_n` (the same
  `workflow_file`-summed count `_toc_block` renders) and **FAILs** when a minority-gate workflow crowns
  the Bottom line while a majority ceiling exists. A required check in the workflow EXEMPTS it (gates by
  definition); no stamped `pole_n` or a below-floor sample is a LOUD narrow SKIP (never reads clean).

### 12.2 Representative-run selection — nearest-P50 (`_persist_pole_logs`)

The drilled run is the **qualifying instance whose duration is closest to the job
P50** (`job_p50_s`, else the check `p50_s`). *Qualifying* = duration ≥ 0.5×the
slowest sampled instance, which drops short-circuit / self-skipped no-ops (a gated
job like `changed-tests` that does nothing on most PRs) so they can't be picked.
Nearest-P50 (not the slowest, not the high-skewed qualifying median) makes the
drill **reconcile with the headline** instead of overstating it.

The level-1 bar is the **check-run** gate time (`p50_s`); the timeline is the
**job** clock (`job_p50_s`). They differ — the job includes the runner
setup/teardown steps the check-run clock excludes — so the report states the gap
explicitly rather than leaving a confusing "8m41 job under a 7m05 headline".

### 12.2a The addressable-ceiling floor — pole-relative co-occurrence, not global presence

The headline win and each pole's "what a change here can buy" note size the addressable ceiling
as `pole_p50 − binding_floor` (`_pole_addressable` / `_floor_note`), where the binding floor is the
slowest concurrent check a *typical gating PR* also waits on — cutting the pole only helps down to
that floor. Selecting the floor by **global** presence (a sibling counts only if present on ≥ 0.8 ×
the most-present check anywhere) is wrong: it demotes a genuinely co-occurring 2nd-slowest sibling
that merely runs on slightly fewer PRs than some trivial universal check, so a *faster* check
becomes the floor and the ceiling overstates (~2× on Infisical). The floor is instead re-derived
**relative to the pole's own gating PRs** — the PRs where it is the per-PR slowest — from
`pr_critical_path.populations`: a sibling qualifies iff it co-occurs with the pole on a strict
**majority** of those gating PRs (`_pole_cooccurrence` stashes the per-pole counts in `render()`;
`_floor_qualifies` applies the majority test inside `_binding_floor` / `_floor_candidate`). Absent
`populations` (a minimal/static doc) it falls back to the legacy global-presence test, unchanged.
`tests/verify_report.py::check_pole_ceiling_within_cooccurrence` is the class invariant that
re-derives this independently and asserts the rendered ceiling never exceeds it.

A **modal-chain MEMBER pole** is the exception to the `pole_p50 − binding_floor` arithmetic
(review V2 / OD-F2, #220): `needs:` serializes it with its chain partners, so a "concurrent floor"
that is really its predecessor/dependent would frame a serial stage as a competing sibling and
overstate the win (deepgram rendered ~28s against a 5.0s stamped headroom). `render()` stashes the
chain story on member poles (`_chain_member`, the same threading pattern as `_cooccur`); both
helpers then cap the member's win at the **stamped chain headroom** (`chain_summary.chain_win_p50_s`,
the same bound the N3 cascade applies to member findings) and the member's own span, and the note
renders the chain-stage story instead. The suppression guards still run first (a suppressed note
stays suppressed), non-members are untouched, and
`tests/verify_report.py::check_headline_chain_matches_stamp` cell 3d re-derives the bound from the
per-PR `chain_facts` (median of `max(chain_s − runner_up_s, 0)`) and fails any drilled member note
that exceeds it.

The floor candidates come from a `floor_pool` that is the **full** concurrent-check set, NOT the
typical-PR chart set `src` (Class A #6). The two are deliberately distinct: `src` is what the L1
waterfall draws (the checks a *typical* PR runs) and what "merge wait" is measured over, while
`floor_pool` is every concurrent check a pole could floor against. Conflating them (floor pool =
`src` = `typical`) hid a check that is heavy but runs on a MINORITY of PRs — it couldn't be in the
typical chart, so it also couldn't be a floor, so a pole it genuinely caps overstated its ceiling AND
the heavy check was disclosed nowhere on the spine (lightdash `E2E: API (Vitest)`, 13m, on ~50% of
PRs). The co-occurrence filter (above) still gates which `floor_pool` members qualify per pole, so
widening the pool can't let a rare non-co-occurring check inflate the floor.
`tests/verify_report.py::check_spine_heavy_check_disclosed` asserts each drilled pole's re-derived
binding floor is disclosed on the spine (drilled / named as the floor / in the "Also slower"
footnote), never silently dropped — a matrix sibling-leg floor is out of scope (the pole represents
its own matrix).

The TOC/header "`<workflow>` gates N/M PRs" count is `wf_gate` — how often that workflow holds the
per-PR pole — and it is summed over **all** of the workflow's checks, not the representative poles
(Class A #2). Summing representatives only dropped a non-representative matrix sibling leg's
`pole_count` (razorpay/blade `blade-validate` undercounted 4/20 vs the true 5/20). Since exactly one
check is the per-PR slowest, summing a workflow's checks never double-counts a PR.
`tests/verify_report.py::check_workflow_gate_frequency_matches` re-derives each workflow's count from
`populations` and asserts the rendered N/M matches exactly.

When the slowest *typical* check is ALSO the frequency gate but non-universal (`gate_is_slowest` and
`floor_lowered`), the headline discloses BOTH its own conditional time and the lower
population-weighted typical wait (`merge_dur` = `_population_typical_floor`, the median of the per-PR
critical-path maxima). WHY the typical wait is lower depends on how often the check runs, and the two
causes are NOT interchangeable: at **minority** presence (`present ≤ npop × _RARE_PRESENCE_FRAC`) a
typical PR SKIPS the check, so its absence is what lowers the median — the presence-causal *"ran on
only N/npop sampled PRs, so a typical PR finishes in {merge_dur}"* framing is faithful; at
**majority** presence a typical PR RUNS the check, so presence CANNOT lower the median (nx
`main-linux`, present 19/20, whose ~46m conditional p50 — measured over a wider run-sample — cannot
be dropped to ~11m by 95% presence), and the renderer instead attributes the gap to
conditional-p50-overstatement (a duration/population skew). Blaming presence there is a non-sequitur
the `populations` contradict. `tests/verify_report.py::check_headline_presence_causal_only_when_minority`
re-derives present/npop for the named check from `populations` and FAILs any form-1 (name-first)
presence-causal headline whose check is majority-present. (Its regex intentionally matches only the
form-1 wording; the form-2 name-after branch renders its presence clause unconditionally and is a
known coverage gap, not a contradiction — its floor-reconciliation is guarded separately.)

**The crowning basis excludes fileless/managed status checks (issue #12 — a contract change).**
The merge-wait headline states *what CI makes a typical PR wait*. A **fileless/managed status check**
— a bot gate, a label gate, an external app check — produces **no sampled workflow job**, so its only
timing is its `pr_check_runs` span: the wall from when the check-run was *created* to when it
completed. For a label/bot gate that span is **PR-lifetime status-gating latency** (a label that sat
open for 8 days reads as an 8-day span), not CI wall-clock. `_pole_caps` builds its de-inflation caps
only from sampled `job_p50` / `job_bimodal`, so a check with no sampled job is never capped, and its
raw span would flow uncapped into `critical_path_s` / `chain_summary.makespan_p50_s` and crown the
headline over file-backed poles tracing <1% of it (electron/electron: `Backport Labels Added` /
`faraday/cage` crowning ~8 days). The physical-bounds clamp below cannot catch this: with the fileless
span in the basis, *both* the crowned figure and the makespan carry the same inflated span, so the
clamp passes trivially — the cap has to happen at the **crowning basis**, not the bound.

The **product rule**: PR-lifetime latency of a fileless/managed status check is NEVER a valid basis
for the CI merge-wait headline. `collect_runs._partition_fileless_checks` (called just before the
present-first ranking) splits the spine's `pr_check_p50` into the **job-groundable** basis and the
**fileless** set — a check is job-groundable iff it has a sampled developer-timed job
(`workflow_jobs`), the sampled-timing mapper resolves it, OR the scanned job graph maps it to a
workflow file (a triage-skipped but file-backed check the crown-recovery pass can still recover — real
CI compute, only its per-run drill was skipped; that notion is the same one the check→file binders and
the degenerate-matrix guard use, never re-derived). The fileless set is DROPPED from `pr_check_p50`
before ranking, so `critical_path_check` / `critical_path_s` / `populations` / `chain_facts` /
`chain_summary.makespan_p50_s` all re-derive from the job-groundable population (the exclusion is at
the data layer, so every crowning input moves together). It is **not** discarded: the excluded set is
stamped in `pr_critical_path.fileless_status_checks` (`name`, `span_s`, `basis`), and `blocking_path`
renders a labelled DISCLOSURE near the headline naming the slowest fileless check and its span,
explicitly framed as PR-lifetime status-gating latency (not CI compute). When **every** tracked check
is fileless (`all_checks_fileless`), the report says so rather than crowning a status-gating span.
`tests/verify_report.py`'s `check_headline_basis_excludes_fileless` re-derives the contract — no
crowned slot (`critical_path_check`, `checks[]`, `poles[]`, modal chain) may name a stamped fileless
check, and the rendered disclosure must bind to the stamped list — so the engine and the verifier
mirror move together (L3/L4/L5).

**The physical-bounds clamp on the headline wait (issue #25 family, #22/#24).** The rendered
"X until all checks finish" figure is a WALL, and the model that produces it (`chain_p50`, a median of
per-PR summed `needs:` spans; or `floor_p50`, the population-weighted typical floor) can drift OFF the
measured wall in either direction. Two coherence rules constrain it, both re-derived by the verifier:
(1) `merge_dur` is CAPPED at the **measured makespan p50** (`chain_summary.makespan_p50_s`, the median
per-PR span-capped `max(end) − min(start)`) — a re-run-inflated check-run clock in `populations` can
leave `floor_p50` above the actual wall (nx `main-linux`: a 15m08 population floor vs an 11m00
makespan), and a wall can never exceed the measured span; (2) for a `needs:` CHAIN the total is
additionally FLOORED to the **largest modal member's own p50** (`checks[].p50_s`) — a serial chain
cannot finish faster than its longest single stage, yet `chain_p50` diluted by fast PRs can render a
"total" below a member it claims to sum (tokio `compile → miri-test`: a 17m18 sum below miri-test's own
18m36). The clamp caps at the wall first, then floors to the member (the member is a hard MEASURED
lower bound, so it wins when a member exceeds the wall). The chain claim carries the coherent
`chain_wait_p50_s`; a report already within `[largest member, makespan]` renders byte-identically.
Note the deliberate asymmetry between (1) and (2): because the member floor is applied AFTER the
makespan cap, a modal member whose own drill p50 out-measures the span-capped makespan (measured over
a wider PR sample than the chain-forming subset) will crown the headline ABOVE the measured makespan
— (2) treats the member as the harder measured bound and lets it win. This is intentional and stays
coherent (the rendered figure is still a measured member p50, and `chain_wait_p50_s`/`makespan_p50_s`/
`largest_member_p50_s` are all stamped so the reader can re-derive it), but it means bound (b) does
NOT enforce a strict makespan ceiling on a chain headline; its ceiling relaxes to
`max(makespan, largest member p50)` precisely so a correct member-floored report does not false-FAIL.
`tests/verify_report.py`'s `check_aggregate_total_ge_largest_member` (bound a) and
`check_headline_wait_within_makespan` (bound b) re-derive both bounds from `chain_facts` + `checks[]`
independently; `check_headline_chain_matches_stamp` and `check_headline_floor_presence_reconciled`
mirror the clamp so the headline-family checks stay consistent with the rendered figure. A companion
guard `check_saving_within_measured_compute` (bound c) holds any finding's credited `runner_min_saving`
at or below its affected jobs' measured monthly billable compute in the `runner_minute_spine`. Bound (c) is now enforced at the SOURCE, for every rm-crediting pattern, by the measured sizing door
([§5.1](#51-the-measured-sizing-door--one-derivation-path-for-every-runner-minute-saving)):
`collect_runs._reground_runner_minute_savings` runs once the spine is final and routes each finding
through one policy. Whole-run-cancel patterns (OPT45 missing-concurrency, `_SIZING` `cost_basis:
affected_jobs`) stay inside the bound BY DERIVATION rather than clamp:
`_reground_whole_run_cancel_saving` re-derives the saving as `hit_rate × Σ(affected-job
billable_equiv_min_per_month from the spine)` — the same measured rows bound (c) reads, joined the
same way (matrix-stripped base, reusable-workflow fallback via the shared `_measured_billable_index`),
and at least as strictly — so `hit_rate ≤ 1` puts it within bound by construction (superseding the
provisional sizing-time figure, which grounds the no-spine case in the affected jobs' summed p50, never
the workflow long pole priced at full volume). The cluster-floor lever (OPT73) is CLAMPED to the same
Σ(measured billable) — #43, nx 1919.7 → 1404.4. A finding whose affected jobs ALL miss the spine join
is unsized at the source (`runner_min_saving = null`, basis `unmeasured_no_spine_match`) rather than
left carrying its provisional figure — bound (c)'s loud coverage SKIP only fires when EVERY
runner-minute finding misses the spine, so an unmatched one in a mixed report would otherwise render
unbounded. The old long-pole × full-volume model over-credited a workflow whose cancelled jobs run on
only a fraction of its runs (#33). Every sized finding stamps `runner_min_basis`, and
`check_saving_carries_measured_basis` FAILs a saving that carries none.

A VALUELESS finding whose jobs are ALL drilled long poles is excluded from the off-path "Also noticed"
appendix (Class A #5): the pole headlines that job as the single biggest lever, so re-listing a
valueless co-located finding on it (e.g. OPT24 on the pole's own `pytest-torch` job) as ~0-wall-clock
"minor cleanup" contradicts the headline. `_also_noticed_block` drops a finding only when it has no
bill saving AND is not a credited wall-clock lever (`_saves_wall_clock`) AND every one of its
`affected_jobs` (matrix `(variant)` stripped via `_job_base`) is a drilled pole — NARROW on purpose, so
a credited OPT73 bill lever, a wall-clock lever, or a finding that also touches a non-pole job is kept
(never a silent drop of a real lever). `tests/verify_report.py::check_pole_not_reframed_as_hygiene`
asserts no valueless all-pole-job finding (criteria read from `findings.json`, not the lossy rendered
`**Where:**`) appears in the appendix.

The TOC "🧹 Also noticed" pointer must agree with the appendix on on/off-path (Class A #7). When a
shown finding is a credited wall-clock lever that sits ON the critical path, `_also_noticed_block`
returns `any_wc_lever=True` and `_toc_block` reframes the pointer rather than blanket-labelling the
section "off-path … below the critical path / ~0 wall-clock" (which would contradict the inline
on-path row — getlago). `tests/verify_report.py::check_toc_also_noticed_label_honest` asserts the
pointer never makes the blanket off-path claim while the appendix carries the on-path sentence.

Two DECOUPLED gates govern the appendix "sits ON the merge-gating critical path" note, and the
split is load-bearing. `_saves_wall_clock` is the credited-MAGNITUDE / catalog-COVERAGE gate: it
declines a finding below the ≥30s long-pole floor (rejects sub-floor rounding artifacts) or stamped
`off_spine` (a required-scoped drop — encord §6 Cause 2), and it is what `_data_driven_for_pole` /
`_catalog_covers` consult to decide a pole is catalog-covered (NOT a coverage gap → no phase-4a
gap-fill, no `.ci-speedup-gaps/` capture, no phase-4c detector-draft). `_frames_on_path`
(= `_saves_wall_clock` AND not `spine_rare`) is the SEPARATE appendix on-path FRAMING gate. The
`spine_rare` demotion is applied ONLY in `_frames_on_path`, never in `_saves_wall_clock` — because
a job the footnote demotes as OPT-IN / RARE (present on a MINORITY of sampled PRs, so "a typical PR
doesn't wait on it") keeps its FULL multi-minute `wall_clock_p50_s` (the concurrency cascade bounds
only against concurrent checks, not against the presence demotion) and IS still genuinely
catalog-covered; declining it inside `_saves_wall_clock` would manufacture a FALSE coverage gap for
a rare job that IS the drilled pole (the paradedb `Test pg_search` shape — its log matched no
detector but an OPT24 fired on it). So the appendix keeps showing such a lever's real wall-clock
magnitude but reframes it "opt-in / rare — helps the minority of PRs that run it, not the typical
merge path", and the pole-waterfall's data-driven pointer is reworded to match
(`_pole_waterfall(..., data_driven_on_path=…)`). `render()` stamps `spine_rare` via
`_stamp_spine_rare` using the renderer's OWN opt-in test (`not _typical_check(name)` AND present on
a minority — the same populations/`present_on` basis the footnote uses). The stamp join is by check
NAME — exact, or unexpanded matrix base↔rendered leg — NOT by `(workflow, job)` identity, because
the footnote demotes a check NAME ("`X` … a typical PR doesn't wait on it") and the reader can't
tell which workflow's `X` a later "sits ON the critical path" note is about. So it deliberately does
NOT fold DISTINCT sibling matrix legs (`_same_matrix` — a finding on the exact RARE leg `test
(macos-latest)` must not be blocked by its TYPICAL sibling `test (windows-latest)`) and does NOT
skip a `_wf_conflict` (a same-NAME finding in another workflow must be demoted too — the tauri
`test (macos-latest)` double-framing had both arms). The kept-guard (mirrors
`_stamp_off_spine_findings`) still stamps only when the job maps ONLY to opt-in/rare checks, so a
job also on a typical check — or an unexpanded-base finding covering a typical leg — keeps its
legitimate on-path framing; over-stamping under the NAME rule can only make the appendix MORE
conservative, never manufacture the opposite contradiction. A multi-leg cluster (OPT73) finding is
a distinct arm: it is genuinely on-path via its TYPICAL leg (so it is NOT `spine_rare`), but its
appendix `**Where:**` must LEAD with that on-path leg, not `affected_jobs[0]` — `_also_noticed_block`
picks the first non-`spine_rare` leg (`_lead_job` → `_occ_loc(lead_job=…)`), and `_detect_shared_substep`
now orders `affected_jobs` slowest-first (matching the evidence's "slowest cluster job") so the
displayed location is the on-path job even for older bundles' order. The presence-minority clause is load-bearing: a FREQUENCY-demoted
matrix leg present on EVERY PR but rarely the single slowest (requests `build (3.10,
windows-latest)`, `pole_n` 0 on 11/11 PRs) is NOT opt-in — its matrix IS on the critical path every
PR, so an OPT73/OPT24 across it legitimately cuts wall-clock, and `_ms_freq_demoted` gives it a
different ("throughput/cost, a heavier check gates ahead") footnote.
`tests/verify_report.py::check_dropped_check_not_framed_on_path` re-derives BOTH the hard-dropped
set (any framing context) and the opt-in/rare set (`_rare_demoted_check_names`, from
`checks[].pole_n`/presence) and asserts neither is framed on-path — the rare set scoped to the
appendix note only, since a demoted Long-pole HEADER carries its own opt-in body framing and is no
contradiction.

### 12.3 The leaf detectors and the load-bearing magnitude (`_parse_log`)

`blocking_path._parse_log` is the single source of truth for root-cause detection —
five regex-over-tool-output detectors, each returning `{fix_key, unit_label, deeper:
[levels], evidence, magnitude}` (`fix_key` names the root-cause family + selects the
agent prompt in `_PROMPTS`; it is **not** a prescribed fix):

| `fix_key` | Root cause | `magnitude` (the one load-bearing number) |
| --- | --- | --- |
| `prisma-migrate-once` | `db push --force-reset` per test group | DB-migration share of the slowest test file |
| `vitest-v8-coverage` | istanbul coverage instruments every file | compile+instrument share of test work |
| `vitest-isolate-pool` | per-file isolation re-pays the import cost | import share of the vitest run |
| `turbo-remote-cache` | remote caching off / 0 cached → every package rebuilt | packages rebuilt (cache-miss %) |
| `turbo-partial-cache` | caching ON but ≥40% rebuilt every run → unstable cache key (cache-key churn) | packages rebuilt despite caching (cache-miss %) |
| `install-lifecycle-build` | a root `prepare`/`postinstall` lifecycle script runs a build DURING `<pm> install` — "work runs during install", not a cache miss | build wall run inside install (**seconds**) |
| `buildx-no-cache` | `docker buildx` imports no cache → every layer cold | slowest cold layer's share of the cold-build wall (%) |
| `playwright-parallel` | specs run as sequential invocations | **none** — the finding is the *sequencing*, shown by the timeline itself |

A detector with `magnitude: None` is **categorical only**: the cause is structural
(visible in the timeline / config), so there's no scalar to validate and no
single step is crowned for a drill.

`install-lifecycle-build` fires only from a bare `<pm> install` step (no
`--ignore-scripts`, no explicit `&&`/`;` chain, and — for a `run: |` multi-line block — no
explicit build in the echoed command lines), with a real lifecycle-script marker in the
section and a timed in-install turbo build ≥ `_LIFECYCLE_BUILD_FLOOR_S` (30s). It is placed
BEFORE the turbo detectors so a plain later-step turbo build still classifies as
`turbo-remote-cache`/`turbo-partial-cache`. The install-section boundary + gating block come
from `_install_build_section`, the single source both this detector and `_cache_state_of_log`
read, so the leaf and the per-run cache state can never measure different builds.

### 12.4 Cross-run check — adaptive sampling (`_magnitude_sample`)

The drill comes from one run. The **categorical** cause (istanbul on, cache cold)
is stable run-to-run, but the **magnitude** ("migrations are half the file") is one
sample. The cross-run check validates only the magnitude, and does so **adaptively**
to keep log fetches (the expensive call) proportional to the doubt:

1. **Cheap probe** — parse the drilled run + the fastest & slowest *qualifying* runs
   (`k_probe=3`), bracketing the spread for ~2 extra log fetches.
2. **Escalate only if wide** — if `range/median > _MAG_WIDE_REL` (0.25), widen to
   `k_max=8` runs spread evenly across the qualifying set (by duration), so the
   in-between region — where a second cluster would hide — gets sampled. A tight
   probe stops here: stability is proven cheaply.
3. **Robust verdict** — the renderer (`_mag_line`) reports the drilled value as a
   bracket at small n and a real **median** only at n≥5, and judges spread by **IQR**
   (not raw range) once widened, so a tight cluster plus one stray run reads
   "effectively stable (1 outlier)" rather than "varies run to run". That
   distinction — smooth wide spread vs. tight-plus-outlier — is the entire payoff of
   escalating.

`_magnitude_sample` lives in `collect_runs` but calls `blocking_path._parse_log`
(same-skill import) so the magnitude uses the same detectors as the drill — no
duplicate parsing logic. Cost: 2 extra fetches/pole when tight, ≤`k_max`−1 when wide.

**Singleton sample ⇒ no section, and no citation of it.** When only the drilled run
carries a value (`<2` numeric samples — a repo whose other sampled runs skipped the
job, or a shallow probe), `_mag_line` returns `[]` and the "🔬 Cross-run check" section
does not render. The per-pole agent prompt + the LLM-analysis provenance line therefore
gate their "validated across runs in the cross-run check above" claim on whether the
section actually rendered (`cross_run_rendered = bool(mag_block)`), falling back to
"measured in the drilled run" — a prompt must never cite a section that isn't there.
`verify_report.check_rca_hands_off_never_prescribes` re-derives this per pole (a
cross-run-check locator phrase must co-occur with the section marker in the same pole).

### 12.4a Cache-distribution grounding — a cache pole's framing must match its measured hit rate

A cache-miss finding is born from ONE drilled run's log, and the drilled run is deliberately the
slow-mode representative — so its miss rate is NOT evidence the cache is a broad wall-clock lever.
For a cache leaf (`turbo-*`, `buildx-no-cache`, `install-lifecycle-build`), `_cache_distribution`
stamps a per-pole **`cache_dist`** onto `pr_critical_path.poles[]`: the miss rate across the sampled
runs, split PR vs push, with fork PRs excluded from the upstream median (a fork PR runs cold — it
can't read the repo cache), plus a re-derivable **`verdict`** (`cold | churn | miss-tail |
mostly-warm | insufficient`, `_cache_verdict`). The miss metric is always the per-run
`cache_state.miss_pct` (read by `_cache_state_of_log` uniformly), NOT the leaf's magnitude (which is
seconds for `install-lifecycle-build`).

Two properties make it honest:
- **Every sampled run counts, not only the miss-heavy ones.** The distribution reuses the logs
  `_magnitude_sample` already fetched (no new PR-bucket gh calls), but a run enters iff its
  `cache_state` parsed — regardless of whether `_parse_log` re-fires the leaf on it. Without this a
  warm run (below the leaf's fire floor) would drop out and a `turbo-partial-cache` pole's
  distribution could only ever be ≥40%-miss → stuck at churn. The **push** probe is a targeted
  escalation: ≤ `_CACHE_PUSH_PROBE_MAX` (3) default-branch log fetches, only when a cache leaf
  fired, disclosed in `data_sources.cache_dist_probe`.
- **The renderer obeys the verdict.** `_apply_cache_dist` strips the "BIGGEST LEVER" / "cache-key
  churn" framing on a `mostly-warm`/`miss-tail` verdict and `_cache_health_block` renders the
  distribution + a mandatory cache-context caveat and an invisible `<!-- ci-speedup:cache-context
  -->` marker. `insufficient` (fewer than 2 upstream runs exposed a summary — no cross-run
  grounding) also strips the over-claim but discloses a single-run basis in the note instead of a
  distribution (no marker, since there is nothing measured to size against). `cold`/`churn` keep
  the broad framing.

The class is enforced by `verify_report.check_cache_claim_backed_by_distribution` (a rendered cache
claim must carry a `cache_dist`) and `check_cache_framing_matches_distribution` (the stamped verdict
must equal the verdict RE-DERIVED from the pole's own raw values — a buggy engine can't stamp its
way past it — and a demoted pole must drop the churn/BIGGEST-LEVER framing: `mostly-warm`/`miss-tail`
must also carry the marker, while `insufficient` must drop the framing but is marker-exempt). Both
SKIP on findings that predate `data_sources.cache_dist_probe`.

### 12.4b Off-category leaf demotion — a crowned cause must be the pole's dominant measured work (issue #16)

A `_parse_log` leaf fires on a tool marker ANYWHERE in the joined job log — it does not check that
the tool's work is the pole's DOMINANT step. When it is not, the leaf hijacks the pole's MEASURED
CAUSE: the `eslint-no-cache` (`scan`) leaf fired on nrwl/nx's one combined `Run Checks/Lint/Test/
Build` step (an `nx affected` that lints + tests + builds — binned `test`/payload by §11's
classifier so the OPT72 redundant-work numerator isn't inflated) and crowned it as "the lint step",
pinning the pole's full ~5m08s ceiling on a lint-cache fix though lint is a MINORITY of the measured
time (sveltejs/svelte was the same shape on a type-check-dominated Lint pole).

**The rule** (mirroring the §12.4a "reframe a leaf off a re-derivable measured fact" precedent):
a leaf may crown the cause / claim the pole's ceiling only when its target step-category AGREES
with the pole's measured `dominant_category` — the crown `collect_runs._decompose_job_steps` computes
over the SAME `_step_category` taxonomy (§11) — AND, for a leaf that shares a coarse category with a
distinct sibling tool (eslint vs. type-check, both bin `scan`), the dominant STEP is one the leaf
actually addresses. `blocking_path._offcategory_leaf` maps each `fix_key` to its category
(`_LEAF_STEP_CATEGORY`) and, at the leaf-selection point (right after `_apply_cache_dist`),
`_demote_offcategory_leaf` splits a crowning leaf from a demoted one. A demoted leaf is **not**
dropped silently: the pole falls back to its generic dominant-step hand-off (which points at the
measured dominant step) and the leaf renders below the prompt as a labelled **secondary observation**
(`_offcategory_note_block`, tagged `<!-- ci-speedup:offcategory-leaf -->`). A demoted pole is also
NOT treated as a coverage gap (a detector DID match its log), so it never pulls an LLM gap-fill.

**Ceiling design — demote, don't bound.** Inside a single combined step the lint sub-share is
unmeasurable from step timing alone, so any partial ceiling would be invented; the honest ceiling is
the generic dominant-step wall, which IS measured. So a crowned leaf always has `category ==
dominant_category`, and the pole's `dominant_p50_s` it claims is fully backed by measured time in
that category.

Every pole whose leaf survives demotion emits a per-pole `<!-- ci-speedup:leaf-crown fix_key=… -->`
machine marker. `verify_report.check_detector_leaf_agrees_with_dominant_category` re-derives the
crowned leaf's category from that marker's `fix_key` (`_VR_LEAF_STEP_CATEGORY`, pinned to the engine
map by a coupling test — never a trusted stamped category enum), reads the pole's `dominant_category`
ground truth from `findings.json`, and FAILs any pole whose crowned leaf category ≠ its dominant
category (classified AUTO_SEED / `fabricated-or-unsupported-finding`).

### 12.5 The data bundle (`findings_doc["data_bundle"]`)

`_persist_pole_logs` captures, per pole, into `--data-dir` (all tied to the **one**
nearest-P50 run so L2/L3 stay coherent):

- `<job>-<id>.log` — the drill log (`file`).
- `<job>-<id>.steps.json` — the step timeline (`steps_file`); **no extra gh call**,
  the jobs listing already carries each step's `started_at`/`completed_at`.
- `<job>-<id>.mag.json` — the cross-run magnitude sample (`mag_file`; absent for a
  categorical finding).

Each manifest entry also records the cross-run **duration** `sample` (free — reuses
the sampled instances) and `selected: "nearest-p50"`. The renderer matches a bundle
entry to a pole by workflow-filename substring (longest key wins).

### 12.6 What the reader is told to trust, and the hand-off

The report labels the two scopes so neither is over-read: the **categorical root
cause** is stable across runs (shown once); the **magnitudes** are this run's,
validated by the cross-run check. Level 1 is P50 across PRs; the timeline and the
drill are one representative run, absolute for that run, not P50.

Then it **hands off, it does not prescribe** — the skill-wide contract (§2).
Each pole closes with `_PROMPTS[fix_key]`: a copy-paste prompt that restates the
measured root cause, links the tool's official docs, and asks the user's agent to
investigate the repo and apply a safe change (stating the failure mode). There is
deliberately **no recommendation and no code diff** in the report — the agent has
the repo checkout + the captured bundle and reasons the fix in context. (The
`blocking-path-*.md` worked examples — under `reports/` in the pre-public
development archive; see `examples/` in this repository — are the committed
documentation of this shape (verified by a fresh render each run, §7).)

### 12.6a The PR-floor on a push-only repo — never bury a measured pole as static-only

The merge-wait spine is normally anchored on the **pull_request** flow. A **push-only**
repo (no PR flow — the team merges straight to the default branch, so the PR sample is
0/N and `poles` is empty) used to dead-end to the **static-only** "no run history to
measure" report even when `per_workflow_timing` held a clear measured long pole — the
genuinely slowest measured job (live: `webflow/js-webflow-api`, `test` p50 51.5s vs
`compile` 25.5s) never appeared, and the report answered "why is the merge slow?" with
runner-minute hygiene only. Root cause: `collect_runs._select_pr_floor_workflows` (the
PR-floor synthesis, §12) was gated on `_PR_VOLUME_EVENTS`, so a push-only workflow
synthesized no floor. **Fix:** when NO PR-volume workflow ran, `_select_pr_floor_workflows`
falls back to `_PUSH_VOLUME_EVENTS` (`{push}`) — the push CI *is* the developer's merge
wait there — so a measured long pole still anchors a clearly-demoted PR-floor spine. The
fallback fires **only** when no PR-volume workflow qualifies, so a normal PR repo's
post-merge `push` (deploy) workflow never displaces its PR spine.

The honesty guard is a re-derivation invariant, not a renderer patch: each
`per_workflow_timing` entry now carries its observed `events`, and
`verify_report.check_primary_section_present` re-derives the merge-path eligibility
(mirroring `_select_pr_floor_workflows` — PR-volume first, push fallback, CI-clean) and
**fails any static-only report that has a measured merge-path long pole**. A cron/release-only
repo (timed but on no merge-path event) stays legitimately static-only; an older findings
JSON without the `events` stamp declines to fire rather than false-flag.

### 12.6b The aggregation gate — the degenerate chain SINK (issue #1)

Many repos make a single trivial job the **one required status check**: it runs no
work, `needs:` everything else in its workflow, and exists only so branch
protection has one name to require (vercel/next.js `thank you, build` — job
`buildPassed`, `needs: [deploy-target, build, build-wasm, build-native]`, body
`run: exit 1` behind an `if:`, P50 **3s**). Because it gates every PR it is
correctly crowned by pole frequency — but the generic pole render then handed the
reader a drill placeholder and a "capture timing, then optimize this step" agent
prompt over a 3-second no-op: correct data, **inert advice**, at the very first
thing a reader sees.

`blocking_path._agg_gate_shape` detects the shape at render time from artifact
data already stamped (`workflow_job_graph` + the check spine — no producer
change): **(a)** headline P50 <= `_AGG_GATE_TRIVIAL_S` (30s — below any real
hosted-runner job's checkout+setup floor); **(b)** the pole's job is a *terminal*
node whose transitive `needs:` closure (>= 2 jobs) covers **every non-terminal job
in its workflow** — uncovered siblings must themselves be terminal, which is
exactly the shape of an `if:`-conditional peer sink (`publishRelease`), so
"effectively all the other jobs" is expressed structurally rather than by reading
`if:` (which the scanned graph does not carry); **(c)** no captured/sampled step
above the trivial threshold (step data is a *disqualifier* only — a 3s gate is
never drilled, and the role line discloses when the shape rests on structure
alone); **(d)** at least one upstream member resolves to a **measured** check, so
the report can name where the wait actually is. Duration alone never matches: a
real 3s `lint` job with no `needs:` coverage keeps the ordinary rendering.

On a match the pole renders a `pole_role_line` claim saying what it is — an
aggregation gate whose wait IS its `needs:` upstream — names the slowest measured
upstream member with its P50, and closes with a pointer to the pole that drills
that member (an anchor when it renders, otherwise a named check plus what a re-run
would do — never a dead link). It renders **no drill, no floor note, and no agent
prompt**. Crowning/ranking is untouched (the frequency crown is correct data), and
a sink that is itself a **modal-chain member** keeps the chain-stage framing (the
chain model already frames it as serialized; no double-framing). A pole carrying a
matched log-detector leaf or a routed structural lever also keeps today's render —
there the drill is not inert, and suppressing it would silently drop a measured
lever.

Two coupled invariants hold the seam:
`verify_report.check_speed_poles_complete` **exempts** such a pole from the
"every pole ends in a prompt" rule, and
`check_aggregation_gate_poles_never_prescribe` **fails** any aggregation-framed
pole that carries a prompt, omits the upstream pointer, or whose shape does not
re-derive from `findings.json` (`_agg_gate_pole_keys`, mirroring the renderer) —
so the exemption can never launder a genuinely stunted pole.

### 12.7 The coverage-gap fallback — never dead-end on an unrecognised stack

The catalog can't have a detector for every build tool, test runner, or framework.
A drilled pole whose log `_parse_log` doesn't recognise used to render only the step
timeline + a "no known root-cause pattern — coverage gap" note and stop. That is a
**product failure**: ci-speedup exists to tell the user *what's slow and how to fix
it*, and an honest dead-end still delivers neither. The fix is two-layered, in this
order of preference:

1. **Extend the deterministic catalog first.** When a gap recurs for a common stack,
   add/loosen a `_parse_log` detector so the breakdown stays *measured + auditable*.
   `turbo-partial-cache` (§12.3) is the worked example: the original cold-cache
   detector only fired on a fully cold cache (0 cached / remote disabled), so a build
   with caching ON but most packages still rebuilding (e.g. 57/150 cached, 62% miss —
   the signature of an unstable cache key) fell through. The detector now also fires
   on a high *partial* miss rate (`_PARTIAL_MISS_FLOOR_PCT = 40%`), hedged because
   some misses are legitimately-changed packages (the constraint + cross-run check
   say so). Deterministic coverage is always preferable to the LLM fallback.

2. **LLM gap-fill for whatever the catalog still can't reach.** It is impossible to
   catch every stack deterministically, so when `_parse_log` returns `None` the
   **agent running the skill** (SKILL.md phase 4a) reads the captured job log and
   writes a root-cause analysis — `{cause, breakdown, evidence, prompt}` — passed back
   to the renderer via `blocking_path.py --analysis KEY=PATH`. It renders as a clearly
   **labelled** "🤖 LLM root-cause analysis" section (cause + breakdown + verbatim
   evidence) and a tailored prompt, in place of the dead-end; the waterfall's gap note
   redirects to it (`analysis_present`).

   **The determinism boundary is strict.** Detection, ranking, the critical-path
   spine, the step timeline, and the cross-run magnitude check stay fully
   deterministic and measured — the gap-fill is the *only* non-deterministic step and
   touches **none** of them. It supplies the *cause narrative* for a pole the catalog
   couldn't analyse, and to stay honest it must: (a) **ground** every claim in
   verbatim log lines it quotes in `evidence` (no invented magnitudes — figures are
   computed from lines in the log); (b) be **labelled** LLM-derived ("a strong lead to
   verify, not a measured catalog detector"); (c) end in a prompt, never a prescribed
   diff. Validation: disabling a real detector (e.g. the vitest one) and letting the
   gap-fill read the same log reproduces the detector's conclusion + evidence — the
   fallback reaches catalog-quality findings from the log alone. `verify_report` and
   the adversarial-review rubric (Scope 5) enforce the label + grounding and that no
   pole ships as a dead-end.

## 13. Cross-references

- [`SKILL.md`](SKILL.md) - the canonical contract (phases, admission gate,
  quality review).
- [`references/optimization-patterns.md`](references/optimization-patterns.md) -
  the 73-pattern catalog (METADATA + body per pattern); the source of truth for
  detection and the report's TL;DR / pattern background.
- [`references/wall-clock-methodology.md`](references/wall-clock-methodology.md)
  - critical-path / long-pole / cluster-floor model and the non-additive rule.
- [`references/savings-methodology.md`](references/savings-methodology.md) -
  two-axis sizing and the cache-evidence guardrail.
- [`references/adversarial-review-rubric.md`](references/adversarial-review-rubric.md)
  - the hostile-review contract (actionability, evidence-verifies-the-headline,
  ≥2-pass agreement).
- `maintainers/ci-speedup/MAINTAINERS.md` + `maintainers/ci-speedup/loops/gap-to-catalog-prompt.md`
  (maintainer-only, not shipped in an installed skill — source checkout only) -
  the gap → catalog loop (auto-draft a detector when the LLM fallback fires; §12.7).
