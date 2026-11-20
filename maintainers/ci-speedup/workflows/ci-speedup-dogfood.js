export const meta = {
  name: 'ci-speedup-dogfood',
  description: "Run the REAL ci-speedup skill on each org's top repo and open a PR for any skill bug found",
  whenToUse: 'Maintainer dogfooding. Pass GitHub org slugs as args (array). Runs the actual skill — does NOT reimplement any of its pipeline. Add `--audit-only` (or `--fast`) to skip the Fix + PR tail for a quick detection / smoke run.',
  phases: [{ title: 'Run + audit' }, { title: 'Fix' }],
}

// Accept an array of slugs, {orgs:[...]}, or a single space/comma-separated string. Any
// other shape (a bare object, a number) yields an empty list → the helpful throw below,
// instead of silently stringifying to "[object Object]" and scouting a bogus org.
const rawOrgs = Array.isArray(args) ? args
  : (args && Array.isArray(args.orgs)) ? args.orgs
  : (typeof args === 'string') ? [args]
  : []
const tokens = rawOrgs.flatMap(s => String(s == null ? '' : s).split(/[\s,]+/)).filter(Boolean)

// `--force` (also `--fresh` / `-f`, or `{force:true}`) re-runs every org from scratch, ignoring
// any cached prior artifact. Without it, an org with a complete prior run resumes (see the run
// prompt's step-0 RESUME CHECK below) — so a partial-throttle re-run only re-does the orgs that failed.
const isForceFlag = t => t === '--force' || t === '--fresh' || t === '-f'
// `--audit-only` (alias `--no-fix`) STOPS after Run + audit — it reports the found bugs + grader
// seeds but SKIPS the Fix + integrate fan-out (the slow tail: effort:'high' worktree agents that
// reproduce each bug, run the suite red→green, and open a consolidated PR). It is the fast path for
// DETECTION / smoke runs; re-run without it to draft fixes. `--fast` implies `--audit-only` AND has
// the run agent sample a SMALLER window (`run.py --target 5` vs the default 10) for a quicker,
// lower-fidelity skill pass — a deliberate speed/fidelity trade for iteration, NOT a sizing audit.
const isAuditOnlyFlag = t => t === '--audit-only' || t === '--no-fix'
const isFastFlag = t => t === '--fast'
// --- Hardening flags: `--token-budget=<N>` / `--max-fixes=<N>` (also {token_budget, max_fixes}) --
// `=`-form ONLY, single token: the org tokenizer splits on spaces/commas, so a two-token
// "--token-budget 2000000" would strand a bare number in the list to be scouted as an org slug.
// The =-form parses atomically; the unknown-flag guard below rejects the two-token form loudly.
const TOKEN_BUDGET_RE = /^--token-budget=(\d+)$/
const MAX_FIXES_RE = /^--max-fixes=(\d+)$/
const isNumFlag = t => TOKEN_BUDGET_RE.test(t) || MAX_FIXES_RE.test(t)
// A positive-finite guard MATCHING objNum's — the `\d+` regex matches `=0`, but `0` is not a valid
// ceiling: without this, `--token-budget=0` returns 0, and `0 ?? 2_000_000` keeps the 0 (nullish
// coalescing does NOT fire on 0), so overBudget() trips at the first stage and the whole run is a
// no-op. Returning null instead lets a `=0` (still recognized as this flag, so never scouted as an
// org nor thrown as unknown) fall through to the default, symmetric with objNum's 0/negative reject.
const numFromTokens = re => { for (const t of tokens) { const m = re.exec(t); if (m) { const n = Number(m[1]); return Number.isFinite(n) && n > 0 ? n : null } } return null }
const isFlag = t => isForceFlag(t) || isAuditOnlyFlag(t) || isFastFlag(t) || isNumFlag(t)
const objFlag = k => args && typeof args === 'object' && !Array.isArray(args) && args[k] === true
// Object form: a real positive-number value only. It must be a `number` TYPE — NOT coerced — so the
// boolean idiom used elsewhere in this file (`{force: true}`) can't leak in: `Number(true) === 1`
// would otherwise set a 1-token ceiling that halts the whole run at the first stage. Anything else
// (a boolean, "abc", "1500000", -1, 0, NaN) falls through to the token form and then the default,
// never to an accidental tiny/0/NaN ceiling.
const objNum = k => {
  const v = args && typeof args === 'object' && !Array.isArray(args) ? args[k] : undefined
  return typeof v === 'number' && Number.isFinite(v) && v > 0 ? Math.floor(v) : null
}
const FORCE = objFlag('force') || tokens.some(isForceFlag)
const FAST = objFlag('fast') || tokens.some(isFastFlag)
const AUDIT_ONLY = FAST || objFlag('audit_only') || objFlag('auditOnly') || tokens.some(isAuditOnlyFlag)
// DEFAULT ceilings (override deliberately, never remove). TOKEN_BUDGET: 2M output tokens — a
// measured 6-org --audit-only sweep costs ~1M (round 2, 2026-07-16) and a normal fix tail some
// hundreds of k more, so 2M clears every run shape MEASURED so far while stopping a runaway at
// ~57% of the 3.5M incident that motivated this (a full 4-fix run is estimated, not yet measured,
// and at ~100-250k/bug could land near the ceiling — raise --token-budget deliberately if it trips
// on a legitimate large run). MAX_FIXES: 4 — bounds the loop's most
// expensive stage (each fix = a worktree-isolated effort:'high' agent) and keeps the consolidated
// PR a reviewable size; held-out bugs surface as explicit needs_human rows for the next run.
const TOKEN_BUDGET = objNum('token_budget') ?? numFromTokens(TOKEN_BUDGET_RE) ?? 2_000_000
const MAX_FIXES = objNum('max_fixes') ?? numFromTokens(MAX_FIXES_RE) ?? 4
// De-duplicate slugs (first-seen order): a repeated org (copy-paste, or concatenating two
// overlapping lists) would otherwise schedule two agents into the SAME per-org
// `.ci-speedup-dogfood/<org>/` dir — the run agents are NOT worktree-isolated, so they'd race
// on the shared clone + status.json (the dir-collision the run prompt's "must not collide"
// comment assumes away). One agent per distinct org keeps that invariant intact. Every flag
// (`--force`/`--fast`/`--audit-only`/…) is filtered out so it is never scouted as an org slug.
const ORGS = [...new Set(tokens.filter(t => !isFlag(t)))]
// Any OTHER `--…` token is a mistyped/unknown flag — throw rather than scout it as an org slug.
// This is what makes the two-token "--token-budget 2000000" misuse LOUD: the bare `--token-budget`
// half is unknown (no `=`), so the run aborts before the stranded number is scouted as an org.
const unknownFlags = ORGS.filter(t => t.startsWith('--'))
if (unknownFlags.length)
  throw new Error(`ci-speedup-dogfood: unknown flag(s): ${unknownFlags.join(', ')} — known: `
    + `--force/--fresh/-f, --audit-only/--no-fix, --fast, --token-budget=<N>, --max-fixes=<N>`)
if (!ORGS.length) throw new Error('ci-speedup-dogfood: pass one or more GitHub org slugs as args')

const SKILL = 'skills/ci-speedup'

// Normalize a rejection reason to a non-empty string (a falsy reason must not become "undefined").
const errMsg = e => (e && e.message) ? String(e.message) : (e ? String(e) : 'unknown error (empty rejection)')

// --- Transient-failure resilience (see PLAN-dogfood-rate-limit-resilience) -------------------
// A momentary server-side throttle must NOT discard an org's expensive run (a real run once lost
// 6m37s to a transient rate limit, which surfaced as an agent() *rejection*). withRetry sleeps a
// capped exponential backoff and retries ONLY for a *transient* infra error; a non-transient error
// (a real skill bug) or exhausted retries falls through to the caller's `.catch` → `errored`,
// preserving the no-silent-drops property. SCOPE: retries cover the *rejection* path only — if a
// throttle instead surfaces as agent()'s documented null-resolution (skipped / dead after the
// harness's own retries), it is NOT retried here but is reconciled positionally to `errored` below.
// (We deliberately don't blanket-retry a null resolution, which would also re-run an agent the user
// skipped.) This is the FIRST retry mechanism in the loop — the Python side (GhClient) only paces.
const sleep = ms => new Promise(r => setTimeout(r, ms))

// Match ONLY infra/throttle errors — never a skill failure, which must reach the auditor. The
// marker set is pinned by tests/dogfood-retry.test.mjs (extracted from this very regex) so the
// test and the design can't drift. Covers the canonical rate-limit signals — textual markers
// (`rate limit` / `temporarily limiting` / `too many requests` / `overloaded` / `throttl` /
// `quota`) plus the bare HTTP statuses 429 (the canonical Too-Many-Requests code), 503, 529. Each
// \b…\b-anchored status matches the bare code without firing on an unrelated number that merely
// embeds those digits (e.g. "15039"). TRADEOFF: a bare status can still match when a *skill* error
// quotes a third-party CI log line containing it (the dogfood agent parses those logs); such a
// misclassified bug is retried and then surfaces loud in `errored` (never silently dropped) —
// acceptable vs. missing a real throttle. Prefer a structured status field if one becomes available.
const isTransient = e => /rate limit|temporarily limiting|too many requests|overloaded|throttl|quota|\b429\b|\b503\b|\b529\b/i.test(errMsg(e))

const RETRY_BACKOFF_MS = [5000, 15000, 30000]   // backoff before retry 1, 2, 3 (5s → 15s → 30s); held at the 30s cap beyond
// `retries` is the number of RETRIES after the initial attempt — so the default does up to 4 total
// attempts and exercises every backoff tier (5s, 15s, 30s). A transient throttle backs off + retries;
// a non-transient error (a real skill bug) or exhausted retries throws to the caller's `.catch`. The
// 4-attempt / 50s-total cap keeps a genuinely dead API from hanging the batch — it still surfaces as `errored`.
async function withRetry(fn, { retries = 3, onRetry } = {}) {
  for (let attempt = 0; ; attempt++) {   // attempt 0 = initial try; 1..retries = the retries
    try { return await fn() }
    catch (e) {
      if (attempt >= retries || !isTransient(e)) throw e   // out of retries, or a real (non-transient) failure → caller's .catch
      if (onRetry) onRetry(attempt + 1)
      // Never let a broken/absent timer launder the real cause: if `sleep` itself fails (e.g.
      // `setTimeout` is unavailable in the harness sandbox), rethrow the ORIGINAL `e` so the
      // operator sees the throttle, not "setTimeout is not defined". Retries just don't happen in
      // that case and the run falls through to `errored` with its true reason intact.
      try { await sleep(RETRY_BACKOFF_MS[Math.min(attempt, RETRY_BACKOFF_MS.length - 1)]) }
      catch { throw e }
    }
  }
}

// Pace the run fan-out: firing N full-skill subagents at once is likely what tips the per-account
// throttle, so process orgs in small chunks instead of one flat parallel(). The harness caps
// concurrent agent() at a modest ceiling, but for a small org count that cap won't bind — this
// explicit low concurrency is what actually paces the run. Tune low; not self-throttling > throughput.
const RUN_CONCURRENCY = 3
// The fix fan-out is the loop's other heavy concurrent agent batch (each fix = worktree-isolated,
// effort:'high'), so pace it too — many bugs across many orgs would otherwise fire all fix agents at
// once, the same throttle risk the run stage now avoids. 3 (not 2): the fix wall-clock is gated by
// the SLOWEST single bug, so any bug that queues behind a full slot adds its WHOLE duration to the
// phase — a measured single-org run with 3 bugs at concurrency 2 made the 3rd fix wait ~2.5 min for a
// slot. The fix agents are worktree-isolated, so they don't need a tighter cap than the run stage:
// the only shared-git-dir cost is loose-ref writes under .git/refs (branch + commits), which is
// low-contention. 3 keeps the common ≤3-bug run fully parallel; it's now equal to RUN_CONCURRENCY (a
// deliberate pacing ceiling, not unbounded fan-out — just no longer tighter than RUN).
const FIX_CONCURRENCY = 3
const chunk = (arr, n) => { const out = []; for (let i = 0; i < arr.length; i += n) out.push(arr.slice(i, i + n)); return out }

