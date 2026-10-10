# Adversarial review rubric (ci-speedup reports)

Before a worked-example report is trusted, hostile, independent subagents
re-derive every finding against the **real repo clone** (assume each finding is
WRONG until the repo proves it right). This rubric is the contract for those
reviews. It exists because earlier reviews kept checking only "is the claim
true / does the fix apply" and missed deeper failures — e.g. a failure-rate
"finding" whose only remedy is "go fix your flaky test" was ranked #1 with a
fabricated savings number and instance-link evidence that couldn't show the
rate. The dimensions below are adapted from Anthropic's own review tooling
(`pr-review-toolkit` code-reviewer / pr-test-analyzer, the `code-review` skill)
and evals guidance (rubric specificity, false-positive mitigation, inter-judge
agreement).

Give these to each review subagent verbatim. For EVERY finding (ranked,
advisory, and manual-review), answer each as an adversarial question; a single
"no" is a defect.

## Contents

- **Catalog-finding dimensions** (this first section): the adversarial questions
  every finding must survive — root cause, evidence, fix applicability, ranking,
  false-positive, savings honesty.
- **Recompute-from-source verification** (below): the numeric harness for the
  blocking-path report — Scope 1 critical path/headline/floors, Scope 2 gating
  pole drill, Scope 3 second pole drill, Scope 4 non-spine numbers, Scope 5 LLM
  gap-fill grounding.

1. **Actionability — is the remedy something ci-speedup can PRODUCE?** Does the
   fix reduce to a concrete config/YAML change the tool emits, or to "go fix
   your test / debug your flaky code" — a change in a domain the tool doesn't
   own? If the latter, it is NOT a ranked optimization: it must be advisory
   (reliability note), never carry a savings number, never sit in the ranked
   list. (Catches OPT48-as-#1.)

2. **Evidence verifies the HEADLINE claim, not instances.** If the claim is an
   aggregate (a rate, a count, "runs N times"), does the evidence show that
   aggregate — a dashboard/metrics view, a total, a percentile, or a query the
   reader can re-run — or only a few cherry-picked instances that force the
   reader to aggregate by hand? Linking individual failed runs does NOT prove a
   failure RATE. (Catches OPT48 evidence.)

3. **Sizing is causal and physically possible.** Is the saving caused by THIS
   fix alone, or co-attributed to a change another finding also claims? Does any
   per-row wall-clock exceed the run's critical path? Are overlapping findings
   (skip-the-same-run, dedupe-the-same-setup) summed in the headline? The total
   must be de-overlapped or labelled a non-additive ceiling.

4. **Completeness.** Is it a class (N occurrences) reported with all instances
   and a fix that scales, or one cherry-picked instance of a wider pattern?

5. **Severity/ranking calibration.** If the user ignores this for 6 months, what
   actually happens? A #1-ranked finding must be genuinely frequent AND
   impactful — not a cosmetic or rare one inflated to the top. A 0-runs/dormant
   workflow can't carry a measured wall-clock claim.

6. **Fix applies to THIS repo verbatim & is safe.** Do the quoted "before"
   lines EXIST in the real file (including composite actions `./.github/actions/*`
   and called workflows `uses: ./.github/workflows/*`, or the same paths behind
   the self-repository prefix `$/` — read those, the real
   edit site often lives there)? Would it break a required check / release /
   git-history step / matrix fan-in? Is the premise true (e.g. is the cache it
   says to add ALREADY present)?

7. **Plain-English TL;DR is accurate and clear** — a developer grasps what's
   wrong and why it matters at a glance, with no over/under-statement.

8. **Independent agreement.** These reviews run as ≥2 independent passes that do
   not see each other's output; a finding only one pass flags (or that no pass
   would defend) is a signal to cut or escalate, not to keep.

Output per finding: `VERDICT: KEEP | NEEDS-FIX | CUT` + the specific failing
dimension(s) with real file:line evidence. Then report-level: total honesty,
bucketing (ranked vs advisory vs manual-review), and any missed findings.

## Recompute-from-source verification (the blocking-path report)

The rubric above is for catalog *findings*. The **blocking-path report**
(`blocking_path.py`, the measured-spine drill) is mostly *numbers*, so it needs a
second harness: independent subagents that **recompute every rendered figure from
the raw source** and try to break it. This caught real defects (a migration share
computed by averaging the WRONG files; an incoherent floor note on a non-gating
pole) that string/invariant checks (`verify_report.py`) can't — `verify_report`
checks shape/anchors/no-dead-ends, not whether a number is arithmetically right.

**How to run it.** Fan out one subagent per scope below (they must NOT trust the
report — recompute from source and flag any mismatch / unsound method). Sources:
the report `.md`, its `findings.json`, the `*-timeline.json` / `*-mag.json`
sidecars, the captured job logs, and `gh` (authenticated) to re-fetch other runs.
When parsing a raw GitHub job log, strip ANSI (`\x1b\[[0-9;]*m`) and the leading
`<timestamp>Z ` per line. **Disambiguate jobs by EXACT name** (`prisma-adapter
Integration Test` is a substring of `kysely-prisma-adapter Integration Test`).

