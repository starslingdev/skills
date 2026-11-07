# example/project - why is the merge slow?

**Where your PRs' time goes**

not measured: lifecycle data was not collected

| Repository | `example/project` |
| :--- | :--- |
| **Audited commit** | [`abcdef1`](https://github.com/example/project/commit/abcdef1234567890) - file & line references are anchored to this tree |
| **Runs window** | 2026-10-03 → 2026-11-02 (30-day window) |
| **PR gate sample** | 4 / 4 PRs |
| **Audit** | ran 2026-11-02 · ci-speedup skill commit [`abcdef1`](https://github.com/starslingdev/skills/commit/abcdef1234567890) |

> **Bottom line.** A typical PR waits **10m 00s** for all checks to finish. The biggest single measured win is **~4m 10s** off the slowest fixable check, `tests` - see [Long pole 1](#pole-1) for the drill-down to the biggest lever.
>
> **10m 00s until all checks finish** - `tests` is the slowest check a typical PR waits on.
>
> **What gates next:** After this fix, CI is gated by `quality.yml / lint` at ~350 s.
> This projection assumes the full headline saving is realized; a smaller fix may leave the current gate in place.
>
> **How to check it worked**
>
> - **Measure:** time from the push's first job created to the last job finished, across **all** workflows the push triggers.
> - **Runs:** at least 2 before and 2 after, plus one unchanged rerun as a noise control.
> - **Don't** sum workflow durations, and don't average per-job ratios. Both hide a gain on the one workflow that gates.
> - **Caches:** judge cache-touching fixes on the second run after the change.

## 📋 Contents

**🐌 Critical path** - the checks that gate your merge, each linking to its long-pole drill-down (waterfall → biggest lever → agent prompt):

1. 🔴 [tests](#pole-1) - 10m 00s (the gate)

**🧹 Also noticed** - 1 finding (mostly off-path runner-minute savings; **one or more flagged DO sit on the critical path** and cut wall-clock): [see below](#also-noticed).

<a id="long-pole-map"></a>

## 🗺️ Long pole map

A **workflow** is one YAML file under `.github/workflows/`; a run of it executes its **jobs** in parallel (each on its own runner); each job runs its **steps** in sequence.

```text
   tests · test.yml                   ██████████████████████  10m 00s       ◀
   lint · quality.yml                 █████████████            5m 50s
```

Inside the gate, its **steps** run in sequence - each **Long pole** finding below drills one check's steps down to the addressable lever.

<a id="pole-1"></a>

## 🔴 Long pole 1: `test.yml` ▸ `tests` - 10m 00s

**The slowest check a typical PR waits on.**

> **What a change here can buy (wall-clock):** up to **~4m 10s** - it gates until it drops to the next concurrent check, `lint` (5m 50s); below that the gate moves and further savings are runner-minutes, not wall-clock.

```text
Where the job's ~10m 00s goes - every step, slowest first; they run in sequence and roughly add up to the job:


(no log-level detector fired, but a measured **catalog pattern** matched this pole - see its entry in the **Also noticed** section below, flagged as sitting ON the critical path.)
```

#### 🤖 Prompt for your coding agent

```text
starslingdev/ci-speedup measured where the time goes below but does NOT prescribe the fix - a data-driven catalog pattern (OPT25) matched this pole (see the **Also noticed** section below for the measured lever + its fix recipe); the dominant step below is where that lever's time is spent.

REPO: example/project (audited at commit abcdef1)

THE GATE
- Workflow `test.yml`, job `tests`.
- Slowest check a typical PR waits on: P50 10m 00s.

WHERE THE TIME GOES
- No per-step breakdown was captured for this job; profile its slowest step in the repo.

DATA-DRIVEN CATALOG PATTERN MATCHED
- A data-driven catalog pattern (OPT25) matched this pole - see the **Also noticed** section below for the measured lever and its catalog fix recipe (it sits ON the critical path, so it shrinks this pole's wall time). The step above is the load-bearing one that lever targets; open its log in a recent pull-request run of this job (no direct log link is shown here) to see what inside it the lever reshapes.

WHAT'S ADDRESSABLE (wall-clock ceiling - don't over-promise)
- up to ~4m 10s - it gates until it drops to the next concurrent check, `lint` (5m 50s); below that the gate moves and further savings are runner-minutes, not wall-clock.

WHERE TO LOOK
- The `test.yml` workflow definition for the dominant step, and the tool/config it invokes (build tool, test runner, or install) - that's where its time is spent.

NEVER BUY SPEED BY CHECKING LESS
- The commit that merges must still be verified by exactly what verifies
  it today. Unless the finding above IS that reduction, do not: delete or
  narrow matrix legs so fewer configurations are tested; add
  `continue-on-error`, `|| true`, or any other exit-code suppression;
  narrow or remove a required status check, or change a job so a required
  check stops reporting; skip tests behind a path/branch filter that do
  cover the change; or cut test counts, timeouts, or retries in a way
  that weakens the signal rather than the cost.
- Not running a check against a change it cannot fail on is NOT checking
  less, and several catalog fixes work exactly that way: a `paths:` /
  `paths-ignore:` filter, a draft or job-level `if:`, changed-scope test
  selection. Those stay in bounds as long as the commit that merges is
  still verified by every check its own change could fail - follow the
  guardrail on the catalog recipe where this prompt links one, because
  that is the half it exists to protect.
- A pipeline that finishes sooner because it verifies less is a
  regression, not a win. If that is the only way to make this faster, say
  so and stop.

VERIFICATION BAR
- A scoped local run is pre-flight only. Call the change verified only after the repository's own CI gate passes on the pushed branch, or after exactly the commands that gate CI pass on the change.
- State the pole's observed range beside the predicted saving. Do not open a pull request when the predicted saving is smaller than that range.

DELIVER & VERIFY
- A change that cuts the dominant step's wall time without dropping coverage; re-measure the step on a PR run to confirm the reduction.
```


---

<a id="also-noticed"></a>

## 🧹 Also noticed - residual hygiene

> Most of these stay outside the wall-clock-neutral runner-minute section and do **not** sit on the merge-gating critical path above, so fixing them removes little or no developer wall-clock. **One or more exceptions are flagged inline** with a **Wall-clock** note: those DO sit on the critical path and their fix cuts developer wall-clock (shown first). **Expand any finding** for its locations, evidence, the catalog fix recipe, and a copy-paste agent prompt; exact per-occurrence lines + evidence also live in the findings JSON.

<details>
<summary><strong>OPT25 - Shard imbalance</strong> · ~4m 10s wall-clock · MEDIUM · 1 across 1 wf</summary>

**Where:** `test.yml` (tests)
**Wall-clock:** unlike the other findings in this section, this one **sits ON the merge-gating critical path** (a long pole) - its catalog fix **cuts developer wall-clock**, it is not a bill-only cleanup. See the spine above and this pattern's catalog recipe below for the remedy.
**Evidence:** One shard gates the matrix.
**Catalog (background + fix recipe):** https://github.com/starslingdev/skills/blob/abcdef1234567890/skills/ci-speedup/references/optimization-patterns.md

#### 🤖 Prompt for your coding agent

```text
ci-speedup measured the pattern below but does NOT prescribe the fix -
investigate it in the repo and apply a safe change.

Pattern: OPT25 - Shard imbalance.
Where: test.yml (tests).
What ci-speedup saw: One shard gates the matrix.
Concurrency order: First use OPT88 to measure and fill the existing job's idle cores; only then add shards or jobs. Before adding workers, confirm that they cannot interfere through a shared external server.
Runner-minute effect: With safe in-job concurrency, runner minutes go down because the same job finishes sooner. Extra shard jobs repeat checkout and setup and bill each job; N shards also add roughly (N-1) × the measured pre-test floor in runner minutes. Measure compile and setup before adding jobs.
Saving: developer WALL-CLOCK (~4m 10s) - this job is a long pole ON the merge-gating critical path, so its catalog fix shortens the merge wait. NOT a runner-bill cut, and NOT off the critical path.

Read the catalog entry (background, fix recipe, and guardrail):
  https://github.com/starslingdev/skills/blob/abcdef1234567890/skills/ci-speedup/references/optimization-patterns.md

NEVER BUY SPEED BY CHECKING LESS
- The commit that merges must still be verified by exactly what verifies
  it today. Unless the finding above IS that reduction, do not: delete or
  narrow matrix legs so fewer configurations are tested; add
  `continue-on-error`, `|| true`, or any other exit-code suppression;
  narrow or remove a required status check, or change a job so a required
  check stops reporting; skip tests behind a path/branch filter that do
  cover the change; or cut test counts, timeouts, or retries in a way
  that weakens the signal rather than the cost.
- Not running a check against a change it cannot fail on is NOT checking
  less, and several catalog fixes work exactly that way: a `paths:` /
  `paths-ignore:` filter, a draft or job-level `if:`, changed-scope test
  selection. Those stay in bounds as long as the commit that merges is
  still verified by every check its own change could fail - follow the
  guardrail on the catalog recipe where this prompt links one, because
  that is the half it exists to protect.
- A pipeline that finishes sooner because it verifies less is a
  regression, not a win. If that is the only way to make this faster, say
  so and stop.

VERIFICATION BAR
- A scoped local run is pre-flight only. Call the change verified only after the repository's own CI gate passes on the pushed branch, or after exactly the commands that gate CI pass on the change.
- State the pole's observed range beside the predicted saving. Do not open a pull request when the predicted saving is smaller than that range.

Do: confirm the pattern at each location above, recover the intent from git
history, and apply the catalog's fix recipe where it is safe. State the
failure mode and how you have guarded it before shipping.
```

</details>

## 🗄️ Data sources

> **Where this data comes from**
>
> - **Critical path + step P50:** the committed ci-speedup audit of `example/project`, scanned **2026-11-02** - P50 over **None runs / None jobs** across None workflows (latest runs at scan time).
> - **Which checks gate (the critical-path ordering):** measured from **4/4 sampled PRs**.
> - **Step internals + cross-run checks (the per-pole drill-downs):** the pole jobs' raw logs, fetched **2026-11-02T00:00:00Z** (the same capture as the critical-path audit above). Each drill-down is **one representative run** of that job - the one closest to its typical time (for a bimodal job, a representative of the slow mode the drill explains), linked + labelled per pole - and the **Cross-run check** validates the load-bearing magnitude (median + range) across several runs.

| Source | Coverage | Used for |
| --- | --- | --- |
| ci-speedup static scan (skill commit `NORMALIZED`) | All `.github/workflows/*.yml` under the analyzed tree (abcdef1) | Static pattern detection (full OPT catalog) |
| gh runs/jobs API (timestamps) | not run | Pass `--repo owner/repo` (with `gh`) to sample run/job timing |
| job logs | not run | Sampled only for a slow pole worth log-level inspection |

**Data freshness.** Analyzer ran at `2026-11-02T00:00:00Z`; workflow YAML is read from the analyzed tree at commit `abcdef1`. Timing and activity counts reflect the sampled runs over a rolling 30-day window at scan time.

_The concurrent checks (the Contents critical path) are P50 across sampled PRs. The per-step timeline + the drill are **one representative run** - the one closest to the P50 time - so they are absolute for that run, not P50. The **categorical cause** is stable across runs; where a **Cross-run check** is shown it gives the magnitude's median + range across several runs, so the single run's number isn't taken on faith. Per-step bars are scaled within each drill._

_The drill bars are plain-English labels for what's in the job log (e.g. a `DB migrations` bar is logged as `Total Migration Time:`). To verify any number, follow the pole's **🔗 Audit** link to the gating step, expand it, and search (Ctrl-F) for the verbatim strings the Audit line lists - GitHub anchors to the step, not an exact log line._

---

Generated by [StarSling](https://starsling.dev) 💫