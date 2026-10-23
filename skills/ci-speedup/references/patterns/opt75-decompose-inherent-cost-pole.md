---
pattern: OPT75
impact: HIGH
class: structural
detector: critical-path
risk: MEDIUM
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml'
fix_strategy: decompose-inherent-cost-pole
title_template: 'The long pole''s time is one addressable step — speed it up or move it off the PR path'
category: 14
---

# OPT75 — Long Pole: Optimize or Relocate the Dominant Step

**TL;DR**: The slowest check holding up your PRs has no off-the-shelf hygiene fix, but almost all its time goes to a single step you can target directly — shard/parallelize a test, cache an install, scope a scan, or (for a fileless check you can't edit) move it off the PR trigger. The fix is whatever the measured dominant step calls for; this lever just refuses to dead-end.

**Anti-pattern**: The report's old dead-end — "the gating check has no matching optimization pattern, so cutting wall-clock here is outside this catalog." That stops exactly where the leverage is. A long-pole job is almost never uniformly slow; it's checkout + install + build + the actual test/scan, and usually **one** of those is the bulk.

**Detection heuristic** (routed): decompose the long-pole job into steps (`_step_durations`), classify each by category (checkout / install / build / test / scan / package / setup), and find the **dominant** step and its share of the job. OPT75 is the NEUTRAL catch-all — it carries no presupposed remedy (never "decompose/split" or "scope/drop your tests"), so the fix step is free to land on the right lever (parallelize, cache, scope, relocate) from the measured behaviour. A **build**-dominant pole routes to OPT72 (cache) or OPT70 (scope) instead; everything else — **test** (shard/parallelize, NOT the HIGH-risk "drop tests"), install, setup, scan, format, package, and fileless checks — routes here.

**Fix recipe** (reasoning step — picks the concrete remedy for *this* repo's tooling; these are NOT emitted as distinct findings by the detector): route the dominant category to a concrete lever —

- dominant = **install / checkout / setup** → a caching / shallow-fetch / pin lever (often LOW risk; see OPT73 if shared across the cluster).
- dominant = **build** → warm the build cache, or scope the build (OPT70/OPT72).
- dominant = **test** → shard it (OPT24), or scope it to changed targets (OPT70).
- dominant = **scan / package** → cache the scan DB / incremental scan, or move it advisory-async if non-required (OPT71).
- dominant cost is **several independent steps** run one after another (say a
  build, a docs build and a scan, none of which reads another's output) → run
  them as a `parallel:` group **inside the same job** (syntax, runner
  requirement and limits: OPT77's fix). No extra checkout or setup is paid and no
  new check name appears, so no required-check edit is needed. Treat steps as
  independent only when all four hold:
  1. no sibling reads any file or output another sibling produces (not only
     what it writes to `GITHUB_ENV`, `GITHUB_OUTPUT` or `GITHUB_PATH`); a
     background step's outputs reach later steps only after a wait that
     covers it;
  2. no two siblings write the same cache, lockfile or build-output directory;
  3. the runner has the memory and CPU for them all at once;
  4. no sibling relies on another having succeeded first. Siblings start
     together, so none waits for another's result: the default `if: success()`
     ordering disappears inside a group, so never put a deploy,
     upload, publish or any other step with side effects in a group with the
     check that should gate it.

  **Honesty note**: the saving is "sum of the steps minus the slowest one" only
  if each sibling keeps its solo speed. Siblings share one runner's CPU, memory
  and disk, so that figure is an **upper bound** until a benchmark run measures
  the job with the group in place.

  When the steps qualify and the workflow file proves them independent, the audit
  reports this as [OPT83](#opt83--independent-steps-on-the-long-pole-run-one-after-another)
  with its own card, upper bound and benchmark.

Report the dominant step, its category, and its share so the reader sees *why* the inherent-cost pole is actually addressable.

**Risk**: **MEDIUM** by default — the dominant-step remedy ranges from LOW (cache an install) to HIGH (scope a test/build, inheriting OPT70). The emitted candidate carries the risk of whichever specific lever its dominant category routes to.

**Required-coverage caveat (the relocate branch)**: moving the dominant step out of the gating job moves the *work*, not the *gate*. If the pole is a required status check, the relocated work only keeps gating merges when the coverage is re-established **in the same change**. A `needs:` edge alone is not enough — `needs:` orders jobs, it does not gate merges, so the required check can go green while the moved work failed. Two routes, and they are not equally available:

- **Keep the required check name (no admin needed — prefer this).** Give the *work-carrying* jobs the new names and leave the **required check name** on a verdict/aggregator job that `needs:` them and rejects them when they failed or did not execute (next caveat for how). Branch protection is not edited at all, because the name it requires never moved.
- **Add the new job's check name to branch protection** (or the ruleset equivalent) as a required check. This one is **admin-only**: it belongs in the fix handoff as an explicit step for the operator, and the audit never changes protection rules itself. Do not ship the relocation ungated while waiting on it.

The **advisory-async** branch above — moving a non-required scan off the PR path (OPT71) — stays restricted to genuinely advisory work: a non-required job feeding a required aggregator is required *in effect*, full stop, whatever the aggregator's own verdict logic turns out to do. **The rule chains at every hop**: a job feeding an aggregator that is itself only required *in effect* is required *in effect* too, so tracing one edge and finding a non-required job there settles nothing — follow the chain until it reaches a required check name or runs out. An aggregator whose logic propagates only *some* upstream outcomes (this section's `contains(needs.*.result, 'failure')` trap, below) still counts as propagating here: a leaky verdict is a reason to FIX the verdict, never a licence to de-scope the job feeding it. The nuance can only make MORE things required, never fewer. And an **unknown** required status is treated as required, never as permission to de-scope. OPT71's consumer enumeration still applies in full — and note `needs:` takes **job ids, not check names**, so enumerate every job that `needs:` this one, every later step reading its outputs, every downstream comment/label/deploy, and every aggregator reading its result.

**Dependency-failure skip caveat**: a job skipped because a job in its `needs:` list FAILED does not report failure — it reports as skipped, exactly like an `if:`-skipped job, and a required check satisfied by that job can therefore be satisfied while its work never ran ([GitHub: troubleshooting required status checks](https://docs.github.com/en/pull-requests/how-tos/merge-and-close-pull-requests/troubleshooting-required-status-checks)). The verdict/aggregator job that stands in for the relocated work must therefore do BOTH things: run with `if: always()` (or `!cancelled()`) so a failed dependency cannot skip it away, AND explicitly propagate the upstream outcomes — read each `needs.<job>.result` and exit non-zero unless every required upstream result is `success`, treating `failure`, `cancelled` and `skipped` alike as a fail. Adding `always()` without propagating the results is the trap: the verdict then runs, reports success, and green-lights a merge whose required work failed or never executed.

The whole verdict job, with the required check name kept on it:

```yaml
  test:                       # KEEPS the name branch protection requires
    needs: [lint, integration]
    if: always()              # `!cancelled()` instead if a user-cancelled run
    runs-on: ubuntu-latest    #   should stay cancelled rather than go red
    steps:
      - name: Verdict
        run: |
          echo "lint=${{ needs.lint.result }} integration=${{ needs.integration.result }}"
          [ "${{ needs.lint.result }}" = success ] \
            && [ "${{ needs.integration.result }}" = success ] || exit 1
```

Three ways to get this wrong: putting the `needs.*.result` test in the job-level `if:` (that *skips* the verdict instead of failing it, and a skipped check reports success); using `contains(needs.*.result, 'failure')`, which misses `skipped` and `cancelled`; and renaming the required job so the required check never reports at all — which does not silently pass, it blocks the pull request forever. `always()` runs on cancellation too, so a cancelled run turns the required check red; `!cancelled()` avoids that but leaves the verdict skipped-and-therefore-green on cancel. Pick deliberately.

**Guardrail**: Carry the guardrail of the routed lever (e.g. OPT70's full-suite fallback if the dominant step is a test being scoped). Never present the decomposition as free.

**Rollout**: The routed lever's rollout. Re-measure the pole's p50 after the dominant step is attacked; the next-largest step (or the cluster floor) becomes the new target.

---
