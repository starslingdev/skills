---
pattern: OPT48
impact: MEDIUM
class: data-driven
detector: manual
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml'
fix_strategy: high-job-level-failure-rate-15-over-30d
title_template: 'High Job-Level Failure Rate (>15% over 30d)'
category: 9
aliases:
  - opt48--high-job-level-failure-rate--15--over-30d-
alias_note: 'Alias anchor: findings files stamped before the GFM anchor fix link this older slug; keep it so those deep links still land.'
---

# OPT48 — High Job-Level Failure Rate (>15% over 30d)

**TL;DR**: This workflow fails a large share of the time. If those are real bugs/flakes they waste CI; if it's a deliberate policy gate, the failures are it working as intended.

**Anti-pattern**: A job's `failures / (failures + successes)` rate stays above 15% over a 30-day window. Each failed run wastes the full job duration (the runner still paid for its wall clock) AND usually blocks the PR until retried — doubling or tripling the effective time cost. Distinct from OPT46 (superseded-run cancellations): a _failure_ is a hard error, a _cancel_ is a deliberate stop.

Root causes typically fall in a few buckets:

- **Live external dependency variance** — tests hit real LLM/cloud APIs whose latency/output drifts; no recorder/replay harness.
- **Timing-sensitive tests** — `page.waitForTimeout` or fixed `setTimeout` values picked for median machines; tail runs exceed them.
- **Resource contention** — multiple heavy jobs share a runner pool; one OOMs or times out.
- **Flaky third-party services** — rate-limits, intermittent 5xx, eventual consistency.

**Detection heuristic**:

- For each workflow/job in the workflow's `runs?status=...` totals from `gh api repos/{owner}/{repo}/actions/workflows/{wf}/runs?per_page=1&status=...`, compute `failure / (failure + success)`
- Flag when rate > 15% AND `(failure + success) > 100` (ignore low-volume jobs)
- Cross-reference job duration: high-failure-rate × long-P50 = highest cost; investigate those first
- For each flagged job, investigate root cause: read the job's primary test invocation + its config (vitest.config.ts, jest.config.js, playwright.config.ts) and look for sleeps, tight timeouts, or missing retry/replay wiring

**Fix**: Depends on root cause. For live-LLM variance: install a recorder/replay plugin (Mastra's llmRecorderPlugin, similar in other stacks). For timing-sensitive tests: replace waitForTimeout with event-driven waits, raise timeouts, add targeted retries via vitest's `retry`/playwright's `expect.poll`. For resource contention: increase runner concurrency caps or shard the job. For flaky services: add exponential-backoff retries for specific API calls with a sensible ceiling.

**Real-world example (mastra golden 2026-04-09)**: E2E Tests `E2E kitchen-sink` at 22.5% failure rate (644 failures / 2,864 triggered over 30d), P50=457s. Every failure wastes ~457s × 644 ≈ 4,904 min/mo. Memory Tests `test` at 23.8% failure rate (634/2,660), P50=212s → 2,240 min/mo. Fix: wire in Mastra's `llmRecorderPlugin` for replay mode in CI, bump vitest `testTimeout` / `hookTimeout`, and gate live-API runs to nightly instead of PR.
