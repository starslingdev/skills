---
pattern: OPT37
impact: HIGH
class: static
detector: yaml-workflow-correlated
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml'
fix_strategy: workflow-trigger-dependency-gap-cache-race
title_template: 'Workflow Trigger Dependency Gap (Cache Race)'
category: 7
---

# OPT37 — Workflow Trigger Dependency Gap (Cache Race)

**TL;DR**: The workflow that writes a cache and the one that reads it can race, so the reader sometimes misses a cache that should have been there.

**Anti-pattern**: A read-only downstream workflow/job depends on a cache that a sibling writer workflow produces, but BOTH workflows trigger on the same event (e.g. `pull_request`). The reader races the writer and usually loses — a job that should hit a warm cache (~6s) instead runs the full build (~300-500s) because the writer hasn't finished yet. Distinct from OPT22 (sequential `workflow_run` adding round-trip latency) — here the problem is the OPPOSITE: the reader is missing the `workflow_run` (or `needs`) dependency it should have.

**Detection heuristic**:

- Find workflows/jobs with `TURBO_CACHE: remote:r`, `TURBO_CACHE: remote:ro`, `pnpm install --prefer-offline`, `actions/cache` with `restore-keys:` but no `key:` write, or similar read-only cache modes
- For each, check whether the writer (e.g., a sibling workflow with `TURBO_CACHE: remote:rw`, or a job that populates the cache key) uses the same `on:` trigger
- Flag when reader and writer share the same webhook event (both on `pull_request`, both on `push`, etc.) and there is no `workflow_run` / `needs` linking them
- Cross-check step-duration distribution from sampled successful runs (per-job `steps[]` timing): reader's cache-dependent step shows bimodal timing (fast when warm, slow when cold) with the slow mode dominating — signal of repeated race loss

**Fix**: Option A — move the reader to `on: workflow_run: {workflows: [<writer>], types: [completed]}` so it runs AFTER the writer populates the cache. Option B — if reader and writer are jobs within the same workflow, add a `needs: <writer-job>` dependency. Option C — if the architectural separation is load-bearing, make the reader seed its own cache instead of reading the sibling's.

**Real-world example (mastra golden 2026-04-09)**: `Validate build outputs` job in `lint.yml` (line 73-102) runs on `pull_request` with `TURBO_CACHE: remote:r` while `Prebuild` runs on the SAME `pull_request` event with `TURBO_CACHE: remote:rw`. The validate-build-outputs Build step measures P50=389s (cold) and ~6s (warm) — cold dominates because Prebuild writes after Validate reads. Fix: move Validate to `workflow_run: [Prebuild]`. Saving: 383s/run × 2,555 runs/mo ≈ 16,301 min/mo.

**GUARDRAIL — log-anchored cache-miss evidence is MANDATORY. YAML inspection alone is INSUFFICIENT to emit a OPT37 finding.**

A OPT37 finding may NOT be emitted (at any severity above "review") from
YAML inspection or step-timing bimodality alone. The detector MUST cite at
least one `actions/runs/<id>` log entry showing the reader job's build
step produced a `Tasks: N cached, M total` (or equivalent build-tool
summary) line below some plausible-race threshold (rule of thumb:
**< 70% task hit rate**, or 0% full-run hit rate, on the reader job's
specific build step). Acceptable forms of the cited line:

- Turbo: `Tasks: N successful, M total` + `Cached: K cached, M total`
  (compute hit rate = K/M).
- sccache: `Compile requests executed` / `Cache hits` block.
- Gradle: count of `FROM-CACHE` vs `EXECUTED` task markers.
- pnpm/npm: lockfile-cache restored vs not-restored block.

If the parsed cache_hit / cache_miss line counts from `gh api repos/{owner}/{repo}/actions/jobs/{job_id}/logs` (downloaded per-job by `collect_runs.py --with-logs`
Step 2b from actual build logs) shows the writer AND reader BOTH
routinely hit cache at ≥ 70% task hit rate, **the race is not actually
happening — downgrade the finding to "review" or invalidate it**. The
genuine cost is then elsewhere (cold-tail outliers, retry storms,
codegen-not-committed, failure-tail in job duration) and the finding
must be reframed against that real cost, not the race framing.

**Authoritative re-derivation lesson — mastra 2026-05-06 → 2026-05-11
(re-derived bug).** The 2026-05-06 audit (Finding 2) initially fired
OPT37 against the `check-bundle` job in `Quality assurance`, claiming
a Turbo cache race. Log inspection of actual runs proved cache hit was
**128/130 tasks (98.5%)** and the 13s build step was followed by a
failure tail driven by `check-clean-worktree.bash` failing on
uncommitted regenerated files (Finding 31, codegen-not-committed) — a
**reliability** finding, not a race. The 2026-05-06 report logged
this as a detector bug ("Detector bugs to file"). Five days later, the
2026-05-11 audit re-derived the same OPT37 finding because the detector
guardrail did not require log-anchored evidence. The fix landed in
a prior worked-example report's Phase 6, but only as
a downgrade — the detector still emitted it. **This guardrail closes
that loop: no log line cited → no OPT37 finding.**

Also acceptable as a STRONGER signal when log-anchored evidence is
sparse: a corroborating step-timing bimodality at the reader's
cache-dependent step (P50 < 50% of P95). This is corroboration, NOT a
substitute for the log evidence. A finding that lacks the log line
must be tagged `severity: review` and routed to Phase 4.5 for log
inspection before it can be promoted.

**Cancel-claim countercheck.** Before shipping, also verify the
**failure mode being attributed to the race is actually the
race**. The mastra-2026-05-06 audit's finding #2 originally claimed
the QA job's 64% failure rate was caused by check-bundle losing the
race; log inspection of two failed runs showed cache hit was 128/130
(98.5%) and the failure was at a different step (`check-clean-worktree.bash`
on uncommitted regenerated files — finding #31). A OPT37 finding that
lacks log-anchored cache-miss evidence AND attributes specific
failures to the race must be downgraded to a draft until the race is
independently confirmed.

**Failure of this guardrail** re-derives a cache-race finding that
log inspection later disproves (cache hit was actually ≥98%, and the
real cost was a reliability/codegen tail). Cite the log line; don't
re-derive it.

---