// --- Hardening: the mechanical spend ceiling (see the 2026-07-16 incident) --------------------
// The 2026-07-16 fix-wave run burned 3.5M output tokens before a HUMAN noticed and killed it; the
// TOKEN_BUDGET ceiling makes that stop MECHANICAL. `budget.spent()` is the harness's SESSION-CUMULATIVE
// output-token counter — a single pool the main loop and every workflow share, and one that NEVER
// resets across turns/user messages within a session. The ceiling must therefore bound only THIS
// RUN's own spend (issue #48), not the session's lifetime total: we snapshot the pool ONCE at script
// start (RUN_SPEND_BASE, below, taken before the first agent) and gate on the DELTA
// (`spentNow() - RUN_SPEND_BASE`).
//
// WHY (measured evidence, 2026-07-17): as SHIPPED in PR #29 the guard compared the raw pool reading
// against the ceiling, so once a session's cumulative spend passed 2M the ceiling could never admit a
// run — three launch attempts read spent 2,207,037 → 2,209,699 → 2,213,117 across separate
// turns/user messages, the pool never resetting, so the first org was refused before it started. That
// invites `--token-budget` overrides, which NORMALIZES overriding the guard — strictly worse than
// scoping the ceiling correctly. The run-delta gate binds a runaway WITHIN a launch (the 3.5M shape)
// while letting a fresh run start regardless of what the session spent earlier.
//
// The guard is checked BETWEEN chunks and stages — agents already in flight finish; nothing new is
// scheduled once the ceiling is crossed — and every unit skipped surfaces LOUDLY in the result (an
// errored org row, a needs_human fix row with any already-drafted patch preserved), never as a
// silently smaller run.
//
// Two failure shapes, deliberately kept DISTINCT (a bare `try/catch → 0` conflated them, which
// would silently fail the ceiling OPEN — the exact runaway this PR exists to stop):
//   • `budget` UNDEFINED (an older harness with no counter): `typeof` degrades to spent 0, the
//     guard never trips — exactly the pre-hardening behavior, no NEW failure mode, visible as
//     run_spent_output_tokens: 0 with probe_error: null.
//   • `budget` PRESENT but `.spent()` THROWS (API drift / a transient fault): the ceiling CANNOT
//     bind this run, so we say so LOUDLY (a one-time log) and surface `probe_error` in the result —
//     a `run_spent_output_tokens: 0` with a non-null probe_error is a COVERAGE GAP, not a bounded run,
//     and must never be read as "ceiling verified, spend was low". RUN_SPEND_BASE just captures
//     whatever spentNow() returns at start (0 on an absent/throwing counter), so a counter that
//     throws FROM THE START anchors base 0 and the delta stays honest relative to that reading —
//     fails loud, never read as 0-spent-forever. The remaining case is a counter that WORKED at
//     launch (nonzero base captured) and THROWS mid-run: spentNow() then reads 0, so runSpend()
//     goes NEGATIVE (0 − base). A negative run delta is impossible for a real run, so overBudget
//     treats it as the ceiling going BLIND and fails CLOSED (halts), rather than reading the
//     negative as "under budget" — that would be the one in-flight fail-OPEN the run-delta scoping
//     could otherwise leave (#48 review). The stop is mechanical, matching probe_error's loud tell.
let budgetProbeError = null   // errMsg of a present-but-throwing budget.spent() — surfaced in the result
const spentNow = () => {   // raw SESSION-cumulative pool reading (kept for operator context in the payload)
  if (typeof budget === 'undefined') return 0   // older harness: intended pre-hardening degrade (no ceiling)
  try { return budget.spent() }
  catch (e) {
    if (!budgetProbeError) {
      budgetProbeError = errMsg(e)
      log(`⚠ budget.spent() threw (${budgetProbeError}) — the token ceiling CANNOT bind this run; `
        + `treating spend as 0. This is a COVERAGE GAP (not a bounded run): watch the spend by hand.`)
    }
    return 0
  }
}
// Snapshot the session pool ONCE, at script start, BEFORE the first agent (the run fan-out
// runs further below) so the run's own spend is fully covered by the delta. `runSpend()` is what the
// ceiling governs: this run's own output tokens, immune to whatever the session spent before launch.
const RUN_SPEND_BASE = spentNow()
const runSpend = () => spentNow() - RUN_SPEND_BASE
let budgetStoppedAt = null   // first stage the ceiling halted (null = never tripped) — in the result
const overBudget = stage => {
  const spent = runSpend()
  // A run delta at/above the ceiling halts (>= semantics); a NEGATIVE delta also halts — it is
  // impossible for a real run and means a counter that worked at launch started throwing/vanished
  // mid-run (spentNow() → 0, below a nonzero base), so the ceiling can no longer bind and we fail
  // CLOSED rather than read the negative as "under budget" (#48 review). 0 <= spent < ceiling passes.
  if (spent >= 0 && spent < TOKEN_BUDGET) return false
  if (!budgetStoppedAt) {
    budgetStoppedAt = stage
    if (spent < 0) {
      log(`⛔ TOKEN CEILING BLIND at stage "${stage}": run delta ${spent} < 0 — budget.spent() `
        + `(session pool now ~${spentNow()}, base ${RUN_SPEND_BASE}) dropped below the launch snapshot, `
        + `so the counter threw/vanished mid-run (see probe_error) and the ceiling can no longer bind. `
        + `Failing CLOSED: nothing new is scheduled; completed work is returned and every skipped unit `
        + `is marked loudly. Re-run to resume.`)
    } else {
      log(`⛔ TOKEN BUDGET EXHAUSTED at stage "${stage}": ~${spent} output tokens spent by THIS RUN `
        + `(session pool now ~${spentNow()}, base ${RUN_SPEND_BASE}) >= the ${TOKEN_BUDGET} ceiling. `
        + `Nothing new is scheduled; completed work is returned and every skipped unit is marked loudly. `
        + `Re-run to resume (completed orgs short-circuit), or pass --token-budget=<N> deliberately for a bigger sweep.`)
    }
  }
  return true
}

// --- Write-surface guard (see PLAN-dogfood-write-surface-guard) ------------------------------
// Idea borrowed from Warp's oz-for-oss (https://github.com/warpdotdev/oz-for-oss): its update-*
// runners `assert_write_surface` the diff against an allowlist and abort before `git push`.
// A core-logic fix may write ONLY these paths. The fix agent reports its raw
// `git diff --name-only origin/main...HEAD`; the WORKFLOW re-validates that raw list here (pure
// JS — the harness has no git/fs), and any path outside the allowlist downgrades the fix to
// needs_human. The binding control is this re-validation of the agent's RAW list, NEVER the
// agent's own pass/fail verdict. Threat model (v1): an accidental stray edit while "fixing",
// not an agent that lies about its own diff (that needs an independent verifier — out of scope).
// Every entry is ^-anchored so a path can't match from a non-root position (e.g. a stray
// `adversarial/skills/ci-speedup/scripts/evil.py`).
const FIX_ALLOWLIST = [
  /^skills\/ci-speedup\/scripts\/[^/]+\.py$/,   // the engine fix (flat scripts/*.py — the dir has no nested .py)
  /^skills\/ci-speedup\/tests\//,            // its regression test (incl. tests/verify_report.py — the class invariant sink)
  // The required changelog entry: ONE new fragment (skills/ci-speedup/changelog.d/README.md).
  // CHANGELOG.md itself is NOT allowlisted: only maintainers/scripts/assemble_changelog.py
  // writes it, and CI refuses a hand edit, so a fix that touched it could never merge.
  /^skills\/ci-speedup\/changelog\.d\/[a-z0-9][a-z0-9_-]*\.(added|changed|fixed|removed)\.md$/,
  // The renderer/measurement contract doc. ALLOWED on EVERY fix — this allowlist is PATH-only and
  // has no class-vs-instance notion, so honesty demands saying so: adding this regex permits an
  // ARCHITECTURE.md edit on any patch, not "only on a class fix". It is NOT left unguarded, though:
  // the co-occurrence rule in `fixWriteViolations` rejects a LONE ARCHITECTURE.md edit (one with no
  // sibling engine `scripts/*.py` change) — a doc edit must ride WITH the engine change it documents,
  // which is the real threat ("a stray doc edit while fixing"), so a class fix that updates §12 in
  // the same patch passes while a drive-by doc edit is still caught.
  /^skills\/ci-speedup\/ARCHITECTURE\.md$/,
  // CLASS-fix coupling: a new verify_report.py invariant MUST be classified in grader_seeds.py's
  // TRIAGE_ALLOWLIST (else grader_seeds raises KeyError on the next run). This single exact file —
  // and only the allowlist entry within it — is the one maintainers/ path a class fix may touch.
  /^maintainers\/ci-speedup\/scripts\/grader_seeds\.py$/,
]
// A `..` segment or a leading `/` could escape an allowlisted prefix (the tests/ prefix match would
// otherwise pass `skills/ci-speedup/tests/../SKILL.md`). `git diff --name-only` emits only clean
// repo-root-relative paths, so this is unreachable in practice — but the binding control must not
// DEPEND on that, just as it doesn't depend on the agent's self-check. Reject traversal up front.
const hasTraversal = p => p.startsWith('/') || p.split('/').includes('..')
const isAllowedFixPath = p => !hasTraversal(p) && FIX_ALLOWLIST.some(re => re.test(p))
// ARCHITECTURE.md is allowlisted (path-only) but must NEVER be edited alone — it documents the
// renderer contract a `scripts/*.py` engine change alters, so a doc edit must co-occur with an
// engine edit. These two regexes drive the co-occurrence meta-guard in fixWriteViolations.
const ARCHITECTURE_DOC_RE = /^skills\/ci-speedup\/ARCHITECTURE\.md$/
const ENGINE_SCRIPT_RE = /^skills\/ci-speedup\/scripts\/[^/]+\.py$/
// The out-of-allowlist paths in an agent's reported diff (its violation set). A non-array /
// missing list yields [] — the documented v1 caveat (an agent that under-reports its own diff
// isn't caught; this closes the accidental-stray-edit case, which is the real risk). PLUS the
// co-occurrence meta-guard: a LONE ARCHITECTURE.md edit (allowlisted, so not caught above) with no
// sibling `scripts/*.py` engine change is a stray doc edit and IS flagged — the path-only allowlist
// can't express "only with an engine change", so this set-level rule does. (A class fix that updates
// §12 alongside its engine fix carries both, so it passes.)
const fixWriteViolations = files => {
  const list = (Array.isArray(files) ? files : []).filter(Boolean)
  const violations = list.filter(p => !isAllowedFixPath(p))
  const lonelyArchitecture = list.some(p => ARCHITECTURE_DOC_RE.test(p))
    && !list.some(p => ENGINE_SCRIPT_RE.test(p))
  if (lonelyArchitecture)
    for (const p of list)
      if (ARCHITECTURE_DOC_RE.test(p) && !violations.includes(p)) violations.push(p)
  return violations
}

// --- Committed-report regen routing (see PLAN-dogfood-loop-hardening-v2 Stream 2) --------------
// OBSOLETE PATH (kept as a harmless fallback): `test_committed_reports.py` now verifies a FRESH
// render of each committed `findings.json` (not the STATIC committed `.md`), so a new class
// invariant runs against output that reflects the CURRENT engine — fixing the engine greens the
// guard, with NO committed-example regen needed. This routing should therefore rarely if ever fire;
// a committed-report guard failure now means the engine output is wrong (keep fixing it), not that a
// snapshot is stale. The original rationale is retained below for the fallback's history.
// A CLASS fix adds a new verify_report invariant that RE-DERIVES the truth and asserts the rendered
// report matches. That new invariant, run by `test_committed_reports.py` against the STATIC committed
// worked examples (skills/ci-speedup/reports/**), legitimately FAILS on them — the committed example
// still exhibits the very bug the invariant now catches. The fix can't green that guard without
// regenerating the committed example, and the write-surface FORBIDS editing reports/. So the per-fix
// suite stays red through no fault of the fix. Two bad outcomes without this routing: an honest agent
// returns needs_human with a GENERIC message (no regen guidance); a sloppy agent returns patch_ready
// with a red suite and the integrator's BISECT reverts the (correct) commit and mislabels it "breaks
// the suite". routeCommittedReportFailure detects EXACTLY this shape — the agent's reported failing
// tests are ALL committed-report guards AND the diff is otherwise allowlist-clean (a genuine class
// fix, no stray writes) — and returns a needs_human disposition carrying the regen instruction. The
// caller applies it (downgrading a sloppy patch_ready → needs_human), which holds the fix OUT of
// `ready`, so the integrator never bisects it. Returns null when the shape doesn't match (handled
// normally). Pure + deterministic (unit-tested). Keys on the two committed-report guard FILENAMES.
//
// The disposition's whole purpose is to PRESERVE the agent's COMMITTED class-fix patch so a human can
// land it after regenerating the stale examples — so BOTH the changed-files list AND the `patch` text
// must be NON-EMPTY (the agent actually committed its fix AND captured the diff, per the step-3
// exception, which tells it to finish steps 4-5 before returning). An empty diff or empty patch means
// there is nothing to hand the human, so it is NOT this case (return null → handled as a plain
// needs_human, and the caller logs the "claimed regen but committed nothing" shape loudly). RESIDUAL
// (the documented trust boundary): the workflow has no pytest, so it acts on the agent's SELF-REPORTED
// `failing_tests`. Mis-reporting only ever holds a fix OUT of integration (the safe direction) — it can
// never smuggle a red-suite fix IN. The one gap is an agent that returns patch_ready with an EMPTY
// `failing_tests` despite red committed-report guards: routing can't see it, and the integrator's
// bisect is then the backstop (the no-bisect guarantee is conditional on the agent honestly populating
// `failing_tests`, which step 3 instructs).
// Only test_measured_evidence.py remains a "stale committed DATA" regen case:
// test_committed_reports.py now renders findings.json FRESH, so a failure there is
// a REAL engine/renderer bug to FIX, not a snapshot to regenerate.
const COMMITTED_REPORT_GUARD_RE = /test_measured_evidence\.py(?:::|$)/
const isCommittedReportGuard = t => COMMITTED_REPORT_GUARD_RE.test(String(t == null ? '' : t))
function routeCommittedReportFailure(failingTests, changedFiles, patch) {
  const failing = (Array.isArray(failingTests) ? failingTests : []).filter(Boolean)
  if (!failing.length) return null                                 // suite green / nothing reported
  if (!failing.every(isCommittedReportGuard)) return null          // a real regression elsewhere — not this shape
  const files = (Array.isArray(changedFiles) ? changedFiles : []).filter(Boolean)
  if (!files.length) return null                                   // no committed fix to preserve — not this case
  if (!patch || !String(patch).trim()) return null                 // no captured patch text → nothing to hand the human
  if (fixWriteViolations(files).length) return null                // strayed diff — not a clean class fix
  return {
    outcome: 'needs_human',
    committed_report_regen: true,
    summary:
      'COMMITTED-REPORT REGEN REQUIRED — this class fix\'s new invariant fails ONLY against the '
      + 'stale committed worked example(s) under skills/ci-speedup/reports/** (they still exhibit the '
      + 'bug the invariant now catches), so it is a CORRECT fix, not a regression. The committed-report '
      + 'guard(s) (' + failing.join(', ') + ') can\'t go green until a human regenerates those examples '
      + 'per the committed-report regen discipline: re-fetch the long-pole job logs in a PINNED gh '
      + 'window (--created-before from the committed data_sources), diff to isolate THIS fix\'s delta, '
      + 'surgically patch it onto the prose-bearing committed findings.json, then re-render + verify. '
      + 'Do NOT blind full-rerun (drops prose, drifts imports). Held out of integration so a bisect '
      + 'cannot revert it and mislabel it "breaks the suite".',
  }
}

// --- Diff-level overlap hint for the integrator ----------------------------------------------
// Symptom-signature dedup (the per-bug `seen[sig]` map below) can't catch two fixes that have
// DIFFERENT symptoms but the SAME underlying patch — the #74/#75 case, where one bug was framed
// as "render divergence" and another as "selection cause" yet both rewrote the same lines. Those
// only collapse when you look at the actual diffs. This flags patch-ready fixes that touch the
// SAME engine script file (a flat `scripts/*.py`, not a test or the changelog, which legitimately
// co-change): they are the set at risk of being semantic duplicates OR of textually conflicting,
// so the integrator must reconcile them together. Returns groups of >=2 signatures per shared
// script path; non-overlapping fixes are omitted. Pure + deterministic (unit-tested).
const SCRIPT_PATH_RE = /^skills\/ci-speedup\/scripts\/[^/]+\.py$/
function overlappingScriptGroups(fixes) {
  const byFile = {}
  for (const f of (Array.isArray(fixes) ? fixes : [])) {
    const sig = f && f.signature
    if (!sig) continue
    for (const p of (Array.isArray(f.changed_files) ? f.changed_files : [])) {
      if (!SCRIPT_PATH_RE.test(p)) continue
      if (!byFile[p]) byFile[p] = []
      if (!byFile[p].includes(sig)) byFile[p].push(sig)
    }
  }
  return Object.keys(byFile)
    .filter(file => byFile[file].length >= 2)
    .map(file => ({ file, signatures: byFile[file] }))
}

