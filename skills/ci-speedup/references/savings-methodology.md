# Savings methodology — sizing a finding on both axes

Every finding is sized on **two axes**: a **runner-minute axis** (billing/cost
— the subtractive step-seconds formula below) and a **wall-clock axis**
(developer wait — Δ critical path). These are two different numbers measuring
two different things; a finding can be large on one and zero (or negative) on
the other.

Those two axes are the whole denomination: **runner minutes and wall-clock
seconds**. **Storage is deliberately out of scope** — artifact and cache
storage is a separate line on the GitHub bill, governed by `retention-days`
and cache eviction rather than by how long a job runs, so no finding here is
sized in gigabytes and no saving claimed here reduces a storage charge. Nor is
storage netted the other way: a fix that adds a cache or an artifact hand-off
increases storage and cache-eviction pressure, and that cost is not subtracted
from the minutes it saves.

The report **ranks the findings table and drives the headline** on the
**wall-clock axis** (developer wait — see `wall-clock-methodology.md` §1); the
runner-minute axis is computed per finding. Measured runner-minute findings with
a stamped wall-clock-neutrality certificate promote to **Runner-minute reductions
(wall-clock-neutral)**; modeled or uncertified residuals stay in **Also
noticed**.

All timing inputs to these formulas are **computed by `collect_runs.py`** from
the gh runs/jobs/logs API — step-level p50/p95, run-level wall-clock, queue and
failure rates. There is no external telemetry source. Where this doc names
numbers, they are illustrative figures from real audits, kept to anchor the
model.

## Contents

