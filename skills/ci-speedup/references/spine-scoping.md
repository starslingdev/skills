# Spine scoping — which checks form the measured critical path

How `collect_runs.py` decides *which* checks the wall-clock spine and the
headline pole are built from. This is emitted deterministically upstream (the
pole is emitted already-scoped in `collect_runs.py`, never patched downstream);
a normal run does not need to re-derive any of it — read it only for depth on
why a given check did or didn't headline.

## Contents

- [Required-scoped spine](#required-scoped-spine) — restrict to merge-blocking checks
- [PR-floor fallback (external-gate repos)](#pr-floor-fallback-external-gate-repos)
- [Required-checks reading is branch- and enforcement-scoped](#required-checks-reading-is-branch--and-enforcement-scoped)
- [Pole `provenance` (cross-repo seam)](#pole-provenance-cross-repo-seam)
- [One-path poles are demoted, never the headline](#one-path-poles-are-demoted-never-the-headline)
- [Default-branch pushes use a separate lane](#default-branch-pushes-use-a-separate-lane)

## Required-scoped spine

The title is *"why is the merge slow?"*, so when the data pass resolves a real,
complete, non-empty **required-check** set (branch protection / rulesets —
already fetched, read `required_checks`), the spine and headline pole are
restricted to the **merge-blocking** checks by **`needs:`-reachability** over the
workflow job graph: each required check **union** every job the required work
transitively `needs:` (a required `… / Merge Test Reports` rollup pulls in the
`… / UNIT Test (Shard N)` legs it merges; a required aggregator pulls in the test
jobs it `needs:`). A check that is neither required nor needed by required work is
**dropped from the spine** (recorded in `dropped_non_required_checks`) — it gates
zero merges, so speeding it removes zero time-to-merge and it must never headline,
even if it is the single slowest check. (Reach, not file co-residence: an
independent slow job sharing a required workflow file but that no required job
`needs:` is dropped.) This is the skill's job: the pole is emitted required-scoped
in `collect_runs.py`, never patched downstream. Inert on a partial/unreadable
required read (absent ≠ not-required) and never empties the spine.

Reusable-workflow reachability belongs to an invocation. A caller-prefixed nested
check inherits a required caller's gate through that local call path. A bare check
from the reusable workflow's own `pull_request` run does not: it has no caller path,
so sharing the same leaf job cannot make the standalone run merge-blocking.

## PR-floor fallback (external-gate repos)

When every branch-protection required check is external/managed (CLA bot,
enterprise CI, label-gated e2e, mergeability gate) — none mapping to a workflow
file — the spine auto-falls-back to the measured **PR-floor** (the file-backed
workflows a normal PR runs, ranked by long pole), flagged
`gate_kind = "pr_floor_fallback"` and rendered with a demotion banner.

## Required-checks reading is branch- and enforcement-scoped

A ruleset's required checks only count for the branch we score when the ruleset
is **active** (not an `evaluate` dry-run or `disabled`) **and** its `conditions`
target that branch (`~ALL`/`~DEFAULT_BRANCH`, the exact ref, or a glob that covers
it) — so a `release/*`-scoped or dry-run ruleset never makes a check falsely
"required" for `main` (`_fetch_required_checks`, `collect_runs.py`).

## Pole `provenance` (cross-repo seam)

`pr_critical_path.provenance` records *how* we know the headlined pole is the thing
a merge waits on — `required_scoped` (the spine was narrowed to confirmed
merge-blocking checks), `pr_floor_fallback` (the PR-floor case above), or
`unresolved` (required checks were unreadable, so the pole is a frequency best-guess
we can't confirm gates a merge). It is **additive** and consumed by the downstream
ci-harness auto-fixer, which HALTs on `unresolved` rather than optimizing a pole it
can't trust.

## One-path poles are demoted, never the headline

The headline must be a check that ACTUALLY gates the merge, so the spine is ranked
by **pole frequency** — how often a check is the slowest job a PR waits on — in two
tiers: checks that are the actual pole on ≥ 2 sampled PRs first (by p50), then
one-path outliers (the actual pole on fewer, a label-gated benchmark or a single
path-conditional run). A slow check that ran on 1/20 PRs never headlines just
because it's the slowest job when it runs; and — crucially — a lightweight check
that runs on *every* PR but is **never** the slowest (a heavier sibling always
co-runs) is not the gate either and is demoted, even though it's ever-present. That
second case is the bug this replaced a "present on a majority" rule to fix: on a
path-partitioned monorepo every heavy suite runs on a minority of PRs, so a majority
cutoff buried them all and crowned an always-present check that was the actual
bottleneck on 0 of 20 PRs. Demoted poles are still surfaced, labelled by why they
were demoted: *rarely the merge gate — the actual slowest check a PR waits on, on
only N/npop sampled PRs* (pre-`pole_n` findings fall back to the legacy presence
wording, *opt-in / rare — ran on only N/M sampled PRs*). A required check is exempt.
The signal is per-PR (`pole_n` per check); ranking happens in `collect_runs`, so the
headline, drilled poles, structural findings, and the data-pass summary all agree,
and `verify_report.check_headline_pole_actually_gates` FAILs any report whose
headline gates too few PRs while a genuine recurring gate was passed over.

## Default-branch pushes use a separate lane

Workflows triggered by a `push` to the default branch run after the merge. Their
per-commit wall clock is reported in the **After merge, not a merge gate**
section, with its own p50, p90 and job floor. Those values never enter the
merge-gate Bottom line, headline, Tier 1 ranking or scores. Collection reuses
the all-status workflow pages and bounds the cohort to the ten most recent
pushed commits; the shared commit-wide collector supplies their facts. When no
eligible completed push is sampled, the section says so explicitly.
