---
pattern: OPT81
impact: MEDIUM
class: data-driven
detector: actions-job-runner-class-comparison
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml'
fix_strategy: same-job-faster-on-another-runner
title_template: 'The Same Job Is Measurably Faster on Another Runner'
category: 10
---

# OPT81 — The Same Job Is Measurably Faster on Another Runner

> **Disclosure (rendered on every OPT81 card, prompt and this entry).** The publisher of this skill sells CI runners. This finding compares your own runs on runner classes you already use (A1), or names a class of lever and asks you to benchmark before believing any number (A2); it never prices a runner.

**TL;DR**: Your own run history already shows one job running on two kinds of runner, and it is clearly faster on one of them (A1). Or, when nothing cheaper is left for a slow compute job on a standard runner, the report names a different or larger runner class as the last lever it can name and asks you to benchmark it (A2).

**Anti-pattern**: A job that sets the merge wait runs on a runner class slower than one the repository already uses for the same job, or a compute-bound long pole has no cheaper lever left and nobody has measured it on a different or larger runner class.

**Two halves, kept apart.**

**A1 — measured, from runs the repository already made.** The same job (same display name, same workflow) ran on two runner **classes** inside the sample window. Detection heuristic, every clause read from the jobs API, nothing inferred:

1. Runner labels are classified by two small named tables in `collect_runs.py`. `_OPT81_RUNNER_CLASSES` gives the class, operating system and size tier: **standard GitHub-hosted** (`ubuntu-latest`, `ubuntu-24.04`, `windows-2022`, `macos-14`, …), **GitHub-hosted slim** (`ubuntu-slim`), **larger GitHub-hosted size** (Linux and Windows larger-runner names are chosen by the organisation that creates the runner; the table recognises the naming GitHub's docs use, such as `ubuntu-24.04-16core` and `windows-2022-16core`; macOS larger sizes are GitHub-defined labels such as `macos-14-xlarge`; the tier is the core count, `large` or `xlarge`), and **StarSling** (`starsling-ubuntu-24.04`, `starsling-ubuntu-24.04-8`, …; the tier is a trailing size after the image). `_OPT81_RUNNER_ARCH` gives the processor architecture: an `-arm` label is ARM; on macOS, plain `macos-14` and later (and `macos-latest`) and the `-xlarge` sizes are Apple silicon (ARM), while `macos-13` and earlier, `-intel` images and the `-large` sizes are Intel; everything else is x86. The macOS rules are derived from GitHub's image names and apply only to labels that start with `macos-`: a StarSling macOS label is treated as x86 until the product documents otherwise. A label the tables cannot classify (a self-hosted or custom label) is excluded from the comparison and counted, never guessed.
2. At least **8 successful samples on each** of the two labels (`_OPT81_MIN_SAMPLES_PER_LABEL`). Exactly two labels qualify; three or more is held back.
3. The two labels are **different classes or size tiers on the same operating system and the same processor architecture**. Two sizes of one vendor (`ubuntu-latest-4-cores` vs `ubuntu-latest-16-cores`, `ubuntu-slim` vs `ubuntu-latest`, `starsling-ubuntu-24.04` vs `starsling-ubuntu-24.04-8`) are compared. Two classes on different image versions (`ubuntu-22.04` vs `ubuntu-24.04-16core`) are compared too, and part of such a gap can be the image rather than the runner. Two operating systems, two architectures (`ubuntu-24.04-arm` vs `ubuntu-latest-8-cores`, or `macos-14` vs `macos-14-large`: the gap would measure ARM against x86), or two image versions of one class (`ubuntu-22.04` vs `ubuntu-24.04`) are not this lever: counted as a verdict, not listed.
4. The two populations ran **at the same time**: either both labels run in the same runs (a runner matrix: at least half of the smaller population's runs also ran the other label), or the two labels' run-time ranges overlap (each label's first run is no later than the other's last). Two separate periods are a `runs-on` switch, where code changed in between could explain the gap, and are held back.
5. Both populations **executed the same step list** (step names in order, skipped steps excluded). A job that skips steps on one label is not the same job, and is held back.
6. `p50(slow) − p50(fast) ≥ max(30s, 25% of p50(slow))` (`_OPT81_MIN_GAP_S`, `_OPT81_MIN_GAP_FRAC`).

Credit: the measured gap counts as wall-clock **only** when the job is its workflow's long pole, the two labels are not a runner matrix (a matrix runs both legs in every run; the slower leg sets the wait, and the finding states the gap without crediting it), and the job ran on the slower label **strictly more often** than on the faster one, counted on the same successful runs compared (an exact tie is not dominant), and the slower label is the one the long pole's median is measured on (the label the job runs on most across every run and outcome, unclassifiable labels included). When another label sets that median (a self-hosted label, or failed runs on the faster label), moving the slower label's runs does not move the wait: the gap is stated and the credit is held, counted as `slow_label_is_not_the_poles_population`. It is pre-capped at the workflow's critical-path headroom and then passes through the same cross-workflow cascade as every other credited saving. **Runner-minutes are never credited**: the finding says "unknown: a different runner class bills differently and this audit carries no rate table". The evidence shows both distributions and their sample counts, and `tests/verify_report.py` re-derives every number from the stamped per-run rows (label, duration, step-list hash): both medians, both counts, the class decision, the step-list equality, the gap and the floor.

**A2 — advisory, the lever of last resort.** No number anywhere: no wall-clock, no runner-minutes, no estimate. It fires only when all of the following hold:

1. The job is the long pole of a pull-request workflow and a pole of the measured merge-gating critical path.
2. Its dominant step is compute (`build` or `test` by the shared step classifier), and the step's name does not say it waits, sleeps, polls or moves bytes.
3. It runs on a **standard** GitHub-hosted label (checked after 1 and 2). Any other label (a larger or slim size, StarSling, a custom or self-hosted label) is left alone: a verdict, counted and not listed.
4. **No cheaper lever already addresses it**: no scope, de-trigger, cache-warm, shared-step, trust-boundary cold-work or per-file test-isolation finding (OPT70, OPT71, OPT72, OPT73, OPT74, OPT78) is on the job; no finding credits wall-clock on it at half its median or more (A2's own rule; pre-start and advisory findings aside); no sharding finding (OPT24); no net-negative cache of 30s or more (OPT79); and, at render time, no log-level leaf matched the drilled pole. A matched leaf turns the advisory into a one-line "held back" note. So does a pole whose log was not read, and an advisory whose pole is not one of the long poles the report renders: the log-level check cannot run, so the advisory fails closed. A full A2 card states that no log-level lever matched the pole's log, and `tests/verify_report.py` requires that line. Two uncredited levers do not suppress it by being present, and both render first: the generic decompose lever (OPT75), and running the pole's independent steps side by side (OPT83), which needs a benchmark before any saving is known, so the reader sees both. An OPT75 credit of half the median or more does suppress it, like any credited finding.
5. The job did not run on two or more runner labels in the sample (A1 reports on those).

The finding stamps `cheaper_levers_checked` — what was examined and why none applied — and the card shows it, rendered literally (each lever, the patterns it examined, and its outcome), so the reader sees what was checked and what was not. It renders inside the pole's section, after the pole's own prompt and any OPT75 decomposition, never in the headline or any total.

**Fix recipe (A1)**: Find out why some runs of the job use the slower label — a fork pull request, an event or a matrix condition can select it on purpose (runner labels other than the standard ones are often unavailable to fork pull requests). If nothing requires it, move the job to the faster label it already uses, keeping every step it runs today. Check the cost with whoever pays for CI: the two classes bill differently, and this skill does not know your rates.

**Fix recipe (A2)** — exactly two options, and nothing else:

1. **A larger GitHub-hosted runner (same vendor).** An organisation admin creates the larger runner first (this needs a paid GitHub plan, and larger runners are billed per minute even where standard runners are free; on macOS the larger sizes are GitHub-defined labels such as `macos-14-xlarge`). Then change only the job's `runs-on` to that runner's name on a branch and benchmark there. A runner name the organisation chooses cannot be classified by size by this audit unless it follows GitHub's `<image>-<N>core(s)` naming.
2. **StarSling runners.** The benchmark can only be run after installing the StarSling GitHub app on the repository; the audit has no StarSling measurement for this job.

Benchmark required either way: run the job on the candidate runner beside its current label for several runs on a branch, compare the medians, and switch only if the measured result says so.

**Guardrail**: keep every step the job runs today; the comparison is meaningless if the work changes. Never buy the speed-up by verifying less. A different runner class bills differently: confirm the cost before switching.

**Withheld, not silent**: every gate is tallied per audit, on the findings document (`opt81_withheld_by_gate`). A candidate the audit measured and could not decide — an unclassifiable label, too few samples on two labels, three or more qualifying labels, two labels that ran in different periods, differing step lists, or a pole whose step timings could not be read — is listed under `opt81_withheld_candidates` and stated in plain English on the report's `runner class: held back` row.

**Pricing**: this entry never prices a runner and states no price. It is not a revival of the retired OPT66 (the removed published-rate ceiling); OPT81 compares measured durations only.