// A patch_ready fix is INCOHERENT if it claims success but carries no files to apply OR no diff
// text. Both must be held out of integration: an empty-`changed_files` fix has nothing to
// validate; an empty-`patch` fix would otherwise pass the write-surface guard yet be silently
// dropped from `ready` by the `&& p.patch` filter and resurface as a confusing reason-less
// `not_integrated` row. Pure + unit-tested. The `.filter(Boolean)` keeps this symmetric with the
// write-surface guard / routeCommittedReportFailure (which both filter falsy paths) — an all-falsy
// `['', null]` changed_files is just as incoherent as `[]` (and won't slip into `ready`). The patch
// test is `!String(p.patch || '').trim()` (not bare `!p.patch`) so a WHITESPACE-only patch counts as
// incoherent too — matching routeCommittedReportFailure's `!String(patch).trim()`, so the two guards
// agree on whitespace (a `'  \n'` patch is held out here instead of reaching the integrator to be
// dropped as "won't apply").
const isIncoherentPatch = p => !Array.isArray(p.changed_files) || p.changed_files.filter(Boolean).length === 0 || !String(p.patch || '').trim()

// --- Review-stage routing (S3, the centerpiece) ----------------------------------------------
// The 17-repo corpus did NOT catch the Class A authoring bugs — adversarial / silent-failure REVIEW
// did, at PR time, by a human. The loop has no such stage, so the lessons stay the drafter's wishlist.
// S3 adds an INDEPENDENT reviewer agent per drafted fix (the L1-L9 authoring checklist in
// MAINTAINERS.md as its explicit contract); a CONFIRMED defect downgrades the fix patch_ready →
// needs_human (excluded from `ready`/integration), surfaced loudly. The reviewer is an LLM, so the
// BINDING part is this pure routing (verdict → disposition), unit-tested below; the reviewer's
// judgment quality is not unit-testable.
//
// A fix is held for a human IFF the reviewer CONFIRMED a defect (strict `=== true`). A null / errored
// / ambiguous verdict does NOT auto-hold — blocking a fix on a dead or flaky reviewer would let one
// bad review stall every fix, and the consolidated PR is human-reviewed before merge anyway — so a
// non-verdict is surfaced as a review COVERAGE GAP (loud), not silently treated as "clean" nor as a
// confirmed defect. Pure + unit-tested.
const reviewVerdictRoutesToHuman = v => !!(v && v.defect_confirmed === true)
// Apply a reviewer verdict to a drafted fix. Returns 'flagged' (defect confirmed → downgraded to
// needs_human, held out of integration), 'errored' (no usable verdict — a review coverage gap to
// surface; the fix is NOT auto-held), or 'clean' (reviewer cleared it). Mutates `p` only on 'flagged'.
function applyReviewVerdict(p, verdict) {
  if (!verdict || verdict._review_errored) return 'errored'
  if (!reviewVerdictRoutesToHuman(verdict)) return 'clean'
  const lessons = (Array.isArray(verdict.lessons_violated) ? verdict.lessons_violated : []).join(', ')
  p.outcome = 'needs_human'
  p.review_defect = verdict
  p.summary = `INDEPENDENT REVIEW FLAGGED an authoring defect`
    + (lessons ? ` (${lessons})` : '') + `: ${verdict.summary || '(no summary)'} `
    + `Held out of the consolidated PR for a human. ` + (p.summary || '')
  return 'flagged'
}

// OR-combine a PANEL of reviewer verdicts into one (silent-failure-hunter ∪ code-reviewer): the
// re-derivation lessons (L1/L3/L4/L5/L6) are code-reviewer's strength, the silent-drop ones (L2/L8)
// silent-failure-hunter's — and a false NEGATIVE defeats the stage while a false positive is merely a
// needs_human, so we bias toward CATCHING: ANY reviewer confirming a defect holds the fix. All
// reviewers errored/skipped → null (a coverage gap, surfaced — not a silent clean). Otherwise (at
// least one usable verdict, none confirmed) → cleared. Pure + unit-tested.
function combineReviewVerdicts(verdicts) {
  const usable = (Array.isArray(verdicts) ? verdicts : []).filter(v => v && !v._review_errored)
  if (!usable.length) return null   // every reviewer threw / skipped → coverage gap (→ 'errored')
  const confirmed = usable.filter(v => v.defect_confirmed === true)
  if (confirmed.length)
    return {
      defect_confirmed: true,
      lessons_violated: [...new Set(confirmed.flatMap(v =>
        Array.isArray(v.lessons_violated) ? v.lessons_violated : []))],
      summary: confirmed.map(v => v.summary || '(no summary)').join(' | '),
    }
  return { defect_confirmed: false,
    summary: usable.map(v => v.summary || '').filter(Boolean).join(' | ') || 'cleared' }
}

// Reconcile each found bug's FINAL disposition against the integrator's self-report. The integrator
// is an LLM; its `integrated` array is NOT trusted raw (cf. the write-surface guard's "re-check the
// agent's list, never its verdict"). A bug counts as LANDED only if the integrator names it AND it
// was actually a candidate patch handed to the integrator (`readySigs`). This makes
// `integrated + not_integrated === bugs_found` true BY CONSTRUCTION: a held-out needs_human/duplicate
// bug the integrator falsely names can't be re-bucketed as landed, and a hallucinated/duplicated
// signature can't inflate the count. `unknownReported` surfaces a misbehaving integrator loudly.
// Pure + unit-tested.
function reconcileIntegration(bugs, fixes, ready, integration) {
  const intg = integration || {}
  const integratedSet = new Set(Array.isArray(intg.integrated) ? intg.integrated : [])
  const readySigs = new Set((Array.isArray(ready) ? ready : []).map(p => p && p.signature).filter(Boolean))
  const droppedMap = {}
  for (const d of (Array.isArray(intg.dropped) ? intg.dropped : []))
    if (d && d.signature) droppedMap[d.signature] = d.reason
  const fixBySig = {}
  for (const f of (Array.isArray(fixes) ? fixes : [])) if (f && f.signature) fixBySig[f.signature] = f
  const landed = sig => integratedSet.has(sig) && readySigs.has(sig)
  // When the integrator did NOT open a PR (threw / failed), the patches it was HANDED land in
  // not_integrated; prefix those rows so it's clear the INTEGRATION failed, not the draft. Only
  // prefix a bug that was an actual ready candidate (`readySigs`) — a bug held out earlier
  // (needs_human / not_reproduced / write-surface) was never going to be integrated, so the
  // integration-failure note would misdescribe why it didn't land. It keeps its own reason.
  const integFailed = intg.outcome && intg.outcome !== 'pr_opened'
    ? `integration did not open a PR (${intg.outcome})${intg.summary ? ': ' + intg.summary : ''} — ` : ''
  const sigs = (Array.isArray(bugs) ? bugs : []).map(b => b && b.signature).filter(Boolean)
  const not_integrated = sigs.filter(sig => !landed(sig)).map(sig => {
    const f = fixBySig[sig] || {}
    const prefix = (integFailed && readySigs.has(sig)) ? integFailed : ''
    return { signature: sig, outcome: f.outcome || 'failed', reason: prefix + (droppedMap[sig] || f.summary || '') }
  })
  return {
    integrated: sigs.filter(landed).length,
    not_integrated,
    // signatures the integrator claimed as landed that were never candidates — loud, not trusted
    integrator_reported_unknown: [...integratedSet].filter(sig => !readySigs.has(sig)),
  }
}

// Re-key each fix to the CANONICAL bug slug, positionally (fixesRaw is index-aligned with `bugs` —
// the fix fan-out chunks `bugs` in order and parallel() preserves order). The fix agent is asked to
// return a `signature` but is NOT reliably given the slug — `b.signature` reaches it only as the
// display `label` option, never in the prompt body — so as an LLM it tends to echo the bug TITLE
// instead. When it does, the fix/ready/patch-block/integrator signature space diverges
// from `bugs`' slug space, so reconcileIntegration joins on nothing: `landed()` is always false, a
// real landed PR reports `integrated: 0`, and every bug falls to not_integrated/failed with an empty
// reason (fixBySig misses too) — a false negative that hides a green PR. Re-keying here makes the
// join key canonical regardless of what the agent echoed (same "re-check the agent's output, never
// trust its self-report" stance as the write-surface guard), and it flows downstream: `ready` and
// the patch blocks carry the slug, so the integrator sees and echoes the slug. Also fills the null
// (skipped / dead-after-retries) slot, mirroring the audit stage's positional reconciliation. Pure.
function canonicalizeFixes(fixesRaw, bugs) {
  const bs = Array.isArray(bugs) ? bugs : []
  return (Array.isArray(fixesRaw) ? fixesRaw : []).map((p, i) => {
    const signature = (bs[i] && bs[i].signature) || (p && p.signature)
    return p
      ? { ...p, signature }
      : { signature, outcome: 'failed', _fix_errored: true,
          summary: 'fix agent produced no result (skipped or died after retries)' }
  })
}

// Coalesce the integrator's result so the downstream result-assembly (which reads `integration.outcome`,
// `.pr_url`, `.branch`, `.summary` directly) can never deref null. agent() RESOLVES to null on a skip or
// a terminal API death after retries (e.g. "connection closed mid-response"), and the `.catch` on the
// integrate call only fires on a *rejection* — so without this a resolved-null integrator left
// `integration` null and crashed the whole run, discarding every completed audit + fix. Mirrors the
// audit (auditedRaw.map) and fix (fixesRaw.map) stages' positional null reconciliation. Pure + unit-tested.
function coalesceIntegration(integration) {
  return integration || { outcome: 'failed', pr_url: null, branch: null, integrated: [], dropped: [],
    _integrate_errored: true,
    summary: 'integrator produced no result (agent skipped or died after retries — e.g. connection closed mid-response)' }
}

// --- PR-B: class-wide synthesis (loop-self-improvement-upgrades.md §2, Item 1) -----------------
// Closed-vocab `class` enum for audit bugs. Per the spec's explicit alternative ("add a CLOSED-vocab
// `class` enum to the audit bug schema (OR reuse the transcript summary's `root_cause` enum)"), this
// REUSES the transcript self-improvement loop's `root_cause` enum
// (`maintainers/ci-speedup/loops/loop-summary.schema.json`) verbatim rather than inventing a SECOND
// closed vocabulary the two loops would have to hand-keep in sync — and the values already fit a
// dogfood-audit bug (e.g. `estimated-not-measured`, `fabricated-or-unsupported-finding`,
// `coverage-gap-dead-end`, `mis-ranked-lever` are exactly the report-faithfulness defect shapes this
// loop finds). Mirrored verbatim in `grader_seeds.CLASS_ENUM` (Python) — kept in lockstep by
// `test_grader_seeds.py`'s `test_class_enum_matches_this_workflows_bug_class_enum`, which extracts
// THIS array by regex (the same drift-proof pattern `dogfood-retry.test.mjs` uses in the other
// direction for `isTransient`/`isAllowedFixPath`).
const BUG_CLASS_ENUM = [
  'missing-never-rule', 'ambiguous-phase-instruction', 'missing-phase-check', 'scope-overreach',
  'coverage-gap-dead-end', 'estimated-not-measured', 'fabricated-or-unsupported-finding',
  'mis-ranked-lever', 'missing-second-pole-or-finding', 'skipped-verification-after-regen',
  'prescribed-a-fix', 'unscrubbed-or-disclosure-risk', 'tooling-or-environment', 'other',
]

// The EXISTING-guard inventory §1 says PR-B must steer AWAY from (the four class guards PLUS the
// ~16 verify_report checks PLUS `_PHASE0_CHECK_NAMES` — all of which live in these files). The
// workflow is pure JS with no Python import, so this names each guard's OWNING FILE — a bug's
// `suspected_location` naming one of these basenames is "covered" (routes to tighten-existing,
// never proposed as a new guard). This is coarser than an exact `(check, workflow_file)` join (it
// can't tell "the locus IS this check" from "the evidence merely MENTIONS this file"), but
// `suspected_location` is maintainer-facing free text, not a structured locus, so a name match is
// the best available signal — and a false "covered" only OVER-restrains (routes a possibly-novel
// bug to tighten-existing instead of a new sketch), which a maintainer reviewing the sketch catches;
// it can never AUTO-LAND a duplicate guard (the sketch/tighten note is always maintainer-finished).
const GUARD_INVENTORY = [
  { guard: 'verify_report.py (the ~16-check corpus + the Phase-0 contradiction property)', fileRe: /verify_report\.py/ },
  { guard: 'test_evidence_claim_guards.py', fileRe: /test_evidence_claim_guards\.py/ },
  { guard: 'test_claim_scope_guards.py', fileRe: /test_claim_scope_guards\.py/ },
  { guard: 'test_sizing_cap_guard.py', fileRe: /test_sizing_cap_guard\.py/ },
]

// Which existing guard (if any) plausibly OWNS a bug's suspected_location — null if none matches (a
// candidate for a NOVEL class, per §2-B: "if no existing guard plausibly owns the instance, it's
// branch (b), not (a)"). Pure + unit-tested.
function matchGuardByLocus(suspectedLocation) {
  const loc = String(suspectedLocation == null ? '' : suspectedLocation)
  for (const { guard, fileRe } of GUARD_INVENTORY)
    if (fileRe.test(loc)) return guard
  return null
}

// Group the deduped `bugs` list by `class`. A bug with no class (predates this field, or an
// unclassified LLM return) is NOT clustered — it can't participate in ≥2-distinct-repo synthesis
// without a class to key on. It is never dropped: it just falls through to the normal point-fix
// path unchanged (see `unclassified` below). Pure + unit-tested.
function groupByClass(bugs) {
  const groups = {}
  for (const b of (Array.isArray(bugs) ? bugs : [])) {
    const cls = b && b.class
    if (!cls) continue
    if (!groups[cls]) groups[cls] = []
    groups[cls].push(b)
  }
  return groups
}

// Distinct repos/orgs a class cluster spans — mirrors aggregate_lessons' `distinct_sessions`
// discipline (review #1/P6): TWO framings of the SAME bug in ONE repo must not self-promote a
// fleet-wide guard. `repos` is the array the cross-org dedup above already accumulates (every org
// whose audit reported this exact bug). Union ACROSS the cluster's bugs (a class can span multiple
// distinct signatures) so N distinct repos across DIFFERENT signatures of one class still counts,
// not just repeats of one signature. Pure + unit-tested.
function distinctRepos(clusterBugs) {
  const repos = new Set()
  for (const b of clusterBugs)
    for (const r of (Array.isArray(b.repos) ? b.repos : [])) if (r) repos.add(r)
  return repos
}

// spec §2-B: threshold = >= 2 DISTINCT repos/orgs (mirrors aggregate_lessons.RECURRENCE_MIN=2) —
// two framings of one bug in one repo is N=1 and must not trigger fleet-wide synthesis.
const CLASS_CLUSTER_MIN_REPOS = 2

