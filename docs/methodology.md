# How ci-speedup measures the numbers

This is the short, public-reader version of the measurement model. The full
references ship inside the skill and go one level deeper:

- [`skills/ci-speedup/references/wall-clock-methodology.md`](../skills/ci-speedup/references/wall-clock-methodology.md)
  — the critical-path / long-pole model (the ranking axis).
- [`skills/ci-speedup/references/savings-methodology.md`](../skills/ci-speedup/references/savings-methodology.md)
  — the two-axis sizing rules (wall-clock vs runner-minutes). Those two axes
  are the whole denomination: storage is deliberately out of scope, so nothing
  here is sized in gigabytes and no saving claimed reduces a storage charge.

## Measured, not estimated

Every timing figure comes from your repository's **real CI run history**, read
over the GitHub API. For each job the skill computes a **P50 (median)** and a
**P95 (tail)** over a sampled window of recent runs — never a guess, and never a
single lucky run. P50 is what a typical PR waits; P95 is the bad-day tail, which
is often the number worth fixing.

## The ranking axis is developer wall-clock, on the merge gate

The report ranks findings by **developer wall-clock wait** — how long a pull
request waits for CI to go green — not by the cloud bill. These two produce a
*different order*, and optimizing the bill can make developers wait longer, so
they are kept separate.

Wall-clock is **not** the sum of all job-seconds. Jobs run in parallel, so the
wait is driven by the **long pole** — the single slowest job in a fan-out —
plus the serial glue around it (entry gates, aggregators, scheduling overhead):

```
wall-clock  ≈  entry gate  +  max(parallel jobs)  +  joiner  +  scheduling overhead
```

The skill **validates this model against a real run** before quoting any delta:
it compares the summed critical path (a `needs:`-chain sum through the job graph)
against the run's observed wall-clock. If they diverge, the jobs aren't actually
parallel and the critical path is re-derived from the real dependency graph
before anything is sized. And it scopes the "merge gate" to the checks that
**actually block the merge** (from branch protection / rulesets), so a big but
non-blocking job never headlines a report about *why the merge is slow*.

One consequence worth knowing: cutting the long pole only helps until it hits the
**cluster floor** — the next-slowest job. Below that floor a change saves the
bill but zero developer wait. The report says which is which.

## What the numbers are measured from

Two GitHub data sources, each covering the other's blind spot:

1. **Per-commit check-runs** (`commits/{sha}/check-runs` on sampled PRs) are the
   ground truth for **what a PR actually waits on**. A merge wait is the slowest
   of *all* checks racing concurrently across every workflow at once — so no
   single workflow run's duration can represent it. Check-runs also include
   checks with **no workflow file at all** (CodeQL default setup, third-party
   app checks), which are frequently the true long pole and invisible to any
   scan of the repo's workflow YAML or job listings.
2. **Sampled job timings** (`actions/runs/{id}/jobs` from real runs of each
   workflow) are the measured durations. A raw check-run clock runs from the
   check's *creation* to completion, so it silently absorbs queue time and
   re-run inflation; the sampled job p50s **cap and de-inflate** those clocks
   before anything is ranked. Job data is also what decomposes the long pole
   into steps (the drill-downs) and prices runner minutes.

Neither source alone survives scrutiny: job timings can't see fileless checks or
tell you which checks gate a *typical* PR (that needs presence across the
sampled-PR population), and check-run clocks are inflatable. The report crowns
from the check-run population, capped by the job measurements, and the verify
gate re-derives its checks from the same stamped fields so the two can't drift.

One deliberate exclusion: a fileless/managed status check whose span can't be
grounded in any sampled job (a bot gate, a label gate, an external app) measures
**how long the gate sat open across the PR's lifetime — not GitHub-Actions
compute this skill can measure or optimize**. A label that sat open for days is
not an Actions wait. Such checks never crown the headline; the report discloses
the slowest one separately, labelled for what it is.

## Measured vs modeled, labelled

Numbers derived directly from sampled runs are **measured**. Projections — e.g.
"after stacking these fixes the P50 would drop to X" — are **modeled** and
labelled as such, built from measured step durations rather than post-fix
measurements. The report never lets a modeled projection masquerade as an
observed result.

## Adaptive sampling keeps it frugal

The data pass is deliberately cheap on API calls. It samples every workflow
shallowly first, then **deepens only the top pole candidates** to full depth — so
the merge gate, the long poles, and the cluster floor are measured exactly, while
off-path hygiene figures are approximate and flagged as such. This is why a
typical audit is a couple of minutes and a few hundred API calls rather than
thousands.

## It diagnoses; it does not prescribe

The catalog detection and the run-history measurement are accurate. **Fixes are
where a generic tool goes wrong** — it can't see a file's intent, the real logs,
or whether a "waste" is load-bearing (a "redundant" build may back a correctness
gate; a check on every push may keep triage fresh). So the skill stops at the
measured diagnosis and hands each finding to *your* coding agent as a
ready-to-paste prompt that includes the root cause and instructs the agent to
read the file's git history and intent before changing anything. Measured
diagnosis from the tool; the fix from an agent that can see the code.