- **Scope 1 - critical path + headline + floors.** Recompute every Level-1 check
  P50 from `pr_critical_path`; the "biggest measured win" and each pole's "what a
  change can buy" (a non-gating pole - one behind a slower concurrent check -
  buys ~0 and must emit NO floor note); gate frequencies from `populations`; the
  metadata table (commit, runs window = scanned-30d, counts, gate sample).
- **Scope 2 - the gating pole's drill (load-bearing).** From the representative
  run's raw log: job wall, dominant step + %, the parallel sub-units + their share
  (of the MAX = the wait), and the bottom split. For Prisma specifically: the
  migration figure must be the SLOWEST file's OWN per-file `Total Migration Time`
  (the max in that file's log section = sum of its per-group re-runs), NOT an
  average across engines. Then independently recompute the **cross-run check** by
  refetching each sampled run's job log and confirming each per-run value + that
  any outlier is real, not an artifact.
- **Scope 3 - the second pole's drill.** Same, plus: where values are SUMMED
  across worker threads (vitest transform/import), they exceed wall, so the report
  scales them to the step wall by share - confirm the % is the honest number and
  the scaled seconds are labelled as apportioned, not measured. Check the
  per-package denominator (MAX when packages run concurrently under turbo).
- **Scope 4 - residual non-spine numbers.** "Also noticed" appendix is now the
  residual surface, not the primary runner-minute surface. For each per-pattern
  row, sum only the non-advisory/non-structural/non-promoted occurrences;
  promoted Tier-2 findings with the same OPT-id must be excluded and the appendix
  row must carry the cross-reference note. Check severity, "N across M wf",
  file:line, advisory + structural + pre-start wait exclusion, deep-link
  `fix_recipe_anchor`, and no fabricated OPT-ids.
- **Scope 4b - Runner-minute reductions (Tier 2).** For each R-numbered row,
  re-derive section membership from `findings.json`: `sizing_basis == "measured"`
  and a `tier2_neutrality` certificate. Recompute the certificate class/margin
  where applicable, confirm `wall_clock_p50_s` is 0/None, and ensure the subject
  is not also framed as a long pole. Recompute the section lead total from raw
  credited minutes after the Tier-2 de-overlap pass (and compare the rendered
  naive sum). All figures are runner-minutes; the report leaves the per-minute
  rate to the reader — any rate-derived `$N`/USD amount rendered in the R-rows,
  section lead, cost-spine table, or TOC is VERDICT WRONG. The visible R-row list
  can be capped, but the section lead must still total every eligible stamped
  row.
  Any modeled finding promoted into this section is VERDICT WRONG.
- **Scope 5 - LLM gap-fill poles (no catalog match).** For any pole rendered with a
  **🤖 LLM root-cause analysis** (the phase-4a fallback for a log `_parse_log`
  didn't recognise), the cause is *inferred*, not measured - so the bar is
  GROUNDING, not arithmetic. Check: (a) every claim/number in the `cause` +
  `breakdown` traces to a line actually present in the captured log - re-grep the
  log for each `evidence` line (they must be verbatim) and recompute any figure the
  analysis sums from the log (e.g. transform+import vs tests across packages); a
  number with no supporting log line is FABRICATED, the worst defect here. (b) The
  section is **labelled** LLM-derived / "lead to verify, not measured" - an
  unlabelled LLM cause read as measured is MISLEADING. (c) The measured parts
  (timeline, cross-run check) are untouched by it. (d) No pole anywhere ships as a
  "no known root-cause pattern / no drill-down available" dead-end - that is a
  product failure, VERDICT WRONG. (e) Sanity-check the cause is *plausible from the
  log* (don't rubber-stamp): if the log shows X but the analysis claims Y, flag it.

- **Scope 6 - the headline pole actually gates, and cache framing matches the distribution.**
  Two data-grounded checks the report must not contradict (both mechanized in
  `verify_report`, but re-confirm on a spot audit): (a) the headline `critical_path_check`
  must be the ACTUAL slowest job (the pole) on ≥ 2 sampled PRs — re-derive the per-PR pole from
  `pr_critical_path.populations` and confirm a lightweight always-present check isn't crowned
  over a heavier suite that genuinely gates (the phantom-gate class); a headline that gates ~0
  PRs while a real recurring gate exists is VERDICT WRONG. (b) A cache-miss pole's "BIGGEST
  LEVER" / "cache-key churn" framing must match its stamped `cache_dist.verdict`: re-derive the
  fork-excluded upstream median from `cache_dist.pr.values` — if it's below the 40% floor and
  the pole still frames churn without the cache-context marker/caveat, that is MISLEADING (a
  churn label over a mostly-warm cache).

Output per claim: report value, independently-computed value, `VERDICT: OK |
WRONG | UNSOUND/MISLEADING`, and the evidence (file + numbers). A label that
doesn't match the log term it summarizes (bar says "DB migrations", log says
"Total Migration Time:") is a MISLEADING defect, not OK.