// Route ONE class cluster into the spec's three outcomes. Pure + unit-tested; the LLM-JUDGMENT
// quality of `class`/clustering itself is explicitly OUT OF SCOPE here (spec §3's documented blind
// spot — this validates routing GIVEN correct labels, not label quality):
//   'tighten-existing' — >= CLASS_CLUSTER_MIN_REPOS distinct repos AND at least one bug's locus
//                         matches an existing guard (a LEAK: narrow that guard's predicate).
//   'novel-sketch'      — >= CLASS_CLUSTER_MIN_REPOS distinct repos AND no bug's locus matches an
//                         existing guard (an uncovered class: needs a NEW guard SKETCH).
//   'point-fix-only'    — < CLASS_CLUSTER_MIN_REPOS distinct repos (a singleton, or repeats of the
//                         same repo) regardless of coverage — never synthesize a fleet-wide guard
//                         from under-threshold evidence.
// A grader-seed bug exists BECAUSE an existing `verify_report` check already CAUGHT a
// report-internal inconsistency (grader_seeds.py seeds it from that FAIL) — so its class is
// by-definition COVERED and the owning guard demonstrably works. Keyed on its distinct
// `grader-seed@…` signature namespace, with the `source` tag as a backstop.
function isCoveredGraderSeed(b) {
  return !!(b && ((typeof b.signature === 'string' && b.signature.startsWith('grader-seed@'))
                  || b.source === 'grader-seed'))
}

function routeClassCluster(clusterBugs) {
  const repos = distinctRepos(clusterBugs)
  if (repos.size < CLASS_CLUSTER_MIN_REPOS)
    return { route: 'point-fix-only', distinctRepoCount: repos.size, matchedGuard: null }
  let matchedGuard = null
  for (const b of clusterBugs) {
    const g = matchGuardByLocus(b.suspected_location)
    if (g) { matchedGuard = g; break }
  }
  if (matchedGuard)
    return { route: 'tighten-existing', distinctRepoCount: repos.size, matchedGuard }
  // A covered grader-seed anywhere in the cluster means the class is ALREADY guarded (a check just
  // caught it), so it must NEVER route to 'novel-sketch' — that would sketch a brand-new guard
  // duplicating the check that seeded the bug, the exact re-derivation the inventory rule exists to
  // prevent, and it would hold the loop's most deterministic bugs OUT of the autonomous fix path.
  // Its `suspected_location` is the generic renderer stub (matching no guard basename), so without
  // this it falls through to novel-sketch. A real guard-locus match (a genuine LEAK) still wins
  // above; only absent any match does a covered seed pin the cluster to the normal point-fix path.
  if (clusterBugs.some(isCoveredGraderSeed))
    return { route: 'point-fix-only', distinctRepoCount: repos.size, matchedGuard: null, coveredBySeed: true }
  return { route: 'novel-sketch', distinctRepoCount: repos.size, matchedGuard: null }
}

// Drive the whole classification+routing pass over the deduped `bugs` list. Returns
// `{ routes: { [class]: {route, distinctRepoCount, matchedGuard, bugs} }, unclassified: [bugs w/ no class] }`.
// Pure + unit-tested (the fixture-replay gate for spec §3 item #1).
function classifyAndRoute(bugs) {
  const groups = groupByClass(bugs)
  const routes = {}
  for (const [cls, clusterBugs] of Object.entries(groups))
    routes[cls] = { ...routeClassCluster(clusterBugs), bugs: clusterBugs }
  const unclassified = (Array.isArray(bugs) ? bugs : []).filter(b => !(b && b.class))
  return { routes, unclassified }
}

// Assemble ONE class_sketches[] entry from a class's routing + its (LLM) drafting-agent verdict.
// Pure + unit-tested — the LLM's actual JUDGMENT (is the sketch any good) is out of scope (spec
// §3's stated blind spot); this only assembles/labels what the agent returned, and — the point of
// the 4th fixture case — never silently drops a `landable:false` verdict: a real >= 2-repo cluster
// the agent could NOT sketch cleanly is surfaced as 'not-landable', not discarded, so the maintainer
// still sees WHY. A missing/thrown verdict (`_sketch_errored`) is a coverage gap, surfaced as
// 'errored' — never read as "no cluster found here".
function buildClassSketchEntry(cls, routeInfo, verdict) {
  if (!verdict || verdict._sketch_errored)
    return {
      class: cls, distinct_repo_count: routeInfo.distinctRepoCount, status: 'errored',
      summary: (verdict && verdict.summary) || 'sketch drafting agent produced no result (skipped or died after retries)',
    }
  const landable = verdict.landable !== false   // default true unless the agent explicitly says no
  return {
    class: cls,
    distinct_repo_count: routeInfo.distinctRepoCount,
    status: landable ? 'sketch' : 'not-landable',
    definition: verdict.definition || '',
    candidate_predicate: verdict.candidate_predicate || '',
    synthetic_instances: Array.isArray(verdict.synthetic_instances) ? verdict.synthetic_instances : [],
    negative_cases: Array.isArray(verdict.negative_cases) ? verdict.negative_cases : [],
    maintainer_next_steps: verdict.maintainer_next_steps || '',
    summary: landable
      ? 'Guard SKETCH for the maintainer to finish and land (NEVER auto-landed by this loop).'
      : `FLAGGED NOT LANDABLE by the drafting agent: ${verdict.summary || '(no reason given)'} — `
        + `needs further investigation before a guard can be authored.`,
  }
}

const AUDIT = {
  type: 'object', additionalProperties: true, required: ['org', 'bugs'],
  properties: {
    org: { type: 'string' }, repo: { type: ['string', 'null'] },
    resumed: { type: 'boolean' },   // true when short-circuited from a complete prior run (no re-run)
    bugs: {
      type: 'array', items: {
        type: 'object', additionalProperties: true,
        required: ['title', 'signature', 'suspected_location'],
        properties: {
          title: { type: 'string' }, severity: { type: 'string' },
          suspected_location: { type: 'string' }, evidence: { type: 'string' },
          signature: { type: 'string' },   // "<slug>@<file>:<symbol>" — dedups across orgs
          // PR-B: closed-vocab defect class (see BUG_CLASS_ENUM above). OPTIONAL — the AUDIT schema
          // is open (additionalProperties:true), and grader-seed bugs already carry it from Python
          // (grader_seeds.CLASS_ENUM, kept in lockstep); an LLM-audit bug missing it just never
          // clusters (classifyAndRoute's `unclassified` — falls through to a normal point-fix).
          class: { type: 'string', enum: BUG_CLASS_ENUM },
        },
      },
    },
  },
}

const FIX = {
  type: 'object', additionalProperties: true, required: ['signature', 'outcome', 'changed_files'],
  properties: {
    signature: { type: 'string' },
    // A fix agent now drafts a PATCH; the integrator turns all patches into one PR. So the
    // success outcome is "patch_ready", not "pr_opened".
    outcome: { type: 'string', enum: ['patch_ready', 'not_reproduced', 'needs_human', 'failed'] },
    summary: { type: 'string' },
    // Full `git diff origin/main...HEAD` text — the integrator applies it onto the consolidated
    // branch. "" when the agent created no branch (not_reproduced / needs_human / failed).
    patch: { type: 'string' },
    // Raw `git diff --name-only origin/main...HEAD` from the fix worktree — the workflow
    // re-validates this list against FIX_ALLOWLIST (write-surface guard). [] when no branch.
    changed_files: { type: 'array', items: { type: 'string' } },
    // Names of any NEW top-level helpers the fix introduced — lets the integrator spot two fixes
    // adding equivalent helpers (the duplicate-fix case) even when the patches don't textually conflict.
    new_symbols: { type: 'array', items: { type: 'string' } },
    // The failing pytest ids/paths when (and only when) the suite stays red because the agent's new
    // CLASS invariant fails against the STALE committed worked examples under reports/ (which the
    // write-surface forbids editing). The workflow routes such a fix to a human for committed-example
    // regen (routeCommittedReportFailure) instead of letting the integrator bisect-drop it. [] / omitted
    // when the suite is green or any non-committed-report test failed (a real problem with the fix).
    failing_tests: { type: 'array', items: { type: 'string' } },
  },
}

// The S3 reviewer's verdict on one drafted fix (an independent reviewer agent over the diff +
// the L1-L9 authoring checklist). `defect_confirmed: true` is the ONLY thing that holds a fix
// out (strict, via reviewVerdictRoutesToHuman); the rest is for the surfaced disposition.
const REVIEW_VERDICT = {
  type: 'object', additionalProperties: true, required: ['defect_confirmed', 'summary'],
  properties: {
    defect_confirmed: { type: 'boolean' },   // true ⇒ a real authoring defect the checklist forbids
    lessons_violated: { type: 'array', items: { type: 'string' } },   // e.g. ["L1", "L4"]
    severity: { type: 'string' },
    summary: { type: 'string' },             // what the defect is + why it's a contradiction/false result
  },
}

// PR-B: the drafting agent's verdict for ONE uncovered class cluster (spec §2, Item 1) — a SKETCH
// for the maintainer to finish, never an autonomously-landed guard. `landable:false` is a legitimate
// outcome (a real >= 2-repo cluster the agent could not cleanly reduce to one predicate) and is
// still returned, not omitted — buildClassSketchEntry surfaces it as 'not-landable', never silently.
const CLASS_SKETCH = {
  type: 'object', additionalProperties: true,
  required: ['definition', 'candidate_predicate', 'synthetic_instances', 'negative_cases'],
  properties: {
    definition: { type: 'string' },             // the class, in the maintainer's terms
    candidate_predicate: { type: 'string' },     // a starting-point predicate description (prose, not code)
    synthetic_instances: {                       // >= 2, spanning the class, incl. a scope/monorepo variant
      type: 'array', items: {
        type: 'object', additionalProperties: true,
        required: ['description', 'includes_scope_or_monorepo_variant'],
        properties: {
          description: { type: 'string' },
          includes_scope_or_monorepo_variant: { type: 'boolean' },
        },
      },
    },
    negative_cases: { type: 'array', items: { type: 'string' } },   // must-NOT-fire cases
    maintainer_next_steps: { type: 'string' },
    // false ⇒ the agent could NOT reduce this cluster to a clean, non-vacuous, non-false-positive
    // predicate (e.g. the instances don't actually share one root cause on closer reading). Default
    // true when omitted (a plain sketch).
    landable: { type: 'boolean' },
    summary: { type: 'string' },   // required only when landable:false (why) — not schema-enforced, checked in prose
  },
}

// The integrator's single consolidated result — one PR for the whole dogfood session.
const INTEGRATION = {
  type: 'object', additionalProperties: true, required: ['outcome', 'integrated', 'dropped'],
  properties: {
    outcome: { type: 'string', enum: ['pr_opened', 'nothing_to_integrate', 'failed'] },
    pr_url: { type: ['string', 'null'] }, branch: { type: ['string', 'null'] },
    // True when the integrator ADOPTED a pre-existing open consolidated PR (a prior attempt opened
    // it then the connection dropped) instead of opening a new one — surfaced as a dedicated result
    // flag so an adoption is distinguishable from a fresh integration, not laundered as one.
    adopted_existing: { type: 'boolean' },
    integrated: { type: 'array', items: { type: 'string' } },   // signatures that landed in the PR
    dropped: {   // signatures intentionally NOT landed (duplicate / failed to apply / broke tests), with why
      type: 'array', items: {
        type: 'object', additionalProperties: true, required: ['signature', 'reason'],
        properties: { signature: { type: 'string' }, reason: { type: 'string' } },
      },
    },
    summary: { type: 'string' },
  },
}

// Retry tallies, surfaced in the return so a throttly run is visible (not silently slow).
const retried = {}        // org → transient-retry count (pre-run + run + audit stages)
const fixRetried = {}     // signature → transient-retry count (fix stage)

