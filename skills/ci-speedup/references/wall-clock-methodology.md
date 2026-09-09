# Wall-clock methodology — rank findings by developer wait

This reference is **load-bearing for ranking**. Read it during sizing (before
sizing any finding) and during the report's verification step (before sorting
the findings table).

It encodes one hard lesson: ranking by **the cloud bill** (runner-minutes) and
ranking by **developer wait** (wall-clock) produce a *different order* — and the
runner-minute order can recommend a change that makes developers wait **longer**.
That is why the report ranks on wall-clock and only ever *discloses* the
runner-minute axis (it never ranks on it). The worked proof is the "build once,
fan out" parity benchmark (see §4): it saves ~1,950 runner-min/mo while *adding*
~70–90s of wall-clock per run.

All timing figures referenced here are **computed by `collect_runs.py`** from
the gh runs/jobs/logs API — not from any external telemetry system. Where this
doc names numbers (e.g. `+70–90s/run`, `−1,950 runner-min/mo`), they are
illustrative figures from real audits, kept to anchor the model.

## Contents

- [1. Wall-clock is the ranking axis (runner-minutes is disclosed, never ranked)](#1-wall-clock-is-the-ranking-axis-runner-minutes-is-disclosed-never-ranked)
- [2. Critical-path / long-pole model (how to size wall-clock)](#2-critical-path--long-pole-model-how-to-size-wall-clock)
- [3. Size every finding on BOTH axes](#3-size-every-finding-on-both-axes)
- [4. Serial-gate / "consolidate-then-fan-out" findings can be wall-clock-NEGATIVE](#4-serial-gate--consolidate-then-fan-out-findings-can-be-wall-clock-negative)
- [5. Don't average away the tail](#5-dont-average-away-the-tail)
- [5a. Observed timing spread is descriptive, never an inference](#5a-observed-timing-spread-is-descriptive-never-an-inference)
- [6. Reliability is a wall-clock multiplier](#6-reliability-is-a-wall-clock-multiplier)
- [7. Report structure](#7-report-structure)

---

## 1. Wall-clock is the ranking axis (runner-minutes is disclosed, never ranked)

The report **always ranks on the wall-clock axis** — developer wait on the
critical path. The runner-minute (cloud-bill) axis is still sized per finding,
but it is *disclosed*, never used as the ranking key. Three numbers describe a
finding; only the first sorts the report:

| Axis | What it measures | Role in the report |
| --- | --- | --- |
| **Wall-clock** (developer wait) | `updated_at − created_at` per run = the time a developer waits for CI to go green = the **critical path** through the job DAG | **The ranking key.** Headlines the report and sorts the findings table. The thing engineers feel. |
| **Runner-minutes** (billing/cost) | Sum of all parallel job-seconds = billable compute | Sized per finding. Measured+certified wall-clock-neutral rows render in **Runner-minute reductions**; modeled/uncertified residuals stay in **Also noticed**. Never ranks Tier 1. |
| **Reliability** (first-pass success) | failure rate; flaky-fail = full wait + redo | Folded INTO wall-clock as a multiplier (a re-run = full wait + redo — see §6), not a separate ranking. |

**Rules:**
1. **Rank and headline by wall-clock.** No per-run objective is selected at run
   time; the report's axis is always developer wait on the critical path.
2. **The report names its axis in the headline** — the title ("why is the merge
   slow?") and the `> **Bottom line.**` both frame the result as developer wait
   on the merge gate (`verify_report.check_headline_names_wall_clock` enforces
   it). A reader must never have to guess which number sorts the report.
3. **The runner-minute arithmetic is still computed** (it's cheap and some
   findings are billing-only) — measured+certified neutral rows get a first-class
   Tier-2 section, while modeled/uncertified rows stay in "Also noticed". It
   never changes the Tier-1 wall-clock order.
4. **A finding's rank can differ between the two axes.** A finding can be big on
   the bill yet zero on wall-clock (it sits below the cluster floor); such a
   finding sinks beneath every real lever in the ranking and surfaces only in
   Tier 2 when measured+certified, otherwise in the appendix. A fix that is
   wall-clock-NEGATIVE (serial-gate/dedup, §4) is disclosed as a bill saving
   that *adds* developer wait, not a speed win.

---

## 2. Critical-path / long-pole model (how to size wall-clock)

Wall-clock is **NOT** the sum of job-seconds. For a fan-out/fan-in pipeline:

```
wall-clock  ≈  entry-gate  +  max(parallel jobs)  +  joiner  +  scheduling overhead
```

- **entry-gate** — the job everything `needs:` (e.g. a `pre-job` paths-filter / skip-duplicates gate). Serial, on the critical path.
- **max(parallel jobs)** — the **long pole**: the single slowest job in the fan-out. Everything else overlaps with it for free.
- **joiner** — the `all-*-passed` / required-status aggregator. Serial.
- **scheduling overhead** — runner provisioning + gaps between stages.

> **Runner type.** This public skill targets **gh-hosted (ephemeral) runners**:
> each job gets a fresh VM, nothing persists between jobs, and provisioning +
> queue time is part of the scheduling-overhead term above.

### Define these terms in the report's Methodology section

| Term | Definition |
| --- | --- |
| **Long pole** | The slowest parallel job in a run. **It rotates run-to-run** — sample several runs and show which job is last in each (it is rarely the same one every time). |
| **Cluster** | The set of jobs whose durations are within striking distance of the long pole (e.g. all jobs in the top ~30% band). Any of them can be the long pole on a given run. |
| **Cluster floor** | The duration of the **second-tallest** job. **Cutting the long pole only helps wall-clock until it hits the floor** — then the next job gates. So wall-clock fixes must be **stacked across the whole cluster**, not applied one at a time. A fix that lowers one cluster job below the floor moves nothing until its siblings also come down. |
| **Below the floor** | Jobs that finish well before the cluster. Speeding them up saves runner-minutes but **zero wall-clock** (they were never the bottleneck). |

### MANDATORY: validate the model against a real run

Before quoting any wall-clock delta, validate `wall-clock ≈ entry-gate +
max(parallel) + joiner` against **one sampled run**: compute the
`min(job.started_at) → max(job.completed_at)` span (from the gh jobs API, the
same data `collect_runs.py` reads) and confirm it matches that run's
`updated_at − created_at` (and the fleet mean) within scheduling noise.
If they diverge wildly, jobs are NOT running in parallel (a `needs:` chain
serializes them) — re-derive the critical path from the actual DAG before
sizing anything.

> **Mechanized (ENG-1 PR-N2).** The engine now performs this rule itself:
> `collect_runs.py` stamps per-PR **chain facts** (the longest path through
> each workflow-local `needs:` DAG, capped member spans, plus the empirical
> per-PR makespan as a cross-check) and a `chain_summary`. When the typical
> gate is a **>=2-member chain**, the report's headline names the chain and
> its summed p50 instead of the slowest single check, the Level-1 framing
> says which bars serialize, and a divergence note renders when the chain
> sum and the observed wall disagree beyond tolerance (both signs are
> possible: queue gaps push the wall above the sum; re-run-inflated member
> spans push the sum above the wall). `verify_report`'s
> `check_headline_chain_matches_stamp` re-derives all of it from the
> stamped facts.

> Worked validation (illustrative): on one sampled run the job-span
> `min-start → max-end` = **289s** vs. run wall-clock **292s** vs. fleet mean
> **291s** → model holds, jobs are parallel. (Serial would have been ~46 min =
> the runner-minute total.)

---

## 3. Size every finding on BOTH axes

Each finding states **both**:

- **Δ critical-path** — seconds removed from (or added to) the **long pole**,
  at **P50** and at **tail/P95** (the per-step p50/p95 `collect_runs.py`
  computes). This is the wall-clock number.
- **Δ runner-minutes** — the subtractive `current − post_fix` step-seconds ×
  invocations (the methodology in `savings-methodology.md`). This is the
  billing number.

…plus **when** the wall-clock win is realized:

| Realization | Meaning |
| --- | --- |
| **direct** | Lowers the long pole at P50 immediately (the fix is on a job that is *currently* the long pole, or on all cluster jobs). |
| **tail** | Only shows up at P95 — the fixed step is heavy-tailed (see §5) or the job is only *occasionally* the long pole. |
| **stacked** | Zero on its own (the job sits below the current long pole); realized only after the taller cluster jobs are also reduced. Still worth doing as part of a cluster sweep. |

**The below-floor rule (REQUIRED gate).** A finding that shaves a job which
already finishes below the cluster floor delivers **zero wall-clock**. Label it
**billing / housekeeping only**. Promote it to Tier 2 only when it is measured
and carries a neutrality certificate; otherwise keep it in Also noticed, no
matter how large its runner-minute number. Canonical below-floor examples: lint-cache, missing
`concurrency: cancel-in-progress`, `fetch-depth: 0` on a fast helper job, or
billing-rounding cleanup across tiny off-spine matrix legs whose combined
credited leg p50 stays below the floor.
(In one audit these were a prior report's HIGH-value findings — and they move
developer wait by 0 seconds.)
For per-job billing-rounding cleanup, the below-floor certificate is mandatory:
if any matrix leg can sit on the gate, consolidating legs or lowering parallelism
can add wait instead of removing it.

---

## 4. Serial-gate / "consolidate-then-fan-out" findings can be wall-clock-NEGATIVE

This is the trap that motivated this whole reference.

**The class:** "build once and share" / "prebuild job + downstream consumers" /
any fix that **consolidates work that currently runs overlapped-in-parallel into
a single upstream stage** the rest of the pipeline must `needs:`.

**Why it's a billing win but a latency loss:** in the baseline, N copies of the
work run in parallel, so wall-clock pays for **one** of them. The fix removes
N−1 copies of *compute* (runner-minute win) but inserts a **serial stage** ahead
of the fan-out — so wall-clock now pays for that stage **plus** the artifact
transport, on top of what's left. Net: developer wait goes **up**.

**REQUIRED before recommending any finding in this class:** run the
critical-path check.

```
Δ wall-clock  =  + (new serial stage duration)
              +  (artifact upload + download/extract on the critical consumer)
              −  (work removed from the job that is the long pole)
```

If `Δ wall-clock > 0`, the finding is **wall-clock-negative**. It MUST be
flagged "do NOT ship for wall-clock" and demoted to the "Also noticed"
appendix — never placed in the Tier-1 ranking.

**Worked proof (cite it):** a 10-run parity benchmark of a "build once, fan out
artifacts" optimization measured:
- Runner-minutes: **−1,950 min/mo** (a real cost win, ~34% of the original estimate).
- Wall-clock: **+70–90s per run** (serial `build` gate + ~190 MB artifact download; net-negative on ~20% of runs). The structural delta: build-job (~95s) − build removed from critical consumer (~40s) + download/extract (~29s mean) ≈ **+84s**.

**ALWAYS pair a serial-gate finding with its wall-clock-correct alternative.**
The goal ("stop doing the same build 8×") is usually achievable *without* a
serial gate by **removing the cost of the redundancy instead of the
redundancy itself** — i.e. make the duplicated step a warm cache hit so each
parallel copy is cheap:

| Approach | Mechanism | Δ wall-clock |
| --- | --- | --- |
| Build dedup (serial prebuild) | one gated build + artifact fan-out | **+70–90s** (worse) |
| **Warm the build cache** (Turbo/Next.js key fix) | each parallel build becomes a ~8s cache restore | **−33s on every cluster job, no gate** |

Same redundancy removed, opposite sign on the metric the customer cares about.
The build-input understanding from a dedup investigation is exactly what the
cache-key fix needs — so even a rejected dedup finding feeds the right one.

---

## 5. Don't average away the tail

For wall-clock, a heavy-tailed step is sized by **P95, not the mean** — because
the tail *is* the developer-pain event (a 9-minute pipeline instead of a
4-minute one), and a runner-minute mean hides it.

> Worked example (illustrative — a Playwright browser install step): P50
> **56s**, mean **108.6s**, P95 **370s** — and on one sampled run it stretched
> the `e2e-tests` job to **516s**, gating the entire pipeline. The
> runner-minute view "corrected" this finding *down* (averaging the tail away);
> the wall-clock view ranks it **#1** because it is the single largest source
> of tail wall-clock.

Rule: when `mean / P50 > ~1.5` (or `P95 / P50 > ~3`) the step is heavy-tailed.
Size its wall-clock impact by P95, name the worst sampled run, and treat
eliminating the tail as the headline benefit.

> **Tail findings need a logged root cause.** A bimodal / heavy-tailed step
> isn't actionable from timing alone — the fix depends on *why* the tail
> happens (cold cache miss, network stall, lock contention, a flaky retry).
> Before sizing a tail finding, pull the slowest sampled run's job log
> (`collect_runs.py` reads logs for the affected jobs) and quote the line(s)
> that explain the slow run. Without a logged root cause the finding is a
> probe, not a sized fix.

---

## 5a. Observed timing spread is descriptive, never an inference

Quantiles cannot express how much a check's duration actually *moved*. Twenty
observations of 100s, and nine observations of 1s plus eleven of 100s, have the
**same P50 and the same P95 (100s)** — one sample never varies, the other varies
by 99s. A reader given only the quantiles cannot tell those two repos apart, and
a "P95 − P50 band" reports zero for both.

So each measured pole also carries a **descriptive timing spread**: the count,
minimum, median and maximum of its own observations, re-read from the runs the
sampler already fetched. It costs **no additional GitHub requests and no deeper
sampling** — it is a second reading of the durations the pole's P50 was already
computed from.

**Selection must match the pole's own timing basis, exactly.** Same check
identity, same timing definition (the jobs-API `started_at` → `completed_at`
span, queue time excluded), same retained configuration era, same dominant-runner
scope, and the same one-observation-per-sampled-run/attempt policy. A blended
range would describe a job that never ran. Existing population and mode splits
are **preserved and named**, not collapsed: two runner populations stay two
populations, and a bimodal check's fast and slow modes are reported as two modes
beside the whole-sample range. A rerun attempt is one observation of the check;
it is never counted as an independent pull request.

**What it must never become.** The spread describes the sample. It is not:

- a symmetric ± band around the median;
- a "minimum detectable effect" or an "outside noise / within noise" verdict;
- any claim of statistical significance.

Duration spread is **not** uncertainty in an estimated change. Detectability
depends on sample size, pairing, comparable populations and a comparison method,
none of which one observed sample supplies. The spread never enters the
Bottom-line savings number or any sizing figure; it is context beside the pole,
rendered identically in the pole section and that pole's agent prompt.

**Honesty rules for degenerate samples.**

| Sample | What the report says |
| --- | --- |
| No comparable observations | **unavailable**, with the reason — never `0s`, never an empty range |
| Exactly one | "one observed run" — a single observation, explicitly *not* a spread |
| All identical | "constant in this sample", with the explicit note that this is **not** proof future runs do not vary |
| A different population backs the pole's aggregate | **unavailable** — never attach another population's durations |

An artifact produced before this summary existed simply renders nothing for it.
A missing summary is never rendered as a value.

---

## 6. Reliability is a wall-clock multiplier

A hard failure is not just a reliability stat — it is **a full wall-clock wait,
then a redo.** A first-pass failure rate `f` inflates the *effective* median
time-to-green by roughly `1/(1−f)` for the affected share.

> Worked example (illustrative): CI first-pass success was 66.2% (16.4%
> hard-fail). That ~1-in-6 doubled wait is comparable in magnitude to the
> entire Tier-1 wall-clock win — so
> **triaging the flaky failures is itself a top wall-clock lever**, not a
> separate "reliability" sidebar.

Surface the failure rate as a wall-clock finding (Tier 3), quantify the
doubled-wait, and treat flake reduction as part of the wall-clock program.
(See `savings-methodology.md` for the n ≥ 30 sample-size gate on any failure-rate
finding.)

---

## 7. Report structure

**The report opens with a `## Contents` TOC, and every entry is an anchor link.**
It MUST anchor-link the reversal table, every Tier (1/2/3), **every finding
(`W1…Wn` / `R1…Rn`) as a nested child**, "The stacked wall-clock model", and
"Appendix A". Keep finding headings free of `(REQUIRED …)` / `(NEW …)` clutter so
the GitHub auto-slug stays clean. The report's verification step confirms every
TOC anchor resolves.

### Findings table — ranked by Δ critical-path, two axes visible

| Rank | Finding | Δ long-pole (P50) | Δ tail (P95) | Realized | Δ runner-min | Risk |
| --- | --- | --- | --- | --- | --- | --- |
| **W1** | … | −53s | −300s+ | direct+tail | (secondary) | LOW |
| … | | | | | | |

### Tiers (sort key = Δ wall-clock)

- **Tier 1 — Direct wall-clock levers (W1…Wn).** Findings that lower the long
  pole. Ordered by Δ critical-path. Sharding belongs here even though it
  *raises* runner-minutes — it parallelizes the long pole. Warm-cache,
  browser-binary cache, docker layer cache, in-test sleep removal, on-long-pole
  tool caches all live here.
- **Tier 2 — Runner-minute reductions (R1…Rn).** Measured findings with zero
  wall-clock and a `tier2_neutrality` certificate: e.g. superseded runs that burn
  compute after their signal is dead, or below-floor bill-only work once its
  margin is computed. They live in the first-class runner-minute section, after
  Pre-start wait and before Also noticed. Each states **why** it cannot slow a
  PR. Wall-clock-negative serial-gate/dedup placement is the D4 product call;
  until decided, keep it loud and outside neutral totals.
- **Tier 3 — Reliability, hardening, security.** Failure-rate (a wall-clock
  multiplier, §6), `timeout-minutes` (caps catastrophic-hang wall-clock),
  security pins, invalidated findings.

### Cross-metric reversal table ("What changed vs the {other-metric} view")

When the audit re-frames an existing runner-minute analysis (or whenever a
finding's rank flips between axes), include a short table that names the flips
explicitly, so a reader who knows the billing ranking isn't blindsided:

| Finding | {other metric} rank | This report (primary) | Why it flips |
| --- | --- | --- | --- |
| Build dedup | #1 (billing) | demoted — wall-clock NEGATIVE | serial gate adds ~70–90s |
| Sharding | rejected (no runner-min win) | promoted top lever | parallelizes the long pole |
| Browser-cache | mid-pack (mean) | #1 wall-clock lever | kills the P95 install stall |

### Stacked-model projection (in Projected Impact)

Wall-clock only moves when the **whole top of the cluster** comes down. Show a
job-by-job table of `today → after the stacked Tier-1 fixes`, then translate to
modeled wall-clock P50 and P95. Label outputs **modeled** (built from
measured step durations, not post-merge measurements) and give a sequencing plan
(ship the biggest isolated lever first, re-measure P50/P95 on 5 PRs after each).

### Monthly wall-clock savings total (REQUIRED headline — and NON-additive)

The report MUST headline a **total monthly wall-clock saving**,
expressed as **developer-minutes/mo of wait removed** (and, helpfully, as
hours/mo, since that's what a team feels).

**It is NOT the sum of the per-finding Δ critical-path values.** Per-finding
wall-clock deltas do not add: cutting the long pole only helps until it hits the
cluster floor (§2), so two findings on the same long pole partly overlap, and a
finding on a below-floor or stacked job contributes 0 until its siblings drop.
Summing W1…Wn wall-clock deltas **overstates** the total — sometimes wildly.

**Derive the total from the stacked model, not from the findings list:**

```
Δ wall-clock per run   =  current wall-clock (P50)  −  stacked-model wall-clock (P50)
monthly total          =  Δ wall-clock per run  ×  building-runs/mo  ÷  60
                          (developer-minutes of wait removed per month)
```

- Use **building-runs/mo** (runs that actually execute the cluster), not all
  runs — runs the entry-gate skips are already fast and gain nothing.
- Report **both a P50 line and a P95 line** — the tail saving per affected run
  is much larger, and for heavy-tailed pipelines the P95 relief is the headline
  benefit even if the P50 total looks modest.
- State the basis and a range (the stacked-model P50 is itself a range).

> Worked total (illustrative): stacked model P50 282s → ~150–170s ⇒
> ~112–132s/run (~122s midpoint) × ~1,653 building-runs/mo ÷ 60 ≈ **~3,400
> developer-min/mo (~57 hours/mo)** of wait removed, range ~3,100–3,950. P95
> 594s → ~330–370s ⇒ ~224–264s/run on the affected (tail) runs. Note the
> inversion vs. the billing budget: the rejected build-dedup would have *added*
> ~70–90s × ~1,653 ≈ **+2,000–2,500 developer-min/mo of wait** while saving
> ~1,950 runner-min/mo of bill — the two budgets move in opposite directions.

Place this total in the Executive Summary (headline) AND in Projected Impact
(with the stacked-model derivation). The verification step must confirm it was
derived from the stacked model and is NOT a sum of per-finding deltas.

### Long poles → root-cause fixes (the consumer-facing framing)

The merge gate is `max(concurrent checks)` on a **typical** PR — not on the
single slowest check overall. A check that's huge but **rare** (a label-gated
benchmark, a path-conditional job that ran on a minority of sampled PRs) must not
be crowned over a smaller check that gates most PRs. The spine is therefore ranked
**two-tier by PR-presence**: checks present on a majority of sampled PRs first (by
p50), rare ones demoted below and labelled *opt-in / rare* — done in `collect_runs`
(`_rank_spine_present_first`) so `critical_path_check`, the drilled poles, structural
routing, and the data-pass summary all agree (each check carries `present_on`;
`check_present_n_pr` is the denominator; a *required* check is exempt; inert on a
tiny sample). `pr_critical_path.populations` additionally carries the per-PR pole
for the bimodal/expected-value sizing. The report's **Long poles** section names the
gating checks, how often each is the actual pole, and the per-step breakdown of each
pole's job with the **dominant (root-cause) step** marked.

The long-poles section is the measured **diagnosis** only — ci-speedup does not
prescribe a fix. Each gating check is then a **Finding** below, carrying a
ready-to-paste agent prompt the user hands to their own coding agent (which sees
the repo, the real run logs, and the file's git history) to decide the safe
remedy. Findings are ranked by **impact per risk** — frequency-weighted Δ
wall-clock ÷ a risk divisor (LOW 1.0 / MEDIUM 1.5 / HIGH 2.5) — so a safe lever
outranks an equal-impact risky one, but a far bigger risky lever still leads
(risk is a divisor, not a hard demotion). Effort is deliberately not a factor:
it's a guess about a fix the report no longer prescribes. A 0-wall-clock finding
removes no developer wait and sinks beneath every real lever.

The per-finding **Δ wall-clock** remains the floor-capped saving from the stacked
model above; the headline monthly total is that stacked model, NOT a sum of
per-finding deltas.

### Appendix A — residual runner-minute view

The residual runner-minute appendix holds modeled, uncertified, advisory, or
same-pattern residual findings that are not promoted into Runner-minute
reductions. State the budget inversion plainly for wall-clock-negative rows:
e.g. build-dedup spends developer-minutes of *wait* to save runner-minutes of
*bill* — the two budgets move in opposite directions.
