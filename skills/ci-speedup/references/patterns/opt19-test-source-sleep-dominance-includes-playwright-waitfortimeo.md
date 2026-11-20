---
pattern: OPT19
impact: HIGH
class: static
detector: repo-file-check
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml'
fix_strategy: test-source-sleep-dominance-includes-playwright-waitfortimeo
title_template: 'Test Source Sleep Dominance (includes Playwright `waitForTimeout`)'
category: 3
---

# OPT19 — Test Source Sleep Dominance (includes Playwright `waitForTimeout`)

**TL;DR**: Tests waste time on fixed sleeps baked into the test code — waiting a flat number of seconds instead of waiting for the thing to actually be ready.

**Anti-pattern**: Test steps spend significant time sleeping inside the test source (fixed-ms `setTimeout`, `page.waitForTimeout`, `cy.wait`, raw `sleep()`, polling loops) — not in workflow YAML. OPT17 only detects sleep in workflow files; this pattern catches the much larger category of sleep embedded in test source.

**Subtypes** (all count; use the BROADEST scope — do not pick just one):

- **Playwright hardcoded waits** — `page.waitForTimeout(N)` or bare `waitForTimeout(N)`. These are the most common offender in e2e test suites.
- **Cypress** — `cy.wait(N)` (note: `cy.wait('@alias')` waiting on a network intercept is OK; numeric ms is the anti-pattern).
- **Node/Jest/Vitest** — `await new Promise(r => setTimeout(r, N))`, `await sleep(N)`, `await delay(N)` in `*.test.ts` / `*.spec.ts`.
- **Integration-test fixtures** — `sleep N` in shell wrappers, `time.sleep(N)` in Python tests, fixed waits before assertion retries.
- **Go tests** — on a drilled long pole whose log runs `go test` (plain, `-v` or `-json`): constant `time.Sleep` / `<-time.After` waits in a `_test.go` body that dominate the measured test or package time; repeated, equally spaced wait/poll/retry output inside one test; or a `-race` job whose test files synchronously re-execute the race-built test binary, so each child pays the race runtime's exit sleep.

**Detection heuristic (run ALL greps; aggregate matches; sum `sleep_ms` across every file)**:

```bash
# Playwright (the dominant case in monorepos with an e2e package)
grep -rn 'page\.waitForTimeout(\|waitForTimeout([0-9]' \
  packages/ e2e/ playwright/ tests/ integration-tests/ \
  --include='*.ts' --include='*.spec.ts' --include='*.test.ts' 2>/dev/null

# Cypress
grep -rn 'cy\.wait([0-9]' cypress/ e2e/ --include='*.ts' --include='*.js' 2>/dev/null

# Raw setTimeout / sleep / delay inside test files (large N only — 1000ms+)
grep -rn 'setTimeout(.*,\s*[1-9][0-9]\{3,\}\|await sleep(\|await delay(' \
  packages/ e2e/ tests/ --include='*.test.ts' --include='*.spec.ts' 2>/dev/null

# Python test sleeps (when repo has Python tests)
grep -rn 'time\.sleep([0-9]' tests/ --include='*.py' 2>/dev/null
```

**Canonical search paths for monorepos**: `packages/*/e2e/tests/**`, `packages/*/tests/**`, `packages/*/src/**/*.test.ts`, `packages/playground/**`, `packages/memory/integration-tests/**`, top-level `e2e/**`, `playwright/**`, `cypress/**`, `integration-tests/**`. Do NOT stop after the first directory — iterate through each of these before finalizing the finding.

**Aggregation**: Sum `sleep_ms` across EVERY matched file. Compute total sleep per full test run (if test is a single script, sum is the per-run cost; if test is sharded, divide by shard count only if each shard runs a non-overlapping subset). Report the per-run total and the source-of-truth file list in `evidence`.

**Fix**: Replace with event-driven waits. For Playwright: `page.waitForSelector`, `page.waitForLoadState`, `expect(locator).toBeVisible({ timeout })`. For Cypress: `cy.wait('@alias')` on route aliases. For DB/service readiness: ping the service in a bounded retry loop (e.g. `@testcontainers` readiness probes).

For a Go test that reaches production poll intervals or grace periods, run the waiting test on `testing/synctest`'s virtual clock. If that dependency cannot use the virtual clock, expose only the interval as a package variable that the test shortens and restores with `t.Cleanup`; keep the production default and timeout assertions unchanged. For synchronous race-built helper children, preserve every inherited `GORACE` option and append `atexit_sleep_ms=0` only to the child environment. Do not apply that override to the parent race job or asynchronous children.

The Go routes fail closed. A test-body sleep counts only when its duration is a literal or a package `const` (a `var` interval may already be shortened by the tests), it runs unconditionally (not in a loop, branch, `select`, goroutine or closure other than an inline `t.Run` body), and the test does not call `t.Parallel`, skip, return early or use `testing/synctest`; the constant total must be at most, and at least half of, the measured test time (or, without `-v`, the package time from a `go test` line with no `-run`/`-skip` filter; a `(cached)` package ran nothing), and at least one fifth of the job. The poll route needs at least two consistent intervals inside one test, timed by `go test -json` or the runner's own line timestamps, with the same shares. The race route needs a `-race` `go test` (or `GOFLAGS`) line, a package the job ran, at least three synchronous call sites that re-execute `os.Args[0]` (directly or through a helper returning `*exec.Cmd`), no asynchronous `Start`, and no `atexit_sleep_ms` anywhere in the package or the log. Sleep and poll findings state the runner-minute saving as a ceiling from one representative run and the workflow's 30-day run count; the race finding is unsized, because the log does not count helper re-execs.

Public examples cut a detached-seed matrix under `-race` from 558.58 s to 297.99 s (one local run each side) in [openclaw/crabbox#2969](https://github.com/openclaw/crabbox/pull/2969), move fixture waits to virtual time in [openclaw/crabbox#2455](https://github.com/openclaw/crabbox/pull/2455), and remove the race exit sleep only for synchronous helper children in [openclaw/crabbox#1761](https://github.com/openclaw/crabbox/pull/1761).

**Real-world example (mastra golden 2026-04-09)**: 37 `page.waitForTimeout()` calls across `packages/playground/e2e/tests/` totaling 65,500ms per full run. On E2E kitchen-sink (2,368 runs/mo × 65.5s/run) this is 2,585 min/mo. Initial detection required grepping `page.waitForTimeout` in the `packages/playground/e2e/` scope — looking only at `stores/*.test.ts` misses it entirely.

---