// Per org: RUN THE REAL SKILL, then audit its report for skill bugs. One subagent, no
// reimplementation — it invokes ci-speedup exactly as a user would (incl. phase-4a). Wrapped in
// withRetry so a transient throttle backs off + retries instead of discarding the whole run.
const runOrg = org => () => withRetry(
  () => agent(
    `Dogfood the ci-speedup skill against GitHub org "${org}".\n\n` +
    (FORCE
      ? `0. FORCE mode: run completely fresh. FIRST, before any clone, DELETE \`.ci-speedup-dogfood/${org}/status.json\` (ideally \`rm -rf\` the whole \`.ci-speedup-dogfood/${org}/\` dir). This is REQUIRED: status.json is written only at the END of a successful run, so a prior COMPLETE run left one on disk — if this forced re-run is then interrupted before its own step-4 write, that stale file would survive and a later non-force resume would wrongly treat the org as "done" with pre-force results. Clearing it up front means an interrupted force run leaves NO status.json and is correctly seen as incomplete. Still overwrite \`status.json\` at the end as in step 4.\n`
      : `0. RESUME CHECK — do this FIRST, before any clone: if \`.ci-speedup-dogfood/${org}/status.json\` exists AND parses as a complete prior result (valid JSON with an \`org\` string and a \`bugs\` array — it is written ONLY as the final step of a successful run, so its presence means "done"), this org is already complete: return its exact {org, repo, bugs} with \`resumed: true\` added, and do NOT re-clone or re-run. If the dir is missing OR holds a half-written run with NO \`status.json\`, treat the org as NOT done and proceed with steps 1-4 normally.\n`) +
    `1. Pick the org's highest-starred PUBLIC, non-fork repo that has a \`.github/workflows/\` dir (use \`gh search repos --owner ${org} --sort stars\` then check contents). Shallow-clone it.\n` +
    `2. Run the REAL ci-speedup skill on that clone, EXACTLY as a user would: read and follow ${SKILL}/SKILL.md end to end — the data pass, the render, the **phase-4a LLM gap-fill** for any coverage-gap pole, and verify. DO NOT reimplement, shortcut, or skip any phase; invoke the skill's own scripts as SKILL.md directs.${FAST ? ' FAST MODE: still run EVERY phase, but pass `--target 5` to the data pass (`run.py`) so it samples 5 runs per workflow instead of the default 10 — a smaller sample (quicker, lower-fidelity p50s), NOT a skipped phase. Accept the reduced sampling precision; this pass is for speed, not a sizing-accuracy audit.' : ''} Clone into and stage ALL outputs under \`.ci-speedup-dogfood/${org}/\` (gitignored) — your OWN per-org dir, since other org runs execute concurrently from this same checkout and must not collide.\n` +
    `3. Audit the rendered report against the skill source (${SKILL}/SKILL.md, scripts/) for BUGS IN THE SKILL — a mis-scoped spine, a mislabeled pole, a claim findings.json contradicts, a sizing/floor error, a pole that wrongly dead-ends. Ground each bug in the report prose + the findings.json field + the workflow YAML. Do NOT invent bugs — an empty list is a GOOD result. For EACH bug, also set \`class\` to the single closest label from this CLOSED vocabulary (do not invent a new one; pick "other" only if truly nothing else fits): ${JSON.stringify(BUG_CLASS_ENUM)}. This \`class\` is deterministic PLUMBING downstream (loop-self-improvement-upgrades.md §2, Item 1) — clustering the SAME class across >= 2 DISTINCT repos is what decides whether the loop proposes a guard sketch, so classify by the underlying DEFECT SHAPE (what kind of report-faithfulness failure this is), not by which file happens to be involved.\n` +
    `3b. STRUCTURED-GRADER SEEDS (deterministic second bug source — do NOT skip): run \`python3 maintainers/ci-speedup/scripts/grader_seeds.py --report <the rendered report .md> --findings <the findings .json the skill produced> --skill-repo ${SKILL}\`, pointing it at THIS org's rendered report + findings under \`.ci-speedup-dogfood/${org}/\`. It runs verify_report + the consumer-divergence probe and prints \`{seeds, triage, excluded, skipped, divergence}\` (a deterministic, triage-allowlisted mapping — NOT an LLM verdict). Then:\n` +
    `   - APPEND every \`seeds[]\` entry to your \`bugs\` array VERBATIM — each is already a full audit bug (its \`signature\` is in the distinct \`grader-seed@check:<…>\` namespace, so it dedups with other grader seeds across orgs and never collides with your \`<slug>@<file>:<symbol>\` audit bugs).\n` +
    `   - For each \`triage[]\` entry, ADJUDICATE it yourself: include it in \`bugs\` ONLY if it is a real SKILL bug. Judge by type: (a) ci-secure-template leakage — real only if it is in the skill's OWN prose, NOT the audited repo's content; (b) a provenance mismatch — real only if NOT just this loop running from an unmerged/dirty branch; (c) a \`consumer-divergence\` — this rides a CRUDE proxy that over-counts (~44% vs ~13% real), so include it ONLY if the report genuinely headlines the WRONG pole (a real skill mislabel), NOT an immaterial frequency-vs-p50 ordering difference or a harness-side / managed-bot mis-pick. Otherwise drop it. Never blind-fix a triage item.\n` +
    `   - IGNORE \`excluded[]\` (run/harness artifacts, recorded for transparency only).\n` +
    `   - COVERAGE-GAP CHECK (no silent drops): inspect \`skipped[]\` and \`divergence\`. A \`skipped[]\` entry whose \`disposition\` is \`auto-seed\` AND whose check couldn't run because the findings were unreadable means a real bug-detector did NOT get to check — flag that as a coverage gap in your result (a SKIP is "couldn't check", NOT "clean"). Likewise if \`divergence.ran\` is false, note the probe didn't run; do NOT assume "no divergence".\n` +
    `   Do NOT re-map the grader output by hand or re-run verify_report to second-guess it — the script IS the mapping. If it errors, say so in your result rather than silently skipping the seeds.\n` +
    `4. As the LAST action of a successful run (AFTER the audit + grader seeding are complete), write your {org, repo, bugs} result to \`.ci-speedup-dogfood/${org}/status.json\`. Write it LAST so a run killed mid-way leaves NO status.json and is correctly seen as incomplete (re-runnable) on a later resume.\n\n` +
    `Return {org, repo, bugs:[{title, severity, suspected_location, evidence, signature, class}]} (add \`resumed: true\` only on the step-0 short-circuit). signature = "<short-slug>@<file>:<symbol>" from suspected_location for bugs YOU find; grader seeds keep their own \`grader-seed@check:<…>\` signature (and their own \`class\`) verbatim.`,
    { model: 'opus', label: org, phase: 'Run + audit', schema: AUDIT }),
  { onRetry: n => { retried[org] = n } })
  // `_run_errored` is the crash sentinel — a dedicated flag (NOT a free-text `note`, which
  // a successful agent could also return under additionalProperties:true and be mis-flagged).
  // NOTE: this .catch only fires when agent() *rejects* (incl. after withRetry exhausts a
  // transient throttle). Its documented failure mode is to *resolve to null* (skipped, or dead
  // after retries) — that path is reconciled positionally just below, so a dead run can never be
  // silently dropped by a Boolean filter.
  .catch(e => ({ org, repo: null, bugs: [], _run_errored: true, error: 'run errored: ' + errMsg(e) }))

// Pace the fan-out: run RUN_CONCURRENCY orgs at a time, not all at once. Chunks run in ORGS
// order and each chunk preserves order, so auditedRaw stays index-aligned with ORGS for the
// positional reconciliation below.
const auditedRaw = []
for (const group of chunk(ORGS, RUN_CONCURRENCY)) {
  // Hardening: the token ceiling is checked between chunks — a crossed budget stops SCHEDULING;
  // orgs skipped here surface as explicit errored rows (re-runnable via resume), never a shrunk list.
  if (overBudget('run')) {
    auditedRaw.push(...group.map(org => ({ org, repo: null, bugs: [], _run_errored: true,
      error: `not run: this run's token budget exhausted (~${runSpend()} run output tokens >= ${TOKEN_BUDGET} ceiling) — re-run to resume` })))
    continue
  }
  auditedRaw.push(...await parallel(group.map(runOrg)))
}

// Positional reconciliation: a null entry means agent() resolved to null (skipped / dead
// after retries) and the .catch never saw it. Map it — by its ORGS index — to an explicit
// errored row, so every scouted org yields exactly one labeled result and the org-count
// can never silently shrink.
const audited = auditedRaw.map((a, i) => a || {
  org: ORGS[i], repo: null, bugs: [], _run_errored: true,
  error: 'run produced no result (agent skipped or died after retries)',
})

// Dedup by signature across orgs. Fall back to a synthesized key when an agent omits
// signature — the per-bug index keeps the fallback collision-free, so two distinct
// sig-less bugs (even same repo/location/title) never collapse into one slot.
const seen = {}, bugs = []
for (const a of audited)
  (a.bugs || []).forEach((b, i) => {
    const sig = b.signature || `nosig:${a.org}:${a.repo || ''}:${i}:${b.suspected_location || ''}:${b.title || ''}`
    if (seen[sig]) { if (a.repo) seen[sig].repos.push(a.repo); return }
    seen[sig] = { ...b, signature: sig, repos: a.repo ? [a.repo] : [] }
    bugs.push(seen[sig])
  })

// --- PR-B: class-wide synthesis routing (loop-self-improvement-upgrades.md §2, Item 1) ---------
// Pure JS plumbing, cheap enough to run on EVERY invocation (incl. --audit-only): group the
// deduped bugs by `class`, route each cluster (tighten-existing / novel-sketch / point-fix-only).
// Only the DRAFTING AGENT for 'novel-sketch' clusters is part of the slow tail (below, skipped
// under --audit-only) — the routing itself is informative even on a fast smoke run.
const classRouting = classifyAndRoute(bugs)
// Serialize routes for the result payload (bug OBJECTS stay internal; only signatures surface).
const classRoutingSummary = Object.fromEntries(
  Object.entries(classRouting.routes).map(([cls, r]) => [cls, {
    route: r.route,
    distinct_repo_count: r.distinctRepoCount,
    matched_guard: r.matchedGuard,
    bug_signatures: r.bugs.map(b => b.signature).filter(Boolean),
  }]))

// --- Audit-only / fast exit (skip the Fix + integrate tail) ----------------------------------
// `--audit-only` / `--fast` stop here: the Fix fan-out (one effort:'high' worktree agent per bug
// that reproduces, red→greens the suite, and patches) plus the serial integrator are the loop's
// slow tail; a DETECTION / smoke run doesn't need them. Return the full bug list (LLM-audit bugs +
// grader seeds) so the run still surfaces everything it found — only the auto-fix/PR is skipped.
// `errored`/`resumed` are recomputed here (the full path computes them later) so an audit-only run
// still reports a crashed/coverage-gap org loudly, never as a silent clean pass.
if (AUDIT_ONLY) {
  const erroredA = audited.filter(a => a._run_errored)
  const resumedA = audited.filter(a => a.resumed && !a._run_errored).map(a => a.org)
  return {
    mode: FAST ? 'fast (audit-only; run.py --target 5)' : 'audit-only',
    orgs_scouted: ORGS.length,
    audited: audited.map(a => ({
      org: a.org, repo: a.repo || null, bugs: (a.bugs || []).length,
      ...(a._run_errored ? { error: a.error } : {}),
    })),
    errored: erroredA.map(a => ({ org: a.org, error: a.error })),   // loud coverage gap — re-run these
    resumed: resumedA,
    retried,
    // The spend ceiling's verdict (never-again-3.5M): exhausted=true means orgs above were
    // skipped with explicit errored rows — re-run to resume them, or raise --token-budget=<N>.
    // run_spent_output_tokens is the DELTA the ceiling actually governs (this run's own spend, #48);
    // session_spent_output_tokens is the raw session-cumulative pool reading, kept for operator
    // context. probe_error != null means budget.spent() THREW: the ceiling was blind this run (a
    // coverage gap), so run_spent_output_tokens: 0 here must NOT be read as a verified-low bounded run.
    token_budget: { ceiling: TOKEN_BUDGET, run_spent_output_tokens: runSpend(),
                    session_spent_output_tokens: spentNow(),
                    exhausted: budgetStoppedAt !== null, stopped_at_stage: budgetStoppedAt,
                    probe_error: budgetProbeError },
    bugs_found: bugs.length,
    bugs,   // the full detection output (LLM-audit bugs + grader seeds) — what audit-only exists to surface
    // The DETERMINISTIC routing preview (which classes would cluster, and how) — cheap, so it still
    // runs under --audit-only. The SKETCH-drafting agent itself does NOT run here (part of the slow
    // tail); re-run without --audit-only/--fast to actually draft any novel-sketch cluster's sketch.
    class_routing: classRoutingSummary,
    note: 'Fix + integrate phases SKIPPED (audit-only/fast). Re-run WITHOUT --audit-only/--fast to draft fixes and open a consolidated PR (and any class guard sketches).',
  }
}