- [Sufficient vs insufficient evidence](#sufficient-vs-insufficient-evidence)
- ["Dominant cost" / "largest cost" / "biggest lever" claims](#dominant-cost--largest-cost--biggest-lever-claims)
- [Reliability finding sample-size minimum](#reliability-finding-sample-size-minimum)
- [Runner-minute axis (billing) — the subtractive formula](#runner-minute-axis-billing--the-subtractive-formula)
  - [Term 1: current_30d_sum](#term-1-current_30d_sum)
  - [Term 2: post_fix_30d_sum](#term-2-post_fix_30d_sum)
  - [Billing semantics for the Tier-2 render](#runner-minute-semantics-for-the-tier-2-render)
  - [Forbidden shortcuts](#forbidden-shortcuts)
  - [Term 0 (precondition): verify run-volume from total_count](#term-0-precondition-verify-run-volume-from-total_count)
  - [Term 1.5 (precondition): exclude jobs where the fix costs more](#term-15-precondition-exclude-jobs-where-the-fix-costs-more)
  - [Worked example (runner-minute axis)](#worked-example-runner-minute-axis)
- [Wall-clock axis (Δ critical path)](#wall-clock-axis-δ-critical-path)
- [Monthly wall-clock total is NON-ADDITIVE](#monthly-wall-clock-total-is-non-additive)
- [The tail rule — size the wall-clock axis by P95, not mean](#the-tail-rule--size-the-wall-clock-axis-by-p95-not-mean)
- [Guardrails — never re-derive these](#guardrails--never-re-derive-these)
  - [Speed without coverage is a regression (framework-build removal)](#speed-without-coverage-is-a-regression-framework-build-removal)
  - [Artifact-existence guardrail](#artifact-existence-guardrail)
  - [The dominant-cost-rank trap](#the-dominant-cost-rank-trap)
  - [The dormancy gate](#the-dormancy-gate)
  - [The carry-forward exception](#the-carry-forward-exception)
- [Per-finding internal self-consistency](#per-finding-internal-self-consistency)
- [When evidence is unavailable](#when-evidence-is-unavailable)

## Sufficient vs insufficient evidence

Every finding MUST include concrete, measured evidence that the problem exists
AND that the proposed change would have impact. A structural pattern match alone
is NOT sufficient to create a finding.

| Sufficient | Insufficient |
| --- | --- |
| Cancellation rate comparison: workflow WITH concurrency cancels 16% of runs, workflow WITHOUT cancels 0% — proving stale runs complete unnecessarily | "These workflows lack concurrency groups" (structural match only) |
| Measured P50/P95 with COUNT showing the bottleneck runs frequently | "This job takes X seconds and could be faster" (no baseline comparison) |
| Before/after data from a similar fix in another workflow or repo | "Best practice recommends X" (no evidence the problem exists here) |
| Actual hung-run or timeout incidents from the run history | "Missing timeouts could cause 6h hangs" (speculative without incidents) |
| Shard duration comparison from jobs data across multiple runs showing >3× imbalance, with run IDs cited | "The shards look unbalanced" (no measured data) |
| Cross-workflow build count per PR push with identical command hashes and measured build time per invocation | "Multiple workflows build the same thing" (no count or time data) |
| Missing cache action + measured build time per job showing redundant compilation | "No caching is present" (structural match only — must include time impact) |
| Build script inspection showing internal `rm -rf` that defeats caching, with file path and line number | "The build probably cleans up" (no source verification) |

## "Dominant cost" / "largest cost" / "biggest lever" claims

Any prose framing that calls a step the "dominant cost", "largest cost",
"biggest lever", "long pole", "primary bottleneck", or any synonym MUST
disambiguate which of two ranks it means. Conflating these is a methodology
bug — see [the dominant-cost-rank trap](#the-dominant-cost-rank-trap) below.

The two ranks are:

1. **Raw P50 rank** — what step is largest by measured P50 / P95 duration,
   regardless of whether any change shape can address it.
2. **Addressable-savings rank** — what step is the largest one a given change
   shape (caching, sharding, paths-filter, parallelism, tool swap, etc.) could
   actually eliminate or reduce.

These are NOT the same ranking. The largest raw P50 step is often a test step
that cannot be cached or sharded further; the largest addressable step under a
given change shape is whatever is at the top of the rank AFTER filtering out
steps the change can't touch.

**Rule.** Before writing "dominant cost" / "largest cost" / "biggest lever" /
equivalent in any finding, recommendation, or report prose, verify that ONE of
the following two evidence forms is satisfied:

| Form (a): unqualified raw rank | Form (b): qualified addressable rank |
| --- | --- |
| Cite the raw P50 rank with the **full top-5 list of step names + P50 + count** (from the step-duration breakdown `collect_runs.py` computes). The cited step MUST be #1 in the raw P50 list. If the largest raw step is NOT the recommended target, switch to form (b). | Qualify the prose explicitly: "the largest **addressable** cost", "the largest cost a **cache** change can eliminate", "the largest cost a **sharding** change can eliminate", etc. Name the change shape. Then cite the raw top-5 list AND state which steps you filtered out as non-addressable and why (e.g. "test execution time is not cacheable; sharding is already at 4 shards"). |

If neither form fits, the prose is wrong. Rewrite without the "dominant cost"
framing — describe what the step does and what fraction of job time it accounts
for, without claiming primacy.

This rule applies to Performance Findings prose, Executive Summary paragraphs,
the PR-description text embedded in any AI prompt block, and any deep-dive doc
derived from a report.

## Reliability finding sample-size minimum

Reliability findings citing a workflow failure rate require **n ≥ 30 runs over
the audit window** to be promoted to MEDIUM or higher severity. Small-sample
failure rates are sampling artifacts, not signal.

| Sample size (n) | Treatment |
| --- | --- |
| n ≥ 30 | Eligible for MEDIUM / HIGH / CRITICAL based on measured failure rate. |
| 5 ≤ n < 30 | Classify as `severity: PROBE`. Note: "small sample — true rate may differ materially". No quantified saving. |
| n < 5 | Omit the finding entirely. The rate is not interpretable. |

A `PROBE` reliability finding must be surfaced as a **probe**, not a finding,
and must not assign a quantitative "Monthly Saving (runner-min)". Probes
recommend a follow-up investigation, not a fix.

---

## Runner-minute axis (billing) — the subtractive formula

The runner-minute Monthly Saving of a finding is computed by the **subtractive
formula**:

```
saving_30d = current_step_time_30d_sum  −  post_fix_step_time_30d_sum
```

where each term is a sum of step-seconds over the 30-day window for the affected
step(s). Unlike the wall-clock axis (below), **these per-finding runner-minute
savings DO add across findings** — they are sums of disjoint step-seconds, so
the monthly runner-minute total is the sum of the per-finding savings.

### Runner-minute semantics for the Tier-2 render

The Tier-2 section displays runner-minutes only:

- **Raw credited minutes** — the subtractive runner-minute saving above, summed
  after the Tier-2 de-overlap pass.
- **Billable-equivalent minutes** — the same saving under GitHub's per-job minute
  round-up (`ceil(job_seconds/60)`), the basis the cost-spine table and Tier-2
  rankings use.

All figures are runner-minutes; multiply by your runner's per-minute rate to get
dollars. The report leaves the per-minute rate to the reader. The calculation
does not model sub-minute rounding deltas; cards disclose that per-job billing
rounds can change the actual bill. The single exception is OPT65, which measures
the billing-rounding delta itself from sampled job timestamps using
`sum(ceil(job_seconds/60)) - ceil(sum(job_seconds)/60)` and claims
billable-minute waste only, not runtime or wall-clock speedup. Its scale uses the
monthly volume for the sampled event scope, and its verifier re-derives the
credited billable minutes from structured per-sample rounding evidence.

### Term 1: current_30d_sum

`current_step_time_30d_sum = mean_step_duration × invocations_30d`

- `mean_step_duration` comes from the per-step timing `collect_runs.py` computes
  for the affected (workflow, job, step). **Use the mean, NOT p50.** P50
  systematically understates bimodal distributions — e.g. a Build step can be
  28s P50 (warm) but its true mean across cold runs is 40–50s. The total time
  over a month is `sum = mean × count`, not `P50 × count`.

> **Cache-context sensitivity (cache-miss findings).** A cache-miss finding is measured from
> ONE drilled run — deliberately the slow-mode one — so its miss rate is not the typical
> experience. Before sizing it as a broad lever, read the pole's stamped `cache_dist`: the
> per-event, fork-excluded miss distribution + `verdict`. If the verdict is `mostly-warm` or
> `miss-tail`, frame the saving as *"helps cache-miss-heavy PRs"* (a tail/variance win), NOT
> *"speeds up CI by X%"*, and size against the miss-heavy tail, not every run. Never project a
> full-job wall-clock saving from a cold or fork-clone benchmark: a fork can't read the repo
> cache, so it shows a worst-case cold build. And distinguish a **cache-MISS problem** (fix the
> key / restore) from **work that runs even when cached** (`install-lifecycle-build`: a build
> runs during `pnpm install` — the fix is `--ignore-scripts` + an explicit cached step, not a
> cache key). The report's cache-context caveat + `<!-- ci-speedup:cache-context -->` marker
> carry this; don't override it.
- `invocations_30d` is the number of times the step ran in 30 days. For matrix
  jobs that's `matrix_size × total_30d_runs`. For single-instance jobs it's
  `total_30d_runs`.
- Use canonical run-volume denominators from the figures `collect_runs.py`
  computes (Term 0 below).

### Term 2: post_fix_30d_sum

`post_fix_step_time_30d_sum = estimated_post_fix_duration × invocations_30d`

- **NEVER set this to zero unless the fix removes the step entirely.** Most
  fixes have a non-zero post-fix cost. Common examples:
  - Cache restore: 1–5s per invocation
  - Artifact download: 5–10s per invocation
  - Replacement assertion (`expect.toBeVisible({timeout: 5000})`): mean
    ~500–1500ms depending on page-load timing
  - Docker build with warm cache: 30s if all layers cached, more on misses
  - Composite-action invocation: 1–2s overhead
- The `invocations_30d` factor is the same as in Term 1 unless the fix changes
  the call count (e.g. a "remove this matrix variant" fix divides invocations by
  the matrix size).

### Forbidden shortcuts

The following formulae are **invalid** and must not appear in any Finding's
saving calculation:

| Bad shortcut | Why it's wrong |
| --- | --- |
| `saving = per_run_saving × runs/mo` | Implicitly treats post-fix as 0, and uses a hand-eyed per-run delta instead of measured step times |
| `saving = (current_P50 − post_fix_P50) × invocations` | P50 is not a sum-preserving statistic; for bimodal step durations the true sum is mean × count, not P50 × count |
| `saving ≈ X% of wall_clock_budget` | (Scoped to **runner-minute (bill) axis** sizing.) Wall-clock × runs UNDERSTATES runner-minutes for matrix workflows; use the monthly runner-minute budget (the sum-of-jobs estimate `collect_runs.py` computes) when comparing against a budget |

**Scope note on the last row.** That shortcut is forbidden **only when sizing
the runner-minute (bill) axis**. For the **wall-clock ranking** (the report's
headline), the critical-path delta — and the `Δ wall-clock per run × building-runs/mo` total
([below](#monthly-wall-clock-total-is-non-additive)) — IS the correct headline
figure, expressed as developer-minutes/mo of wait; the runner-minute budget
comparison lives in the "Also noticed" appendix. The row is not deleted because
it remains the right rule when sizing the runner-minute axis.

### Term 0 (precondition): verify run-volume from total_count

Before computing Term 1, get `total_30d_runs` from the AUTHORITATIVE source —
GitHub's own `total_count` field, which reports the real total even when the
result set is capped at 1,000:

```bash
gh api "repos/{owner}/{repo}/actions/workflows/{id}/runs?per_page=1&created=>=<30d-ago>" \
  --jq '.total_count'
```

**Do NOT infer monthly volume from a run-frequency window shorter than 30 days,
and do NOT trust a capped `collect` fetch as the monthly figure.** A real audit
once used "~60 runs/month" — that was ~1 day of a capped fetch; the true 30-day
count was ~1,800 (a **~30× error** that made the benchmark report's monthly
saving ~30× too low). Cross-check with a 7-day count × 30/7. Quote the
`total_count` query in the Finding's evidence.

### Term 1.5 (precondition): exclude jobs where the fix costs more

A fix is only a saving on a job where `post_fix_duration < current_duration`.
If a job's baseline step is already cheaper than the post-fix replacement,
including it in the consumer set is a NEGATIVE contribution — drop it.

> Worked example (illustrative): a build-dedup fix replaces each consumer's
> `pnpm run build` with an artifact download (~8s). Consumers whose build is
> 40–65s → download is a clear win. But a job running a narrow
> `pnpm --filter=worker... run build` at ~10s is *cheaper* than the ~8s
> download + extract + prebuild-share overhead — a benchmark proved that making
> it a consumer ADDS ~75s/run. An earlier draft wrongly counted that job's
> builds as saveable, inflating the saving by ~118,000 step-seconds/mo. Always
> list excluded jobs and the one-line reason.

**Wall-clock note (build-dedup is a runner-minute win but wall-clock-NEGATIVE).**
The above is the *runner-minute* axis. On the **wall-clock** axis the same
build-dedup fix is **negative**: it consolidates builds that today run
overlapped-in-parallel into a single upstream `build` job the rest of the
pipeline must `needs:`, inserting a serial stage plus a ~190 MB artifact
download — measured at **+70–90s/run** (`wall-clock-methodology.md` §4). So
in the wall-clock ranking this finding must be **demoted out of the ranked
findings** (flag "do NOT ship for wall-clock") and **paired with its warm-cache
alternative** — make each parallel build a warm cache hit instead of removing
the redundancy via a serial gate.

### Worked example (runner-minute axis)

**Finding:** Duplicate `pnpm run build` across 8 build consumers per CI/CD run.

**Term 0 — volume:** `total_count` = **~1,800** runs/30d (GitHub API, not
capped). ~92% run the matrix (46/50 sampled) → **~1,653 building runs**.

**Term 1.5 — consumer set:** 8 consumers. One worker job EXCLUDED (filtered
build ~10s < download cost; bench-verified it adds ~75s/run).

**Current cost (sum of step times in 30d), mean durations from the per-step
timing collect_runs.py computes:**

| Job | mean Build step | builds / building-run | Step-seconds / building-run |
| --- | --- | --- | --- |
| `tests-web (matrix)` | 40.4s | 6 | 242.4 |
| `e2e-tests` | 34.0s | 1 | 34.0 |
| `e2e-server-tests` | 65.5s | 1 | 65.5 |
| **Gross / building-run** | | **8** | **341.9s** |
| **× 1,653 building runs ÷ 60** | | | **~9,419 min/mo** |

**Post-fix cost:** 1 prebuild (~70s cold full build) + 8 downloads × ~8s =
**134s/building-run** → 134 × 1,653 ÷ 60 = **~3,692 min/mo**.

**Saving:** 9,419 − 3,692 = **~5,727 min/mo** (point estimate).

**Sensitivity (always state it):** building-runs derived from the
directly-counted consumer-builds ÷ 8 = 1,532 → ~5,310 min/mo; pricing builds at
the benchmark's conservative cold-only 62s instead of the 40.4s production mean
→ ~9,000 min/mo. Honest headline: **~5,300–5,700 min/mo, up to ~9,000 cold.**

**Wall-clock axis for this same finding (opposite sign).** Every number above
is the **runner-minute** axis. On the **wall-clock** axis this fix is
**negative**: the serial `build` gate + ~190 MB artifact download adds
**+70–90s/run** (structural delta ≈ +84s; `wall-clock-methodology.md` §4). So
although build-dedup is the **#1 finding on the runner-minute (bill) axis**,
it is **wall-clock-negative**, so the report **demotes it out of the ranked
findings** (it surfaces in "Also noticed", flagged wall-clock-negative) and
pairs it with the warm-cache alternative (each parallel build → ~8s cache
restore, **−33s on every cluster job, no gate**).
The two budgets move in opposite directions: this fix spends ~70–90s × ~1,653 ≈
**+2,000–2,500 developer-min/mo of wait** to save ~1,950 runner-min/mo of bill.

The Finding's "Estimated saving" table cell shows both terms and the
difference, not just the difference, AND renders the six-step derivation
(volume → building fraction → consumer set → mean duration → gross → patched →
net) as a table in the Evidence section so a reader can reproduce it.

---

## Wall-clock axis (Δ critical path)

The wall-clock saving of a finding is **NOT** step-seconds × invocations. It is
the number of seconds the fix removes from (or adds to) the **long pole** — the
single slowest parallel job in a run (`wall-clock-methodology.md` §2). Size it
at **P50 AND P95**, both, per finding.

State **when** the win is realized — the three realization classes
(`wall-clock-methodology.md` §3):

| Class | Meaning (short) |
| --- | --- |
| **direct** | The fix is on a job that is *currently* the long pole (or on all cluster jobs) → lowers P50 immediately. |
| **tail** | Shows up only at P95 — heavy-tailed step, or job that is *occasionally* the long pole. |
| **stacked** | Zero on its own (job sits below the current long pole); realized only after the taller cluster jobs also come down. |

**The below-floor rule (REQUIRED gate).** A finding that only speeds a job which
**already finishes below the cluster floor** (the second-tallest job's duration
— `wall-clock-methodology.md` §2) delivers **ZERO wall-clock**, no matter how
large its runner-minute number. Label such a finding **billing / housekeeping
only** and rank it in the runner-minute tier — never in the wall-clock Tier-1
ranking. Canonical below-floor examples: lint-cache, missing
`concurrency: cancel-in-progress`, `fetch-depth: 0` on a fast helper job. The
runner-minute subtractive formula above will still produce a positive number for
these — that number is real, but it is bill, not wait.

## Monthly wall-clock total is NON-ADDITIVE

The two monthly totals are computed **differently**:

- **Runner-minute monthly total** = the **SUM** of the per-finding subtractive
  savings (they are disjoint step-seconds — see "Runner-minute axis" above).
  Summing is correct.
- **Wall-clock monthly total** = derived from the **STACKED MODEL**, NOT the sum
  of per-finding Δ critical-path values. Per-finding wall-clock deltas overlap
  on the cluster floor (cutting the long pole only helps until it hits the
  floor; a below-floor or stacked finding contributes 0 until its siblings
  drop), so **summing them overstates the total — sometimes wildly**
  (`wall-clock-methodology.md` §7).

Compute the wall-clock total from the stacked model:

```
Δ wall-clock per run  =  current wall-clock (P50)  −  stacked-model wall-clock (P50)
monthly total         =  Δ wall-clock per run  ×  building-runs/mo  ÷  60
                         (developer-minutes of wait removed per month)
```

Use **building-runs/mo** (runs that actually execute the cluster), not all runs,
and report both a **P50 line and a P95 line**. See `wall-clock-methodology.md`
§7 for the full derivation and the worked total (~3,400 developer-min/mo ≈ 57
hours/mo at P50). **Contrast the two totals explicitly in the report: the
runner-minute total sums across findings; the wall-clock total does not.**

## The tail rule — size the wall-clock axis by P95, not mean

For the **wall-clock axis**, a heavy-tailed step is sized by **P95, not the
mean** — the tail *is* the developer-pain event, and a mean averages it away
(`wall-clock-methodology.md` §5). Heavy-tailed test: `mean / P50 > ~1.5` or
`P95 / P50 > ~3`.

This **differs from** the runner-minute rule above, and both coexist:

| Axis | Statistic for the monthly figure | Why |
| --- | --- | --- |
| **Runner-minute** monthly SUM (Term 1) | **mean** (NOT p50) | The sum over the month is `mean × count`; P50 understates bimodal/cold-run distributions. |
| **Wall-clock** tail sizing | **P95** (NOT mean) | The tail is the wait a developer actually feels; the mean hides the worst run. |

> Worked example (illustrative — a Playwright browser install step): P50
> **56s**, mean **108.6s**, P95 **370s** — one sampled run stretched it to
> **516s**, gating the whole pipeline. The **runner-minute view averages the
> tail away** (the mean "corrects" the finding down); the **wall-clock view
> ranks it #1** by P95 because it is the single largest source of tail
> wall-clock. Same step, opposite priority on the two axes — the report ranks by
> the wall-clock (P95) view; certified wall-clock-neutral runner-minute findings
> move to Tier 2, while residual modeled/uncertified runner-minute figures stay in
> "Also noticed" and can average the tail away.

**A bimodal tail needs a logged root cause.** A heavy-tailed step is not
actionable from timing alone — the fix depends on *why* the tail happens (cold
cache miss, network stall, lock contention, a flaky retry). Pull the slowest
sampled run's job log (`collect_runs.py` reads logs for the affected jobs) and
quote the line(s) that explain the slow run before sizing the finding. Without a
logged root cause, it is a probe, not a sized fix.

---

## Guardrails — never re-derive these

These are empirical lessons paid for in benchmark cycles and bad
recommendations that backfired. Re-deriving any of them costs real time and
erodes user trust.

### Speed without coverage is a regression (framework-build removal)

**What happened:** an audit recommended deleting a framework build step (e.g.
`next build` / `yarn build`) from a test job because the test runner doesn't
read its output — replacing it with `tsc --noEmit`. Measured **88s/run saved,
2.87× speedup** (verified, 10 runs). It shipped, then was **reverted**: the
framework build does much more than `tsc --noEmit` — route validation, Server
Component prop validation, RSC boundary checks, page-data collection, env-var
inlining, dead-code elimination, codegen wiring. None are covered by
`tsc --noEmit`. Removing the build loses unique breakage signal that the deploy
build would then have to catch later.

**Rule.** A step that's faster but loses unique validation coverage is a net
negative. Before recommending step removal, enumerate what the step uniquely
validates and confirm a specific OTHER step covers each item. Any "delete the
framework build" finding requires a **coverage table** (each thing the build
catches → the specific CI step that still catches it, or "GAP — must be filled
before shipping"). **Severity is capped at MEDIUM until the table is complete.**

This guardrail extends to a subtler trap: a build can be **load-bearing at
runtime** even when no test imports its output directly. If non-test source
spawns a Worker thread or child process that `require`s the compiled output
(`new Worker(...)`, `child_process`, `require()`/`import()` of a `dist/` path,
`path.join(__dirname, "*.js")`), dropping the build makes those workers fail at
runtime — often silently (the test passes with zero workers). Before removing
any build, grep the affected package's `src/` for these four patterns; if ANY
non-test source matches, the build is load-bearing and the finding is
**INVALIDATED**, not probed.

### Artifact-existence guardrail

**What happened:** a detector emitted a CRITICAL finding — "10 parallel E2E jobs
each rebuild instead of consuming the Prebuild artifact", estimated 12,000
min/mo — reasoning from YAML alone: the downstream workflow triggers on
`workflow_run: Prebuild completed` but has no `actions/download-artifact` step,
so it "ignores" Prebuild's output. **Direct re-read of the producer showed it
has NO `actions/upload-artifact` step at all** — its only outputs were a warm
remote build cache and a PR status update, which the downstream jobs DO consume.
The genuine redundancy was per-job install + cold-cache-tail recovery, ~4–6×
smaller than claimed. Downgraded CRITICAL → HIGH.

**Rule.** Any finding framed as "downstream ignores the producer's artifact" or
"wire up the [producer] artifact" must verify the producer actually contains
`actions/upload-artifact` BEFORE the savings number is computed. If the producer
doesn't upload, the fix is a NEW pipeline (not a wiring fix) and the savings are
limited to install/equivalent dedup plus measured cold-tail recovery — never
assume "downstream rebuilds from scratch" without P50/P95 step data showing the
cache miss.

### The dominant-cost-rank trap

This is the failure mode behind the
["Dominant cost" claims rule](#dominant-cost--largest-cost--biggest-lever-claims)
above. Calling a step the "dominant cost" when it is only the largest
*addressable* cost (not the largest raw P50 step) has produced misleading
customer-facing PR descriptions. The largest raw step is frequently a test step
that no caching/sharding change can touch; the largest step a given change
*can* reduce is a different rank. Always satisfy form (a) (the step is #1 raw,
with the top-5 list) or form (b) (qualify the prose as "largest addressable …",
name the change shape, and list the steps filtered out as non-addressable).

### The dormancy gate

A finding's savings are only real if the affected workflow/step actually runs in
the audit window. Before sizing anything, confirm from the run volume
`collect_runs.py` computes that the workflow fired enough times for the
statistics to mean something. A workflow that ran a handful of times in 30 days
(or is effectively dormant) cannot carry a quantified monthly saving — treat it
as a probe or omit it. This is the run-volume analog of the n ≥ 30 reliability
sample-size gate: no volume, no quantified saving.

### The carry-forward exception

Even a finding you **reject** can be load-bearing for the *right* fix. The
serial-gate build-dedup finding (rejected on the wall-clock axis) is rejected as
shipped, but the build-input understanding it produced — which packages are
built, with what filters, in what order — is exactly what the warm-cache
alternative needs to set its cache key correctly. Don't discard the
investigation when you discard the recommendation; carry the structural
understanding forward into the alternative fix.

---

## Per-finding internal self-consistency

Every numeric value in a Finding's prose (Recommendation paragraph, Risk
description, plain-English summary, narrative analysis) MUST match the Finding's
table-cell values (Estimated saving, Severity, Workflow activity).

> A real audit's Finding had a table cell reading `~2,000–3,500 runner-minutes
> per month` while its own Recommendation prose said `"could be at the lower end
> (~100 min/mo or less)"` — a ~30× contradiction carried over from an earlier
> draft with a different denominator. Such contradictions mislead the reader
> (which number is real?) and undermine the report's credibility.

**Rule.** After writing any finding, scan the prose for numeric values and
verify each against the table cells. If they disagree, identify which is correct
(usually the one most recently recomputed against current data), fix the other,
and do NOT silently rationalize the discrepancy with hedging language — resolve
it.

## When evidence is unavailable

If you cannot gather sufficient evidence for a pattern match:

1. Re-run `collect_runs.py` with a wider window / larger run sample so bimodal
   distributions and rare events show up.
2. Pull the relevant gh job logs directly for the specific behavior.
3. If evidence genuinely doesn't exist after retries, downgrade to an
   **Informational Note** (not a numbered finding) — e.g. "best practice, no
   measured impact".
4. Do NOT include speculative savings estimates.
