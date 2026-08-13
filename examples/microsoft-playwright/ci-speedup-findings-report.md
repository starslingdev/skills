# microsoft/playwright - why is the merge slow?

| Repository | `microsoft/playwright` |
| :--- | :--- |
| **Audited commit** | [`3827650`](https://github.com/microsoft/playwright/commit/3827650d171cc1b035cbefb7e00bf5948d6809df) - file & line references are anchored to this tree |
| **Runs analyzed** | 161 runs / 1630 jobs across 18 workflows |
| **Runs window** | 2026-06-25 → 2026-07-25 (30-day window) |
| **PR gate sample** | 20 / 20 PRs |
| **Audit** | ran 2026-07-25 · ci-speedup skill commit [`2f048be`](https://github.com/starslingdev/skills/commit/2f048be) |

> **Bottom line.** A typical PR waits **41m 12s** for all checks to finish. The biggest single measured win is **~1m 06s** off the slowest fixable check, `ubuntu-22.04 (webkit - Node.js 2…` - see [Long pole 1](#pole-1) for the drill-down to the biggest lever.
>
> **41m 12s until all checks finish** - the slowest check a typical PR waits on is `Windows (firefox)` (~72m 57s), but it ran on only 4/20 sampled PRs, so a typical PR finishes in 41m 12s; `ubuntu-22.04 (webkit - Node.js 20)` is the check most PRs gate on (drilled below). (`Test chrome on macos-latest` is slower (~59m 33s) but it ran on only 5/20 sampled PRs - it looks opt-in / conditional (e.g. label-gated), so a typical PR doesn't wait on it and its time is throughput/cost, not merge-wait; unless it's a *required* status check it isn't the gate here. See its long pole below.) 
>
> **⚠️ `.github/workflows/tests_webview_simulator.yml` changed ~36 days ago - this audit measures ONLY the new configuration on a thin sample.** Only 1 sampled run have run on the new configuration (the gate-bearing PRs are all post-change, so there is no pre-change gate run to measure the old config) - treat these numbers as provisional; re-run as post-change history accumulates for stable numbers.
>
> **`.github/workflows/fix-flakes.yml` changed ~26 days ago - narrowed to the current configuration.** This audit measures only the 11 runs since that change; the 8 earlier runs measured the retired configuration and were excluded so no drill-down blends the two.
>
> **`.github/workflows/publish_release.yml` changed ~28 days ago - narrowed to the current configuration.** This audit measures only the 14 runs since that change; the 6 earlier runs measured the retired configuration and were excluded so no drill-down blends the two.
>
> **`.github/workflows/tests_bidi.yml` changed ~33 days ago - narrowed to the current configuration.** This audit measures only the 8 runs since that change; the 12 earlier runs measured the retired configuration and were excluded so no drill-down blends the two.
>
> **`.github/workflows/tests_secondary.yml` changed ~40 days ago - narrowed to the current configuration.** This audit measures only the 18 runs since that change; the 2 earlier runs measured the retired configuration and were excluded so no drill-down blends the two.
>
> **After the gate.** 73,441 min/mo of wall-clock-neutral runner minutes is recoverable (10 neutral findings; none can slow a merge).

## 📋 Contents

**🐌 Critical path** - the checks that gate your merge, each linking to its long-pole drill-down (waterfall → biggest lever → agent prompt):

1. 🔴 [ubuntu-22.04 (webkit - Node.js 2…](#pole-1) - 41m 21s · `tests_primary.yml` gates 7/20 PRs
2. 🔴 [Windows (firefox)](#pole-2) - 72m 57s · `tests_secondary.yml` gates 5/20 PRs
3. 🔴 [windows-latest - firefox](#pole-3) - 40m 15s · `tests_mcp.yml` gates 6/20 PRs
4. 🔴 [Test chrome on macos-latest](#pole-4) - 59m 33s · rarely the merge pole
5. 🔴 [Test msedge on windows-latest](#pole-5) - 46m 01s · rarely the merge pole

**⏳ Pre-start wait** - 41 jobs wait in queue before starting (developer wall-clock the spine above doesn't capture): [see below](#pre-start-wait).

**💸 Runner-minute reductions** - ~73,441 min/mo of measured, merge-safe runner-minute savings, backed by a 133-row cost spine: [section](#runner-minute-reductions).

1. 🟢 [Superseded Runs Not Cancelled](#r-1) - 71,644 min/mo
2. 🟢 [Superseded Runs Not Cancelled](#r-2) - 1,072 min/mo
3. 🟢 [Repeated Workflow Attempts From Same…](#r-3) - 268 min/mo
4. 🟢 [Superseded Runs Not Cancelled](#r-4) - 159 min/mo
5. 🟢 [Superseded Runs Not Cancelled](#r-5) - 141 min/mo
6. 🟢 [Repeated Workflow Attempts From Same…](#r-6) - 82 min/mo
7. 🟢 [Repeated Workflow Attempts From Same…](#r-7) - 54 min/mo
8. 🟢 [Cron Schedule Too Frequent](#r-8) - 13 min/mo
9. 🟢 [Repeated Workflow Attempts From Same…](#r-9) - 8 min/mo
10. 🟢 [Repeated Workflow Attempts From Same…](#r-10) - 0.7 min/mo

**🧹 Also noticed** - 16 findings (mostly off-path runner-minute savings; **one or more flagged DO sit on the critical path** and cut wall-clock): [see below](#also-noticed).

<a id="long-pole-map"></a>

## 🗺️ Long pole map

A **workflow** is one YAML file under `.github/workflows/`; a run of it executes its **jobs** in parallel (each on its own runner); each job runs its **steps** in sequence.

```text
Level 1 - checks racing on a typical PR; the merge waits for the slowest - rows marked † ran on a minority of sampled PRs (path-conditional - they gate only the PRs that trigger them):

   Windows (firefox) · tests_seco… †  ██████████████████████  72m 57s
   ubuntu-22.04 (webkit - Node.js 2…  ████████████            41m 21s       ◀┐
   windows-latest - firefox · tests…  ████████████            40m 15s        │
   ┌─────────────────────────────────────────────────────────────────────────┘

   ▼ Level 2 - inside ubuntu-22.04 (webkit - Node.js 20), steps run one after another:

   Run ./.github/actions/run-test     ██████████████████████  41m 14s  100% ◀
   Run actions/checkout@v6            █                            2s    0%
   Set up job                         █                            1s    0%
   Complete job                       █                            1s    0%
   Post Run actions/checkout@v6       █                            1s    0%
   Post Run ./.github/actions/run-t…  █                            1s    0%
```

Each ◀ marks the blocker the next level opens. Long pole 1 below drills the marked step to its root cause and hand-off prompt.

† `Windows (firefox)` ran on 4/20 sampled PRs.

> Also slower on **some** of the sampled PRs (not the typical path, not in the Contents critical path): opt-in / conditional workflow check(s) that ran on a minority of sampled PRs (label-gated or path-filtered - a typical PR doesn't wait on them): `Test chrome on macos-latest` (~59m 33s), `Test msedge on windows-latest` (~46m 01s), `Test msedge-dev on windows-latest` (~44m 28s). Unless one is a *required* status check it does not gate the merge - treat it as throughput/cost (an opt-in job) or make an external check non-blocking, rather than the long pole.

<a id="pole-1"></a>

## 🔴 Long pole 1: `tests_primary.yml` ▸ `ubuntu-22.04 (webkit - Node.js 20)` - 41m 21s

**The check most PRs gate on.** A typical PR waits on this most often; the slowest concurrent check is `Windows (firefox)` (~72m 57s).

> **What a change here can buy (wall-clock):** this job's matrix legs run in parallel, so speeding **this one leg** saves only ~1m 06s (the next leg, `windows-latest - firefox`, is 40m 15s). Because the legs share one job config, a change that speeds *every* leg at once drops the whole matrix toward the next check, `windows-latest - firefox` (40m 15s), for up to **~1m 06s** of merge wait.

```text
Where the job's ~41m 21s goes - every step, slowest first; they run in sequence and roughly add up to the job:

   Run ./.github/actions/run-test     ██████████████████████  41m 14s       ◀
   Run actions/checkout@v6            █                            2s
   Set up job                         █                            1s
   Complete job                       █                            1s
   Post Run actions/checkout@v6       █                            1s
   Post Run ./.github/actions/run-t…  █                            1s

(no log-level detector fired, but a **structural catalog pattern** matched this pole - see the **structural root-cause** below; the dominant step is the addressable lever.)
```

**📐 Structural root-cause - OPT75 · The long pole's time is one addressable step - speed it up or move it off the PR path - `ubuntu-22.04 (webkit - Node.js 20)`** - risk **MEDIUM**

A measured **structural** lever on the critical path (it IS this pole, so it's not repeated in the off-path appendix). It carries a risk profile - review the guardrail and rollout before shipping:

- **What ci-speedup measured:** critical-path check `ubuntu-22.04 (webkit - Node.js 20)` (2481s): dominant step `Run ./.github/actions/run-test` (test, 100% of job `ubuntu-22.04 (webkit - Node.js 20)`)
- **Guardrail:** carry the guardrail of the routed lever (e.g. OPT70's full-suite fallback if the dominant step is a test being scoped); never present the decomposition as free
- **Rollout:** the routed lever's rollout; re-measure the pole's p50 after the dominant step is attacked - the next-largest step becomes the target
- **Failure mode:** the dominant-step remedy ranges from LOW (cache an install) to HIGH (scope a test/build, inheriting OPT70) - the candidate carries the risk of whichever specific lever its dominant category routes to
- **Catalog (background + fix recipe):** https://github.com/starslingdev/skills/blob/2f048be/skills/ci-speedup/references/optimization-patterns.md#opt75--long-pole-optimize-or-relocate-the-dominant-step

#### 🤖 Prompt for your coding agent

```text
starslingdev/ci-speedup measured where the time goes below but does NOT prescribe the fix - a structural catalog pattern (OPT75) matched this pole (see the **structural root-cause** section above for the measured lever + its risk axis); the dominant step below is where that lever's time is spent.

REPO: microsoft/playwright (audited at commit 3827650)

THE GATE
- Workflow `tests_primary.yml`, job `ubuntu-22.04 (webkit - Node.js 20)`.
- Slowest check a typical PR waits on: P50 41m 21s; its workflow `tests_primary.yml` gates 7/20 sampled PRs.

WHERE THE TIME GOES
- The job's time is dominated by the `Run ./.github/actions/run-test` step: ~41m 14s (100% of the job wall), from the sampled per-step decomposition (no single-run timeline was captured for this job).

STRUCTURAL CATALOG PATTERN MATCHED
- A structural catalog pattern (OPT75) matched this pole - see the **structural root-cause** section above for the measured lever, its risk / guardrail / rollout, and the catalog fix recipe. The step above is the load-bearing one that lever targets; open its log (the Audit link) to see exactly what inside it the lever reshapes.

WHAT'S ADDRESSABLE (wall-clock ceiling - don't over-promise)
- this job's matrix legs run in parallel, so speeding this one leg saves only ~1m 06s (the next leg, `windows-latest - firefox`, is 40m 15s). Because the legs share one job config, a change that speeds *every* leg at once drops the whole matrix toward the next check, `windows-latest - firefox` (40m 15s), for up to ~1m 06s of merge wait.

WHERE TO LOOK
- The `tests_primary.yml` workflow definition for the dominant step, and the tool/config it invokes (build tool, test runner, or install) - that's where its time is spent.

DELIVER & VERIFY
- A change that cuts the dominant step's wall time without dropping coverage; re-measure the step on a PR run to confirm the reduction.
```


<a id="pole-2"></a>

## 🔴 Long pole 2: `tests_secondary.yml` ▸ `Windows (firefox)` - 72m 57s

_Runs concurrently - becomes the gate once the slower checks above it drop below 72m 57s._

> **What a change here can buy (wall-clock):** this job's matrix legs run in parallel, so speeding **this one leg** saves only ~13m 24s (the next leg, `Test chrome on macos-latest`, is 59m 33s). Because the legs share one job config, a change that speeds *every* leg at once drops the whole matrix toward the next check, `Test chrome on macos-latest` (59m 33s), for up to **~13m 24s** of merge wait.

```text
Where the job's ~72m 57s goes - every step, slowest first; they run in sequence and roughly add up to the job:

   Run ./.github/actions/run-test     ██████████████████████  72m 46s       ◀
   Run actions/checkout@v6            █                            6s
   Post Run actions/checkout@v6       █                            2s
   Set up job                         █                            1s

(no log-level detector fired, but a **structural catalog pattern** matched this pole - see the **structural root-cause** below; the dominant step is the addressable lever.)
```

**📐 Structural root-cause - OPT75 · The long pole's time is one addressable step - speed it up or move it off the PR path - `Windows (firefox)`** - risk **MEDIUM**

A measured **structural** lever on the critical path (it IS this pole, so it's not repeated in the off-path appendix). It carries a risk profile - review the guardrail and rollout before shipping:

- **What ci-speedup measured:** critical-path check `Windows (firefox)` (4377s): dominant step `Run ./.github/actions/run-test` (test, 100% of job `Windows (firefox)`)
- **Guardrail:** carry the guardrail of the routed lever (e.g. OPT70's full-suite fallback if the dominant step is a test being scoped); never present the decomposition as free
- **Rollout:** the routed lever's rollout; re-measure the pole's p50 after the dominant step is attacked - the next-largest step becomes the target
- **Failure mode:** the dominant-step remedy ranges from LOW (cache an install) to HIGH (scope a test/build, inheriting OPT70) - the candidate carries the risk of whichever specific lever its dominant category routes to
- **Catalog (background + fix recipe):** https://github.com/starslingdev/skills/blob/2f048be/skills/ci-speedup/references/optimization-patterns.md#opt75--long-pole-optimize-or-relocate-the-dominant-step

#### 🤖 Prompt for your coding agent

```text
starslingdev/ci-speedup measured where the time goes below but does NOT prescribe the fix - a structural catalog pattern (OPT75) matched this pole (see the **structural root-cause** section above for the measured lever + its risk axis); the dominant step below is where that lever's time is spent.

REPO: microsoft/playwright (audited at commit 3827650)

THE GATE
- Workflow `tests_secondary.yml`, job `Windows (firefox)`.
- Slowest check a typical PR waits on: P50 72m 57s; its workflow `tests_secondary.yml` gates 5/20 sampled PRs.

WHERE THE TIME GOES
- The job's time is dominated by the `Run ./.github/actions/run-test` step: ~72m 46s (100% of the job wall), from the sampled per-step decomposition (no single-run timeline was captured for this job).

STRUCTURAL CATALOG PATTERN MATCHED
- A structural catalog pattern (OPT75) matched this pole - see the **structural root-cause** section above for the measured lever, its risk / guardrail / rollout, and the catalog fix recipe. The step above is the load-bearing one that lever targets; open its log (the Audit link) to see exactly what inside it the lever reshapes.

WHAT'S ADDRESSABLE (wall-clock ceiling - don't over-promise)
- this job's matrix legs run in parallel, so speeding this one leg saves only ~13m 24s (the next leg, `Test chrome on macos-latest`, is 59m 33s). Because the legs share one job config, a change that speeds *every* leg at once drops the whole matrix toward the next check, `Test chrome on macos-latest` (59m 33s), for up to ~13m 24s of merge wait.

WHERE TO LOOK
- The `tests_secondary.yml` workflow definition for the dominant step, and the tool/config it invokes (build tool, test runner, or install) - that's where its time is spent.

DELIVER & VERIFY
- A change that cuts the dominant step's wall time without dropping coverage; re-measure the step on a PR run to confirm the reduction.
```


<a id="pole-3"></a>

## 🔴 Long pole 3: `tests_mcp.yml` ▸ `windows-latest - firefox` - 40m 15s

_Runs concurrently behind `Windows (firefox)` (72m 57s); it becomes the gate only once every slower concurrent check drops below 40m 15s._

> **What a change here can buy (wall-clock):** this job's matrix legs run in parallel, so speeding **this one leg** saves only ~8m 24s (the next leg, `macos-latest - firefox`, is 31m 51s). Because the legs share one job config, a change that speeds *every* leg at once drops the whole matrix toward the next check, `macos-latest - chromium` (20m 14s), for up to **~20m 00s** of merge wait.

```text
Where the job's ~40m 15s goes - every step, slowest first; they run in sequence and roughly add up to the job:

   Run ./.github/actions/run-test     ██████████████████████  40m 04s       ◀
   Run actions/checkout@v6            █                            6s
   Post Run actions/checkout@v6       █                            2s
   Set up job                         █                            1s

(no log-level detector fired, but a **structural catalog pattern** matched this pole - see the **structural root-cause** below; the dominant step is the addressable lever.)
```

**📐 Structural root-cause - OPT75 · The long pole's time is one addressable step - speed it up or move it off the PR path - `windows-latest - firefox`** - risk **MEDIUM**

A measured **structural** lever on the critical path (it IS this pole, so it's not repeated in the off-path appendix). It carries a risk profile - review the guardrail and rollout before shipping:

- **What ci-speedup measured:** critical-path check `windows-latest - firefox` (2415s): dominant step `Run ./.github/actions/run-test` (test, 100% of job `windows-latest - firefox`)
- **Guardrail:** carry the guardrail of the routed lever (e.g. OPT70's full-suite fallback if the dominant step is a test being scoped); never present the decomposition as free
- **Rollout:** the routed lever's rollout; re-measure the pole's p50 after the dominant step is attacked - the next-largest step becomes the target
- **Failure mode:** the dominant-step remedy ranges from LOW (cache an install) to HIGH (scope a test/build, inheriting OPT70) - the candidate carries the risk of whichever specific lever its dominant category routes to
- **Catalog (background + fix recipe):** https://github.com/starslingdev/skills/blob/2f048be/skills/ci-speedup/references/optimization-patterns.md#opt75--long-pole-optimize-or-relocate-the-dominant-step

#### 🤖 Prompt for your coding agent

```text
starslingdev/ci-speedup measured where the time goes below but does NOT prescribe the fix - a structural catalog pattern (OPT75) matched this pole (see the **structural root-cause** section above for the measured lever + its risk axis); the dominant step below is where that lever's time is spent.

REPO: microsoft/playwright (audited at commit 3827650)

THE GATE
- Workflow `tests_mcp.yml`, job `windows-latest - firefox`.
- Slowest check a typical PR waits on: P50 40m 15s; its workflow `tests_mcp.yml` gates 6/20 sampled PRs.

WHERE THE TIME GOES
- The job's time is dominated by the `Run ./.github/actions/run-test` step: ~40m 04s (100% of the job wall), from the sampled per-step decomposition (no single-run timeline was captured for this job).

STRUCTURAL CATALOG PATTERN MATCHED
- A structural catalog pattern (OPT75) matched this pole - see the **structural root-cause** section above for the measured lever, its risk / guardrail / rollout, and the catalog fix recipe. The step above is the load-bearing one that lever targets; open its log (the Audit link) to see exactly what inside it the lever reshapes.

WHAT'S ADDRESSABLE (wall-clock ceiling - don't over-promise)
- this job's matrix legs run in parallel, so speeding this one leg saves only ~8m 24s (the next leg, `macos-latest - firefox`, is 31m 51s). Because the legs share one job config, a change that speeds *every* leg at once drops the whole matrix toward the next check, `macos-latest - chromium` (20m 14s), for up to ~20m 00s of merge wait.

WHERE TO LOOK
- The `tests_mcp.yml` workflow definition for the dominant step, and the tool/config it invokes (build tool, test runner, or install) - that's where its time is spent.

DELIVER & VERIFY
- A change that cuts the dominant step's wall time without dropping coverage; re-measure the step on a PR run to confirm the reduction.
```


<a id="pole-4"></a>

## 🔴 Long pole 4: `tests_secondary.yml` ▸ `Test chrome on macos-latest` - 59m 33s

**Rarely the merge gate - the actual slowest check a PR waits on, on only 1/20 sampled PRs.** Present on 5/20 PRs, but a slower concurrent check almost always gates ahead of it, so its 59m 33s is throughput/cost, not merge-wait. Speeding it helps only the PRs where it IS the pole - it won't move typical merge-wait.

> **What a change here can buy (wall-clock):** this job's matrix legs run in parallel, so speeding **this one leg** saves only ~13m 32s (the next leg, `Test msedge on windows-latest`, is 46m 01s). Because the legs share one job config, a change that speeds *every* leg at once drops the whole matrix toward the next check, `Test msedge on windows-latest` (46m 01s), for up to **~13m 32s** of merge wait.

```text
Where the job's ~59m 33s goes - every step, slowest first; they run in sequence and roughly add up to the job:

   Run ./.github/actions/run-test     ██████████████████████  59m 21s       ◀
   Run actions/checkout@v6            █                            4s
   Complete job                       █                            4s
   Set up job                         █                            2s
   Post Run ./.github/actions/run-t…  █                            1s

(no log-level detector fired, but a **structural catalog pattern** matched this pole - see the **structural root-cause** below; the dominant step is the addressable lever.)
```

**📐 Structural root-cause - OPT75 · The long pole's time is one addressable step - speed it up or move it off the PR path - `Test chrome on macos-latest`** - risk **MEDIUM**

A measured **structural** lever on the critical path (it IS this pole, so it's not repeated in the off-path appendix). It carries a risk profile - review the guardrail and rollout before shipping:

- **What ci-speedup measured:** critical-path check `Test chrome on macos-latest` (3573s): dominant step `Run ./.github/actions/run-test` (test, 100% of job `Test chrome on macos-latest`)
- **Guardrail:** carry the guardrail of the routed lever (e.g. OPT70's full-suite fallback if the dominant step is a test being scoped); never present the decomposition as free
- **Rollout:** the routed lever's rollout; re-measure the pole's p50 after the dominant step is attacked - the next-largest step becomes the target
- **Failure mode:** the dominant-step remedy ranges from LOW (cache an install) to HIGH (scope a test/build, inheriting OPT70) - the candidate carries the risk of whichever specific lever its dominant category routes to
- **Catalog (background + fix recipe):** https://github.com/starslingdev/skills/blob/2f048be/skills/ci-speedup/references/optimization-patterns.md#opt75--long-pole-optimize-or-relocate-the-dominant-step

#### 🤖 Prompt for your coding agent

```text
starslingdev/ci-speedup measured where the time goes below but does NOT prescribe the fix - a structural catalog pattern (OPT75) matched this pole (see the **structural root-cause** section above for the measured lever + its risk axis); the dominant step below is where that lever's time is spent.

REPO: microsoft/playwright (audited at commit 3827650)

THE GATE
- Workflow `tests_secondary.yml`, job `Test chrome on macos-latest`.
- Rarely the merge pole - the actual slowest check a PR waits on, on only 1/20 sampled PRs (present on 5/20): P50 59m 33s. A slower concurrent check usually gates ahead, so speeding it helps only the PRs where it IS the pole, not typical merge-wait.

WHERE THE TIME GOES
- The job's time is dominated by the `Run ./.github/actions/run-test` step: ~59m 21s (100% of the job wall), from the sampled per-step decomposition (no single-run timeline was captured for this job).

STRUCTURAL CATALOG PATTERN MATCHED
- A structural catalog pattern (OPT75) matched this pole - see the **structural root-cause** section above for the measured lever, its risk / guardrail / rollout, and the catalog fix recipe. The step above is the load-bearing one that lever targets; open its log (the Audit link) to see exactly what inside it the lever reshapes.

WHAT'S ADDRESSABLE (wall-clock ceiling - don't over-promise)
- this job's matrix legs run in parallel, so speeding this one leg saves only ~13m 32s (the next leg, `Test msedge on windows-latest`, is 46m 01s). Because the legs share one job config, a change that speeds *every* leg at once drops the whole matrix toward the next check, `Test msedge on windows-latest` (46m 01s), for up to ~13m 32s of merge wait.

WHERE TO LOOK
- The `tests_secondary.yml` workflow definition for the dominant step, and the tool/config it invokes (build tool, test runner, or install) - that's where its time is spent.

DELIVER & VERIFY
- A change that cuts the dominant step's wall time without dropping coverage; re-measure the step on a PR run to confirm the reduction.
```


<a id="pole-5"></a>

## 🔴 Long pole 5: `tests_secondary.yml` ▸ `Test msedge on windows-latest` - 46m 01s

**Rarely the merge gate - the actual slowest check a PR waits on, on only 0/20 sampled PRs.** Present on 5/20 PRs, but a slower concurrent check almost always gates ahead of it, so its 46m 01s is throughput/cost, not merge-wait. Speeding it helps only the PRs where it IS the pole - it won't move typical merge-wait.

> **What a change here can buy (wall-clock):** this job's matrix legs run in parallel, so speeding **this one leg** saves only ~4m 40s (the next leg, `ubuntu-22.04 (webkit - Node.js 20)`, is 41m 21s). Because the legs share one job config, a change that speeds *every* leg at once drops the whole matrix toward the next check, `ubuntu-22.04 (webkit - Node.js 20)` (41m 21s), for up to **~4m 40s** of merge wait.

```text
Where the job's ~46m 01s goes - every step, slowest first; they run in sequence and roughly add up to the job:

   Run ./.github/actions/run-test     ██████████████████████  45m 48s       ◀
   Run actions/checkout@v6            █                            8s
   Post Run actions/checkout@v6       █                            2s
   Set up job                         █                            1s
   Complete job                       █                            1s

(no log-level detector fired, but a **structural catalog pattern** matched this pole - see the **structural root-cause** below; the dominant step is the addressable lever.)
```

**📐 Structural root-cause - OPT75 · The long pole's time is one addressable step - speed it up or move it off the PR path - `Test msedge on windows-latest`** - risk **MEDIUM**

A measured **structural** lever on the critical path (it IS this pole, so it's not repeated in the off-path appendix). It carries a risk profile - review the guardrail and rollout before shipping:

- **What ci-speedup measured:** critical-path check `Test msedge on windows-latest` (2761s): dominant step `Run ./.github/actions/run-test` (test, 100% of job `Test msedge on windows-latest`)
- **Guardrail:** carry the guardrail of the routed lever (e.g. OPT70's full-suite fallback if the dominant step is a test being scoped); never present the decomposition as free
- **Rollout:** the routed lever's rollout; re-measure the pole's p50 after the dominant step is attacked - the next-largest step becomes the target
- **Failure mode:** the dominant-step remedy ranges from LOW (cache an install) to HIGH (scope a test/build, inheriting OPT70) - the candidate carries the risk of whichever specific lever its dominant category routes to
- **Catalog (background + fix recipe):** https://github.com/starslingdev/skills/blob/2f048be/skills/ci-speedup/references/optimization-patterns.md#opt75--long-pole-optimize-or-relocate-the-dominant-step

#### 🤖 Prompt for your coding agent

```text
starslingdev/ci-speedup measured where the time goes below but does NOT prescribe the fix - a structural catalog pattern (OPT75) matched this pole (see the **structural root-cause** section above for the measured lever + its risk axis); the dominant step below is where that lever's time is spent.

REPO: microsoft/playwright (audited at commit 3827650)

THE GATE
- Workflow `tests_secondary.yml`, job `Test msedge on windows-latest`.
- Rarely the merge pole - the actual slowest check a PR waits on, on only 0/20 sampled PRs (present on 5/20): P50 46m 01s. A slower concurrent check usually gates ahead, so speeding it helps only the PRs where it IS the pole, not typical merge-wait.

WHERE THE TIME GOES
- The job's time is dominated by the `Run ./.github/actions/run-test` step: ~45m 48s (100% of the job wall), from the sampled per-step decomposition (no single-run timeline was captured for this job).

STRUCTURAL CATALOG PATTERN MATCHED
- A structural catalog pattern (OPT75) matched this pole - see the **structural root-cause** section above for the measured lever, its risk / guardrail / rollout, and the catalog fix recipe. The step above is the load-bearing one that lever targets; open its log (the Audit link) to see exactly what inside it the lever reshapes.

WHAT'S ADDRESSABLE (wall-clock ceiling - don't over-promise)
- this job's matrix legs run in parallel, so speeding this one leg saves only ~4m 40s (the next leg, `ubuntu-22.04 (webkit - Node.js 20)`, is 41m 21s). Because the legs share one job config, a change that speeds *every* leg at once drops the whole matrix toward the next check, `ubuntu-22.04 (webkit - Node.js 20)` (41m 21s), for up to ~4m 40s of merge wait.

WHERE TO LOOK
- The `tests_secondary.yml` workflow definition for the dominant step, and the tool/config it invokes (build tool, test runner, or install) - that's where its time is spent.

DELIVER & VERIFY
- A change that cuts the dominant step's wall time without dropping coverage; re-measure the step on a PR run to confirm the reduction.
```


---

<a id="pre-start-wait"></a>

## ⏳ Pre-start wait (queue time)

> Time a PR waits in queue **before its jobs start running** - developer wall-clock the critical-path spine above does **not** capture (the spine measures each job from start to finish). Usually runner-pool saturation or a restrictive concurrency group. Ranked by savable wait (the floor-capped addressable part; each finding's evidence quotes the raw P90 queue). **Note:** for a `needs:`-gated job this is *wait-to-start* - it includes the gating job's run time, so the savable part is bounded by the gating job's own fix.

> ⚠️ _Approximate: computed across all workflows, but 9 capped workflow(s) still use the shallow 10-run job sample for finding/queue values; 8 runner-minute source workflow(s) still use a shallow 10-run cost-spine sample. Figures can shift run-to-run; re-run with `--shallow-runs 20` to confirm exact values._

> _12 more queue findings have no addressable wait (queue overlaps the gate, or a job no one merge-waits on) - kept in the findings JSON, omitted here._

<details>
<summary><strong>OPT43 - Excessive Queue Time</strong> · worst savable wait 5m 48s · MEDIUM · 41 across 3 wf</summary>

**Where:** `tests_mcp.yml` (windows-latest - msedge), `tests_mcp.yml` (ubuntu-latest - firefox), `tests_mcp.yml` (macos-latest - chrome), `tests_mcp.yml` (macos-latest - chromium), `tests_mcp.yml` (ubuntu-latest - chromium), `tests_mcp.yml` (macos-latest - webkit), `tests_mcp.yml` (windows-latest - firefox), `tests_mcp.yml` (windows-latest - chromium), +33 more
**Evidence:** job `windows-latest - msedge` P90 queue 4396s over 8 runs (threshold 60s for PR workflows)
**Catalog (background + fix recipe):** https://github.com/starslingdev/skills/blob/2f048be/skills/ci-speedup/references/optimization-patterns.md#opt43--excessive-queue-time

#### 🤖 Prompt for your coding agent

```text
ci-speedup measured the pattern below but does NOT prescribe the fix -
investigate it in the repo and apply a safe change.

Pattern: OPT43 - Excessive Queue Time.
Where: tests_mcp.yml (windows-latest - msedge); tests_mcp.yml (ubuntu-latest - firefox); tests_mcp.yml (macos-latest - chrome); tests_mcp.yml (macos-latest - chromium); tests_mcp.yml (ubuntu-latest - chromium); tests_mcp.yml (macos-latest - webkit); tests_mcp.yml (windows-latest - firefox); tests_mcp.yml (windows-latest - chromium); +33 more (see findings JSON).
What ci-speedup saw: job `windows-latest - msedge` P90 queue 4396s over 8 runs (threshold 60s for PR workflows)
Cost: developer WALL-CLOCK wait before the job starts (queue / wait-to-start) - NOT a runner-bill saving and NOT off the critical path. For a `needs:`-gated job this span includes the gating job's own run time, so the savable part is bounded by the gating job's own fix.

Read the catalog entry (background, fix recipe, and guardrail):
  https://github.com/starslingdev/skills/blob/2f048be/skills/ci-speedup/references/optimization-patterns.md#opt43--excessive-queue-time

Do: confirm the pattern at each location above, recover the intent from git
history, and apply the catalog's fix recipe where it is safe. State the
failure mode and how you have guarded it before shipping.
```

</details>

---

<a id="runner-minute-reductions"></a>

## Runner-minute reductions (wall-clock-neutral)

<!-- ci-speedup:runner-minute-spine -->
### Cost spine: where runner minutes go

All figures are runner-minutes; multiply by your runner's per-minute rate to get dollars.

| Workflow | Job | Runner | Event | Status | Attempt | Volume | Raw min/mo | Billable min/mo | Share |
| --- | --- | --- | --- | --- | --- | --- | ---: | ---: | ---: |
| .github/workflows/tests_mcp.yml | windows-latest - firefox | windows-latest | all-events | success | latest | all-status | 34713.000 | 35112.000 | 3.500% |
| .github/workflows/tests_primary.yml | ubuntu-22.04 (webkit - Node.js 20) | ubuntu-22.04 | all-events | success | latest | all-status | 31765.092 | 32209.200 | 3.200% |
| .github/workflows/tests_primary.yml | ubuntu-22.04 (firefox - Node.js 20) | ubuntu-22.04 | all-events | success | latest | all-status | 27755.150 | 28124.700 | 2.800% |
| .github/workflows/tests_primary.yml | Test Runner (macos-latest, 22, 2, 2, 58:42) | macos-latest | all-events | success | latest | all-status | 23927.390 | 24273.600 | 2.400% |
| .github/workflows/tests_mcp.yml | macos-latest - firefox | macos-latest | all-events | success | latest | all-status | 23698.500 | 24066.000 | 2.400% |
| .github/workflows/tests_primary.yml | Installation Test windows-latest | windows-latest | all-events | success | latest | all-status | 22726.028 | 23106.600 | 2.300% |
| .github/workflows/tests_mcp.yml | windows-latest - msedge | windows-latest | all-events | success | latest | all-status | 22438.500 | 22932.000 | 2.300% |
| .github/workflows/tests_secondary.yml | Windows (firefox) | windows-latest | all-events | success | latest | all-status | 21731.113 | 21840.000 | 2.200% |
| .github/workflows/tests_mcp.yml | windows-latest - chrome | windows-latest | all-events | success | latest | all-status | 21126.700 | 21588.000 | 2.100% |
| .github/workflows/tests_primary.yml | Test Runner (macos-latest, 22, 1, 2, 58:42) | macos-latest | all-events | success | latest | all-status | 21244.587 | 21550.600 | 2.100% |
| .github/workflows/tests_mcp.yml | windows-latest - chromium | windows-latest | all-events | success | latest | all-status | 20340.600 | 20790.000 | 2.100% |
| .github/workflows/tests_mcp.yml | ubuntu-latest - firefox | ubuntu-latest | all-events | success | latest | all-status | 19838.000 | 20244.000 | 2.000% |
| Total |  |  |  |  |  |  | 987431.780 | 1013124.508 | 100.000% |
+121 more runner-minute rows hidden

> These findings cut wall-clock-neutral runner spend without touching your merge gate; each R-numbered finding carries a machine-derived proof it cannot slow a PR.
> **73,441 min/mo credited after de-overlap** (naive sum 73,441 min/mo; 10 neutral findings; not promoted: 3 measured item(s) (3 without source rows) · 8 modeled item(s) · 6 structural shared-step item(s); see Also noticed). All figures are runner-minutes; multiply by your runner's per-minute rate to get dollars.

<!-- ci-speedup:tier2-finding id=f114 pattern=OPT46 -->
<a id="r-1"></a>

## 🟢 Runner saving 1: `tests_secondary.yml` - 71,644 min/mo

**The largest merge-safe runner-minute saving measured on this repo.**

| Workflow | Overlapping (raced) runs | Mean compute/run (timed basis) | Reclaimable remainder (mean per run) | Reclaimable runner-min/mo (range) |
| --- | --- | --- | --- | --- |
| `.github/workflows/tests_secondary.yml` | 54 confirmed (naive 85) | 585.8 job-min over 4 timed run(s) | 67% of run (Σremainder/Σduration 68%) | ~71644-167291 (lower=remainder, upper=naive runs-1) |

_Superseded = a run a NEWER run started before it finished - measured by timestamp overlap, so sequential (non-racing) commits are NOT charged. Cancellation cause is unknowable from the API, so the attribution is INFERENCE. REMAINDER BASIS: cancel-in-progress cancels a superseded run the moment its successor starts, so only the compute AFTER that moment is reclaimable - the credited (lower) figure prices the MEAN per-run compute pro-rated by each superseded run's wall-clock remainder fraction (mean 67%; Σremainder/Σduration 68%), NOT the whole run; exact per-second compute is unknowable because a run's jobs run in parallel, so the pro-rata of the mean is the honest estimate. The whole-run figure is now only the loose UPPER bound (naive runs-1). Basis: the superseded COUNT and the remainder ratio are from the all-status slice (100 runs, from each run's own timestamps); the per-run PRICE is the mean of 4 PR-success timed runs (superseded runs' own jobs aren't fetched) - different populations. GUARDRAIL: verify this is NOT a deploy/release/publish workflow (a mid-flight run may be uploading artifacts / pushing a tag) before enabling cancellation - and take the predicate from the catalog recipe, which scopes cancellation with an expression; never a bare `cancel-in-progress: true`, which also kills in-flight runs on the default branch and on release tags. ROUTING: this workflow triggers on `pull_request`/`push` - with a `pull_request` trigger use the catalog's DEFAULT (PR-scoped) predicate; without one, the PR-scoped predicate is never true and saves nothing, so use the catalog's WIDENED predicate._

**💸 Bill root-cause - OPT46 · Superseded Runs Not Cancelled (Missing Concurrency or `cancel-in-progress: false`)** - risk **MEDIUM**

- **What ci-speedup measured:** 54 run(s) across 5 branch(es) were superseded (a newer run started before they finished) in the sampled window; ~71644-167291 runner-min/mo of cancellable-remainder compute - the lower figure credits only the 67% mean remainder each superseded run would have burned AFTER its successor started, not the whole run (mean over 4 timed run(s); ×3.36 to the 30d volume (336 runs); 100-run recent slice (not a full 30d census)). Superseded attribution is INFERENCE - the API marks no run 'cancelled-by-concurrency'. (sensitivity range: 71,644 min/mo to 167,291 min/mo)
- **Why this can't slow your merge:** machine-derived proof: `post_completion_waste` - compute burned after the run signal is already decided (superseded runs: same head_branch, a newer run started before this one finished (timestamp overlap); cancellation cause is inference).
- **Source block:** `runner_minute_spine` matched 29 rows for `.github/workflows/tests_secondary.yml`; current measured cost spine for those rows is 263103.875 raw min/mo, 267586.928 billable min/mo.
- **Catalog (background + fix recipe):** https://github.com/starslingdev/skills/blob/2f048be/skills/ci-speedup/references/optimization-patterns.md#opt46--superseded-runs-not-cancelled-missing-concurrency-or-cancel-in-progress-false

#### 🤖 Prompt for your coding agent

```text
ci-speedup measured the pattern below but does NOT prescribe the fix -
investigate it in the repo and apply a safe change.

Pattern: OPT46 - Superseded Runs Not Cancelled (Missing Concurrency or `cancel-in-progress: false`).
Where: tests_secondary.yml.
What ci-speedup saw: 54 run(s) across 5 branch(es) were superseded (a newer run started before they finished) in the sampled window; ~71644-167291 runner-min/mo of cancellable-remainder compute - the lower figure credits only the 67% mean remainder each superseded run would have burned AFTER its successor started, not the whole run (mean over 4 timed run(s); ×3.36 to the 30d volume (336 runs); 100-run recent slice (not a full 30d census)). Superseded attribution is INFERENCE - the API marks no run 'cancelled-by-concurrency'.
Saving: 71,644 min/mo of runner capacity - a bill/capacity reduction, not a merge-wait cut. Neutrality certificate: `post_completion_waste` - compute burned after the run signal is already decided (superseded runs: same head_branch, a newer run started before this one finished (timestamp overlap); cancellation cause is inference). GUARDRAIL: verify this is NOT a deploy/release/publish workflow (a mid-flight run may be uploading artifacts / pushing a tag) before enabling cancellation - and take the predicate from the catalog recipe, which scopes cancellation with an expression; never a bare `cancel-in-progress: true`, which also kills in-flight runs on the default branch and on release tags. ROUTING: this workflow triggers on `pull_request`/`push` - with a `pull_request` trigger use the catalog's DEFAULT (PR-scoped) predicate; without one, the PR-scoped predicate is never true and saves nothing, so use the catalog's WIDENED predicate.

Read the catalog entry (background, fix recipe, and guardrail):
  https://github.com/starslingdev/skills/blob/2f048be/skills/ci-speedup/references/optimization-patterns.md#opt46--superseded-runs-not-cancelled-missing-concurrency-or-cancel-in-progress-false

Do: confirm the pattern at each location above, recover the intent from git
history, and apply the catalog's fix recipe where it is safe. State the
failure mode and how you have guarded it before shipping.
```

<!-- ci-speedup:tier2-finding id=f48 pattern=OPT46 -->
<a id="r-2"></a>

## 🟢 Runner saving 2: `tests_components.yml` - 1,072 min/mo

**The #2 merge-safe runner-minute saving measured on this repo, by size.**

| Workflow | Overlapping (raced) runs | Mean compute/run (timed basis) | Reclaimable remainder (mean per run) | Reclaimable runner-min/mo (range) |
| --- | --- | --- | --- | --- |
| `.github/workflows/tests_components.yml` | 7 confirmed (naive 57) | 26.9 job-min over 4 timed run(s) | 73% of run (Σremainder/Σduration 69%) | ~1072-11922 (lower=remainder, upper=naive runs-1) |

_Superseded = a run a NEWER run started before it finished - measured by timestamp overlap, so sequential (non-racing) commits are NOT charged. Cancellation cause is unknowable from the API, so the attribution is INFERENCE. REMAINDER BASIS: cancel-in-progress cancels a superseded run the moment its successor starts, so only the compute AFTER that moment is reclaimable - the credited (lower) figure prices the MEAN per-run compute pro-rated by each superseded run's wall-clock remainder fraction (mean 73%; Σremainder/Σduration 69%), NOT the whole run; exact per-second compute is unknowable because a run's jobs run in parallel, so the pro-rata of the mean is the honest estimate. The whole-run figure is now only the loose UPPER bound (naive runs-1). Basis: the superseded COUNT and the remainder ratio are from the all-status slice (100 runs, from each run's own timestamps); the per-run PRICE is the mean of 4 PR-success timed runs (superseded runs' own jobs aren't fetched) - different populations. GUARDRAIL: verify this is NOT a deploy/release/publish workflow (a mid-flight run may be uploading artifacts / pushing a tag) before enabling cancellation - and take the predicate from the catalog recipe, which scopes cancellation with an expression; never a bare `cancel-in-progress: true`, which also kills in-flight runs on the default branch and on release tags. ROUTING: this workflow triggers on `pull_request`/`push` - with a `pull_request` trigger use the catalog's DEFAULT (PR-scoped) predicate; without one, the PR-scoped predicate is never true and saves nothing, so use the catalog's WIDENED predicate._

**💸 Bill root-cause - OPT46 · Superseded Runs Not Cancelled (Missing Concurrency or `cancel-in-progress: false`)** - risk **MEDIUM**

- **What ci-speedup measured:** 7 run(s) across 3 branch(es) were superseded (a newer run started before they finished) in the sampled window; ~1072-11922 runner-min/mo of cancellable-remainder compute - the lower figure credits only the 73% mean remainder each superseded run would have burned AFTER its successor started, not the whole run (mean over 4 timed run(s); ×7.78 to the 30d volume (778 runs); 100-run recent slice (not a full 30d census)). Superseded attribution is INFERENCE - the API marks no run 'cancelled-by-concurrency'. (sensitivity range: 1,072 min/mo to 11,922 min/mo)
- **Why this can't slow your merge:** machine-derived proof: `post_completion_waste` - compute burned after the run signal is already decided (superseded runs: same head_branch, a newer run started before this one finished (timestamp overlap); cancellation cause is inference).
- **Source block:** `runner_minute_spine` matched 5 rows for `.github/workflows/tests_components.yml`; current measured cost spine for those rows is 21636.828 raw min/mo, 23534.500 billable min/mo.
- **Catalog (background + fix recipe):** https://github.com/starslingdev/skills/blob/2f048be/skills/ci-speedup/references/optimization-patterns.md#opt46--superseded-runs-not-cancelled-missing-concurrency-or-cancel-in-progress-false

#### 🤖 Prompt for your coding agent

```text
ci-speedup measured the pattern below but does NOT prescribe the fix -
investigate it in the repo and apply a safe change.

Pattern: OPT46 - Superseded Runs Not Cancelled (Missing Concurrency or `cancel-in-progress: false`).
Where: tests_components.yml.
What ci-speedup saw: 7 run(s) across 3 branch(es) were superseded (a newer run started before they finished) in the sampled window; ~1072-11922 runner-min/mo of cancellable-remainder compute - the lower figure credits only the 73% mean remainder each superseded run would have burned AFTER its successor started, not the whole run (mean over 4 timed run(s); ×7.78 to the 30d volume (778 runs); 100-run recent slice (not a full 30d census)). Superseded attribution is INFERENCE - the API marks no run 'cancelled-by-concurrency'.
Saving: 1,072 min/mo of runner capacity - a bill/capacity reduction, not a merge-wait cut. Neutrality certificate: `post_completion_waste` - compute burned after the run signal is already decided (superseded runs: same head_branch, a newer run started before this one finished (timestamp overlap); cancellation cause is inference). GUARDRAIL: verify this is NOT a deploy/release/publish workflow (a mid-flight run may be uploading artifacts / pushing a tag) before enabling cancellation - and take the predicate from the catalog recipe, which scopes cancellation with an expression; never a bare `cancel-in-progress: true`, which also kills in-flight runs on the default branch and on release tags. ROUTING: this workflow triggers on `pull_request`/`push` - with a `pull_request` trigger use the catalog's DEFAULT (PR-scoped) predicate; without one, the PR-scoped predicate is never true and saves nothing, so use the catalog's WIDENED predicate.

Read the catalog entry (background, fix recipe, and guardrail):
  https://github.com/starslingdev/skills/blob/2f048be/skills/ci-speedup/references/optimization-patterns.md#opt46--superseded-runs-not-cancelled-missing-concurrency-or-cancel-in-progress-false

Do: confirm the pattern at each location above, recover the intent from git
history, and apply the catalog's fix recipe where it is safe. State the
failure mode and how you have guarded it before shipping.
```

<!-- ci-speedup:tier2-finding id=f115 pattern=OPT64 -->
<a id="r-3"></a>

## 🟢 Runner saving 3: `tests_webview_simulator.yml` - 268 min/mo

**The #3 merge-safe runner-minute saving measured on this repo, by size.**

| Run | Latest attempt | Dominant failing job | Prior attempts | Prior attempt compute min | Dominant failure min |
| --- | --- | --- | --- | --- | --- |
| `28560703032` | 2 | `WebView on iOS Simulator (4/4)` | 1 | 65.7 | 21.8 |
| `28555153022` | 3 | `WebView on iOS Simulator (4/4)` | 2 | 155.0 | 50.0 |
| `28470726784` | 2 | `WebView on iOS Simulator (4/4)` | 1 | 69.1 | 27.2 |
| `28412462737` | 4 | `WebView on iOS Simulator (4/4)` | 3 | 203.8 | 65.0 |
| `27986856884` | 2 | `WebView on iOS Simulator (4/4)` | 1 | 62.1 | 26.4 |
| `27973339667` | 2 | `WebView on iOS Simulator (4/4)` | 1 | 66.5 | 30.5 |

_Measured from GitHub jobs API `filter=all` minus `filter=latest` for workflow runs whose run_attempt > 1. The finding is emitted only when each credited prior attempt has the same unique dominant failed/timed-out job and that dominant failing job appears in the latest attempt; ambiguous ties, mixed-cause attempts, and retry-only volume are withheld._

**💸 Bill root-cause - OPT64 · Repeated Workflow Attempts From Same Failing Job** - risk **LOW**

- **What ci-speedup measured:** 6 sampled run_attempt>1 workflow run(s) had prior-attempt jobs present in `filter=all` but absent from `filter=latest`; the unique dominant failing job was `WebView on iOS Simulator (4/4)` (9 failed/timed-out prior-attempt job(s), 221.0 failed min) and it appeared again in the latest attempt. ~268 runner-min/mo of prior-attempt compute (43/30d ÷ 100 sampled all-status run(s)).
- **Why this can't slow your merge:** machine-derived proof: `post_completion_waste` - compute burned after the run signal is already decided (run_attempt>1: `filter=all` exposes prior-attempt jobs, `filter=latest` is the superseding latest attempt; the dominant failing job `WebView on iOS Simulator (4/4)` identifies the retry cause, so prior-attempt compute is post-completion waste).
- **Source block:** `runner_minute_spine` matched 4 prior-attempt rows for `.github/workflows/tests_webview_simulator.yml`; current measured cost spine for those rows is 403.203 raw min/mo, 415.807 billable min/mo.
- **Catalog (background + fix recipe):** https://github.com/starslingdev/skills/blob/2f048be/skills/ci-speedup/references/optimization-patterns.md#opt64--repeated-workflow-attempts-from-same-failing-job

#### 🤖 Prompt for your coding agent

```text
ci-speedup measured the pattern below but does NOT prescribe the fix -
investigate it in the repo and apply a safe change.

Pattern: OPT64 - Repeated Workflow Attempts From Same Failing Job.
Where: tests_webview_simulator.yml.
What ci-speedup saw: 6 sampled run_attempt>1 workflow run(s) had prior-attempt jobs present in `filter=all` but absent from `filter=latest`; the unique dominant failing job was `WebView on iOS Simulator (4/4)` (9 failed/timed-out prior-attempt job(s), 221.0 failed min) and it appeared again in the latest attempt. ~268 runner-min/mo of prior-attempt compute (43/30d ÷ 100 sampled all-status run(s)).
Saving: 268 min/mo of runner capacity - a bill/capacity reduction, not a merge-wait cut. Neutrality certificate: `post_completion_waste` - compute burned after the run signal is already decided (run_attempt>1: `filter=all` exposes prior-attempt jobs, `filter=latest` is the superseding latest attempt; the dominant failing job `WebView on iOS Simulator (4/4)` identifies the retry cause, so prior-attempt compute is post-completion waste).

Read the catalog entry (background, fix recipe, and guardrail):
  https://github.com/starslingdev/skills/blob/2f048be/skills/ci-speedup/references/optimization-patterns.md#opt64--repeated-workflow-attempts-from-same-failing-job

Do: confirm the pattern at each location above, recover the intent from git
history, and apply the catalog's fix recipe where it is safe. State the
failure mode and how you have guarded it before shipping.
```

<!-- ci-speedup:tier2-finding id=f42 pattern=OPT46 -->
<a id="r-4"></a>

## 🟢 Runner saving 4: `infra.yml` - 159 min/mo

**The #4 merge-safe runner-minute saving measured on this repo, by size.**

| Workflow | Overlapping (raced) runs | Mean compute/run (timed basis) | Reclaimable remainder (mean per run) | Reclaimable runner-min/mo (range) |
| --- | --- | --- | --- | --- |
| `.github/workflows/infra.yml` | 7 confirmed (naive 58) | 4.7 job-min over 6 timed run(s) | 56% of run (Σremainder/Σduration 56%) | ~159-2378 (lower=remainder, upper=naive runs-1) |

_Superseded = a run a NEWER run started before it finished - measured by timestamp overlap, so sequential (non-racing) commits are NOT charged. Cancellation cause is unknowable from the API, so the attribution is INFERENCE. REMAINDER BASIS: cancel-in-progress cancels a superseded run the moment its successor starts, so only the compute AFTER that moment is reclaimable - the credited (lower) figure prices the MEAN per-run compute pro-rated by each superseded run's wall-clock remainder fraction (mean 56%; Σremainder/Σduration 56%), NOT the whole run; exact per-second compute is unknowable because a run's jobs run in parallel, so the pro-rata of the mean is the honest estimate. The whole-run figure is now only the loose UPPER bound (naive runs-1). Basis: the superseded COUNT and the remainder ratio are from the all-status slice (100 runs, from each run's own timestamps); the per-run PRICE is the mean of 6 PR-success timed runs (superseded runs' own jobs aren't fetched) - different populations. GUARDRAIL: verify this is NOT a deploy/release/publish workflow (a mid-flight run may be uploading artifacts / pushing a tag) before enabling cancellation - and take the predicate from the catalog recipe, which scopes cancellation with an expression; never a bare `cancel-in-progress: true`, which also kills in-flight runs on the default branch and on release tags. ROUTING: this workflow triggers on `pull_request`/`push` - with a `pull_request` trigger use the catalog's DEFAULT (PR-scoped) predicate; without one, the PR-scoped predicate is never true and saves nothing, so use the catalog's WIDENED predicate._

**💸 Bill root-cause - OPT46 · Superseded Runs Not Cancelled (Missing Concurrency or `cancel-in-progress: false`)** - risk **MEDIUM**

- **What ci-speedup measured:** 7 run(s) across 5 branch(es) were superseded (a newer run started before they finished) in the sampled window; ~159-2378 runner-min/mo of cancellable-remainder compute - the lower figure credits only the 56% mean remainder each superseded run would have burned AFTER its successor started, not the whole run (mean over 6 timed run(s); ×8.75 to the 30d volume (875 runs); 100-run recent slice (not a full 30d census)). Superseded attribution is INFERENCE - the API marks no run 'cancelled-by-concurrency'. (sensitivity range: 159 min/mo to 2,378 min/mo)
- **Why this can't slow your merge:** machine-derived proof: `post_completion_waste` - compute burned after the run signal is already decided (superseded runs: same head_branch, a newer run started before this one finished (timestamp overlap); cancellation cause is inference).
- **Source block:** `runner_minute_spine` matched 2 rows for `.github/workflows/infra.yml`; current measured cost spine for those rows is 4086.979 raw min/mo, 5250.000 billable min/mo.
- **Catalog (background + fix recipe):** https://github.com/starslingdev/skills/blob/2f048be/skills/ci-speedup/references/optimization-patterns.md#opt46--superseded-runs-not-cancelled-missing-concurrency-or-cancel-in-progress-false

#### 🤖 Prompt for your coding agent

```text
ci-speedup measured the pattern below but does NOT prescribe the fix -
investigate it in the repo and apply a safe change.

Pattern: OPT46 - Superseded Runs Not Cancelled (Missing Concurrency or `cancel-in-progress: false`).
Where: infra.yml.
What ci-speedup saw: 7 run(s) across 5 branch(es) were superseded (a newer run started before they finished) in the sampled window; ~159-2378 runner-min/mo of cancellable-remainder compute - the lower figure credits only the 56% mean remainder each superseded run would have burned AFTER its successor started, not the whole run (mean over 6 timed run(s); ×8.75 to the 30d volume (875 runs); 100-run recent slice (not a full 30d census)). Superseded attribution is INFERENCE - the API marks no run 'cancelled-by-concurrency'.
Saving: 159 min/mo of runner capacity - a bill/capacity reduction, not a merge-wait cut. Neutrality certificate: `post_completion_waste` - compute burned after the run signal is already decided (superseded runs: same head_branch, a newer run started before this one finished (timestamp overlap); cancellation cause is inference). GUARDRAIL: verify this is NOT a deploy/release/publish workflow (a mid-flight run may be uploading artifacts / pushing a tag) before enabling cancellation - and take the predicate from the catalog recipe, which scopes cancellation with an expression; never a bare `cancel-in-progress: true`, which also kills in-flight runs on the default branch and on release tags. ROUTING: this workflow triggers on `pull_request`/`push` - with a `pull_request` trigger use the catalog's DEFAULT (PR-scoped) predicate; without one, the PR-scoped predicate is never true and saves nothing, so use the catalog's WIDENED predicate.

Read the catalog entry (background, fix recipe, and guardrail):
  https://github.com/starslingdev/skills/blob/2f048be/skills/ci-speedup/references/optimization-patterns.md#opt46--superseded-runs-not-cancelled-missing-concurrency-or-cancel-in-progress-false

Do: confirm the pattern at each location above, recover the intent from git
history, and apply the catalog's fix recipe where it is safe. State the
failure mode and how you have guarded it before shipping.
```

<!-- ci-speedup:tier2-finding id=f46 pattern=OPT46 -->
<a id="r-5"></a>

## 🟢 Runner saving 5: `tests_bidi.yml` - 141 min/mo

**The #5 merge-safe runner-minute saving measured on this repo, by size.**

| Workflow | Overlapping (raced) runs | Mean compute/run (timed basis) | Reclaimable remainder (mean per run) | Reclaimable runner-min/mo (range) |
| --- | --- | --- | --- | --- |
| `.github/workflows/tests_bidi.yml` | 9 confirmed (naive 74) | 36.2 job-min over 8 timed run(s) | 64% of run (Σremainder/Σduration 64%) | ~141-1821 (lower=remainder, upper=naive runs-1) |

_Superseded = a run a NEWER run started before it finished - measured by timestamp overlap, so sequential (non-racing) commits are NOT charged. Cancellation cause is unknowable from the API, so the attribution is INFERENCE. REMAINDER BASIS: cancel-in-progress cancels a superseded run the moment its successor starts, so only the compute AFTER that moment is reclaimable - the credited (lower) figure prices the MEAN per-run compute pro-rated by each superseded run's wall-clock remainder fraction (mean 64%; Σremainder/Σduration 64%), NOT the whole run; exact per-second compute is unknowable because a run's jobs run in parallel, so the pro-rata of the mean is the honest estimate. The whole-run figure is now only the loose UPPER bound (naive runs-1). Basis: the superseded COUNT and the remainder ratio are from the all-status slice (100 runs, from each run's own timestamps); the per-run PRICE is the mean of 8 PR-success timed runs (superseded runs' own jobs aren't fetched) - different populations. GUARDRAIL: verify this is NOT a deploy/release/publish workflow (a mid-flight run may be uploading artifacts / pushing a tag) before enabling cancellation - and take the predicate from the catalog recipe, which scopes cancellation with an expression; never a bare `cancel-in-progress: true`, which also kills in-flight runs on the default branch and on release tags. ROUTING: this workflow triggers on `pull_request` - with a `pull_request` trigger use the catalog's DEFAULT (PR-scoped) predicate; without one, the PR-scoped predicate is never true and saves nothing, so use the catalog's WIDENED predicate._

**💸 Bill root-cause - OPT46 · Superseded Runs Not Cancelled (Missing Concurrency or `cancel-in-progress: false`)** - risk **MEDIUM**

- **What ci-speedup measured:** 9 run(s) across 4 branch(es) were superseded (a newer run started before they finished) in the sampled window; ~141-1821 runner-min/mo of cancellable-remainder compute - the lower figure credits only the 64% mean remainder each superseded run would have burned AFTER its successor started, not the whole run (mean over 8 timed run(s); ×0.68 to the 30d volume (68 runs); 100-run recent slice (not a full 30d census)). Superseded attribution is INFERENCE - the API marks no run 'cancelled-by-concurrency'. (sensitivity range: 141 min/mo to 1,821 min/mo)
- **Why this can't slow your merge:** machine-derived proof: `post_completion_waste` - compute burned after the run signal is already decided (superseded runs: same head_branch, a newer run started before this one finished (timestamp overlap); cancellation cause is inference).
- **Source block:** `runner_minute_spine` matched 1 row for `.github/workflows/tests_bidi.yml`; current measured cost spine for those rows is 2354.941 raw min/mo, 2391.356 billable min/mo.
- **Catalog (background + fix recipe):** https://github.com/starslingdev/skills/blob/2f048be/skills/ci-speedup/references/optimization-patterns.md#opt46--superseded-runs-not-cancelled-missing-concurrency-or-cancel-in-progress-false

#### 🤖 Prompt for your coding agent

```text
ci-speedup measured the pattern below but does NOT prescribe the fix -
investigate it in the repo and apply a safe change.

Pattern: OPT46 - Superseded Runs Not Cancelled (Missing Concurrency or `cancel-in-progress: false`).
Where: tests_bidi.yml.
What ci-speedup saw: 9 run(s) across 4 branch(es) were superseded (a newer run started before they finished) in the sampled window; ~141-1821 runner-min/mo of cancellable-remainder compute - the lower figure credits only the 64% mean remainder each superseded run would have burned AFTER its successor started, not the whole run (mean over 8 timed run(s); ×0.68 to the 30d volume (68 runs); 100-run recent slice (not a full 30d census)). Superseded attribution is INFERENCE - the API marks no run 'cancelled-by-concurrency'.
Saving: 141 min/mo of runner capacity - a bill/capacity reduction, not a merge-wait cut. Neutrality certificate: `post_completion_waste` - compute burned after the run signal is already decided (superseded runs: same head_branch, a newer run started before this one finished (timestamp overlap); cancellation cause is inference). GUARDRAIL: verify this is NOT a deploy/release/publish workflow (a mid-flight run may be uploading artifacts / pushing a tag) before enabling cancellation - and take the predicate from the catalog recipe, which scopes cancellation with an expression; never a bare `cancel-in-progress: true`, which also kills in-flight runs on the default branch and on release tags. ROUTING: this workflow triggers on `pull_request` - with a `pull_request` trigger use the catalog's DEFAULT (PR-scoped) predicate; without one, the PR-scoped predicate is never true and saves nothing, so use the catalog's WIDENED predicate.

Read the catalog entry (background, fix recipe, and guardrail):
  https://github.com/starslingdev/skills/blob/2f048be/skills/ci-speedup/references/optimization-patterns.md#opt46--superseded-runs-not-cancelled-missing-concurrency-or-cancel-in-progress-false

Do: confirm the pattern at each location above, recover the intent from git
history, and apply the catalog's fix recipe where it is safe. State the
failure mode and how you have guarded it before shipping.
```

<!-- ci-speedup:tier2-finding id=f116 pattern=OPT64 -->
<a id="r-6"></a>

## 🟢 Runner saving 6: `tests_webview_simulator.yml` - 82 min/mo

**The #6 merge-safe runner-minute saving measured on this repo, by size.**

| Run | Latest attempt | Dominant failing job | Prior attempts | Prior attempt compute min | Dominant failure min |
| --- | --- | --- | --- | --- | --- |
| `28043360084` | 2 | `WebView on iOS Simulator (3/4)` | 1 | 88.1 | 20.2 |
| `27850434623` | 2 | `WebView on iOS Simulator (3/4)` | 1 | 44.2 | 9.1 |
| `27849871516` | 2 | `WebView on iOS Simulator (3/4)` | 1 | 57.9 | 16.9 |

_Measured from GitHub jobs API `filter=all` minus `filter=latest` for workflow runs whose run_attempt > 1. The finding is emitted only when each credited prior attempt has the same unique dominant failed/timed-out job and that dominant failing job appears in the latest attempt; ambiguous ties, mixed-cause attempts, and retry-only volume are withheld._

**💸 Bill root-cause - OPT64 · Repeated Workflow Attempts From Same Failing Job** - risk **LOW**

- **What ci-speedup measured:** 3 sampled run_attempt>1 workflow run(s) had prior-attempt jobs present in `filter=all` but absent from `filter=latest`; the unique dominant failing job was `WebView on iOS Simulator (3/4)` (3 failed/timed-out prior-attempt job(s), 46.1 failed min) and it appeared again in the latest attempt. ~82 runner-min/mo of prior-attempt compute (43/30d ÷ 100 sampled all-status run(s)).
- **Why this can't slow your merge:** machine-derived proof: `post_completion_waste` - compute burned after the run signal is already decided (run_attempt>1: `filter=all` exposes prior-attempt jobs, `filter=latest` is the superseding latest attempt; the dominant failing job `WebView on iOS Simulator (3/4)` identifies the retry cause, so prior-attempt compute is post-completion waste).
- **Source block:** `runner_minute_spine` matched 4 prior-attempt rows for `.github/workflows/tests_webview_simulator.yml`; current measured cost spine for those rows is 403.203 raw min/mo, 415.807 billable min/mo.
- **Catalog (background + fix recipe):** https://github.com/starslingdev/skills/blob/2f048be/skills/ci-speedup/references/optimization-patterns.md#opt64--repeated-workflow-attempts-from-same-failing-job

#### 🤖 Prompt for your coding agent

```text
ci-speedup measured the pattern below but does NOT prescribe the fix -
investigate it in the repo and apply a safe change.

Pattern: OPT64 - Repeated Workflow Attempts From Same Failing Job.
Where: tests_webview_simulator.yml.
What ci-speedup saw: 3 sampled run_attempt>1 workflow run(s) had prior-attempt jobs present in `filter=all` but absent from `filter=latest`; the unique dominant failing job was `WebView on iOS Simulator (3/4)` (3 failed/timed-out prior-attempt job(s), 46.1 failed min) and it appeared again in the latest attempt. ~82 runner-min/mo of prior-attempt compute (43/30d ÷ 100 sampled all-status run(s)).
Saving: 82 min/mo of runner capacity - a bill/capacity reduction, not a merge-wait cut. Neutrality certificate: `post_completion_waste` - compute burned after the run signal is already decided (run_attempt>1: `filter=all` exposes prior-attempt jobs, `filter=latest` is the superseding latest attempt; the dominant failing job `WebView on iOS Simulator (3/4)` identifies the retry cause, so prior-attempt compute is post-completion waste).

Read the catalog entry (background, fix recipe, and guardrail):
  https://github.com/starslingdev/skills/blob/2f048be/skills/ci-speedup/references/optimization-patterns.md#opt64--repeated-workflow-attempts-from-same-failing-job

Do: confirm the pattern at each location above, recover the intent from git
history, and apply the catalog's fix recipe where it is safe. State the
failure mode and how you have guarded it before shipping.
```

<!-- ci-speedup:tier2-finding id=f117 pattern=OPT64 -->
<a id="r-7"></a>

## 🟢 Runner saving 7: `tests_webview_simulator.yml` - 54 min/mo

**The #7 merge-safe runner-minute saving measured on this repo, by size.**

| Run | Latest attempt | Dominant failing job | Prior attempts | Prior attempt compute min | Dominant failure min |
| --- | --- | --- | --- | --- | --- |
| `29045183195` | 2 | `WebView on iOS Simulator (2/4)` | 1 | 65.3 | 20.2 |
| `28843490162` | 2 | `WebView on iOS Simulator (2/4)` | 1 | 59.9 | 24.4 |

_Measured from GitHub jobs API `filter=all` minus `filter=latest` for workflow runs whose run_attempt > 1. The finding is emitted only when each credited prior attempt has the same unique dominant failed/timed-out job and that dominant failing job appears in the latest attempt; ambiguous ties, mixed-cause attempts, and retry-only volume are withheld._

**💸 Bill root-cause - OPT64 · Repeated Workflow Attempts From Same Failing Job** - risk **LOW**

- **What ci-speedup measured:** 2 sampled run_attempt>1 workflow run(s) had prior-attempt jobs present in `filter=all` but absent from `filter=latest`; the unique dominant failing job was `WebView on iOS Simulator (2/4)` (2 failed/timed-out prior-attempt job(s), 44.6 failed min) and it appeared again in the latest attempt. ~54 runner-min/mo of prior-attempt compute (43/30d ÷ 100 sampled all-status run(s)).
- **Why this can't slow your merge:** machine-derived proof: `post_completion_waste` - compute burned after the run signal is already decided (run_attempt>1: `filter=all` exposes prior-attempt jobs, `filter=latest` is the superseding latest attempt; the dominant failing job `WebView on iOS Simulator (2/4)` identifies the retry cause, so prior-attempt compute is post-completion waste).
- **Source block:** `runner_minute_spine` matched 4 prior-attempt rows for `.github/workflows/tests_webview_simulator.yml`; current measured cost spine for those rows is 403.203 raw min/mo, 415.807 billable min/mo.
- **Catalog (background + fix recipe):** https://github.com/starslingdev/skills/blob/2f048be/skills/ci-speedup/references/optimization-patterns.md#opt64--repeated-workflow-attempts-from-same-failing-job

#### 🤖 Prompt for your coding agent

```text
ci-speedup measured the pattern below but does NOT prescribe the fix -
investigate it in the repo and apply a safe change.

Pattern: OPT64 - Repeated Workflow Attempts From Same Failing Job.
Where: tests_webview_simulator.yml.
What ci-speedup saw: 2 sampled run_attempt>1 workflow run(s) had prior-attempt jobs present in `filter=all` but absent from `filter=latest`; the unique dominant failing job was `WebView on iOS Simulator (2/4)` (2 failed/timed-out prior-attempt job(s), 44.6 failed min) and it appeared again in the latest attempt. ~54 runner-min/mo of prior-attempt compute (43/30d ÷ 100 sampled all-status run(s)).
Saving: 54 min/mo of runner capacity - a bill/capacity reduction, not a merge-wait cut. Neutrality certificate: `post_completion_waste` - compute burned after the run signal is already decided (run_attempt>1: `filter=all` exposes prior-attempt jobs, `filter=latest` is the superseding latest attempt; the dominant failing job `WebView on iOS Simulator (2/4)` identifies the retry cause, so prior-attempt compute is post-completion waste).

Read the catalog entry (background, fix recipe, and guardrail):
  https://github.com/starslingdev/skills/blob/2f048be/skills/ci-speedup/references/optimization-patterns.md#opt64--repeated-workflow-attempts-from-same-failing-job

Do: confirm the pattern at each location above, recover the intent from git
history, and apply the catalog's fix recipe where it is safe. State the
failure mode and how you have guarded it before shipping.
```

<!-- ci-speedup:tier2-finding id=f44 pattern=OPT36 -->
<a id="r-8"></a>

## 🟢 Runner saving 8: `publish_release.yml` - 13 min/mo

**The #8 merge-safe runner-minute saving measured on this repo, by size.**

| Workflow | Consecutive same-head_sha schedule runs | Mean compute/run | Credited runner-min/mo |
| --- | --- | --- | --- |
| `.github/workflows/publish_release.yml` | 18 redundant run(s) in 12 group(s) | 2.4 job-min over 20 timed run(s) | ~13 |

_Schedule burn is counted only on event=schedule runs whose head_sha repeats consecutively, so the detector proves the workflow ran again without a code change. Basis: the count is from the all-status schedule slice; the per-run price is the mean of 20 successful schedule-event timed run(s). GUARDRAIL: confirm the current cadence is not an operational SLA before increasing the cron interval._

**💸 Bill root-cause - OPT36 · Cron Schedule Too Frequent** - risk **LOW**

- **What ci-speedup measured:** 18 scheduled run(s) in 12 consecutive same-head_sha group(s) re-ran without a code change in the sampled schedule slice (18% of 100 schedule run(s)); ~13 runner-min/mo of schedule-event compute (mean over 20 timed run(s); ×0.30 to the 30d volume (30 runs); 100-run recent slice (not a full 30d census)).
- **Why this can't slow your merge:** machine-derived proof: `non_pr_event` - `schedule` runs do not gate a PR merge (event=schedule subset only; consecutive same-head_sha schedule runs; schedule is not a developer PR/merge event).
- **Source block:** `runner_minute_spine` matched 2 rows for `.github/workflows/publish_release.yml`; current measured cost spine for those rows is 104.943 raw min/mo, 140.600 billable min/mo.
- **Guardrail:** Confirm the cron cadence is not an operational SLA; prefer widening the interval only for cleanup/triage/build jobs where delayed execution is acceptable.
- **Catalog (background + fix recipe):** https://github.com/starslingdev/skills/blob/2f048be/skills/ci-speedup/references/optimization-patterns.md#opt36--cron-schedule-too-frequent

#### 🤖 Prompt for your coding agent

```text
ci-speedup measured the pattern below but does NOT prescribe the fix -
investigate it in the repo and apply a safe change.

Pattern: OPT36 - Cron Schedule Too Frequent.
Where: publish_release.yml.
What ci-speedup saw: 18 scheduled run(s) in 12 consecutive same-head_sha group(s) re-ran without a code change in the sampled schedule slice (18% of 100 schedule run(s)); ~13 runner-min/mo of schedule-event compute (mean over 20 timed run(s); ×0.30 to the 30d volume (30 runs); 100-run recent slice (not a full 30d census)).
Saving: 13 min/mo of runner capacity - a bill/capacity reduction, not a merge-wait cut. Neutrality certificate: `non_pr_event` - `schedule` runs do not gate a PR merge (event=schedule subset only; consecutive same-head_sha schedule runs; schedule is not a developer PR/merge event). GUARDRAIL: confirm the current cadence is not an operational SLA before increasing the cron interval.

Read the catalog entry (background, fix recipe, and guardrail):
  https://github.com/starslingdev/skills/blob/2f048be/skills/ci-speedup/references/optimization-patterns.md#opt36--cron-schedule-too-frequent

Do: confirm the pattern at each location above, recover the intent from git
history, and apply the catalog's fix recipe where it is safe. State the
failure mode and how you have guarded it before shipping.
```

<!-- ci-speedup:tier2-finding id=f49 pattern=OPT64 -->
<a id="r-9"></a>

## 🟢 Runner saving 9: `tests_docker_changes.yml` - 8 min/mo

**The #9 merge-safe runner-minute saving measured on this repo, by size.**

| Run | Latest attempt | Dominant failing job | Prior attempts | Prior attempt compute min | Dominant failure min |
| --- | --- | --- | --- | --- | --- |
| `26806344641` | 2 | `test_linux_docker / Docker noble amd64` | 1 | 16.8 | 1.4 |

_Measured from GitHub jobs API `filter=all` minus `filter=latest` for workflow runs whose run_attempt > 1. The finding is emitted only when each credited prior attempt has the same unique dominant failed/timed-out job and that dominant failing job appears in the latest attempt; ambiguous ties, mixed-cause attempts, and retry-only volume are withheld._

**💸 Bill root-cause - OPT64 · Repeated Workflow Attempts From Same Failing Job** - risk **LOW**

- **What ci-speedup measured:** 1 sampled run_attempt>1 workflow run(s) had prior-attempt jobs present in `filter=all` but absent from `filter=latest`; the unique dominant failing job was `test_linux_docker / Docker noble amd64` (1 failed/timed-out prior-attempt job(s), 1.4 failed min) and it appeared again in the latest attempt. ~8 runner-min/mo of prior-attempt compute (11/30d ÷ 24 sampled all-status run(s)).
- **Why this can't slow your merge:** machine-derived proof: `post_completion_waste` - compute burned after the run signal is already decided (run_attempt>1: `filter=all` exposes prior-attempt jobs, `filter=latest` is the superseding latest attempt; the dominant failing job `test_linux_docker / Docker noble amd64` identifies the retry cause, so prior-attempt compute is post-completion waste).
- **Source block:** `runner_minute_spine` matched 4 prior-attempt rows for `.github/workflows/tests_docker_changes.yml`; current measured cost spine for those rows is 7.715 raw min/mo, 8.251 billable min/mo.
- **Catalog (background + fix recipe):** https://github.com/starslingdev/skills/blob/2f048be/skills/ci-speedup/references/optimization-patterns.md#opt64--repeated-workflow-attempts-from-same-failing-job

#### 🤖 Prompt for your coding agent

```text
ci-speedup measured the pattern below but does NOT prescribe the fix -
investigate it in the repo and apply a safe change.

Pattern: OPT64 - Repeated Workflow Attempts From Same Failing Job.
Where: tests_docker_changes.yml.
What ci-speedup saw: 1 sampled run_attempt>1 workflow run(s) had prior-attempt jobs present in `filter=all` but absent from `filter=latest`; the unique dominant failing job was `test_linux_docker / Docker noble amd64` (1 failed/timed-out prior-attempt job(s), 1.4 failed min) and it appeared again in the latest attempt. ~8 runner-min/mo of prior-attempt compute (11/30d ÷ 24 sampled all-status run(s)).
Saving: 8 min/mo of runner capacity - a bill/capacity reduction, not a merge-wait cut. Neutrality certificate: `post_completion_waste` - compute burned after the run signal is already decided (run_attempt>1: `filter=all` exposes prior-attempt jobs, `filter=latest` is the superseding latest attempt; the dominant failing job `test_linux_docker / Docker noble amd64` identifies the retry cause, so prior-attempt compute is post-completion waste).

Read the catalog entry (background, fix recipe, and guardrail):
  https://github.com/starslingdev/skills/blob/2f048be/skills/ci-speedup/references/optimization-patterns.md#opt64--repeated-workflow-attempts-from-same-failing-job

Do: confirm the pattern at each location above, recover the intent from git
history, and apply the catalog's fix recipe where it is safe. State the
failure mode and how you have guarded it before shipping.
```

<!-- ci-speedup:tier2-finding id=f43 pattern=OPT64 -->
<a id="r-10"></a>

## 🟢 Runner saving 10: `publish_release.yml` - 0.7 min/mo

**The #10 merge-safe runner-minute saving measured on this repo, by size.**

| Run | Latest attempt | Dominant failing job | Prior attempts | Prior attempt compute min | Dominant failure min |
| --- | --- | --- | --- | --- | --- |
| `28022804749` | 2 | `publish NPM and driver` | 1 | 1.9 | 1.0 |

_Measured from GitHub jobs API `filter=all` minus `filter=latest` for workflow runs whose run_attempt > 1. The finding is emitted only when each credited prior attempt has the same unique dominant failed/timed-out job and that dominant failing job appears in the latest attempt; ambiguous ties, mixed-cause attempts, and retry-only volume are withheld._

**💸 Bill root-cause - OPT64 · Repeated Workflow Attempts From Same Failing Job** - risk **LOW**

- **What ci-speedup measured:** 1 sampled run_attempt>1 workflow run(s) had prior-attempt jobs present in `filter=all` but absent from `filter=latest`; the unique dominant failing job was `publish NPM and driver` (1 failed/timed-out prior-attempt job(s), 1.0 failed min) and it appeared again in the latest attempt. ~1 runner-min/mo of prior-attempt compute (38/30d ÷ 100 sampled all-status run(s)).
- **Why this can't slow your merge:** machine-derived proof: `post_completion_waste` - compute burned after the run signal is already decided (run_attempt>1: `filter=all` exposes prior-attempt jobs, `filter=latest` is the superseding latest attempt; the dominant failing job `publish NPM and driver` identifies the retry cause, so prior-attempt compute is post-completion waste).
- **Source block:** `runner_minute_spine` matched 2 prior-attempt rows for `.github/workflows/publish_release.yml`; current measured cost spine for those rows is 0.729 raw min/mo, 1.140 billable min/mo.
- **Catalog (background + fix recipe):** https://github.com/starslingdev/skills/blob/2f048be/skills/ci-speedup/references/optimization-patterns.md#opt64--repeated-workflow-attempts-from-same-failing-job

#### 🤖 Prompt for your coding agent

```text
ci-speedup measured the pattern below but does NOT prescribe the fix -
investigate it in the repo and apply a safe change.

Pattern: OPT64 - Repeated Workflow Attempts From Same Failing Job.
Where: publish_release.yml.
What ci-speedup saw: 1 sampled run_attempt>1 workflow run(s) had prior-attempt jobs present in `filter=all` but absent from `filter=latest`; the unique dominant failing job was `publish NPM and driver` (1 failed/timed-out prior-attempt job(s), 1.0 failed min) and it appeared again in the latest attempt. ~1 runner-min/mo of prior-attempt compute (38/30d ÷ 100 sampled all-status run(s)).
Saving: 0.7 min/mo of runner capacity - a bill/capacity reduction, not a merge-wait cut. Neutrality certificate: `post_completion_waste` - compute burned after the run signal is already decided (run_attempt>1: `filter=all` exposes prior-attempt jobs, `filter=latest` is the superseding latest attempt; the dominant failing job `publish NPM and driver` identifies the retry cause, so prior-attempt compute is post-completion waste).

Read the catalog entry (background, fix recipe, and guardrail):
  https://github.com/starslingdev/skills/blob/2f048be/skills/ci-speedup/references/optimization-patterns.md#opt64--repeated-workflow-attempts-from-same-failing-job

Do: confirm the pattern at each location above, recover the intent from git
history, and apply the catalog's fix recipe where it is safe. State the
failure mode and how you have guarded it before shipping.
```

---

<a id="also-noticed"></a>

## 🧹 Also noticed - residual hygiene

> Most of these stay outside the wall-clock-neutral runner-minute section and do **not** sit on the merge-gating critical path above, so fixing them removes little or no developer wall-clock. **One or more exceptions are flagged inline** with a **Wall-clock** note: those DO sit on the critical path and their fix cuts developer wall-clock (shown first). **Expand any finding** for its locations, evidence, the catalog fix recipe, and a copy-paste agent prompt; exact per-occurrence lines + evidence also live in the findings JSON.

> ⚠️ _Approximate: computed across all workflows, but 9 capped workflow(s) still use the shallow 10-run job sample for finding/queue values; 8 runner-minute source workflow(s) still use a shallow 10-run cost-spine sample. Figures can shift run-to-run; re-run with `--shallow-runs 20` to confirm exact values._

<details>
<summary><strong>OPT24 - Long Test Job Without Sharding</strong> · ~6m 51s wall-clock · HIGH · 9 across 2 wf</summary>

**Where:** `tests_primary.yml` (Installation Test ubuntu-latest), `tests_primary.yml` (Installation Test windows-latest), `tests_primary.yml` (Installation Test macos-latest), `tests_secondary.yml` (time test runner - realtime), `tests_secondary.yml` (Test chrome on ubuntu-22.04), `tests_secondary.yml` (Test chrome-beta on ubuntu-22.04), `tests_secondary.yml` (time test runner - frozen), `tests_secondary.yml` (Test chrome on macos-latest), +1 more
**Wall-clock:** this job carries a real wall-clock saving, but the spine demotes it as **opt-in / rare** - it runs on only a minority of sampled PRs, so a typical PR doesn't wait on it (see the spine's opt-in footnote above). Its fix cuts wall-clock on the PRs that DO run it, not on the typical merge path.
**Evidence:** job `Test chrome on macos-latest` p50 3573s over 4 runs, no shard axis observed (job names lack a `shard` / `partition` matrix marker)
**Catalog (background + fix recipe):** https://github.com/starslingdev/skills/blob/2f048be/skills/ci-speedup/references/optimization-patterns.md#opt24--long-test-job-without-sharding

#### 🤖 Prompt for your coding agent

```text
ci-speedup measured the pattern below but does NOT prescribe the fix -
investigate it in the repo and apply a safe change.

Pattern: OPT24 - Long Test Job Without Sharding.
Where: tests_primary.yml (Installation Test ubuntu-latest); tests_primary.yml (Installation Test windows-latest); tests_primary.yml (Installation Test macos-latest); tests_secondary.yml (time test runner - realtime); tests_secondary.yml (Test chrome on ubuntu-22.04); tests_secondary.yml (Test chrome-beta on ubuntu-22.04); tests_secondary.yml (time test runner - frozen); tests_secondary.yml (Test chrome on macos-latest); +1 more (see findings JSON).
What ci-speedup saw: job `Test chrome on macos-latest` p50 3573s over 4 runs, no shard axis observed (job names lack a `shard` / `partition` matrix marker)
Saving: developer WALL-CLOCK (~6m 51s) - this job is a long pole ON the merge-gating critical path, so its catalog fix shortens the merge wait. NOT a runner-bill cut, and NOT off the critical path.

Read the catalog entry (background, fix recipe, and guardrail):
  https://github.com/starslingdev/skills/blob/2f048be/skills/ci-speedup/references/optimization-patterns.md#opt24--long-test-job-without-sharding

Do: confirm the pattern at each location above, recover the intent from git
history, and apply the catalog's fix recipe where it is safe. State the
failure mode and how you have guarded it before shipping.
```

</details>

<details>
<summary><strong>OPT25 - Matrix Leg Imbalance</strong> · ~3m 21s wall-clock · MEDIUM · 2 across 2 wf</summary>

**Where:** `tests_components.yml` (- Node.js 20), `tests_secondary.yml` (Windows)
**Wall-clock:** unlike the other findings in this section, this one **sits ON the merge-gating critical path** (a long pole) - its catalog fix **cuts developer wall-clock**, it is not a bill-only cleanup. See the spine above and this pattern's catalog recipe below for the remedy.
**Evidence:** leg `Windows (firefox)` median 4377s vs `Windows (chromium)` 1598s - 2.7× imbalance over 3 sampled runs (heterogeneous legs - split the slow leg, don't rebalance)
**Catalog (background + fix recipe):** https://github.com/starslingdev/skills/blob/2f048be/skills/ci-speedup/references/optimization-patterns.md#opt25--shard-imbalance

#### 🤖 Prompt for your coding agent

```text
ci-speedup measured the pattern below but does NOT prescribe the fix -
investigate it in the repo and apply a safe change.

Pattern: OPT25 - Matrix Leg Imbalance.
Where: tests_components.yml (- Node.js 20); tests_secondary.yml (Windows).
What ci-speedup saw: leg `Windows (firefox)` median 4377s vs `Windows (chromium)` 1598s - 2.7× imbalance over 3 sampled runs (heterogeneous legs - split the slow leg, don't rebalance)
Saving: developer WALL-CLOCK (~3m 21s) - this job is a long pole ON the merge-gating critical path, so its catalog fix shortens the merge wait. NOT a runner-bill cut, and NOT off the critical path.

Read the catalog entry (background, fix recipe, and guardrail):
  https://github.com/starslingdev/skills/blob/2f048be/skills/ci-speedup/references/optimization-patterns.md#opt25--shard-imbalance

Do: confirm the pattern at each location above, recover the intent from git
history, and apply the catalog's fix recipe where it is safe. State the
failure mode and how you have guarded it before shipping.
```

</details>

<details>
<summary><strong>OPT73 - Shared step recurs across the cluster - fix once, lower the floor</strong> · 109,114 min/mo · HIGH · 1 across 1 wf</summary>

**Where:** `tests_primary.yml` (ubuntu-22.04 (webkit - Node.js 20))
**Evidence:** the `Run ./.github/actions/run-test` step is 100% of the slowest cluster job `ubuntu-22.04 (webkit - Node.js 20)` (2480s) and recurs across 5 concurrent jobs of `.github/workflows/tests_primary.yml` (~1693-2474s per job) - a cluster-floor lever
**Catalog (background + fix recipe):** https://github.com/starslingdev/skills/blob/2f048be/skills/ci-speedup/references/optimization-patterns.md#opt73--shared-sub-step-across-critical-path-jobs-cluster-floor-lever

#### 🤖 Prompt for your coding agent

```text
ci-speedup measured the pattern below but does NOT prescribe the fix -
investigate it in the repo and apply a safe change.

Pattern: OPT73 - Shared step recurs across the cluster - fix once, lower the floor.
Where: tests_primary.yml (ubuntu-22.04 (webkit - Node.js 20)).
What ci-speedup saw: the `Run ./.github/actions/run-test` step is 100% of the slowest cluster job `ubuntu-22.04 (webkit - Node.js 20)` (2480s) and recurs across 5 concurrent jobs of `.github/workflows/tests_primary.yml` (~1693-2474s per job) - a cluster-floor lever
Saving: ~109,114 runner-min/mo - off the merge-gating critical path, so ~0 developer wall-clock (a cloud-bill cut, not a merge-wait cut).

Read the catalog entry (background, fix recipe, and guardrail):
  https://github.com/starslingdev/skills/blob/2f048be/skills/ci-speedup/references/optimization-patterns.md#opt73--shared-sub-step-across-critical-path-jobs-cluster-floor-lever

Do: confirm the pattern at each location above, recover the intent from git
history, and apply the catalog's fix recipe where it is safe. State the
failure mode and how you have guarded it before shipping.
```

</details>

<details>
<summary><strong>OPT73 - Shared step recurs across the cluster - fix once, lower the floor</strong> · 101,710 min/mo · HIGH · 1 across 1 wf</summary>

**Where:** `tests_mcp.yml` (windows-latest - firefox)
**Evidence:** the `Run ./.github/actions/run-test` step is 100% of the slowest cluster job `windows-latest - firefox` (2413s) and recurs across 5 concurrent jobs of `.github/workflows/tests_mcp.yml` (~1463-2404s per job) - a cluster-floor lever
**Catalog (background + fix recipe):** https://github.com/starslingdev/skills/blob/2f048be/skills/ci-speedup/references/optimization-patterns.md#opt73--shared-sub-step-across-critical-path-jobs-cluster-floor-lever

#### 🤖 Prompt for your coding agent

```text
ci-speedup measured the pattern below but does NOT prescribe the fix -
investigate it in the repo and apply a safe change.

Pattern: OPT73 - Shared step recurs across the cluster - fix once, lower the floor.
Where: tests_mcp.yml (windows-latest - firefox).
What ci-speedup saw: the `Run ./.github/actions/run-test` step is 100% of the slowest cluster job `windows-latest - firefox` (2413s) and recurs across 5 concurrent jobs of `.github/workflows/tests_mcp.yml` (~1463-2404s per job) - a cluster-floor lever
Saving: ~101,710 runner-min/mo - off the merge-gating critical path, so ~0 developer wall-clock (a cloud-bill cut, not a merge-wait cut).

Read the catalog entry (background, fix recipe, and guardrail):
  https://github.com/starslingdev/skills/blob/2f048be/skills/ci-speedup/references/optimization-patterns.md#opt73--shared-sub-step-across-critical-path-jobs-cluster-floor-lever

Do: confirm the pattern at each location above, recover the intent from git
history, and apply the catalog's fix recipe where it is safe. State the
failure mode and how you have guarded it before shipping.
```

</details>

<details>
<summary><strong>OPT73 - Shared step recurs across the cluster - fix once, lower the floor</strong> · 59,226 min/mo · HIGH · 1 across 1 wf</summary>

**Where:** `tests_secondary.yml` (Windows (firefox))
**Evidence:** the `Run ./.github/actions/run-test` step is 100% of the slowest cluster job `Windows (firefox)` (4375s) and recurs across 4 concurrent jobs of `.github/workflows/tests_secondary.yml` (~2654-4366s per job) - a cluster-floor lever
**Catalog (background + fix recipe):** https://github.com/starslingdev/skills/blob/2f048be/skills/ci-speedup/references/optimization-patterns.md#opt73--shared-sub-step-across-critical-path-jobs-cluster-floor-lever

#### 🤖 Prompt for your coding agent

```text
ci-speedup measured the pattern below but does NOT prescribe the fix -
investigate it in the repo and apply a safe change.

Pattern: OPT73 - Shared step recurs across the cluster - fix once, lower the floor.
Where: tests_secondary.yml (Windows (firefox)).
What ci-speedup saw: the `Run ./.github/actions/run-test` step is 100% of the slowest cluster job `Windows (firefox)` (4375s) and recurs across 4 concurrent jobs of `.github/workflows/tests_secondary.yml` (~2654-4366s per job) - a cluster-floor lever
Saving: ~59,226 runner-min/mo - off the merge-gating critical path, so ~0 developer wall-clock (a cloud-bill cut, not a merge-wait cut).

Read the catalog entry (background, fix recipe, and guardrail):
  https://github.com/starslingdev/skills/blob/2f048be/skills/ci-speedup/references/optimization-patterns.md#opt73--shared-sub-step-across-critical-path-jobs-cluster-floor-lever

Do: confirm the pattern at each location above, recover the intent from git
history, and apply the catalog's fix recipe where it is safe. State the
failure mode and how you have guarded it before shipping.
```

</details>

<details>
<summary><strong>OPT35 - Missing `fail-fast` on Non-Diagnostic Matrix Dimensions</strong> · 5,230 min/mo · LOW · 2 across 2 wf</summary>

**Where:** `tests_primary.yml` (test_test_runner), `tests_webview_simulator.yml` (test_webview_simulator)
**Tier-2 note:** measured wall-clock-neutral instance(s) of this pattern did not have matching render-ready `runner_minute_spine` source rows, so they are kept here instead of rendered as source-backed savings cards. Their computed neutrality certificate(s) (`post_completion_waste`) are stamped in findings.json and re-derived by verify_report.
**Evidence:** 11 sampled failed matrix occurrence(s) left shard sibling jobs running after the first failed shard; ~5015 runner-min/mo of post-failure matrix compute (778/30d ÷ 100 sampled all-status run(s)).; 43 sampled failed matrix occurrence(s) left shard sibling jobs running after the first failed shard; ~215 runner-min/mo of post-failure matrix compute (43/30d ÷ 100 sampled all-status run(s)).
**Catalog (background + fix recipe):** https://github.com/starslingdev/skills/blob/2f048be/skills/ci-speedup/references/optimization-patterns.md#opt35--missing-fail-fast-on-non-diagnostic-matrix-dimensions

#### 🤖 Prompt for your coding agent

```text
ci-speedup measured the pattern below but does NOT prescribe the fix -
investigate it in the repo and apply a safe change.

Pattern: OPT35 - Missing `fail-fast` on Non-Diagnostic Matrix Dimensions.
Where: tests_primary.yml (test_test_runner); tests_webview_simulator.yml (test_webview_simulator).
What ci-speedup saw: 11 sampled failed matrix occurrence(s) left shard sibling jobs running after the first failed shard; ~5015 runner-min/mo of post-failure matrix compute (778/30d ÷ 100 sampled all-status run(s)).; 43 sampled failed matrix occurrence(s) left shard sibling jobs running after the first failed shard; ~215 runner-min/mo of post-failure matrix compute (43/30d ÷ 100 sampled all-status run(s)).
Saving: ~5,230 runner-min/mo - off the merge-gating critical path, so ~0 developer wall-clock (a cloud-bill cut, not a merge-wait cut).

Read the catalog entry (background, fix recipe, and guardrail):
  https://github.com/starslingdev/skills/blob/2f048be/skills/ci-speedup/references/optimization-patterns.md#opt35--missing-fail-fast-on-non-diagnostic-matrix-dimensions

Do: confirm the pattern at each location above, recover the intent from git
history, and apply the catalog's fix recipe where it is safe. State the
failure mode and how you have guarded it before shipping.
```

</details>

<details>
<summary><strong>OPT64 - Repeated Workflow Attempts From Same Failing Job</strong> · 2,451 min/mo · LOW · 1 across 1 wf</summary>

**Where:** `tests_mcp.yml`
**Tier-2 note:** measured wall-clock-neutral instances of this same pattern are promoted above; this appendix row shows only the remaining modeled, uncertified, or source-unbacked instance(s).
**Evidence:** 1 sampled run_attempt>1 workflow run(s) had prior-attempt jobs present in `filter=all` but absent from `filter=latest`; the unique dominant failing job was `ubuntu-latest - chrome` (1 failed/timed-out prior-attempt job(s), 15.8 failed min) and it appeared again in the latest attempt. ~2451 runner-min/mo of prior-attempt compute (840/30d ÷ 100 sampled all-status run(s)).
**Catalog (background + fix recipe):** https://github.com/starslingdev/skills/blob/2f048be/skills/ci-speedup/references/optimization-patterns.md#opt64--repeated-workflow-attempts-from-same-failing-job

#### 🤖 Prompt for your coding agent

```text
ci-speedup measured the pattern below but does NOT prescribe the fix -
investigate it in the repo and apply a safe change.

Pattern: OPT64 - Repeated Workflow Attempts From Same Failing Job.
Where: tests_mcp.yml.
What ci-speedup saw: 1 sampled run_attempt>1 workflow run(s) had prior-attempt jobs present in `filter=all` but absent from `filter=latest`; the unique dominant failing job was `ubuntu-latest - chrome` (1 failed/timed-out prior-attempt job(s), 15.8 failed min) and it appeared again in the latest attempt. ~2451 runner-min/mo of prior-attempt compute (840/30d ÷ 100 sampled all-status run(s)).
Saving: ~2,451 runner-min/mo - off the merge-gating critical path, so ~0 developer wall-clock (a cloud-bill cut, not a merge-wait cut).

Read the catalog entry (background, fix recipe, and guardrail):
  https://github.com/starslingdev/skills/blob/2f048be/skills/ci-speedup/references/optimization-patterns.md#opt64--repeated-workflow-attempts-from-same-failing-job

Do: confirm the pattern at each location above, recover the intent from git
history, and apply the catalog's fix recipe where it is safe. State the
failure mode and how you have guarded it before shipping.
```

</details>

<details>
<summary><strong>OPT2 - Uncached Large Downloads</strong> · 1,315 min/mo · MEDIUM · 5 across 5 wf</summary>

**Where:** `infra.yml:24` (doc-and-lint), `tests_bidi.yml:54` (test_bidi), `tests_components.yml:46` (test_components), `tests_extension.yml:50` (test_extension), `tests_primary.yml:178` (test_vscode_extension)
**Evidence:** job `doc-and-lint` runs `playwright install` with no preceding `actions/cache` step keyed on the Playwright version; job `test_bidi` runs `playwright install` with no preceding `actions/cache` step keyed on the Playwright version; job `test_components` runs `playwright install` with no preceding `actions/cache` step keyed on the Playwright version; job `test_extension` runs `playwright install` with no preceding `actions/cache` step keyed on the Playwright version; job `test_vscode_extension` runs `playwright install` with no preceding `actions/cache` step keyed on the Playwright version
**Catalog (background + fix recipe):** https://github.com/starslingdev/skills/blob/2f048be/skills/ci-speedup/references/optimization-patterns.md#opt2--uncached-large-downloads

#### 🤖 Prompt for your coding agent

```text
ci-speedup measured the pattern below but does NOT prescribe the fix -
investigate it in the repo and apply a safe change.

Pattern: OPT2 - Uncached Large Downloads.
Where: infra.yml:24 (doc-and-lint); tests_bidi.yml:54 (test_bidi); tests_components.yml:46 (test_components); tests_extension.yml:50 (test_extension); tests_primary.yml:178 (test_vscode_extension).
What ci-speedup saw: job `doc-and-lint` runs `playwright install` with no preceding `actions/cache` step keyed on the Playwright version; job `test_bidi` runs `playwright install` with no preceding `actions/cache` step keyed on the Playwright version; job `test_components` runs `playwright install` with no preceding `actions/cache` step keyed on the Playwright version; job `test_extension` runs `playwright install` with no preceding `actions/cache` step keyed on the Playwright version; job `test_vscode_extension` runs `playwright install` with no preceding `actions/cache` step keyed on the Playwright version
Saving: ~1,315 runner-min/mo - off the merge-gating critical path, so ~0 developer wall-clock (a cloud-bill cut, not a merge-wait cut).

Read the catalog entry (background, fix recipe, and guardrail):
  https://github.com/starslingdev/skills/blob/2f048be/skills/ci-speedup/references/optimization-patterns.md#opt2--uncached-large-downloads

Do: confirm the pattern at each location above, recover the intent from git
history, and apply the catalog's fix recipe where it is safe. State the
failure mode and how you have guarded it before shipping.
```

</details>

<details>
<summary><strong>OPT14 - Repeated Checkout/Setup Without Artifact Handoff (and Slow Tool Replacement)</strong> · 1,240 min/mo · MEDIUM · 2 across 2 wf</summary>

**Where:** `infra.yml:1` (doc-and-lint), `tests_primary.yml:1` (test_vscode_extension)
**Wall-clock:** this saves runner-minutes but its fix is **wall-clock-negative** (build-once-then-fan-out adds a serial gate), so it lengthens the merge wait. Treat it as a bill saving, not a speed win.
**Evidence:** 2 jobs each run checkout + dependency install with no `actions/upload-artifact` / `download-artifact` handoff: doc-and-lint, lint-snippets; 2 jobs each run checkout + dependency install with no `actions/upload-artifact` / `download-artifact` handoff: test_vscode_extension, test_package_installations
**Catalog (background + fix recipe):** https://github.com/starslingdev/skills/blob/2f048be/skills/ci-speedup/references/optimization-patterns.md#opt14--repeated-checkoutsetup-without-artifact-handoff-and-slow-tool-replacement

#### 🤖 Prompt for your coding agent

```text
ci-speedup measured the pattern below but does NOT prescribe the fix -
investigate it in the repo and apply a safe change.

Pattern: OPT14 - Repeated Checkout/Setup Without Artifact Handoff (and Slow Tool Replacement).
Where: infra.yml:1 (doc-and-lint); tests_primary.yml:1 (test_vscode_extension).
What ci-speedup saw: 2 jobs each run checkout + dependency install with no `actions/upload-artifact` / `download-artifact` handoff: doc-and-lint, lint-snippets; 2 jobs each run checkout + dependency install with no `actions/upload-artifact` / `download-artifact` handoff: test_vscode_extension, test_package_installations
Saving: ~1,240 runner-min/mo - off the merge-gating critical path, so ~0 developer wall-clock (a cloud-bill cut, not a merge-wait cut).

Read the catalog entry (background, fix recipe, and guardrail):
  https://github.com/starslingdev/skills/blob/2f048be/skills/ci-speedup/references/optimization-patterns.md#opt14--repeated-checkoutsetup-without-artifact-handoff-and-slow-tool-replacement

Do: confirm the pattern at each location above, recover the intent from git
history, and apply the catalog's fix recipe where it is safe. State the
failure mode and how you have guarded it before shipping.
```

</details>

<details>
<summary><strong>OPT73 - Shared step recurs across the cluster - fix once, lower the floor</strong> · 1,098 min/mo · HIGH · 1 across 1 wf</summary>

**Where:** `tests_webview_simulator.yml` (WebView on iOS Simulator (4/4))
**Evidence:** the `Run WebView tests` step is 77% of the slowest cluster job `WebView on iOS Simulator (4/4)` (1310s) and recurs across 2 concurrent jobs of `.github/workflows/tests_webview_simulator.yml` (~776-1006s per job) - a cluster-floor lever
**Catalog (background + fix recipe):** https://github.com/starslingdev/skills/blob/2f048be/skills/ci-speedup/references/optimization-patterns.md#opt73--shared-sub-step-across-critical-path-jobs-cluster-floor-lever

#### 🤖 Prompt for your coding agent

```text
ci-speedup measured the pattern below but does NOT prescribe the fix -
investigate it in the repo and apply a safe change.

Pattern: OPT73 - Shared step recurs across the cluster - fix once, lower the floor.
Where: tests_webview_simulator.yml (WebView on iOS Simulator (4/4)).
What ci-speedup saw: the `Run WebView tests` step is 77% of the slowest cluster job `WebView on iOS Simulator (4/4)` (1310s) and recurs across 2 concurrent jobs of `.github/workflows/tests_webview_simulator.yml` (~776-1006s per job) - a cluster-floor lever
Saving: ~1,098 runner-min/mo - off the merge-gating critical path, so ~0 developer wall-clock (a cloud-bill cut, not a merge-wait cut).

Read the catalog entry (background, fix recipe, and guardrail):
  https://github.com/starslingdev/skills/blob/2f048be/skills/ci-speedup/references/optimization-patterns.md#opt73--shared-sub-step-across-critical-path-jobs-cluster-floor-lever

Do: confirm the pattern at each location above, recover the intent from git
history, and apply the catalog's fix recipe where it is safe. State the
failure mode and how you have guarded it before shipping.
```

</details>

<details>
<summary><strong>OPT73 - Shared step recurs across the cluster - fix once, lower the floor</strong> · 72 min/mo · HIGH · 1 across 1 wf</summary>

**Where:** `tests_docker_changes.yml` (test_linux_docker / Docker noble arm64)
**Evidence:** the `Run @smoke tests inside docker` step is 27% of the slowest cluster job `test_linux_docker / Docker noble arm64` (288s) and recurs across 6 concurrent jobs of `.github/workflows/tests_docker_changes.yml` (~75-112s per job) - a cluster-floor lever
**Catalog (background + fix recipe):** https://github.com/starslingdev/skills/blob/2f048be/skills/ci-speedup/references/optimization-patterns.md#opt73--shared-sub-step-across-critical-path-jobs-cluster-floor-lever

#### 🤖 Prompt for your coding agent

```text
ci-speedup measured the pattern below but does NOT prescribe the fix -
investigate it in the repo and apply a safe change.

Pattern: OPT73 - Shared step recurs across the cluster - fix once, lower the floor.
Where: tests_docker_changes.yml (test_linux_docker / Docker noble arm64).
What ci-speedup saw: the `Run @smoke tests inside docker` step is 27% of the slowest cluster job `test_linux_docker / Docker noble arm64` (288s) and recurs across 6 concurrent jobs of `.github/workflows/tests_docker_changes.yml` (~75-112s per job) - a cluster-floor lever
Saving: ~72 runner-min/mo - off the merge-gating critical path, so ~0 developer wall-clock (a cloud-bill cut, not a merge-wait cut).

Read the catalog entry (background, fix recipe, and guardrail):
  https://github.com/starslingdev/skills/blob/2f048be/skills/ci-speedup/references/optimization-patterns.md#opt73--shared-sub-step-across-critical-path-jobs-cluster-floor-lever

Do: confirm the pattern at each location above, recover the intent from git
history, and apply the catalog's fix recipe where it is safe. State the
failure mode and how you have guarded it before shipping.
```

</details>

<details>
<summary><strong>OPT73 - Shared step recurs across the cluster - fix once, lower the floor</strong> · 33 min/mo · HIGH · 1 across 1 wf</summary>

**Where:** `tests_docker_release.yml` (test_linux_docker / Docker jammy amd64)
**Evidence:** the `Run @smoke tests inside docker` step is 37% of the slowest cluster job `test_linux_docker / Docker jammy amd64` (308s) and recurs across 6 concurrent jobs of `.github/workflows/tests_docker_release.yml` (~76-114s per job) - a cluster-floor lever
**Catalog (background + fix recipe):** https://github.com/starslingdev/skills/blob/2f048be/skills/ci-speedup/references/optimization-patterns.md#opt73--shared-sub-step-across-critical-path-jobs-cluster-floor-lever

#### 🤖 Prompt for your coding agent

```text
ci-speedup measured the pattern below but does NOT prescribe the fix -
investigate it in the repo and apply a safe change.

Pattern: OPT73 - Shared step recurs across the cluster - fix once, lower the floor.
Where: tests_docker_release.yml (test_linux_docker / Docker jammy amd64).
What ci-speedup saw: the `Run @smoke tests inside docker` step is 37% of the slowest cluster job `test_linux_docker / Docker jammy amd64` (308s) and recurs across 6 concurrent jobs of `.github/workflows/tests_docker_release.yml` (~76-114s per job) - a cluster-floor lever
Saving: ~33 runner-min/mo - off the merge-gating critical path, so ~0 developer wall-clock (a cloud-bill cut, not a merge-wait cut).

Read the catalog entry (background, fix recipe, and guardrail):
  https://github.com/starslingdev/skills/blob/2f048be/skills/ci-speedup/references/optimization-patterns.md#opt73--shared-sub-step-across-critical-path-jobs-cluster-floor-lever

Do: confirm the pattern at each location above, recover the intent from git
history, and apply the catalog's fix recipe where it is safe. State the
failure mode and how you have guarded it before shipping.
```

</details>

> [!TIP]
> **+4 more hygiene pattern(s) (24 occurrence(s)) not shown** - lower bill saving, kept in the findings JSON so nothing is dropped.

## 🗄️ Data sources

> **Where this data comes from**
>
> - **Critical path + step P50:** the committed ci-speedup audit of `microsoft/playwright`, scanned **2026-07-25** - P50 over **161 runs / 1630 jobs** across 18 workflows (latest runs at scan time).
> - **Data-collection cost:** **860 gh API call(s)** in ~2m 34s - adaptive sampling - a 10-run shallow pass over every workflow, then 3 of 9 PR-gating pole candidate(s) deepened to 20 runs, plus 6 bill-pole workflow candidate(s) deepened to 20 runs for the runner-minute source block (the gate, drill-set, and floor are full-depth; other finding-level values may still rest on the shallow sample).
> - **Which checks gate (the critical-path ordering):** measured from **20/20 sampled PRs**.
> - ⚠️ **Required checks were unreadable** (no admin / branch protection 404), so 'gate' here means the **slowest check on a typical PR** (observed), not a *confirmed required* check. Slow checks that run on only a minority of PRs are shown as a footnote, not the headline.

| Source | Coverage | Used for |
| --- | --- | --- |
| ci-speedup static scan (skill commit `2f048be`, scripts tree `021bb07`) | All `.github/workflows/*.yml` under the analyzed tree (3827650) | Static pattern detection (OPT1-OPT69 catalog) |
| gh runs/jobs API (timestamps) | 161 runs / 1630 jobs sampled | Critical-path + per-step P50 |
| job logs | not run | Sampled only for a slow pole worth log-level inspection |
| workflow YAML | 18 from the analyzed checkout | `on:` triggers, matrix/shard axes, job timeouts (detector inputs) |

**Data freshness.** Analyzer ran at `2026-07-25T19:05:00.444799+00:00`; workflow YAML is read from the analyzed tree at commit `3827650`. Timing and activity counts reflect the sampled runs over a rolling 30-day window at scan time. 860 gh API queries were made.

> _The runner-minute / cost-spine figures in this report keep the full sample by design (they size total compute, not the critical path), so they still include the earlier configuration; a duration- or structure-changing edit (e.g. a shard split) blends both layouts._

_The concurrent checks (the Contents critical path) are P50 across sampled PRs. The per-step timeline + the drill are **one representative run** - the one closest to the P50 time - so they are absolute for that run, not P50. The **categorical cause** is stable across runs; where a **Cross-run check** is shown it gives the magnitude's median + range across several runs, so the single run's number isn't taken on faith. Per-step bars are scaled within each drill._

_The drill bars are plain-English labels for what's in the job log (e.g. a `DB migrations` bar is logged as `Total Migration Time:`). To verify any number, follow the pole's **🔗 Audit** link to the gating step, expand it, and search (Ctrl-F) for the verbatim strings the Audit line lists - GitHub anchors to the step, not an exact log line._

---

Generated by [StarSling](https://starsling.dev) 💫
