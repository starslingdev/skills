# pallets/flask - why is the merge slow?

| Repository | `pallets/flask` |
| :--- | :--- |
| **Audited commit** | [`36e4a82`](https://github.com/pallets/flask/commit/36e4a824f340fdee7ed50937ba8e7f6bc7d17f81) - file & line references are anchored to this tree |
| **Runs analyzed** | 47 runs / 152 jobs across 5 workflows |
| **Runs window** | 2026-06-24 → 2026-07-24 (30-day window) |
| **PR gate sample** | 20 / 20 PRs |
| **Audit** | ran 2026-07-24 · ci-speedup skill commit [`2f048be`](https://github.com/starslingdev/skills/commit/2f048be) |

> **Bottom line.** A typical PR waits **34s** for all checks to finish. The biggest single measured win is **~7s** off the slowest fixable check, `Windows` - see [Long pole 1](#pole-1) for the drill-down to the biggest lever.
>
> **34s until all checks finish** - `Windows` is the slowest check a typical PR waits on. 
>
> **`.github/workflows/tests.yaml` changed ~129 days ago - narrowed to the current configuration.** This audit measures only the 9 runs since that change; the 11 earlier runs measured the retired configuration and were excluded so no drill-down blends the two.
>
> **`.github/workflows/zizmor.yaml` changed ~131 days ago - narrowed to the current configuration.** This audit measures only the 6 runs since that change; the 4 earlier runs measured the retired configuration and were excluded so no drill-down blends the two.
>
> **After the gate.** 3 min/mo of wall-clock-neutral runner minutes is recoverable (1 neutral finding; none can slow a merge).

## 📋 Contents

**🐌 Critical path** - the checks that gate your merge, each linking to its long-pole drill-down (waterfall → biggest lever → agent prompt):

1. 🟡 [Windows](#pole-1) - 34s (the gate)
2. 🟡 [main](#pole-2) - 19s
3. 🟡 [PyPy](#pole-3) - 26s · rarely the merge pole
4. 🟡 [3.14t](#pole-4) - 25s · rarely the merge pole
5. 🟡 [3.9](#pole-5) - 24s · rarely the merge pole

**💸 Runner-minute reductions** - ~3 min/mo of measured, merge-safe runner-minute savings, backed by a 19-row cost spine: [section](#runner-minute-reductions).

1. 🟢 [Cron Schedule Too Frequent](#r-1) - 3 min/mo

**🧹 Also noticed** - 2 additional hygiene findings kept outside the neutral runner-minute section (modeled/uncertified, mostly ~0 wall-clock), below the critical path: [see below](#also-noticed).

<a id="long-pole-map"></a>

## 🗺️ Long pole map

A **workflow** is one YAML file under `.github/workflows/`; a run of it executes its **jobs** in parallel (each on its own runner); each job runs its **steps** in sequence.

```text
Level 1 - checks racing on every PR; the merge waits for the slowest:

   Windows · tests.yaml               ██████████████████████      34s       ◀┐
   main · pre-commit.yaml             ████████████                19s        │
   ┌─────────────────────────────────────────────────────────────────────────┘

   ▼ Level 2 - inside Windows, steps run one after another:

   Run uv run --locked --no-default…  ██████████████████████      14s   43% ◀
   Run astral-sh/setup-uv@fac544c07…  ████████████████████        14s   40%
   Run actions/checkout@df4cb1c069e…  █████████████                8s   25%
   Post Run actions/checkout@df4cb1…  ████                         2s    7%
   Set up job                         ██                           1s    3%
   Post Run astral-sh/setup-uv@fac5…  ██                           1s    3%
```

Each ◀ marks the blocker the next level opens. Long pole 1 below drills the marked step to its root cause and hand-off prompt.

<a id="pole-1"></a>

## 🟡 Long pole 1: `tests.yaml` ▸ `Windows` - 34s

**The slowest check a typical PR waits on.**

> **What a change here can buy (wall-clock):** up to **~7s** - it gates until it drops to the next concurrent check, `PyPy` (26s); below that the gate moves and further savings are runner-minutes, not wall-clock.

```text
Where the job's ~34s goes - every step, slowest first - each step's P50 is measured on its own, so they sum to ~42s vs the job's own 34s P50; read the bars as proportions, not an exact sum:

   Run uv run --locked --no-default…  ██████████████████████      14s       ◀
   Run astral-sh/setup-uv@fac544c07…  ████████████████████        14s
   Run actions/checkout@df4cb1c069e…  █████████████                8s
   Post Run actions/checkout@df4cb1…  ████                         2s
   Set up job                         ██                           1s
   Post Run astral-sh/setup-uv@fac5…  ██                           1s
   …1 smaller steps (setup, cache, …  ██                          ~1s

(no log-level detector fired, but a **structural catalog pattern** matched this pole - see the **structural root-cause** below; the dominant step is the addressable lever.)
```

**📐 Structural root-cause - OPT75 · The long pole's time is one addressable step - speed it up or move it off the PR path - `Windows`** - risk **MEDIUM**

A measured **structural** lever on the critical path (it IS this pole, so it's not repeated in the off-path appendix). It carries a risk profile - review the guardrail and rollout before shipping:

- **What ci-speedup measured:** critical-path check `Windows` (34s): dominant step `Run uv run --locked --no-default-groups --group dev tox run + 1 more other step` (other, 67% of job `Windows`)
- **Guardrail:** carry the guardrail of the routed lever (e.g. OPT70's full-suite fallback if the dominant step is a test being scoped); never present the decomposition as free
- **Rollout:** the routed lever's rollout; re-measure the pole's p50 after the dominant step is attacked - the next-largest step becomes the target
- **Failure mode:** the dominant-step remedy ranges from LOW (cache an install) to HIGH (scope a test/build, inheriting OPT70) - the candidate carries the risk of whichever specific lever its dominant category routes to
- **Catalog (background + fix recipe):** https://github.com/starslingdev/skills/blob/2f048be/skills/ci-speedup/references/optimization-patterns.md#opt75--long-pole-optimize-or-relocate-the-dominant-step

#### 🤖 Prompt for your coding agent

```text
starslingdev/ci-speedup measured where the time goes below but does NOT prescribe the fix - a structural catalog pattern (OPT75) matched this pole (see the **structural root-cause** section above for the measured lever + its risk axis); the dominant step below is where that lever's time is spent.

REPO: pallets/flask (audited at commit 36e4a82)

THE GATE
- Workflow `tests.yaml`, job `Windows`.
- Slowest check a typical PR waits on: P50 34s.

WHERE THE TIME GOES
- The job's time is dominated by the `Run uv run --locked --no-default-groups --group dev tox run + 1 more other step` step: ~28s (67% of the job wall), from the sampled per-step decomposition (no single-run timeline was captured for this job).

STRUCTURAL CATALOG PATTERN MATCHED
- A structural catalog pattern (OPT75) matched this pole - see the **structural root-cause** section above for the measured lever, its risk / guardrail / rollout, and the catalog fix recipe. The step above is the load-bearing one that lever targets; open its log (the Audit link) to see exactly what inside it the lever reshapes.

WHAT'S ADDRESSABLE (wall-clock ceiling - don't over-promise)
- up to ~7s - it gates until it drops to the next concurrent check, `PyPy` (26s); below that the gate moves and further savings are runner-minutes, not wall-clock.

WHERE TO LOOK
- The `tests.yaml` workflow definition for the dominant step, and the tool/config it invokes (build tool, test runner, or install) - that's where its time is spent.

DELIVER & VERIFY
- A change that cuts the dominant step's wall time without dropping coverage; re-measure the step on a PR run to confirm the reduction.
```


<a id="pole-2"></a>

## 🟡 Long pole 2: `pre-commit.yaml` ▸ `main` - 19s

_Runs concurrently behind `Windows` (34s); it becomes the gate only once every slower concurrent check drops below 19s._

```text
Where the job's ~19s goes - every step, slowest first; they run in sequence and roughly add up to the job:

   Run uv run --locked --no-default…  ██████████████████████       9s       ◀
   Run astral-sh/setup-uv@cec208311…  █████                        2s
   Set up job                         ██                           1s
   Run actions/checkout@de0fac2e450…  ██                           1s
   Run actions/setup-python@a309ff8…  ██                           1s
   Run actions/cache@668228422ae6a0…  ██                           1s
   …4 smaller steps (setup, cache, …  ██████████                  ~4s

(no log-level detector fired, but a **structural catalog pattern** matched this pole - see the **structural root-cause** below; the dominant step is the addressable lever.)
```

**📐 Structural root-cause - OPT75 · The long pole's time is one addressable step - speed it up or move it off the PR path - `main`** - risk **MEDIUM**

A measured **structural** lever on the critical path (it IS this pole, so it's not repeated in the off-path appendix). It carries a risk profile - review the guardrail and rollout before shipping:

- **What ci-speedup measured:** critical-path check `main` (19s): dominant step `Run uv run --locked --no-default-groups --group pre-commit pre-commit run --show-diff-on-failure --color=always --all-files + 1 more other step` (other, 58% of job `main`)
- **Guardrail:** carry the guardrail of the routed lever (e.g. OPT70's full-suite fallback if the dominant step is a test being scoped); never present the decomposition as free
- **Rollout:** the routed lever's rollout; re-measure the pole's p50 after the dominant step is attacked - the next-largest step becomes the target
- **Failure mode:** the dominant-step remedy ranges from LOW (cache an install) to HIGH (scope a test/build, inheriting OPT70) - the candidate carries the risk of whichever specific lever its dominant category routes to
- **Catalog (background + fix recipe):** https://github.com/starslingdev/skills/blob/2f048be/skills/ci-speedup/references/optimization-patterns.md#opt75--long-pole-optimize-or-relocate-the-dominant-step

#### 🤖 Prompt for your coding agent

```text
starslingdev/ci-speedup measured where the time goes below but does NOT prescribe the fix - a structural catalog pattern (OPT75) matched this pole (see the **structural root-cause** section above for the measured lever + its risk axis); the dominant step below is where that lever's time is spent.

REPO: pallets/flask (audited at commit 36e4a82)

THE GATE
- Workflow `pre-commit.yaml`, job `main`.
- Slowest check a typical PR waits on: P50 19s.

WHERE THE TIME GOES
- The job's time is dominated by the `Run uv run --locked --no-default-groups --group pre-commit pre-commit run --show-diff-on-failure --color=always --all-files + 1 more other step` step: ~11s (58% of the job wall), from the sampled per-step decomposition (no single-run timeline was captured for this job).

STRUCTURAL CATALOG PATTERN MATCHED
- A structural catalog pattern (OPT75) matched this pole - see the **structural root-cause** section above for the measured lever, its risk / guardrail / rollout, and the catalog fix recipe. The step above is the load-bearing one that lever targets; open its log (the Audit link) to see exactly what inside it the lever reshapes.

WHERE TO LOOK
- The `pre-commit.yaml` workflow definition for the dominant step, and the tool/config it invokes (build tool, test runner, or install) - that's where its time is spent.

DELIVER & VERIFY
- A change that cuts the dominant step's wall time without dropping coverage; re-measure the step on a PR run to confirm the reduction.
```


<a id="pole-3"></a>

## 🟡 Long pole 3: `tests.yaml` ▸ `PyPy` - 26s

**Rarely the merge gate - the actual slowest check a PR waits on, on only 0/20 sampled PRs.** Present on 18/20 PRs, but a slower concurrent check almost always gates ahead of it, so its 26s is throughput/cost, not merge-wait. Speeding it helps only the PRs where it IS the pole - it won't move typical merge-wait.

```text
Where the job's ~26s goes - every step, slowest first - each step's P50 is measured on its own, so they sum to ~35s vs the job's own 26s P50; read the bars as proportions, not an exact sum:

   Run uv run --locked --no-default…  ██████████████████████      17s       ◀
   Run astral-sh/setup-uv@fac544c07…  █████████████████           14s
   Run actions/checkout@df4cb1c069e…  ██                           2s
   Set up job                         █                            1s
   Post Run astral-sh/setup-uv@fac5…  █                            1s
   Run actions/setup-python@a309ff8…  █                            1s

(no log-level detector fired, but a **structural catalog pattern** matched this pole - see the **structural root-cause** below; the dominant step is the addressable lever.)
```

**📐 Structural root-cause - OPT75 · The long pole's time is one addressable step - speed it up or move it off the PR path - `PyPy`** - risk **MEDIUM**

A measured **structural** lever on the critical path (it IS this pole, so it's not repeated in the off-path appendix). It carries a risk profile - review the guardrail and rollout before shipping:

- **What ci-speedup measured:** critical-path check `PyPy` (26s): dominant step `Run uv run --locked --no-default-groups --group dev tox run + 1 more other step` (other, 87% of job `PyPy`)
- **Guardrail:** carry the guardrail of the routed lever (e.g. OPT70's full-suite fallback if the dominant step is a test being scoped); never present the decomposition as free
- **Rollout:** the routed lever's rollout; re-measure the pole's p50 after the dominant step is attacked - the next-largest step becomes the target
- **Failure mode:** the dominant-step remedy ranges from LOW (cache an install) to HIGH (scope a test/build, inheriting OPT70) - the candidate carries the risk of whichever specific lever its dominant category routes to
- **Catalog (background + fix recipe):** https://github.com/starslingdev/skills/blob/2f048be/skills/ci-speedup/references/optimization-patterns.md#opt75--long-pole-optimize-or-relocate-the-dominant-step

#### 🤖 Prompt for your coding agent

```text
starslingdev/ci-speedup measured where the time goes below but does NOT prescribe the fix - a structural catalog pattern (OPT75) matched this pole (see the **structural root-cause** section above for the measured lever + its risk axis); the dominant step below is where that lever's time is spent.

REPO: pallets/flask (audited at commit 36e4a82)

THE GATE
- Workflow `tests.yaml`, job `PyPy`.
- Rarely the merge pole - the actual slowest check a PR waits on, on only 0/20 sampled PRs (present on 18/20): P50 26s. A slower concurrent check usually gates ahead, so speeding it helps only the PRs where it IS the pole, not typical merge-wait.

WHERE THE TIME GOES
- The job's time is dominated by the `Run uv run --locked --no-default-groups --group dev tox run + 1 more other step` step: ~30s (87% of the job wall), from the sampled per-step decomposition (no single-run timeline was captured for this job).

STRUCTURAL CATALOG PATTERN MATCHED
- A structural catalog pattern (OPT75) matched this pole - see the **structural root-cause** section above for the measured lever, its risk / guardrail / rollout, and the catalog fix recipe. The step above is the load-bearing one that lever targets; open its log (the Audit link) to see exactly what inside it the lever reshapes.

WHERE TO LOOK
- The `tests.yaml` workflow definition for the dominant step, and the tool/config it invokes (build tool, test runner, or install) - that's where its time is spent.

DELIVER & VERIFY
- A change that cuts the dominant step's wall time without dropping coverage; re-measure the step on a PR run to confirm the reduction.
```


<a id="pole-4"></a>

## 🟡 Long pole 4: `tests.yaml` ▸ `3.14t` - 25s

**Rarely the merge gate - the actual slowest check a PR waits on, on only 0/20 sampled PRs.** Present on 18/20 PRs, but a slower concurrent check almost always gates ahead of it, so its 25s is throughput/cost, not merge-wait. Speeding it helps only the PRs where it IS the pole - it won't move typical merge-wait.

```text
Where the job's ~25s goes - every step, slowest first; they run in sequence and roughly add up to the job:

   Run actions/setup-python@a309ff8…  ██████████████████████      10s       ◀
   Run uv run --locked --no-default…  █████████████                6s
   Run astral-sh/setup-uv@fac544c07…  ███████                      3s
   Run actions/checkout@df4cb1c069e…  ████                         2s
   Set up job                         ██                           1s
   Post Run actions/setup-python@a3…  ██                           1s
   …1 smaller steps (setup, cache, …  ██                          ~1s

(no log-level detector fired, but a **structural catalog pattern** matched this pole - see the **structural root-cause** below; the dominant step is the addressable lever.)
```

**📐 Structural root-cause - OPT75 · The long pole's time is one addressable step - speed it up or move it off the PR path - `3.14t`** - risk **MEDIUM**

A measured **structural** lever on the critical path (it IS this pole, so it's not repeated in the off-path appendix). It carries a risk profile - review the guardrail and rollout before shipping:

- **What ci-speedup measured:** critical-path check `3.14t` (25s): dominant step `Run actions/setup-python@a309ff8b426b58ec0e2a45f0f869d46889d02405` (install, 42% of job `3.14t`)
- **Guardrail:** carry the guardrail of the routed lever (e.g. OPT70's full-suite fallback if the dominant step is a test being scoped); never present the decomposition as free
- **Rollout:** the routed lever's rollout; re-measure the pole's p50 after the dominant step is attacked - the next-largest step becomes the target
- **Failure mode:** the dominant-step remedy ranges from LOW (cache an install) to HIGH (scope a test/build, inheriting OPT70) - the candidate carries the risk of whichever specific lever its dominant category routes to
- **Catalog (background + fix recipe):** https://github.com/starslingdev/skills/blob/2f048be/skills/ci-speedup/references/optimization-patterns.md#opt75--long-pole-optimize-or-relocate-the-dominant-step

#### 🤖 Prompt for your coding agent

```text
starslingdev/ci-speedup measured where the time goes below but does NOT prescribe the fix - a structural catalog pattern (OPT75) matched this pole (see the **structural root-cause** section above for the measured lever + its risk axis); the dominant step below is where that lever's time is spent.

REPO: pallets/flask (audited at commit 36e4a82)

THE GATE
- Workflow `tests.yaml`, job `3.14t`.
- Rarely the merge pole - the actual slowest check a PR waits on, on only 0/20 sampled PRs (present on 18/20): P50 25s. A slower concurrent check usually gates ahead, so speeding it helps only the PRs where it IS the pole, not typical merge-wait.

WHERE THE TIME GOES
- The job's time is dominated by the `Run actions/setup-python@a309ff8b426b58ec0e2a45f0f869d46889d02405` step: ~10s (42% of the job wall), from the sampled per-step decomposition (no single-run timeline was captured for this job).

STRUCTURAL CATALOG PATTERN MATCHED
- A structural catalog pattern (OPT75) matched this pole - see the **structural root-cause** section above for the measured lever, its risk / guardrail / rollout, and the catalog fix recipe. The step above is the load-bearing one that lever targets; open its log (the Audit link) to see exactly what inside it the lever reshapes.

WHERE TO LOOK
- The `tests.yaml` workflow definition for the dominant step, and the tool/config it invokes (build tool, test runner, or install) - that's where its time is spent.

DELIVER & VERIFY
- A change that cuts the dominant step's wall time without dropping coverage; re-measure the step on a PR run to confirm the reduction.
```


<a id="pole-5"></a>

## 🟡 Long pole 5: `tests.yaml` ▸ `3.9` - 24s

**Rarely the merge gate - the actual slowest check a PR waits on, on only 0/20 sampled PRs.** Present on 4/20 PRs, but a slower concurrent check almost always gates ahead of it, so its 24s is throughput/cost, not merge-wait. Speeding it helps only the PRs where it IS the pole - it won't move typical merge-wait.

```text
Where the job's ~24s goes - every step, slowest first; they run in sequence and roughly add up to the job:

   Run actions/setup-python@a309ff8…  ██████████████████████      12s       ◀
   Run uv run --locked --no-default…  ██████████                   5s
   Run astral-sh/setup-uv@fac544c07…  █████                        2s
   Set up job                         ██                           1s
   Run actions/checkout@df4cb1c069e…  ██                           1s

(no log-level detector fired, but a **structural catalog pattern** matched this pole - see the **structural root-cause** below; the dominant step is the addressable lever.)
```

**📐 Structural root-cause - OPT75 · The long pole's time is one addressable step - speed it up or move it off the PR path - `3.9`** - risk **MEDIUM**

A measured **structural** lever on the critical path (it IS this pole, so it's not repeated in the off-path appendix). It carries a risk profile - review the guardrail and rollout before shipping:

- **What ci-speedup measured:** critical-path check `3.9` (24s): dominant step `Run actions/setup-python@a309ff8b426b58ec0e2a45f0f869d46889d02405` (install, 55% of job `3.9`)
- **Guardrail:** carry the guardrail of the routed lever (e.g. OPT70's full-suite fallback if the dominant step is a test being scoped); never present the decomposition as free
- **Rollout:** the routed lever's rollout; re-measure the pole's p50 after the dominant step is attacked - the next-largest step becomes the target
- **Failure mode:** the dominant-step remedy ranges from LOW (cache an install) to HIGH (scope a test/build, inheriting OPT70) - the candidate carries the risk of whichever specific lever its dominant category routes to
- **Catalog (background + fix recipe):** https://github.com/starslingdev/skills/blob/2f048be/skills/ci-speedup/references/optimization-patterns.md#opt75--long-pole-optimize-or-relocate-the-dominant-step

#### 🤖 Prompt for your coding agent

```text
starslingdev/ci-speedup measured where the time goes below but does NOT prescribe the fix - a structural catalog pattern (OPT75) matched this pole (see the **structural root-cause** section above for the measured lever + its risk axis); the dominant step below is where that lever's time is spent.

REPO: pallets/flask (audited at commit 36e4a82)

THE GATE
- Workflow `tests.yaml`, job `3.9`.
- Rarely the merge pole - the actual slowest check a PR waits on, on only 0/20 sampled PRs (present on 4/20): P50 24s. A slower concurrent check usually gates ahead, so speeding it helps only the PRs where it IS the pole, not typical merge-wait.

WHERE THE TIME GOES
- The job's time is dominated by the `Run actions/setup-python@a309ff8b426b58ec0e2a45f0f869d46889d02405` step: ~12s (55% of the job wall), from the sampled per-step decomposition (no single-run timeline was captured for this job).

STRUCTURAL CATALOG PATTERN MATCHED
- A structural catalog pattern (OPT75) matched this pole - see the **structural root-cause** section above for the measured lever, its risk / guardrail / rollout, and the catalog fix recipe. The step above is the load-bearing one that lever targets; open its log (the Audit link) to see exactly what inside it the lever reshapes.

WHERE TO LOOK
- The `tests.yaml` workflow definition for the dominant step, and the tool/config it invokes (build tool, test runner, or install) - that's where its time is spent.

DELIVER & VERIFY
- A change that cuts the dominant step's wall time without dropping coverage; re-measure the step on a PR run to confirm the reduction.
```


---

<a id="runner-minute-reductions"></a>

## Runner-minute reductions (wall-clock-neutral)

<!-- ci-speedup:runner-minute-spine -->
### Cost spine: where runner minutes go

All figures are runner-minutes; multiply by your runner's per-minute rate to get dollars.

| Workflow | Job | Runner | Event | Status | Attempt | Volume | Raw min/mo | Billable min/mo | Share |
| --- | --- | --- | --- | --- | --- | --- | ---: | ---: | ---: |
| .github/workflows/tests.yaml | Windows | windows-latest | all-events | success | latest | all-status | 26.647 | 42.846 | 8.200% |
| .github/workflows/pre-commit.yaml | main | ubuntu-latest | all-events | success | latest | all-status | 12.708 | 39.000 | 7.500% |
| .github/workflows/tests.yaml | PyPy | ubuntu-latest | all-events | success | latest | all-status | 20.480 | 37.000 | 7.100% |
| .github/workflows/tests.yaml | 3.14t | ubuntu-latest | all-events | success | latest | all-status | 15.352 | 37.000 | 7.100% |
| .github/workflows/tests.yaml | Mac | macos-latest | all-events | success | latest | all-status | 13.599 | 37.000 | 7.100% |
| .github/workflows/tests.yaml | typing | ubuntu-latest | all-events | success | latest | all-status | 12.950 | 37.000 | 7.100% |
| .github/workflows/tests.yaml | Minimum Versions | ubuntu-latest | all-events | success | latest | all-status | 11.879 | 37.000 | 7.100% |
| .github/workflows/tests.yaml | Development Versions | ubuntu-latest | all-events | success | latest | all-status | 11.684 | 37.000 | 7.100% |
| .github/workflows/tests.yaml | 3.14 | ubuntu-latest | all-events | success | latest | all-status | 10.581 | 37.000 | 7.100% |
| .github/workflows/tests.yaml | 3.13 | ubuntu-latest | all-events | success | latest | all-status | 10.288 | 37.000 | 7.100% |
| .github/workflows/tests.yaml | 3.12 | ubuntu-latest | all-events | success | latest | all-status | 9.899 | 37.000 | 7.100% |
| .github/workflows/tests.yaml | 3.10 | ubuntu-latest | all-events | success | latest | all-status | 9.315 | 37.000 | 7.100% |
| Total |  |  |  |  |  |  | 179.195 | 522.741 | 100.000% |
+7 more runner-minute rows hidden

> These findings cut wall-clock-neutral runner spend without touching your merge gate; each R-numbered finding carries a machine-derived proof it cannot slow a PR.
> **3 min/mo credited after de-overlap** (naive sum 3 min/mo; 1 neutral finding). All figures are runner-minutes; multiply by your runner's per-minute rate to get dollars.

<!-- ci-speedup:tier2-finding id=f4 pattern=OPT36 -->
<a id="r-1"></a>

## 🟢 Runner saving 1: `lock.yaml` - 3 min/mo

**The largest merge-safe runner-minute saving measured on this repo.**

| Workflow | Consecutive same-head_sha schedule runs | Mean compute/run | Credited runner-min/mo |
| --- | --- | --- | --- |
| `.github/workflows/lock.yaml` | 95 redundant run(s) in 5 group(s) | 0.1 job-min over 20 timed run(s) | ~3 |

_Schedule burn is counted only on event=schedule runs whose head_sha repeats consecutively, so the detector proves the workflow ran again without a code change. Basis: the count is from the all-status schedule slice; the per-run price is the mean of 20 successful schedule-event timed run(s). GUARDRAIL: confirm the current cadence is not an operational SLA before increasing the cron interval._

**💸 Bill root-cause - OPT36 · Cron Schedule Too Frequent** - risk **LOW**

- **What ci-speedup measured:** 95 scheduled run(s) in 5 consecutive same-head_sha group(s) re-ran without a code change in the sampled schedule slice (95% of 100 schedule run(s)); ~3 runner-min/mo of schedule-event compute (mean over 20 timed run(s); ×0.30 to the 30d volume (30 runs); 100-run recent slice (not a full 30d census)).
- **Why this can't slow your merge:** machine-derived proof: `non_pr_event` - `schedule` runs do not gate a PR merge (event=schedule subset only; consecutive same-head_sha schedule runs; schedule is not a developer PR/merge event).
- **Source block:** `runner_minute_spine` matched 1 row for `.github/workflows/lock.yaml`; current measured cost spine for those rows is 3.200 raw min/mo, 30.000 billable min/mo.
- **Guardrail:** Confirm the cron cadence is not an operational SLA; prefer widening the interval only for cleanup/triage/build jobs where delayed execution is acceptable.
- **Catalog (background + fix recipe):** https://github.com/starslingdev/skills/blob/2f048be/skills/ci-speedup/references/optimization-patterns.md#opt36--cron-schedule-too-frequent

#### 🤖 Prompt for your coding agent

```text
ci-speedup measured the pattern below but does NOT prescribe the fix -
investigate it in the repo and apply a safe change.

Pattern: OPT36 - Cron Schedule Too Frequent.
Where: lock.yaml.
What ci-speedup saw: 95 scheduled run(s) in 5 consecutive same-head_sha group(s) re-ran without a code change in the sampled schedule slice (95% of 100 schedule run(s)); ~3 runner-min/mo of schedule-event compute (mean over 20 timed run(s); ×0.30 to the 30d volume (30 runs); 100-run recent slice (not a full 30d census)).
Saving: 3 min/mo of runner capacity - a bill/capacity reduction, not a merge-wait cut. Neutrality certificate: `non_pr_event` - `schedule` runs do not gate a PR merge (event=schedule subset only; consecutive same-head_sha schedule runs; schedule is not a developer PR/merge event). GUARDRAIL: confirm the current cadence is not an operational SLA before increasing the cron interval.

Read the catalog entry (background, fix recipe, and guardrail):
  https://github.com/starslingdev/skills/blob/2f048be/skills/ci-speedup/references/optimization-patterns.md#opt36--cron-schedule-too-frequent

Do: confirm the pattern at each location above, recover the intent from git
history, and apply the catalog's fix recipe where it is safe. State the
failure mode and how you have guarded it before shipping.
```

---

<a id="also-noticed"></a>

## 🧹 Also noticed - residual hygiene

> These findings stay outside the wall-clock-neutral runner-minute section because they are modeled, uncertified, advisory-by-shape, missing source-spine backing, or below that section's measured admission gate. Most do **not** sit on the merge-gating critical path above, so fixing them removes little or no developer wall-clock - but they can still cut runner-minutes. **Expand any finding** for its locations, evidence, the catalog fix recipe, and a copy-paste agent prompt; exact per-occurrence lines + evidence also live in the findings JSON.

> ⚠️ _Approximate: computed across all workflows, but 1 capped workflow(s) still use the shallow 10-run job sample for finding/queue values; 1 runner-minute source workflow(s) still use a shallow 10-run cost-spine sample. Figures can shift run-to-run; re-run with `--shallow-runs 20` to confirm exact values._

<details>
<summary><strong>OPT32 - Missing `paths`/`paths-ignore` on Expensive Workflows</strong> · no bill saving · HIGH · 1 across 1 wf</summary>

**Where:** `publish.yaml:2` (build)
**Evidence:** workflow triggers on push but declares no `paths:`/`paths-ignore:` filter (the `on:` block below has no `paths:` key).
**Catalog (background + fix recipe):** https://github.com/starslingdev/skills/blob/2f048be/skills/ci-speedup/references/optimization-patterns.md#opt32--missing-pathspaths-ignore-on-expensive-workflows

#### 🤖 Prompt for your coding agent

```text
ci-speedup measured the pattern below but does NOT prescribe the fix -
investigate it in the repo and apply a safe change.

Pattern: OPT32 - Missing `paths`/`paths-ignore` on Expensive Workflows.
Where: publish.yaml:2 (build).
What ci-speedup saw: workflow triggers on push but declares no `paths:`/`paths-ignore:` filter (the `on:` block below has no `paths:` key).
Saving: no measured runner-min saving - off the merge-gating critical path, so ~0 developer wall-clock (a cloud-bill cut, not a merge-wait cut).

Read the catalog entry (background, fix recipe, and guardrail):
  https://github.com/starslingdev/skills/blob/2f048be/skills/ci-speedup/references/optimization-patterns.md#opt32--missing-pathspaths-ignore-on-expensive-workflows

CAVEAT - the required-status 'Pending' landmine: if ANY check this
workflow produces is a required status check, do NOT skip it via
paths:/branches: filters, [skip ci], or by removing/narrowing a trigger
event - a workflow that no longer fires leaves its
required check 'Pending' and the PR can never merge (official guidance:
do not use path/branch filtering on required workflows). The
documented-safe shape is a job-level `if:` - a skipped job reports
Success and satisfies the gate. The no-op twin-workflow trick (same
workflow AND job name, inverse filter) is a community-known workaround,
NOT in current GitHub docs. Treat required-status UNKNOWN as required:
if branch protection/rulesets are not readable, assume every check this
workflow produces may be required.

Do: confirm the pattern at each location above, recover the intent from git
history, and apply the catalog's fix recipe where it is safe. State the
failure mode and how you have guarded it before shipping.
```

</details>

<details>
<summary><strong>OPT33 - No Draft PR Gating on Expensive Jobs</strong> · no bill saving · MEDIUM · 1 across 1 wf</summary>

**Where:** `tests.yaml:13` (tests)
**Evidence:** expensive job `tests` (matrix) runs on every PR that changes the workflow's filtered `paths:` including drafts - no `if: github.event.pull_request.draft == false` gate
**Catalog (background + fix recipe):** https://github.com/starslingdev/skills/blob/2f048be/skills/ci-speedup/references/optimization-patterns.md#opt33--no-draft-pr-gating-on-expensive-jobs

#### 🤖 Prompt for your coding agent

```text
ci-speedup measured the pattern below but does NOT prescribe the fix -
investigate it in the repo and apply a safe change.

Pattern: OPT33 - No Draft PR Gating on Expensive Jobs.
Where: tests.yaml:13 (tests).
What ci-speedup saw: expensive job `tests` (matrix) runs on every PR that changes the workflow's filtered `paths:` including drafts - no `if: github.event.pull_request.draft == false` gate
Saving: no measured runner-min saving - off the merge-gating critical path, so ~0 developer wall-clock (a cloud-bill cut, not a merge-wait cut).

Read the catalog entry (background, fix recipe, and guardrail):
  https://github.com/starslingdev/skills/blob/2f048be/skills/ci-speedup/references/optimization-patterns.md#opt33--no-draft-pr-gating-on-expensive-jobs

CAVEAT - the required-status 'Pending' landmine: if ANY check this
workflow produces is a required status check, do NOT skip it via
paths:/branches: filters, [skip ci], or by removing/narrowing a trigger
event - a workflow that no longer fires leaves its
required check 'Pending' and the PR can never merge (official guidance:
do not use path/branch filtering on required workflows). The
documented-safe shape is a job-level `if:` - a skipped job reports
Success and satisfies the gate. The no-op twin-workflow trick (same
workflow AND job name, inverse filter) is a community-known workaround,
NOT in current GitHub docs. Treat required-status UNKNOWN as required:
if branch protection/rulesets are not readable, assume every check this
workflow produces may be required.

Do: confirm the pattern at each location above, recover the intent from git
history, and apply the catalog's fix recipe where it is safe. State the
failure mode and how you have guarded it before shipping.
```

</details>

## 🗄️ Data sources

> **Where this data comes from**
>
> - **Critical path + step P50:** the committed ci-speedup audit of `pallets/flask`, scanned **2026-07-24** - P50 over **47 runs / 152 jobs** across 5 workflows (latest runs at scan time).
> - **Data-collection cost:** **320 gh API call(s)** in ~42s - adaptive sampling - a 10-run shallow pass over every workflow, then 1 of 3 PR-gating pole candidate(s) deepened to 20 runs, plus 1 bill-pole workflow candidate(s) deepened to 20 runs for the runner-minute source block (the gate, drill-set, and floor are full-depth; other finding-level values may still rest on the shallow sample).
> - **Which checks gate (the critical-path ordering):** measured from **20/20 sampled PRs**.
> - ⚠️ **Required checks were unreadable** (no admin / branch protection 404), so 'gate' here means the **slowest check on a typical PR** (observed), not a *confirmed required* check. Slow checks that run on only a minority of PRs are shown as a footnote, not the headline.

| Source | Coverage | Used for |
| --- | --- | --- |
| ci-speedup static scan (skill commit `2f048be`, scripts tree `021bb07`) | All `.github/workflows/*.yml` under the analyzed tree (36e4a82) | Static pattern detection (OPT1-OPT69 catalog) |
| gh runs/jobs API (timestamps) | 47 runs / 152 jobs sampled | Critical-path + per-step P50 |
| job logs | 1 job log(s) sampled | Step internals + cross-run magnitude (deeper levels) |
| workflow YAML | 5 from the analyzed checkout | `on:` triggers, matrix/shard axes, job timeouts (detector inputs) |

**Data freshness.** Analyzer ran at `2026-07-24T22:57:35.743876+00:00`; workflow YAML is read from the analyzed tree at commit `36e4a82`. Timing and activity counts reflect the sampled runs over a rolling 30-day window at scan time. 320 gh API queries were made.

> _The runner-minute / cost-spine figures in this report keep the full sample by design (they size total compute, not the critical path), so they still include the earlier configuration; a duration- or structure-changing edit (e.g. a shard split) blends both layouts._

_The concurrent checks (the Contents critical path) are P50 across sampled PRs. The per-step timeline + the drill are **one representative run** - the one closest to the P50 time - so they are absolute for that run, not P50. The **categorical cause** is stable across runs; where a **Cross-run check** is shown it gives the magnitude's median + range across several runs, so the single run's number isn't taken on faith. Per-step bars are scaled within each drill._

_The drill bars are plain-English labels for what's in the job log (e.g. a `DB migrations` bar is logged as `Total Migration Time:`). To verify any number, follow the pole's **🔗 Audit** link to the gating step, expand it, and search (Ctrl-F) for the verbatim strings the Audit line lists - GitHub anchors to the step, not an exact log line._

---

Generated by [StarSling](https://starsling.dev) 💫