// --- Fix → draft PATCHES (not per-bug PRs) ---------------------------------------------------
// Each distinct bug gets a worktree-isolated fix agent that reproduces it, writes the smallest
// fix + a red→green regression test + a changelog entry, runs the suite green — then emits a
// PATCH instead of opening a PR. The integrator below reconciles all patches into ONE consolidated
// PR: one review surface, and two fixes that are secretly the same edit (different symptoms, same
// patch — the #74/#75 case) get merged at integration instead of racing as duplicate PRs. Same
// transient-resilience as the run stage.
const fixBug = b => () => withRetry(
  () => agent(
  `Draft a fix for ONE ci-speedup skill bug as a PATCH, in this isolated git worktree off main. Do NOT push and do NOT open a PR — the workflow integrates all patches into a single consolidated PR afterwards.\n\n` +
  (b._tighten_existing_guard
    ? `PR-B ROUTING NOTE — this bug is part of a COVERED-CLASS LEAK (loop-self-improvement-upgrades.md §2-B(a)): its class already recurred across >= ${CLASS_CLUSTER_MIN_REPOS} distinct repos, and an EXISTING guard plausibly owns it — **${b._tighten_existing_guard}** — but its predicate was too NARROW and missed this instance (a false-negative, the pre-#99 \`_cmp_name\` exact-name-compare shape: canonical example is a guard that string-matched a check name and silently never intersected a scoped \`@scope/pkg build\` variant). Your job: TIGHTEN that existing guard's predicate (do NOT author a brand-new, parallel guard for the same class) + add THIS instance as a fixture proving the narrower predicate now catches it. If the tightened predicate represents a NEW cross-seam CONTRADICTION (per \`measure_contradictions.py\`'s own definition of that term, not merely a routine FAIL), also add its Check.name to \`measure_contradictions._PHASE0_CHECK_NAMES\` so the panel-wide contradiction rate reflects it — otherwise leave that set alone.\n\n`
    : '') +
  `Bug: ${b.title}\nSuspected location: ${b.suspected_location}\nEvidence: ${b.evidence || '(see audit)'}\nSeen on: ${b.repos.join(', ') || '(n/a)'}\n\n` +
  `1. Confirm it reproduces on the CURRENT tree by tracing the code. If it does not, STOP and return outcome:"not_reproduced".\n` +
  `2. Branch from main so the patch carries ONLY your fix: \`git checkout -b ci-speedup-dogfood-fix-<slug> origin/main\` (pick a deterministic <slug> from the bug). Use the EXISTING origin/main ref — do NOT run \`git fetch\` (the parallel fix agents share one git dir, so concurrent fetches collide on the packed-refs lock). Do NOT branch off the current HEAD (the loop may be launched from an unmerged branch). Write a regression test that FAILS first (run it, confirm red).\n` +
  `3. DEFAULT TO A CLASS FIX, not a one-off patch — the point is to drain the whole class so the same defect can't reappear on the next repo's report. First ask: can a DETERMINISTIC INVARIANT catch this bug — a check in \`${SKILL}/tests/verify_report.py\` that RE-DERIVES the truth from the findings JSON (\`pr_critical_path\`, especially the per-PR \`populations\` ground truth) and asserts the rendered report matches? Most ci-speedup faithfulness / sizing / framing / labeling bugs CAN be expressed this way (a wrong addressable ceiling, a silently-dropped spine check, a mislabeled on/off-path pole, a fabricated structural lever are all re-derivable from the data — never a proxy of the renderer).\n` +
  `   • CLASS PATH (the default): add or strengthen that \`verify_report.py\` check so it FAILS on this report AND on ANY report of the class. Register it in \`run_checks\`, and classify it in \`maintainers/ci-speedup/scripts/grader_seeds.py\` \`TRIAGE_ALLOWLIST\` (AUTO_SEED for a report-internal-consistency property; TRIAGE if env/content-coupled) — that wires the new invariant back into this very loop so the class stays caught forever. THEN fix the ENGINE code (\`${SKILL}/scripts/*.py\`) until the new invariant is green across reports. Your regression test IS the new invariant: make it red first, green after. If your engine fix changes the renderer/measurement contract that \`${SKILL}/ARCHITECTURE.md\` §12 documents, update that section IN THE SAME PATCH — ARCHITECTURE.md is allowlisted, but ONLY when it co-occurs with your \`scripts/*.py\` engine change (a lone doc edit is rejected by the write-surface guard). AUTHOR your invariant against the L1-L9 checklist in \`maintainers/ci-speedup/MAINTAINERS.md\` (section "The L1-L9 invariant-authoring checklist") — re-derive the ground truth from findings.json (never lossy rendered text), mirror the engine's exact keying/metric/selection, suppress only the exact contradiction, and pin your renderer literals. An INDEPENDENT reviewer agent will check your patch against that same checklist before integration; a confirmed defect sends your fix to needs_human, so get it right here.\n` +
  `   • INSTANCE PATH (fallback ONLY, and you must justify it): only if the bug genuinely cannot be expressed as a re-derivation invariant (a true one-off with no general property) do you make the smallest code fix + a bespoke repro test. State plainly WHY it can't generalize.\n` +
  `   ITERATE using ONLY your new invariant/test (\`python3 -m pytest <path> -q\` or \`-k <name>\`) until red→green. THEN, as the FINAL gate, run the WHOLE suite ONCE: BOTH \`python3 -m pytest ${SKILL} -q\` AND \`python3 -m pytest maintainers/ci-speedup -q\` must be green. Run the maintainers suite UNCONDITIONALLY (it is seconds): it is what catches a forgotten/misnamed \`grader_seeds.py\` TRIAGE_ALLOWLIST classification (which would make grader_seeds raise KeyError and go dark on the next dogfood run) BEFORE your patch leaves the loop. The single end-of-step full-suite run is REQUIRED.\n` +
  `   • NOTE — \`test_committed_reports.py\` renders each committed \`findings.json\` FRESH with the current renderer, so a failure there is a REAL engine/renderer bug: FIX it via the class/instance path above, do NOT treat it as a regen case. The exception below applies ONLY to \`test_measured_evidence.py\` (static hand-written assertions on the committed \`findings.json\` DATA, which a data-shape change can legitimately stale).\n` +
  `   • COMMITTED-DATA EXCEPTION (do NOT fight it): a CORRECT new CLASS invariant may FAIL when \`test_measured_evidence.py\` runs against the STATIC committed \`findings.json\` under \`${SKILL}/reports/**\` — that committed data still EXHIBITS the very bug your invariant now catches, and the write-surface FORBIDS you to edit \`reports/\`. If — and ONLY if — the suite's ONLY remaining failure is that committed-data guard (every other test green), do NOT try to force it green and do NOT edit \`reports/\` or any \`findings*.json\`. Instead, your fix is CORRECT and must be PRESERVED for a human to land after they regenerate the examples — so STILL DO steps 4 and 5: commit your engine fix + new invariant + changelog (+ ARCHITECTURE.md §12 if you changed that contract) and capture the full \`git diff origin/main...HEAD\` patch. THEN return outcome:"needs_human" with a NON-EMPTY \`patch\` + \`changed_files\` (your committed fix) AND \`failing_tests\` set to those failing committed-report pytest ids/paths. (Do NOT return an empty patch — the whole point is to hand your fix to the human; an empty diff means there is nothing to land.) The workflow routes your committed patch to a human for committed-example REGENERATION and holds it out of integration so it is never bisect-dropped as "breaks the suite". If ANY other test fails, that is a real problem with your fix — keep iterating; this exception does NOT apply.\n` +
  `4. Add a changelog fragment: ONE new file ${SKILL}/changelog.d/<slug>.fixed.md (format in ${SKILL}/changelog.d/README.md) holding a dated bullet that notes CLASS vs INSTANCE and the invariant name. Do NOT edit ${SKILL}/CHANGELOG.md — CI refuses hand edits. Stage ONLY the files you touched (explicit paths — never \`git add -A\`) and COMMIT them on your branch with the repo-mandated \`Co-Authored-By:\` trailer, so the patch is self-contained.\n` +
  `4b. WRITE-SURFACE CHECK: run \`git diff --name-only origin/main...HEAD\` and confirm EVERY path is inside the allowlist — \`skills/ci-speedup/scripts/*.py\` (the engine fix), \`skills/ci-speedup/tests/**\` (the invariant + tests), one \`skills/ci-speedup/changelog.d/<slug>.<kind>.md\` fragment, \`skills/ci-speedup/ARCHITECTURE.md\` (the §12 contract doc — allowed ONLY when your diff ALSO changes a \`scripts/*.py\` engine file; a lone ARCHITECTURE.md edit is rejected), and — ONLY when the CLASS path added a new \`verify_report.py\` check that must be classified — \`maintainers/ci-speedup/scripts/grader_seeds.py\` (the \`TRIAGE_ALLOWLIST\` entry, nothing else in that file). A fix must touch NOTHING else: NOT \`references/\`, \`evals/\`, \`reports/\`, any \`findings*.json\`, \`SKILL.md\`, or anything else outside those paths. If ANY path is outside (or ARCHITECTURE.md appears without an engine \`scripts/*.py\` change), STOP and return outcome:"needs_human" with the offending paths. (The workflow re-validates the list you return; don't rely on this self-check alone.)\n` +
  `5. Emit your work as a PATCH: capture the FULL \`git diff origin/main...HEAD\` text. Do NOT push, do NOT \`gh pr create\`.\n` +
  `If you cannot produce a clean, fully-tested fix, STOP and return outcome:"needs_human" with a precise summary. (But if the ONLY thing standing between you and green is the \`test_measured_evidence.py\` committed-data guard, that is the COMMITTED-DATA EXCEPTION above, NOT this catch-all: commit your fix and set \`failing_tests\` so it routes to regen instead of reading as an unfinished fix.)\n\n` +
  `Return {signature, outcome, patch, changed_files, new_symbols, failing_tests, summary}. outcome: "patch_ready" | "not_reproduced" | "needs_human" | "failed". patch = the full \`git diff origin/main...HEAD\` text ("" if you created no branch). changed_files = the raw \`git diff --name-only origin/main...HEAD\` list (the workflow re-validates it against the allowlist). new_symbols = names of any NEW top-level functions/helpers your fix introduced (so the integrator can spot two fixes adding equivalent helpers). failing_tests = the pytest ids/paths still failing ONLY because of the committed-data exception in step 3 (\`test_measured_evidence.py\` against the stale committed findings.json); [] otherwise.`,
  { model: 'opus', label: b.signature, phase: 'Fix', schema: FIX, isolation: 'worktree', effort: 'high' }),
  { onRetry: n => { fixRetried[b.signature] = n } })
  // `_fix_errored` distinguishes a fix agent that *threw* from one that legitimately
  // returned outcome:"failed" — different operator responses (retry the harness vs.
  // investigate the bug). Mirrors `_run_errored` in the audit phase.
  .catch(e => ({ signature: b.signature, outcome: 'failed', _fix_errored: true, summary: 'fix agent threw: ' + errMsg(e) }))

// --- PR-B: split bugs by class routing BEFORE the fix fan-out ---------------------------------
// 'novel-sketch' bugs are held OUT of the normal autonomous fix path entirely — the spec is
// explicit that a guard sketch is for the MAINTAINER to finish, never auto-landed, so these bugs
// get a class-level SKETCH (below) instead of a per-bug fixBug agent. 'tighten-existing' bugs still
// go through the normal fixBug agent, just with the extra routing note (above) telling it which
// guard to narrow. 'point-fix-only' / unclassified bugs are completely unaffected.
const bugRouteBySig = {}
for (const [cls, r] of Object.entries(classRouting.routes))
  for (const b of r.bugs) if (b.signature) bugRouteBySig[b.signature] = { route: r.route, cls, matchedGuard: r.matchedGuard }
for (const b of bugs) {
  const rc = b.signature && bugRouteBySig[b.signature]
  // Stamp the tighten note PER-BUG (M1): only on a bug whose OWN locus owns a guard. A cluster is
  // 'tighten-existing' because SOME bug matched, but a bug in that cluster whose own locus matches
  // no guard must not receive a note steering its fix agent at an unrelated guard.
  if (rc && rc.route === 'tighten-existing') {
    const own = matchGuardByLocus(b.suspected_location)
    if (own) b._tighten_existing_guard = own
  }
}
const sketchHeldBugs = bugs.filter(b => b.signature && bugRouteBySig[b.signature]?.route === 'novel-sketch')
const fixableBugs = bugs.filter(b => !(b.signature && bugRouteBySig[b.signature]?.route === 'novel-sketch'))

// --- PR-B: draft ONE guard-sketch agent per uncovered (>= 2-distinct-repo, no matching guard)
// class cluster — NOT one per bug. The sketch is grounded in the REAL instances found (title +
// evidence + suspected_location, across their distinct repos) and seeded with the FULL
// existing-guard inventory so it steers away from re-deriving a guard that already exists (§2-B).
// Read-only + worktree-isolated like the S3 reviewers: it must NEVER write, commit, or open a PR —
// its whole output is a SKETCH for a human. Paced like the fix fan-out.
const classSketchRetried = {}
const sketchOneClass = cls => () => withRetry(
  () => agent(
    `Draft a GUARD SKETCH for ONE uncovered ci-speedup skill defect CLASS, in this isolated git worktree off main. This is READ-ONLY work: do NOT write, edit, or commit ANY file, do NOT run \`git apply\`, do NOT open a PR. Your entire output is the structured sketch below, for a MAINTAINER to finish and land by hand — you are NOT authoring or landing a guard.\n\n` +
    `Class: ${cls}\n` +
    `This class recurred across ${classRouting.routes[cls].distinctRepoCount} DISTINCT repos with NO existing guard plausibly owning it (loop-self-improvement-upgrades.md §2-B(b)). Real instances found:\n` +
    classRouting.routes[cls].bugs.map((b, i) =>
      `  ${i + 1}. [${(b.repos || []).join(', ') || 'repo n/a'}] ${b.title}\n     location: ${b.suspected_location || '(n/a)'}\n     evidence: ${b.evidence || '(n/a)'}`
    ).join('\n') + '\n\n' +
    `EXISTING-guard inventory you must steer AWAY from (do not propose something these already do): ` +
    GUARD_INVENTORY.map(g => g.guard).join('; ') + `. If, on inspection, one of THESE actually does plausibly own this class after all, say so in \`summary\` and set \`landable:false\` rather than proposing a duplicate.\n\n` +
    `Produce: (1) a plain-English \`definition\` of the class (the shared root cause, not just the shared symptom); (2) a \`candidate_predicate\` — prose describing a deterministic check a maintainer could implement in \`${SKILL}/tests/verify_report.py\` that would catch every instance of this class by RE-DERIVING the truth from findings.json (never a proxy of rendered text); (3) \`synthetic_instances\` — AT LEAST 2 synthetic (not necessarily from the real instances above) examples spanning the class, and AT LEAST ONE of them must be a scope/monorepo variant (a scoped package name like \`@scope/pkg\`, or two same-named packages in one monorepo) — the pre-#99 \`_cmp_name\` lesson is that a class synthesis which skips this variant ships a guard with a known blind spot; (4) \`negative_cases\` — inputs that must NOT trip your candidate predicate (so it isn't gameable / over-broad); (5) \`maintainer_next_steps\` — what's left for a human to actually author and land this, INCLUDING whether the new invariant should be registered in \`maintainers/ci-speedup/scripts/measure_contradictions.py\`'s \`_PHASE0_CHECK_NAMES\` (it should be, ONLY if this class is a cross-seam CONTRADICTION per that module's own definition — a report claim that self-contradicts, not merely a routine FAIL). If, after this analysis, you conclude the real instances do NOT actually share one clean root cause (they only LOOK similar), or no non-vacuous, non-false-positive predicate is achievable, set \`landable:false\` and explain why in \`summary\` — that is a legitimate, expected outcome, not a failure to hide.\n\n` +
    `Return {definition, candidate_predicate, synthetic_instances:[{description, includes_scope_or_monorepo_variant}], negative_cases:[...], maintainer_next_steps, landable, summary}.`,
    { model: 'opus', label: `sketch:${cls}`, phase: 'Fix', schema: CLASS_SKETCH, isolation: 'worktree', effort: 'high' }),
  { onRetry: n => { classSketchRetried[cls] = n } })
  .catch(e => ({ _sketch_errored: true, summary: `sketch agent threw: ${errMsg(e)}` }))

// This stage runs to completion BEFORE the fixBug fan-out below (sequential, not concurrent with
// it) — simplicity over squeezing out parallelism: the two stages are independent and COULD race,
// but sketch clusters are rare (most runs have none, so `sketchClasses` is usually empty and this
// loop is a no-op) and keeping them sequential keeps the pacing math (FIX_CONCURRENCY reused as-is
// for both) trivial to reason about.
const sketchClasses = Object.keys(classRouting.routes).filter(cls => classRouting.routes[cls].route === 'novel-sketch')
const classSketchesRaw = []
for (const group of chunk(sketchClasses, FIX_CONCURRENCY)) {
  // Hardening: same between-chunk token-ceiling check as the run stage — a skipped class surfaces
  // as an errored sketch entry (buildClassSketchEntry keeps the cluster visible), never dropped.
  if (overBudget('sketch')) {
    classSketchesRaw.push(...group.map(() => ({ _sketch_errored: true,
      summary: `not drafted: this run's token budget exhausted (~${runSpend()} run output tokens >= ${TOKEN_BUDGET} ceiling) — re-run to draft` })))
    continue
  }
  classSketchesRaw.push(...await parallel(group.map(sketchOneClass)))
}
// Index-aligned with sketchClasses (chunks run in order, parallel preserves order) — same
// discipline as the fix/review stages' positional reconciliation.
const classSketches = sketchClasses.map((cls, i) =>
  buildClassSketchEntry(cls, classRouting.routes[cls], classSketchesRaw[i]))
// A held-out sketch bug is accounted for as a synthetic needs_human "fix" row (never autonomously
// drafted, never silently dropped) — reconcileIntegration/not_integrated see it like any other
// held-out bug, just with a routing-specific reason pointing at class_sketches[].
const sketchHeldFixes = sketchHeldBugs.map(b => {
  const rc = bugRouteBySig[b.signature]
  return {
    signature: b.signature, outcome: 'needs_human', patch: '', changed_files: [],
    class_sketch_pending: true,
    summary: `UNCOVERED CLASS CLUSTER (class="${rc.cls}", ${classRouting.routes[rc.cls].distinctRepoCount} distinct repos) — `
      + `held out of the autonomous fix path by design (a guard sketch is for the maintainer to finish, `
      + `never auto-landed). See class_sketches[] for the drafted sketch (or its not-landable/errored status).`,
  }
})

// --- Hardening: the --max-fixes batch cap -----------------------------------------------------
// The first MAX_FIXES fixable bugs (discovery order — stable across resumes, since completed orgs
// short-circuit and re-surface their bugs in the same order) get fix agents; the rest are held as
// explicit needs_human rows ("re-run to draft"), never silently dropped. This bounds the loop's
// most expensive stage (each fix = a worktree-isolated effort:'high' agent) and keeps the
// consolidated PR a reviewable size.
const draftableBugs = fixableBugs.slice(0, MAX_FIXES)
const capHeldBugs = fixableBugs.slice(MAX_FIXES)
if (capHeldBugs.length)
  log(`⚠ --max-fixes cap (${MAX_FIXES}): drafting ${draftableBugs.length}/${fixableBugs.length} fixable bugs — `
    + `${capHeldBugs.length} held as needs_human; re-run after this batch lands (completed orgs resume for free)`)
const capHeldFixes = capHeldBugs.map(b => ({
  signature: b.signature, outcome: 'needs_human', patch: '', changed_files: [],
  max_fixes_capped: true,
  summary: `HELD BY --max-fixes CAP (${MAX_FIXES}) — not drafted this run so the fix fan-out and the `
    + `consolidated PR stay bounded; re-run the loop after this batch lands to draft it.`,
}))

