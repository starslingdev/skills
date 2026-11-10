---
pattern: OPT103
impact: MEDIUM
class: data-driven
detector: log-leaf
risk: MEDIUM
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml,lighthouserc*,.lighthouserc*'
fix_strategy: lighthouse-audit-count
title_template: 'Lighthouse CI audits every URL N times per pass, one after another'
category: 11
---

# OPT103 — Lighthouse CI Audits Every URL N Times Per Pass, One After Another

**TL;DR**: A merge-gating job spends most of its wall time inside Lighthouse CI's
collect phase. Every configured URL is audited `numberOfRuns` times, and each
run is a complete page load plus the full audit. The runs go one after another,
and a job with one pass per device preset pays all of it again per pass. The
audit count is the product passes x URLs x runs. The report states that
product, measured from the log, and hands the owner three dials: runs per URL,
the URL list, and where the passes run. It credits no saving.

**Scope: Lighthouse CI only** (`@lhci/cli` through `lhci autorun` or
`lhci collect`, and the actions that drive the same collector). Other audit
tools print other shapes and stay out of scope.

**Anti-pattern**: A `lighthouserc` config lists every page of the site and keeps
or raises the default of 3 runs per URL. The workflow then runs the collector
once per device preset, as consecutive steps. The budget check that was meant
to take a minute now takes ten. With 4 URLs, 5 runs and 2 passes the job runs
40 audits, and 36 of them audit a page it has already audited.

```
before:  job: [ mobile pass: 4 URLs x 5 runs ] -> [ desktop pass: 4 URLs x 5 runs ]   (40 audits in a row)
after:   leg mobile:  4 URLs x N runs   |  separate runners, the same assertions,
         leg desktop: 4 URLs x N runs   |  N kept at what the assertions need
```

**Detection heuristic** (the `lighthouse-serial-audits` leaf in
`blocking_path.py`, run over the drilled job's captured log, see ARCHITECTURE
§12.3). Everything is read from the log:

1. The log holds at least one completed collect pass. A pass is a block of
   `Running Lighthouse N time(s) on <url>` lines, one per configured URL,
   closed by `Done running Lighthouse!`. A block with no closing line (a
   cancelled or crashed collect) is not counted.
2. Summing N over every URL of every completed pass gives at least 20 audits.
   lhci's default of 3 runs over a couple of URLs is a smoke audit and stays
   silent.
3. The sum is at least two audits per distinct URL, so the count is a
   multiplier from repeat runs or repeat passes. One run over many URLs is a
   different lever (the URL list) and stays silent.
4. The job's measured dominant step is the Lighthouse step: its name carries
   `lighthouse` or `lhci`. Otherwise the leaf is shown as a labelled secondary
   observation and never as the job's cause. The step classifier bins those
   names as `scan`, so Lighthouse time counts as the job's payload.

The magnitude is the share of audits beyond the first per distinct URL: the
repeat runs and repeat passes. It follows from the config, so it is stable from
run to run. The evidence is verbatim lhci output: each pass's first
`Running Lighthouse` line and its
`Checking assertions against U URL(s), M total run(s)` line, which states the
pass's audit count in the tool's own words. A pass is
named after the `--config` or preset flag of its command when the log pairs
one command with one pass.

**Fix recipe** (the report hands off, it does not prescribe):

1. Read the config history first (`git log -p` on `lighthouserc*` and the
   workflow): why these runs, URLs and presets. A deliberate choice is a policy
   change for the owner.
2. Runs per URL. lhci's assertions aggregate across the runs of a URL
   (`aggregationMethod`, set per assertion: `median`, `optimistic`,
   `pessimistic` or `median-run`). Keep N at what those assertions need for a
   stable verdict, and compare old and new medians side by side before
   lowering it. Dropping to 1 trades speed for a flakier or falsely green
   budget.
3. The URL list. Audit one representative page per template, and keep every
   URL that carries a budget.
4. Where the passes run. The device presets and URL shards are independent, so
   run them on separate runners (a matrix leg per preset). Never run two
   Lighthouse passes at the same time on one runner. Concurrent audits
   compete for the same cores, which skews the scores the budget gates on, and
   each collect clears the shared `.lighthouseci/` report folder when it
   starts. OPT83 holds back two Lighthouse CI steps in one tree for that
   reason.

**Required-checks caveat**: a split into matrix legs creates new check names.
If the Lighthouse job is a required status check, add the new jobs to branch
protection as required checks (or the ruleset equivalent). Otherwise the
split-out audits silently stop gating merges while everything stays green.

**Sizing: uncredited.** The skill measures the audit count and the repeat
share, not a saving. How far N can drop is the owner's measurement decision, a
shorter URL list is a coverage decision, and a runner split moves wall time off
the job without removing it. Each added leg also repeats checkout, install and
build, so runner minutes go up while the wait goes down.

**Risk: MEDIUM.** Fewer runs or fewer URLs weaken a performance budget's
signal: a flakier gate, or a regression that slips through. Keep every
assertion gating, and check that a deliberately regressed page still fails
after the change.

**References**: [Lighthouse CI configuration](https://googlechrome.github.io/lighthouse-ci/docs/configuration.html)
(`collect.numberOfRuns`, `collect.url`, `settings.preset`, `aggregationMethod`).
[Lighthouse performance scoring](https://developer.chrome.com/docs/lighthouse/performance/performance-scoring)
(why scores fluctuate between runs).