// Pace the fix fan-out the same way as the run stage: FIX_CONCURRENCY bugs at a time, chunks in
// order so fixesRaw stays index-aligned with `draftableBugs` for the positional reconciliation below.
const fixesRaw = []
for (const group of chunk(draftableBugs, FIX_CONCURRENCY)) {
  // Hardening: between-chunk token-ceiling check — a bug skipped here keeps its explicit
  // needs_human row (re-run to draft), so `integrated + not_integrated === bugs_found` still holds.
  if (overBudget('fix')) {
    fixesRaw.push(...group.map(b => ({ signature: b.signature, outcome: 'needs_human', patch: '', changed_files: [],
      token_budget_held: true,
      summary: `NOT DRAFTED: this run's token budget exhausted (~${runSpend()} run output tokens >= ${TOKEN_BUDGET} ceiling) — re-run to draft.` })))
    continue
  }
  fixesRaw.push(...await parallel(group.map(fixBug)))
}

// Same positional reconciliation as the audit phase: a null = fix agent resolved to null
// (skipped / dead after retries). Every distinct bug must yield exactly one outcome row —
// a found bug whose fix died must never silently disappear. canonicalizeFixes ALSO re-keys each
// fix's signature to the canonical bug slug so the fix→ready→integrator→reconcile join can't drift
// onto the bug title the agent may have echoed instead (see the helper's comment). Sketch-held bugs
// are appended separately (by signature, not position — reconcileIntegration only needs a fixBySig
// join, never positional alignment against the FULL `bugs` list) so `bugs_found` accounting for
// EVERY found bug (fixable or sketch-held) stays complete.
const fixes = [...canonicalizeFixes(fixesRaw, draftableBugs), ...capHeldFixes, ...sketchHeldFixes]

// Committed-report regen routing (binding) — runs BEFORE the write-surface guard. A class fix whose
// ONLY failing tests are the committed-report guards (the stale worked examples still exhibit the
// caught bug) and whose diff is otherwise allowlist-clean is a CORRECT fix that a human must regen —
// attach the regen guidance and force outcome → needs_human, holding it out of `ready` so the
// integrator's bisect can never revert it and mislabel it "breaks the suite". Applies whether the
// agent honestly returned needs_human (we enrich the message) or sloppily returned patch_ready with a
// red suite (we downgrade it). We act on the agent's reported failing_tests, re-validated here (same
// "re-check the agent's list, never trust its verdict" stance as the write-surface guard).
const committedReportRegenNeeded = []
for (const p of fixes) {
  if (p.outcome === 'not_reproduced') continue   // nothing was built — no suite to be red
  const route = routeCommittedReportFailure(p.failing_tests, p.changed_files, p.patch)
  if (!route) {
    // Did the agent CLAIM the committed-report regen case (all failing_tests are committed-report
    // guards) yet leave nothing to preserve (empty changed_files or empty patch)? Routing can't act
    // on that, so surface it LOUDLY — a maintainer should see "reported the regen case but committed
    // nothing" rather than read it as a plain needs_human. The fix is still safe (held out of `ready`
    // by its own non-patch_ready / incoherent-patch handling below), just not regen-routed.
    const claimedRegen = Array.isArray(p.failing_tests)
      && p.failing_tests.filter(Boolean).length
      && p.failing_tests.filter(Boolean).every(isCommittedReportGuard)
    if (claimedRegen)
      log(`⚠ ${p.signature}: reported a committed-report-only failure but committed no preservable patch — NOT regen-routed (treat as an unfinished fix; the agent skipped step-3 commit/capture)`)
    continue
  }
  const agentNote = p.summary ? ` [agent note: ${p.summary}]` : ''
  p.outcome = route.outcome                       // → needs_human (excluded from `ready`/integration)
  p.committed_report_regen = true
  p.summary = route.summary + agentNote
  committedReportRegenNeeded.push(p.signature)
  log(`⚠ ${p.signature}: committed worked example(s) exhibit the caught bug — needs human regen (held out of integration, not bisected)`)
}

// Write-surface guard (the binding control) — re-validate each patch_ready fix's RAW
// `changed_files` against FIX_ALLOWLIST in pure JS (the workflow has no git/fs of its own). A
// patch touching a non-allowlisted path is downgraded patch_ready → needs_human and EXCLUDED
// from integration, surfaced loudly. We re-check the agent's reported list, never its self-check.
const writeSurfaceUnverified = []
for (const p of fixes) {
  if (p.outcome !== 'patch_ready') continue
  // A patch_ready fix with no changed_files OR an empty patch is incoherent (a real patch must
  // touch files AND carry diff text). Catch BOTH here — otherwise an empty-patch fix passes this
  // guard (non-empty changed_files, no violations) yet is silently dropped from `ready` by the
  // `&& p.patch` filter below, surfacing in `not_integrated` as a confusing `patch_ready`/no-reason
  // row that looks like the integrator dropped it. Hold it out via the same needs_human path.
  if (isIncoherentPatch(p)) {
    p.outcome = 'needs_human'
    p.summary = `patch_ready but incoherent (empty changed_files, or an empty / whitespace-only patch) — held out of integration (nothing to apply or validate). ` + (p.summary || '')
    writeSurfaceUnverified.push(p.signature)
    log(`⚠ ${p.signature}: patch_ready but empty changed_files / empty-or-whitespace patch — excluded from integration`)
    continue
  }
  const violations = fixWriteViolations(p.changed_files)
  if (!violations.length) continue
  p.write_surface_violation = violations
  p.outcome = 'needs_human'
  p.summary = `WRITE-SURFACE VIOLATION — patch touched non-allowlisted path(s): ${violations.join(', ')}. `
    + `Excluded from the consolidated PR; review by hand. ` + (p.summary || '')
  log(`⚠ write-surface violation on ${p.signature}: ${violations.join(', ')}`)
}
const writeSurfaceViolations = fixes
  .filter(p => p.write_surface_violation)
  .map(p => ({ signature: p.signature, paths: p.write_surface_violation }))

// --- S3 review stage: an independent reviewer PANEL per drafted fix (between draft and integration) -
// Reviews only fixes still patch_ready with a patch (already past committed-report routing + the
// write-surface guard). Each fix is judged by a 2-agent panel — pr-review-toolkit:silent-failure-hunter
// (the silent-drop lessons L2/L8) ∪ code-reviewer (the re-derivation lessons L1/L3/L4/L5/L6) — with the
// L1-L9 authoring checklist (canonical copy in MAINTAINERS.md) as the explicit contract. OR-combined:
// EITHER reviewer confirming a defect holds the fix (a false negative defeats the stage; a false
// positive is merely a needs_human). Reviewers are worktree-isolated + told READ-ONLY so they can't
// touch the run's untracked data. Paced like the fix fan-out and index-aligned (via the bugs↔fixes
// positional join) so each verdict maps to its fix + its original bug context. A CONFIRMED defect →
// needs_human (excluded from the `ready` filter just below); ALL reviewers erroring → a surfaced
// coverage gap, NOT auto-held (blocking on a flaky reviewer would stall every fix; the consolidated PR
// is human-reviewed before merge anyway).
const REVIEWERS = ['pr-review-toolkit:silent-failure-hunter', 'pr-review-toolkit:code-reviewer']
const reviewerPrompt = (p, bug) =>
  `You are an INDEPENDENT, ADVERSARIAL, READ-ONLY reviewer of ONE drafted ci-speedup skill fix. Do NOT modify the working tree, do NOT \`git apply\` the patch, do NOT run anything that writes files — reason about the diff statically. Your job is to REFUTE the fix: find a real authoring defect that would ship a FALSE result (a false positive / false negative, a self-contradiction, a silently-dropped check). Set defect_confirmed:false ONLY if you cannot substantiate a concrete defect — do NOT rubber-stamp, but do NOT invent nitpicks.\n\n` +
  `THE BUG this fix targets (what the fix must eliminate):\n` +
  `Title: ${bug ? bug.title : '(unknown)'}\nSuspected location: ${(bug && bug.suspected_location) || '(see patch)'}\n` +
  `Audit evidence (the concrete false result): ${(bug && bug.evidence) || '(none captured)'}\n\n` +
  `THE FIX — a git patch off main. NOTE: it is NOT applied to the tree you can read; the files you open are the BEFORE state, so apply the patch's hunks mentally. Your checkout may also be slightly AHEAD of the patch's origin/main base, so cross-check against the CURRENT engine symbols, not exact line numbers.\n--- PATCH ---\n${p.patch}\n--- END PATCH ---\n\n` +
  `CONTRACT — the L1-L9 ci-speedup invariant-authoring checklist. The CANONICAL version (with the Class A evidence behind each lesson) is in maintainers/ci-speedup/MAINTAINERS.md, section "The L1-L9 invariant-authoring checklist" — READ IT, then judge this patch against it. In brief, a faithful class fix must:\n` +
  `  L1 — locate the claim in rendered text, but SOURCE the ground-truth comparison value from findings.json, never from collapsed/truncated rendered text (strip render artifacts via _strip_render_artifacts).\n` +
  `  L2 — suppress ONLY the exact contradiction; preserve anything with real value on any axis (no over-suppression that drops a credited lever).\n` +
  `  L3 — mirror the engine's exact KEYING (raw vs scope-normalized) when re-deriving a count/match; add a monorepo/scoped-name discriminator.\n` +
  `  L4 — mirror the engine's exact METRIC on both sides of a comparison (don't compare a global-p50 pole against a gating-median floor).\n` +
  `  L5 — mirror the engine's SELECTION aggregation (e.g. _eff_floor_s = max(p50, bimodal-high)) when choosing WHICH item is the floor/pole.\n` +
  `  L6 — choose the assertion shape to the data: EXACT for a deterministic integer; a DIRECTIONAL upper-bound + tolerance when the engine's aggregation can't be cheaply reproduced.\n` +
  `  L7 — a text-keyed invariant must PIN its renderer literals (and engine constants/predicates) so a reword breaks a coupling test, not the check silently.\n` +
  `  L8 — surface every coverage skip in Check.detail; a SKIP that reads clean is a false negative.\n` +
  `  L9 — corpus discipline: "green across all reports" can be faked by OVER-suppression; a remaining RED may be a true positive of a DIFFERENT class, not something this fix should silence.\n\n` +
  `If this is an INSTANCE-path fix (no new invariant), judge only the lessons that apply (L7/L8/L9) and whether its "can't generalize" justification holds. A fix whose new invariant legitimately reds the STALE committed worked examples under skills/ci-speedup/reports/** is EXPECTED (it is separately routed for human regen) — do NOT flag that as a defect.\n\n` +
  `Verify the invariant's re-derivation against the engine code it mirrors (read skills/ci-speedup/scripts/*.py and skills/ci-speedup/tests/verify_report.py). Return {defect_confirmed, lessons_violated:[…the L-ids…], severity, summary}. If you CONFIRM a defect, name the lesson(s) and the exact false result the patch would produce. A correct assertion that is merely LOOSER or STRICTER than strictly necessary but produces and permits NO false result is NOT a defect — a confirmed defect REQUIRES the patch to produce or permit a concrete false positive / false negative / self-contradiction. If you CLEAR it (defect_confirmed:false), your summary MUST enumerate which of L1-L9 you actively checked and, for each re-derivation the patch makes, name the findings.json field + engine symbol you verified it mirrors — a bare "looks correct" is not an acceptable clear.`

const reviewFlagged = []
const reviewErrored = []
// Pair each fix with its original bug BY SIGNATURE (not position — PR-B's class-sketch split means
// `fixes` is fixableBugs-then-sketchHeldFixes, no longer index-aligned with the FULL `bugs` list),
// so the reviewer gets the audit evidence — the concrete false result the fix must eliminate.
const bugBySig = {}
for (const b of bugs) if (b.signature) bugBySig[b.signature] = b
const reviewable = fixes
  .map(p => ({ p, bug: p.signature ? bugBySig[p.signature] : undefined }))
  .filter(({ p }) => p.outcome === 'patch_ready' && p.patch)
// Hardening: a token budget crossed before the review panels stops the run CLEANLY — the drafted
// patches are demoted to needs_human (patch preserved in fixes[], a re-run integrates them), NOT
// integrated unreviewed. The ceiling means stop spending, never "skip QA to finish cheaper".
if (reviewable.length && overBudget('review')) {
  for (const { p } of reviewable) {
    p.outcome = 'needs_human'
    p.token_budget_held = true
    p.summary = "HELD: this run's token budget exhausted before the independent review panel — patch preserved "
      + 'in fixes[]; re-run to review + integrate. ' + (p.summary || '')
  }
} else if (reviewable.length) {
  // Each fix → a 2-reviewer panel run concurrently; combineReviewVerdicts OR-combines them. The inner
  // panel + the outer per-fix chunk both go through `parallel`, so the global concurrency cap applies.
  // `agentType` is the Workflow harness's documented agent() option (a custom subagent type resolved
  // from the SAME registry as the Agent tool); the REVIEWERS strings are pinned to registered
  // pr-review-toolkit reviewers by dogfood-retry.test.mjs, so a rename/typo fails the suite rather
  // than silently falling back to the default agent.
  const reviewOne = ({ p, bug }) => async () => combineReviewVerdicts(
    await parallel(REVIEWERS.map(at => () => withRetry(
      () => agent(reviewerPrompt(p, bug), { model: 'opus', label: `review:${at.split(':').pop()}:${p.signature}`,
        phase: 'Fix', agentType: at, schema: REVIEW_VERDICT, effort: 'high', isolation: 'worktree' }),
      { onRetry: n => { fixRetried[`review:${at}:${p.signature}`] = n } })
      // `_review_errored` distinguishes a reviewer that THREW from one that cleared the fix — a
      // coverage gap to surface, never a silent "clean". A null resolution (skip / dead after retries)
      // is filtered the same way by combineReviewVerdicts.
      .catch(e => ({ _review_errored: true, summary: `${at} threw: ` + errMsg(e) })))))
  const reviewsRaw = []
  // Each fix's panel fires REVIEWERS.length agents, so chunk the FIXES so peak concurrent reviewer
  // agents (chunk-size × REVIEWERS) stays within the fix-stage pacing ceiling — don't DOUBLE the
  // deliberately-tuned FIX_CONCURRENCY by ignoring the inner fan-out (the global cap "won't bind" at
  // the small fix counts this loop sees — same rationale as the run/fix stages' explicit chunking).
  const REVIEW_CHUNK = Math.max(1, Math.floor(FIX_CONCURRENCY / REVIEWERS.length))
  for (const group of chunk(reviewable, REVIEW_CHUNK))
    reviewsRaw.push(...await parallel(group.map(reviewOne)))
  // reviewsRaw is index-aligned with `reviewable` (chunks run in order; parallel preserves order).
  reviewable.forEach(({ p }, i) => {
    const status = applyReviewVerdict(p, reviewsRaw[i])
    if (status === 'flagged') {
      reviewFlagged.push(p.signature)
      log(`⚠ ${p.signature}: independent review panel flagged an authoring defect — held for human (not integrated)`)
    } else if (status === 'errored') {
      reviewErrored.push(p.signature)
      log(`⚠ ${p.signature}: review panel returned no verdict (coverage gap) — proceeding to integration, surfaced for human attention`)
    }
  })
}

// --- Integrate every clean patch into ONE consolidated PR ------------------------------------
// The duplicate-PR coordination problem (two fixes, same edit) is solved HERE: all patches land on
// one branch, so a textual conflict is FORCED to surface (git apply) and a same-file semantic
// duplicate is FLAGGED (via the overlappingScriptGroups hints) for the integrator to reconcile —
// rather than deferred to a human merging N PRs. (A cross-file semantic duplicate that applies
// cleanly is the residual case the integrator must still judge from the patch contents.) One commit
// per fix keeps the single PR granularly revertable. A single serial integrator (worktree-isolated)
// runs after the fix fan-out, so it owns its checkout and never races the run-stage untracked data.
// Hardening: the budget can also cross DURING the review stage — demote still-ready patches the
// same way (patch preserved, needs_human) rather than spending an integrator agent past the ceiling.
if (fixes.some(p => p.outcome === 'patch_ready' && p.patch) && overBudget('integrate'))
  for (const p of fixes)
    if (p.outcome === 'patch_ready' && p.patch) {
      p.outcome = 'needs_human'
      p.token_budget_held = true
      p.summary = "HELD: this run's token budget exhausted before integration — patch preserved in fixes[]; "
        + 're-run to integrate. ' + (p.summary || '')
    }
const ready = fixes.filter(p => p.outcome === 'patch_ready' && p.patch)
const overlaps = overlappingScriptGroups(ready)   // diff-level dup/conflict hints for the integrator
let integration = { outcome: 'nothing_to_integrate', pr_url: null, branch: null,
                    integrated: [], dropped: [], summary: 'no clean patches to integrate' }
if (ready.length) {
  const patchBlocks = ready.map(p =>
    `### signature: ${p.signature}\nfiles: ${(p.changed_files || []).join(', ')}\n` +
    `new_symbols: ${(p.new_symbols || []).join(', ') || '(none)'}\nsummary: ${p.summary || ''}\n` +
    `--- PATCH ---\n${p.patch}\n--- END PATCH ---`
  ).join('\n\n')
  const overlapNote = overlaps.length
    ? `DIFF-OVERLAP HINTS — these fixes touch the SAME engine file and may be semantic duplicates or textual conflicts; reconcile each group as a set (keep one if they are the same fix; merge if complementary):\n`
      + overlaps.map(g => `  - ${g.file}: ${g.signatures.join(', ')}`).join('\n')
    : `No two patches touch the same engine script file — expect clean application; per step 2, apply + commit all patches and run the suite ONCE at the end (step 4).`
  integration = await withRetry(() => agent(
    `Integrate ${ready.length} drafted ci-speedup fix patch(es) into ONE consolidated PR, in this isolated git worktree. Each patch is a self-contained \`git diff origin/main...HEAD\`.\n\n` +
    `${overlapNote}\n\n` +
    `Procedure:\n` +
    `0. DUPLICATE-PR GUARD — do this FIRST, before creating any branch. An earlier integrate attempt may have already opened the consolidated PR and then had its connection drop (agent() RESOLVES to null AFTER \`gh pr create\` succeeded), so a resume re-runs THIS stage with the SAME patches and must not open a second PR for them. List the OPEN candidates with \`gh pr list --state open --limit 200 --json number,headRefName,url\` and keep ONLY those whose \`headRefName\` literally starts with \`ci-speedup-dogfood-consolidated-\` (do NOT use a \`--search\` qualifier — \`in:head\` is not real and \`head:\` matching is unreliable; filter the branch name yourself). For each remaining candidate, inspect it with \`gh pr view <n> --json commits,files\` and decide whether it ALREADY contains THESE fixes (compare its changed files + commit subjects against the patches below).\n` +
    `   - If a candidate DOES contain these fixes, it is this integration's OWN PR from an interrupted prior attempt — do NOT open a duplicate. Return outcome:"pr_opened", \`adopted_existing:true\`, that PR's url + head branch, \`integrated\`=the input signatures, \`dropped\`=[], and a \`summary\` that says you ADOPTED a pre-existing consolidated PR (the maintainer should still confirm it covers these fixes — the loop never merges).\n` +
    `   - If NO open consolidated PR contains these fixes (only unrelated prior-session consolidated PRs, or none at all), this is NOT a duplicate: proceed to step 1 and open this run's PR normally. A different session's still-open PR must NOT be adopted — that would mis-attribute these fixes to a PR that doesn't contain them.\n` +
    `1. Create a UNIQUE branch off the existing origin/main ref (do NOT branch off HEAD): \`git checkout -b ci-speedup-dogfood-consolidated-$(date +%Y%m%d-%H%M%S) origin/main\`. The suffix is REQUIRED — a fixed branch name collides on a re-run (a prior session's branch already exists locally/remotely), which fails the checkout and the whole integration. Return the exact branch name you used in \`branch\`.\n` +
    `2. Apply the patches ONE AT A TIME (\`git apply\` / \`git apply --3way\`, or re-create the edit by hand if a hunk is stale), COMMITTING each as its OWN commit (one commit per fix, with the \`Co-Authored-By:\` trailer) so the PR stays granularly revertable. Do NOT run the suite after every patch — apply + commit them ALL, then run the suite ONCE in step 4. ('apply-all, test-once': patches touch disjoint files in the common case, so one full ~11s suite run + a bisect ONLY if it breaks is faster than re-running it after each patch.)\n` +
    `   - DUPLICATE: if a patch is the same fix as one already applied (equivalent helper / same lines per the overlap hints) — judged from the patch CONTENTS, no test needed — DROP it (don't add a redundant helper/test). Record it in \`dropped\` (reason "duplicate of <sig>").\n` +
    `   - WON'T APPLY at apply time (\`git apply\` fails, hunk stale and not quickly reconcilable by hand): DROP it then and there (record the reason) — an apply failure is decided without the suite.\n` +
    `   - CONFLICT but complementary: reconcile by hand (merge the logic, keep one helper).\n` +
    `3. Check the changelog fragments: each patch adds its own \`skills/ci-speedup/changelog.d/<slug>.<kind>.md\`, so they do not conflict — keep ONE fragment per LANDED fix (rename one if two patches chose the same file name), delete the fragment of any dropped duplicate, never edit CHANGELOG.md, and leave NO \`<<<<<<<\`/\`=======\`/\`>>>>>>>\` markers.\n` +
    `4. Final gate — run the suite ONCE for the whole batch: BOTH \`python3 -m pytest ${SKILL} -q\` AND \`python3 -m pytest maintainers/ci-speedup -q\` must be fully green (the maintainers suite catches a CLASS fix's forgotten grader_seeds classification). If RED, a patch broke something: BISECT to the MINIMAL breaking set — revert the per-fix commits one at a time (newest first), re-running the suite; for each revert, if the suite is STILL red the reverted commit was innocent, so RE-APPLY it (cherry-pick it back) and continue to the next-newest. Keep going until green. DROP only the commits whose revert actually moved the suite toward green and that you left reverted (record reason "breaks the suite: <detail>" in \`dropped\`), so a single bad patch never sinks the batch AND independent good fixes still land. For every patch you DROP here, also DELETE its changelog fragment from step 3 (a dropped fix must leave no changelog entry). Then confirm both suites are green again and \`git diff --name-only origin/main...HEAD\` is entirely within the allowlist (skills/ci-speedup/scripts/*.py, skills/ci-speedup/tests/**, skills/ci-speedup/changelog.d/<slug>.<kind>.md fragments, skills/ci-speedup/ARCHITECTURE.md — only alongside a scripts/*.py change — and, for a CLASS fix that added a verify_report check, maintainers/ci-speedup/scripts/grader_seeds.py).\n` +
    `5. Push the branch and open ONE PR to main with \`gh pr create\` (title summarizing the batch; body listing each landed fix + any dropped duplicates). Do NOT merge.\n\n` +
    `Patches:\n\n${patchBlocks}\n\n` +
    `Return {outcome, pr_url, branch, integrated, dropped, summary}. outcome: "pr_opened" (PR created) | "failed" (could not open a PR). integrated = signatures that LANDED; dropped = [{signature, reason}] for every patch you did NOT land. Every input signature must appear in exactly one of integrated/dropped.`,
    { model: 'opus', label: 'integrate', phase: 'Fix', schema: INTEGRATION, isolation: 'worktree', effort: 'high' }
  ), { onRetry: n => { fixRetried['integrate'] = n } })
    .catch(e => ({ outcome: 'failed', pr_url: null, branch: null, integrated: [], dropped: [],
                   _integrate_errored: true, summary: 'integrator threw: ' + errMsg(e) }))
  // coalesceIntegration handles the OTHER failure mode the .catch above can't: agent() RESOLVING to
  // null (skip / terminal API death after retries). Without it a resolved-null integrator crashes the
  // result assembly below on `integration.outcome`, discarding every completed audit + fix. The
  // patches survive in `fixes`, so the run is re-runnable on a resume.
  integration = coalesceIntegration(integration)
}

const errored = audited.filter(a => a._run_errored)   // a run that crashed or vanished — NOT a clean / no-repo pass
const resumed = audited.filter(a => a.resumed && !a._run_errored).map(a => a.org)   // short-circuited from a prior complete run
const countOutcome = o => fixes.filter(p => p.outcome === o).length
// What the integrator actually LANDED overrides the per-fix patch_ready (a patch can be patch_ready
// yet dropped as a duplicate at integration). Derived from canonical sources, not the integrator's
// raw self-report — see reconcileIntegration. `integrated + not_integrated === bugs_found` holds by
// construction, so a found bug that didn't land is never silently dropped.
const recon = reconcileIntegration(bugs, fixes, ready, integration)
return {
  orgs_scouted: ORGS.length,
  audited: audited.map(a => ({
    org: a.org, repo: a.repo || null, bugs: (a.bugs || []).length,
    ...(a._run_errored ? { error: a.error } : {}),   // surface the crash, never launder it as "0 bugs"
  })),
  errored: errored.map(a => ({ org: a.org, error: a.error })),   // loud coverage gap — re-run these
  resumed,   // orgs short-circuited from a complete prior run (re-run skipped re-doing them); [] unless resuming
  // Transient-retry tallies: empty when nothing throttled, populated (org/sig → retry count) when
  // a run survived a throttle via backoff instead of dying. Makes a slow-but-recovered run visible.
  retried,
  fix_retried: fixRetried,
  class_sketch_retried: classSketchRetried,
  // Hardening surfaces (never-again-3.5M). token_budget: the spend ceiling's verdict — exhausted
  // means later stages were halted, with every skipped unit marked loudly (errored org rows /
  // needs_human fix rows with patches preserved); re-run to resume. run_spent_output_tokens is the
  // DELTA the ceiling governs (this run's own spend, #48); session_spent_output_tokens is the raw
  // session-cumulative pool reading, kept for operator context. max_fixes: the batch cap —
  // cap_held bugs carry explicit max_fixes_capped needs_human rows in fixes[]/not_integrated.
  token_budget: { ceiling: TOKEN_BUDGET, run_spent_output_tokens: runSpend(),
                  session_spent_output_tokens: spentNow(),
                  exhausted: budgetStoppedAt !== null, stopped_at_stage: budgetStoppedAt,
                  probe_error: budgetProbeError },
  max_fixes: { cap: MAX_FIXES, drafted: draftableBugs.length, cap_held: capHeldBugs.length },
  bugs_found: bugs.length,
  // PR-B (loop-self-improvement-upgrades.md §2, Item 1): the deterministic class-cluster routing
  // (tighten-existing / novel-sketch / point-fix-only per class, with distinct-repo counts + the
  // matched existing guard where applicable) — always present, even on a clean run with no clusters.
  class_routing: classRoutingSummary,
  // The drafted guard SKETCHES for every 'novel-sketch' class this run — NEVER auto-landed; each
  // entry is `status: 'sketch' | 'not-landable' | 'errored'` (buildClassSketchEntry never silently
  // drops a real >= 2-repo cluster's outcome). [] when no class crossed the uncovered-cluster bar.
  class_sketches: classSketches,
  // The SINGLE consolidated PR for this dogfood session (null if nothing clean was integrated).
  consolidated_pr: integration.outcome === 'pr_opened'
    ? { url: integration.pr_url || null, branch: integration.branch || null }
    : null,
  // True when consolidated_pr was ADOPTED from a prior interrupted attempt rather than freshly
  // opened — a dead-integrator-then-resume artifact made visible (greppable) instead of laundered
  // into a clean-looking fresh integration. See the integrate prompt's step-0 DUPLICATE-PR GUARD.
  consolidated_pr_adopted: integration.adopted_existing === true,
  integrated: recon.integrated,   // bugs that landed in the PR (integrator-claimed ∩ actually a candidate)
  // Every bug NOT in the PR, each with a reason — held out by the reproduce gate / write-surface
  // guard / needs_human, or dropped by the integrator as a duplicate or unapplyable patch. So a
  // found bug that didn't land is never silently dropped; `integrated` + these = bugs_found.
  not_integrated: recon.not_integrated,
  not_reproduced: countOutcome('not_reproduced'),
  needs_human: countOutcome('needs_human'),
  // Class fixes held out for committed-worked-example regen (a CORRECT fix whose new invariant reds
  // the stale committed reports) — surfaced so a maintainer regenerates them per the regen discipline,
  // and so it's visible these were NOT bisect-dropped as "breaks the suite". [] on a clean run.
  committed_report_regen_needed: committedReportRegenNeeded,
  // Fixes the S3 independent review confirmed an authoring defect in — downgraded to needs_human and
  // held out of the consolidated PR (the mechanized version of the human review that caught Class A).
  review_flagged: reviewFlagged,
  // Fixes whose review returned NO verdict (reviewer threw / died) — a review COVERAGE GAP, surfaced
  // loudly: the fix proceeds to integration (not auto-held), but a maintainer should review it by hand.
  review_errored: reviewErrored,
  // Loud coverage gaps — a strayed/unverified patch can't read as a clean landed fix.
  write_surface_violations: writeSurfaceViolations,
  write_surface_unverified: writeSurfaceUnverified,
  // Signatures the integrator claimed as landed but were never candidate patches — a misbehaving
  // integrator surfaced loudly instead of silently inflating `integrated`. [] on a well-behaved run.
  integrator_reported_unknown_signatures: recon.integrator_reported_unknown,
  // Dedicated machine-greppable flag for a DEAD integrator (threw, or resolved to null and was
  // coalesced), so an automated consumer can detect it without parsing `integration_summary` prose
  // — mirroring `errored` / `write_surface_violations`. True only when the integrate stage itself
  // failed; a clean "nothing_to_integrate" (no patches) or a successful PR leaves it false.
  integration_errored: integration._integrate_errored === true,
  integration_summary: integration.summary || '',
  fixes,   // full per-bug patch detail — one row per distinct bug, never dropped
}
