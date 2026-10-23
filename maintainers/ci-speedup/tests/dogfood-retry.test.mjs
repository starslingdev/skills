// Pins the dogfood loop's transient-error matcher (see PLAN-dogfood-rate-limit-resilience).
//
// The ci-speedup-dogfood workflow runs in the Workflow harness (ambient `agent`/`parallel`/`args`
// globals + top-level `return`), so it can't be imported by plain node. Instead this test EXTRACTS
// the live `isTransient` regex literal straight from the workflow source and exercises it — so the
// design's marker set and the shipped matcher can never silently drift apart. If someone drops a
// marker from the regex, the corresponding assertion below goes red.
//
// Run:  node maintainers/ci-speedup/tests/dogfood-retry.test.mjs
//       (also runs under `pytest` via the test_dogfood_retry_node.py shim, so CI exercises it)

import { readFileSync } from 'node:fs'
import { strict as assert } from 'node:assert'

const WORKFLOW = new URL('../workflows/ci-speedup-dogfood.js', import.meta.url)
const src = readFileSync(WORKFLOW, 'utf8')

// Pull the exact `isTransient = e => /.../i.test(...)` regex literal out of the source. The lazy
// `.*?` stops at the first `/`, which is the regex's own closing delimiter (the pattern has no
// internal slashes), so this captures the whole literal and nothing more.
const m = src.match(/isTransient\s*=\s*e\s*=>\s*(\/.*?\/[a-z]*)\.test/)
assert(m, 'could not locate the isTransient regex literal in the workflow source')
const isTransientRe = (0, eval)(m[1])   // reconstruct the real RegExp from the captured literal
const isTransient = msg => isTransientRe.test(msg)

// Every transient marker the design promises to catch — the real throttle message plus each token
// the plan enumerates. Keep this list in lockstep with the regex; a new marker needs a new case.
const TRANSIENT = [
  'API Error: Server is temporarily limiting requests · Rate limited',
  'Server is temporarily limiting requests',
  'rate limit',
  'too many requests',
  'overloaded',
  'throttled',
  'quota exceeded',
  '429',
  '503',
  '529',
]
for (const msg of TRANSIENT)
  assert(isTransient(msg), `expected TRANSIENT: ${JSON.stringify(msg)}`)

// A bare status must match on its own, but the digits must not fire when merely embedded in an
// unrelated number (\b boundaries) — otherwise an ordinary error carrying "1503" would be retried.
assert(isTransient('HTTP 503'), '503 should match standalone')
assert(isTransient('HTTP 429 Too Many Requests'), '429 should match standalone')
assert(!isTransient('processed 15039 rows'), '503 must not match inside an unrelated number')
assert(!isTransient('processed 14290 rows'), '429 must not match inside an unrelated number')

// Ordinary skill failures are NOT transient — they must reach the auditor, never get retried away.
const NON_TRANSIENT = [
  'TypeError: cannot read property foo of undefined',
  'AssertionError: spine mismatch',
  'pole wrongly dead-ended',
  '',
]
for (const msg of NON_TRANSIENT)
  assert(!isTransient(msg), `expected NON-transient: ${JSON.stringify(msg)}`)

// --- Per-org resume: the --force flag predicate (PLAN step 4) --------------------------------
// Extract the live `isForceFlag` predicate the same way, so the recognized force-flag spellings
// and the test can't drift. The flags must be stripped from the org list (not scouted as a slug),
// and an ordinary slug must NOT be mistaken for a flag.
const fm = src.match(/isForceFlag\s*=\s*t\s*=>\s*([^\n]+)/)
assert(fm, 'could not locate the isForceFlag predicate in the workflow source')
const isForceFlag = (0, eval)(`t => ${fm[1].replace(/\s*$/, '')}`)

for (const flag of ['--force', '--fresh', '-f'])
  assert(isForceFlag(flag), `expected force flag: ${flag}`)
for (const slug of ['embrace-io', 'reflex-dev', 'force', 'f', '--forceful'])
  assert(!isForceFlag(slug), `ordinary token must not be a force flag: ${slug}`)

// --- Audit-only / fast flags (the speed modes) ----------------------------------------------
// Same drift-proof extraction for the `--audit-only`/`--no-fix` and `--fast` predicates: the
// recognized spellings are pinned, an ordinary slug is never a flag, and every flag is stripped
// from the org list (the combined `isFlag` filter) so it is never scouted as a bogus org.
const aom = src.match(/isAuditOnlyFlag\s*=\s*t\s*=>\s*([^\n]+)/)
const ffm = src.match(/isFastFlag\s*=\s*t\s*=>\s*([^\n]+)/)
assert(aom && ffm, 'could not locate the isAuditOnlyFlag / isFastFlag predicates in the workflow source')
const isAuditOnlyFlag = (0, eval)(`t => ${aom[1].replace(/\s*$/, '')}`)
const isFastFlag = (0, eval)(`t => ${ffm[1].replace(/\s*$/, '')}`)

for (const flag of ['--audit-only', '--no-fix'])
  assert(isAuditOnlyFlag(flag), `expected audit-only flag: ${flag}`)
assert(isFastFlag('--fast'), 'expected --fast to be the fast flag')
for (const slug of ['audit-only', 'no-fix', 'fast', '--audited', 'fastly'])
  assert(!isAuditOnlyFlag(slug) && !isFastFlag(slug),
    `ordinary token must not be a speed flag: ${slug}`)

// The combined `isFlag` filter (what strips flags out of the org list) must drop EVERY flag
// spelling and keep ordinary slugs. Re-composed from the leaf predicates above (mirrors source).
const isFlag = t => isForceFlag(t) || isAuditOnlyFlag(t) || isFastFlag(t)
for (const flag of ['--force', '--fresh', '-f', '--audit-only', '--no-fix', '--fast'])
  assert(isFlag(flag), `isFlag must strip ${flag} from the org list`)
for (const slug of ['encode', 'pallets', 'reflex-dev'])
  assert(!isFlag(slug), `isFlag must keep ordinary slug ${slug}`)

// --- Write-surface guard: the isAllowedFixPath predicate (PLAN-dogfood-write-surface-guard) ----
// Extract the ACTUAL binding predicate from source — `isAllowedFixPath` and the `fixWriteViolations`
// filter that wraps it, NOT a re-composition of their parts. Re-composing would let a regression in
// the workflow's own composition (e.g. `&&` → `||`, or dropping the traversal clause) pass green; by
// pulling and exercising the real arrows, a drift in the shipped guard goes red here. Inputs are FULL
// repo-relative paths (what `git diff --name-only` emits) — a short `scripts/x.py` would spuriously
// fail the ^-anchored regexes. The arrows reference FIX_ALLOWLIST + hasTraversal, so bind those in.
const am = src.match(/const FIX_ALLOWLIST = (\[[\s\S]*?\n\])/)
assert(am, 'could not locate the FIX_ALLOWLIST array in the workflow source')
const FIX_ALLOWLIST = (0, eval)(am[1])   // reconstruct the real RegExp[] from the captured literal
const tm = src.match(/const hasTraversal = (p => [^\n]*)/)
assert(tm, 'could not locate the hasTraversal helper in the workflow source')
const hasTraversal = (0, eval)(tm[1])    // the real traversal/leading-slash reject, reconstructed
const im = src.match(/const isAllowedFixPath = (p => [^\n]*)/)
assert(im, 'could not locate isAllowedFixPath in the workflow source')
const isAllowedFixPath = new Function('hasTraversal', 'FIX_ALLOWLIST', `return (${im[1]})`)(hasTraversal, FIX_ALLOWLIST)
// `fixWriteViolations` is now a MULTI-LINE function (it carries the ARCHITECTURE.md co-occurrence
// meta-guard) and closes over ARCHITECTURE_DOC_RE + ENGINE_SCRIPT_RE — extract and bind those too,
// so the test exercises the REAL guard (incl. the co-occurrence rule), not a re-composition.
const adm = src.match(/const ARCHITECTURE_DOC_RE = (\/.*\/)/)
const esm = src.match(/const ENGINE_SCRIPT_RE = (\/.*\/)/)
assert(adm && esm, 'could not locate ARCHITECTURE_DOC_RE / ENGINE_SCRIPT_RE in the workflow source')
const ARCHITECTURE_DOC_RE = (0, eval)(adm[1])
const ENGINE_SCRIPT_RE = (0, eval)(esm[1])
const vm = src.match(/const fixWriteViolations = (files => \{[\s\S]*?\n\})/)
assert(vm, 'could not locate fixWriteViolations in the workflow source')
const fixWriteViolations = new Function('isAllowedFixPath', 'ARCHITECTURE_DOC_RE', 'ENGINE_SCRIPT_RE',
  `return (${vm[1]})`)(isAllowedFixPath, ARCHITECTURE_DOC_RE, ENGINE_SCRIPT_RE)

const ALLOWED = [
  'skills/ci-speedup/scripts/collect_runs.py',
  'skills/ci-speedup/scripts/x.py',
  'skills/ci-speedup/tests/test_x.py',
  'skills/ci-speedup/tests/sub/test_nested.py',            // tests/** allows nesting
  'skills/ci-speedup/CHANGELOG.md',
  // Stream 2 (S2b): ARCHITECTURE.md is now allowlisted. The allowlist is PATH-only — it has no
  // class-vs-instance notion — so `isAllowedFixPath` accepts it on EVERY fix (moved here from
  // REJECTED below). It is NOT unguarded: the LONE-doc-edit case is caught by the co-occurrence
  // meta-guard in `fixWriteViolations` (asserted separately below), not by the path predicate.
  'skills/ci-speedup/ARCHITECTURE.md',
  'maintainers/ci-speedup/scripts/grader_seeds.py',        // CLASS-fix coupling: classify a new verify_report check
]
for (const p of ALLOWED)
  assert(isAllowedFixPath(p), `allowlist should ACCEPT ${p}`)

const REJECTED = [
  'maintainers/ci-speedup/scripts/draft_detector.py',      // the grader_seeds entry is EXACT — no other maintainers script
  'maintainers/ci-speedup/workflows/ci-speedup-dogfood.js',// nor the loop workflow
  'skills/ci-speedup/references/optimization-patterns.md',  // the catalog
  'skills/ci-speedup/references/patterns/opt81-same-job-faster-on-another-runner.md', // per-pattern catalog files
  'skills/ci-speedup/references/patterns/INDEX.md',         // the catalog index
  'skills/ci-speedup/evals/evals.json',                     // evals
  'skills/ci-speedup/reports/a/findings.json',              // worked-example provenance
  'skills/ci-speedup/SKILL.md',                             // the contract (still rejected — only ARCHITECTURE.md was opened)
  'adversarial/skills/ci-speedup/scripts/evil.py',          // ^-anchor must bite (non-root)
  'skills/ci-secure/scripts/x.py',                          // a different skill
  'skills/ci-speedup/scripts/sub/nested.py',                // scripts/*.py is flat — no nested dirs
  'skills/ci-speedup/tests/../SKILL.md',                    // `..` must not escape the tests/ prefix
  'skills/ci-speedup/scripts/../../evil.py',                // `..` must not escape the scripts/ prefix
  '/etc/passwd',                                            // a leading `/` (absolute path) is never allowed
]
for (const p of REJECTED)
  assert(!isAllowedFixPath(p), `allowlist should REJECT ${p}`)

// The binding filter the guard actually runs: a non-array yields [] (documented v1 caveat — an
// agent under-reporting its own diff isn't caught here), and an array surfaces every stray path.
assert.deepEqual(fixWriteViolations(undefined), [], 'non-array changed_files → []')
assert.deepEqual(fixWriteViolations(null), [])
assert.deepEqual(
  fixWriteViolations(['skills/ci-speedup/scripts/x.py', 'skills/ci-speedup/SKILL.md', 'skills/ci-secure/scripts/y.py']),
  ['skills/ci-speedup/SKILL.md', 'skills/ci-secure/scripts/y.py'],
  'fixWriteViolations must surface exactly the non-allowlisted paths')

// --- S2b: ARCHITECTURE.md co-occurrence meta-guard ------------------------------------------
// ARCHITECTURE.md is allowlisted (path-only), so `isAllowedFixPath` accepts it — but a LONE doc edit
// (no sibling `scripts/*.py` engine change) is a stray edit and `fixWriteViolations` MUST flag it.
const ARCH = 'skills/ci-speedup/ARCHITECTURE.md'
const ENGINE = 'skills/ci-speedup/scripts/blocking_path.py'
const CHANGELOG = 'skills/ci-speedup/CHANGELOG.md'
const TEST = 'skills/ci-speedup/tests/test_x.py'
// LONE ARCHITECTURE.md (with only changelog/test, no engine script) → flagged, even though allowlisted.
assert.deepEqual(fixWriteViolations([ARCH, CHANGELOG, TEST]), [ARCH],
  'a lone ARCHITECTURE.md edit (no engine scripts/*.py) must be flagged by the co-occurrence guard')
assert.deepEqual(fixWriteViolations([ARCH]), [ARCH], 'ARCHITECTURE.md entirely alone is flagged')
// ARCHITECTURE.md ALONGSIDE an engine scripts/*.py change → clean (a real class fix updating §12).
assert.deepEqual(fixWriteViolations([ARCH, ENGINE, TEST, CHANGELOG]), [],
  'ARCHITECTURE.md co-occurring with an engine scripts/*.py change must pass')
// A lone ARCHITECTURE.md PLUS a genuine stray must surface BOTH (the guard doesn't mask other strays).
assert.deepEqual(
  fixWriteViolations([ARCH, 'skills/ci-speedup/SKILL.md']).sort(),
  [ARCH, 'skills/ci-speedup/SKILL.md'].sort(),
  'a lone ARCHITECTURE.md and a separate stray are both surfaced')
// Duplicate ARCHITECTURE.md entries dedup to a single violation (the co-occurrence push is guarded).
assert.deepEqual(fixWriteViolations([ARCH, ARCH]), [ARCH],
  'a duplicated lone ARCHITECTURE.md is surfaced once, not double-listed')
// FALSY entries in changed_files are DROPPED (not crashed on) — the `.filter(Boolean)` is load-bearing:
// `fixWriteViolations` runs on the agent's self-reported list, and a `['', null]` payload would
// otherwise throw in hasTraversal (p.startsWith) instead of ignoring the empty entries.
assert.deepEqual(
  fixWriteViolations(['', null, 'skills/ci-speedup/SKILL.md']), ['skills/ci-speedup/SKILL.md'],
  'falsy changed_files entries are dropped, not crashed on, and real strays still surface')

// --- S2a: routeCommittedReportFailure (committed-report regen routing) -----------------------
// A class fix whose ONLY failing tests are the committed-report guards (the stale committed examples
// still exhibit the caught bug) and whose diff is otherwise allowlist-clean must route to a
// needs_human regen disposition — NOT be bisect-dropped. Extract the real helper (it closes over
// fixWriteViolations + isCommittedReportGuard) and pin its behavior.
const icrm = src.match(/const isCommittedReportGuard = (t => [^\n]*)/)
const crgm = src.match(/const COMMITTED_REPORT_GUARD_RE = (\/.*\/)/)
assert(icrm && crgm, 'could not locate isCommittedReportGuard / COMMITTED_REPORT_GUARD_RE in the workflow source')
const COMMITTED_REPORT_GUARD_RE = (0, eval)(crgm[1])
const isCommittedReportGuard = new Function('COMMITTED_REPORT_GUARD_RE', `return (${icrm[1]})`)(COMMITTED_REPORT_GUARD_RE)
const rcfm = src.match(/function routeCommittedReportFailure[\s\S]*?\n\}/)
assert(rcfm, 'could not locate routeCommittedReportFailure in the workflow source')
const routeCommittedReportFailure = new Function('fixWriteViolations', 'isCommittedReportGuard',
  `return (${rcfm[0]})`)(fixWriteViolations, isCommittedReportGuard)

const CLEAN_CLASS_DIFF = [ENGINE, 'skills/ci-speedup/tests/verify_report.py', CHANGELOG]
const PATCH = 'diff --git a/skills/ci-speedup/scripts/blocking_path.py b/...\n+ fix line\n'   // a non-empty captured patch
// Only committed-report guards failing + a clean NON-EMPTY diff + a captured patch → a needs_human
// regen disposition that PRESERVES the committed fix.
const routed = routeCommittedReportFailure(
  ['skills/ci-speedup/tests/test_measured_evidence.py::test_committed_mastra_headline_is_required_reachable'],
  CLEAN_CLASS_DIFF, PATCH)
assert(routed && routed.outcome === 'needs_human', 'committed-data-only failure routes to needs_human')
assert(routed.committed_report_regen === true, 'routed disposition flags committed_report_regen')
assert(/COMMITTED-REPORT REGEN REQUIRED/.test(routed.summary), 'routed summary leads with the regen header')
assert(/regenerates? those examples/i.test(routed.summary) && /pinned gh\b/i.test(routed.summary),
  'routed summary carries the regen instruction (pinned gh window)')
assert(routed.summary.includes('test_committed_mastra_headline_is_required_reachable'),
  'routed summary echoes the failing guard id(s)')
// test_committed_reports.py is NO LONGER a committed-report guard — it renders the
// findings.json FRESH, so a failure there is a real engine bug to FIX, not a regen case.
assert.equal(
  routeCommittedReportFailure(['skills/ci-speedup/tests/test_committed_reports.py::test_x'],
    CLEAN_CLASS_DIFF, PATCH),
  null, 'test_committed_reports.py (fresh-render) is not a regen guard — routes as a real bug')
// A REAL regression elsewhere (a non-committed-data test failing) → NOT this disposition.
assert.equal(
  routeCommittedReportFailure(
    ['skills/ci-speedup/tests/test_verify_report_self.py::test_x',
     'skills/ci-speedup/tests/test_measured_evidence.py::test_y'], CLEAN_CLASS_DIFF, PATCH),
  null, 'a mixed failure set (a real regression too) must NOT route to the regen disposition')
// Tighter guard regex: a `.py.bak`-style suffix is NOT a committed-data guard id (no over-match).
assert.equal(
  routeCommittedReportFailure(['tests/test_measured_evidence.py.bak'], CLEAN_CLASS_DIFF, PATCH),
  null, 'a .py.bak suffix must not match the committed-data guard regex')
// Committed-report failure but a STRAYED diff (touches reports/) → NOT clean, so NOT routed.
assert.equal(
  routeCommittedReportFailure(['tests/test_measured_evidence.py::t'],
    [ENGINE, 'skills/ci-speedup/reports/a/findings.json'], PATCH),
  null, 'a strayed diff (not allowlist-clean) is not a clean class fix — not routed')
// No failing tests (suite green) → nothing to route.
assert.equal(routeCommittedReportFailure([], CLEAN_CLASS_DIFF, PATCH), null, 'no failing tests → null')
assert.equal(routeCommittedReportFailure(undefined, CLEAN_CLASS_DIFF, PATCH), null, 'undefined failing tests → null')
// EMPTY diff (agent reported a committed-report failure but committed NOTHING) → null. The disposition
// exists to PRESERVE a committed fix for the human to land; an empty diff has nothing to preserve, so
// it falls through to a plain needs_human instead of the misleading regen message.
assert.equal(routeCommittedReportFailure(['tests/test_measured_evidence.py::t'], [], PATCH), null,
  'committed-report failure with an EMPTY diff is not routed (no committed fix to preserve)')
assert.equal(routeCommittedReportFailure(['tests/test_measured_evidence.py::t'], undefined, PATCH), null,
  'committed-report failure with a missing diff is not routed')
// EMPTY / whitespace patch (files committed but no captured diff text) → null. The patch IS the
// preserved artifact; without it there is nothing to hand the human, so do not claim a regen disposition.
assert.equal(routeCommittedReportFailure(['tests/test_measured_evidence.py::t'], CLEAN_CLASS_DIFF, ''), null,
  'committed-report failure with an EMPTY patch is not routed (no preservable patch text)')
assert.equal(routeCommittedReportFailure(['tests/test_measured_evidence.py::t'], CLEAN_CLASS_DIFF, '   \n'), null,
  'committed-report failure with a whitespace-only patch is not routed')
assert.equal(routeCommittedReportFailure(['tests/test_measured_evidence.py::t'], CLEAN_CLASS_DIFF, undefined), null,
  'committed-report failure with a missing patch is not routed')
// A lone ARCHITECTURE.md in the diff makes it NOT clean (co-occurrence guard) → not routed.
assert.equal(
  routeCommittedReportFailure(['tests/test_measured_evidence.py::t'], [ARCH, CHANGELOG], PATCH),
  null, 'committed-report failure with a lone ARCHITECTURE.md diff is not routed (diff not clean)')

// --- S3: review-stage routing (reviewVerdictRoutesToHuman + applyReviewVerdict) --------------
// The reviewer is an LLM, so the BINDING part is the pure routing: a CONFIRMED defect → needs_human
// (excluded from `ready`); a null/errored/ambiguous verdict → NOT auto-held (surfaced as a gap). Pin
// the routing + the end-to-end wiring (a flagged fix is excluded from integration).
const rvrm = src.match(/const reviewVerdictRoutesToHuman = (v => [^\n]*)/)
assert(rvrm, 'could not locate reviewVerdictRoutesToHuman in the workflow source')
const reviewVerdictRoutesToHuman = (0, eval)(rvrm[1])
const arvm = src.match(/function applyReviewVerdict[\s\S]*?\n\}/)
assert(arvm, 'could not locate applyReviewVerdict in the workflow source')
const applyReviewVerdict = new Function('reviewVerdictRoutesToHuman', `return (${arvm[0]})`)(reviewVerdictRoutesToHuman)

// reviewVerdictRoutesToHuman: ONLY a strict boolean-true defect_confirmed holds a fix.
assert.equal(reviewVerdictRoutesToHuman({ defect_confirmed: true }), true, 'confirmed defect → hold')
assert.equal(reviewVerdictRoutesToHuman({ defect_confirmed: false }), false, 'cleared → no hold')
assert.equal(reviewVerdictRoutesToHuman({}), false, 'missing defect_confirmed → no hold')
assert.equal(reviewVerdictRoutesToHuman(null), false, 'null verdict → no hold')
assert.equal(reviewVerdictRoutesToHuman(undefined), false, 'undefined verdict → no hold')
assert.equal(reviewVerdictRoutesToHuman({ defect_confirmed: 'true' }), false,
  'a truthy-but-non-boolean defect_confirmed does NOT hold (strict === true — no accidental coercion in)')

// applyReviewVerdict: a confirmed defect downgrades the fix to needs_human and records the defect.
const reviewFlaggedFix = { signature: 's', outcome: 'patch_ready', patch: 'd', changed_files: ['x'], summary: 'orig' }
assert.equal(applyReviewVerdict(reviewFlaggedFix,
  { defect_confirmed: true, lessons_violated: ['L1', 'L4'], summary: 'sourced the fact from lossy rendered text' }),
  'flagged', 'a confirmed defect → flagged')
assert.equal(reviewFlaggedFix.outcome, 'needs_human', 'a flagged fix is downgraded to needs_human')
assert(/INDEPENDENT REVIEW FLAGGED/.test(reviewFlaggedFix.summary)
  && /L1, L4/.test(reviewFlaggedFix.summary) && /orig/.test(reviewFlaggedFix.summary),
  'flagged summary names the lessons, the defect, and preserves the agent summary')
assert(reviewFlaggedFix.review_defect && reviewFlaggedFix.review_defect.defect_confirmed === true,
  'the verdict is recorded on the fix (review_defect)')

// A cleared verdict leaves the fix untouched (still patch_ready).
const reviewCleanFix = { signature: 's2', outcome: 'patch_ready', patch: 'd', changed_files: ['x'] }
assert.equal(applyReviewVerdict(reviewCleanFix, { defect_confirmed: false, summary: 'looks correct' }), 'clean')
assert.equal(reviewCleanFix.outcome, 'patch_ready', 'a cleared fix stays patch_ready')

// A dead / null / errored review is a COVERAGE GAP (not auto-held): status 'errored', fix untouched.
const reviewGapFix = { signature: 's3', outcome: 'patch_ready', patch: 'd', changed_files: ['x'] }
assert.equal(applyReviewVerdict(reviewGapFix, null), 'errored', 'a null verdict → errored (not held)')
assert.equal(applyReviewVerdict(reviewGapFix, { _review_errored: true, summary: 'reviewer threw' }), 'errored',
  'a thrown reviewer → errored')
assert.equal(reviewGapFix.outcome, 'patch_ready', 'a review-gap fix is NOT auto-held (it proceeds, surfaced loudly)')

// END-TO-END WIRING (the acceptance test the plan asks for): a drafted fix with a deliberate L1/L2
// flaw that the reviewer CONFIRMS must be EXCLUDED from integration. The reviewer is an LLM (its
// judgment isn't unit-testable), so we pin the WIRING — given a confirmed verdict, the fix is held out
// of `ready = fixes.filter(p => p.outcome === 'patch_ready' && p.patch)` (the real integration gate).
const reviewSimFixes = [
  { signature: 'good', outcome: 'patch_ready', patch: 'd', changed_files: ['x'] },
  { signature: 'flawed', outcome: 'patch_ready', patch: 'd', changed_files: ['x'] },
]
applyReviewVerdict(reviewSimFixes[0], { defect_confirmed: false, summary: 'ok' })
applyReviewVerdict(reviewSimFixes[1],
  { defect_confirmed: true, lessons_violated: ['L1'], summary: 'deliberate L1 flaw: sourced the fact from the collapsed Where line' })
const reviewReadySim = reviewSimFixes.filter(p => p.outcome === 'patch_ready' && p.patch)
assert.deepEqual(reviewReadySim.map(p => p.signature), ['good'],
  'a review-flagged fix is excluded from `ready`/integration; only the cleared fix proceeds')
// Bind the acceptance assertion to the REAL source gate + the real stage ORDER, not just the inline
// replica above — otherwise breaking the real `ready` predicate (dropping its outcome guard) or
// reordering the review stage AFTER the gate would leave a flagged fix able to integrate with CI green.
assert(src.includes("const ready = fixes.filter(p => p.outcome === 'patch_ready' && p.patch)"),
  "the real integration gate must match the e2e-pinned `ready` predicate (outcome 'patch_ready' && patch)")
assert(src.indexOf('const reviewable = fixes') !== -1
  && src.indexOf('const reviewable = fixes') < src.indexOf("const ready = fixes.filter(p => p.outcome === 'patch_ready'"),
  'the review stage (reviewable …) must run BEFORE the ready/integration gate, or a flagged fix could leak in')

// A CONFIRMED verdict missing `lessons_violated` is a realistic LLM output — it must still flag (no
// crash on the empty-lessons fallback), and the summary degrades gracefully (no "()" lessons group).
const reviewNoLessons = { signature: 's4', outcome: 'patch_ready', patch: 'd', changed_files: ['x'] }
assert.equal(applyReviewVerdict(reviewNoLessons, { defect_confirmed: true, summary: 'concrete false positive, no L-id' }),
  'flagged', 'a confirmed defect with no lessons_violated still flags')
assert.equal(reviewNoLessons.outcome, 'needs_human', 'a no-lessons flagged fix is still held')
assert(!/\(\)/.test(reviewNoLessons.summary), 'an empty lessons list does not render a bare "()" group')

// --- S3 panel: combineReviewVerdicts (OR-combine silent-failure-hunter ∪ code-reviewer) ------
const crvm = src.match(/function combineReviewVerdicts[\s\S]*?\n\}/)
assert(crvm, 'could not locate combineReviewVerdicts in the workflow source')
const combineReviewVerdicts = (0, eval)(`(${crvm[0]})`)

// ANY reviewer confirming a defect → combined confirmed (bias toward catching; a false negative
// defeats the stage). Lessons are unioned; summaries concatenated.
const cConfirm = combineReviewVerdicts([
  { defect_confirmed: false, summary: 'sfh: looks fine' },
  { defect_confirmed: true, lessons_violated: ['L4'], summary: 'cr: metric mismatch' }])
assert.equal(cConfirm.defect_confirmed, true, 'one reviewer confirming → combined confirmed (OR)')
assert(reviewVerdictRoutesToHuman(cConfirm), 'a combined-confirmed verdict routes to human')
assert.deepEqual(cConfirm.lessons_violated, ['L4'], 'combined lessons come from the confirming reviewer(s)')
// Both confirm → union of lessons, both summaries.
const cBoth = combineReviewVerdicts([
  { defect_confirmed: true, lessons_violated: ['L1'], summary: 'a' },
  { defect_confirmed: true, lessons_violated: ['L4', 'L1'], summary: 'b' }])
assert.deepEqual(cBoth.lessons_violated.sort(), ['L1', 'L4'], 'both confirming → de-duped union of lessons')
assert(/a/.test(cBoth.summary) && /b/.test(cBoth.summary), 'both summaries are carried')
// Neither confirms (at least one usable) → cleared.
assert.equal(combineReviewVerdicts([{ defect_confirmed: false, summary: 'x' },
  { defect_confirmed: false, summary: 'y' }]).defect_confirmed, false, 'no reviewer confirms → cleared')
// ALL reviewers errored / skipped → null (a coverage gap, NOT a silent clean).
assert.equal(combineReviewVerdicts([{ _review_errored: true }, null]), null,
  'every reviewer erroring/skipping → null (→ applyReviewVerdict errored, surfaced)')
// A MIX of errored + a confirming reviewer → still confirmed (the usable verdict decides).
assert.equal(combineReviewVerdicts([{ _review_errored: true },
  { defect_confirmed: true, lessons_violated: ['L8'], summary: 'drop' }]).defect_confirmed, true,
  'a confirming reviewer alongside an errored one still holds the fix')
// A MIX of errored + a clearing reviewer → cleared (not held, not a gap — one reviewer DID judge).
assert.equal(combineReviewVerdicts([{ _review_errored: true }, { defect_confirmed: false, summary: 'ok' }]).defect_confirmed,
  false, 'a single clearing reviewer (others errored) → cleared, not a coverage gap')
assert.equal(combineReviewVerdicts([]), null, 'empty panel → null')
assert.equal(combineReviewVerdicts(undefined), null, 'non-array panel → null')
// A confirming verdict with NO lessons_violated (a realistic LLM output) combines without crashing on
// the flatMap/Set fallback, and still confirms.
assert.equal(combineReviewVerdicts([{ defect_confirmed: true, summary: 'concrete FP, no L-id' }]).defect_confirmed,
  true, 'a confirming verdict with no lessons_violated combines (empty-lessons flatMap fallback) and confirms')

// --- S3 prompt integrity: the reviewer prompt must embed all nine lessons + a registered agentType --
// Edit-proofing: a future prompt edit that silently drops a lesson (e.g. L4) would gut the contract
// with no test noticing; and a plugin rename / typo in the agentType would only surface at runtime.
// Scope the scan to the reviewerPrompt DEFINITION (not the whole file), so a stray `L4 —` in a
// comment / the canonical doc reference can't mask a lesson silently dropped from the prompt itself.
const rpsm = src.match(/const reviewerPrompt = \(p, bug\) =>[\s\S]*?\nconst reviewFlagged/)
assert(rpsm, 'could not locate the reviewerPrompt definition in the workflow source')
const reviewerPromptSrc = rpsm[0]
for (const L of ['L1', 'L2', 'L3', 'L4', 'L5', 'L6', 'L7', 'L8', 'L9'])
  assert(reviewerPromptSrc.includes(L + ' —'),
    `the reviewer prompt must embed lesson ${L} (dropping one guts the review contract)`)
const revm = src.match(/const REVIEWERS = (\[[^\]]*\])/)
assert(revm, 'could not locate the REVIEWERS panel array in the workflow source')
const REVIEWERS = (0, eval)(revm[1])
const REGISTERED_REVIEWERS = ['pr-review-toolkit:silent-failure-hunter', 'pr-review-toolkit:code-reviewer']
assert(REVIEWERS.length >= 1 && REVIEWERS.every(a => REGISTERED_REVIEWERS.includes(a)),
  `every reviewer agentType must be a registered pr-review-toolkit reviewer; got ${JSON.stringify(REVIEWERS)}`)

// --- Diff-level overlap grouping: overlappingScriptGroups (the consolidated-PR dup coordinator) -
// The integrator uses this to spot two fixes that are secretly the same edit (the #74/#75 case).
// Extract the live function (it closes over SCRIPT_PATH_RE) and pin its behavior.
// Greedy to the line's LAST `/` (the closing delimiter) — the pattern has internal `\/`, so a lazy
// match would stop at the first escaped slash and capture an unterminated literal.
const sm = src.match(/const SCRIPT_PATH_RE = (\/.*\/)/)
assert(sm, 'could not locate SCRIPT_PATH_RE in the workflow source')
const SCRIPT_PATH_RE = (0, eval)(sm[1])
const om = src.match(/function overlappingScriptGroups[\s\S]*?\n\}/)
assert(om, 'could not locate overlappingScriptGroups in the workflow source')
const overlappingScriptGroups = new Function('SCRIPT_PATH_RE', `return (${om[0]})`)(SCRIPT_PATH_RE)

// Two fixes touching the SAME engine script → one group flagged for the integrator to reconcile.
const dupGroups = overlappingScriptGroups([
  { signature: 'a', changed_files: ['skills/ci-speedup/scripts/collect_runs.py', 'skills/ci-speedup/tests/test_a.py', 'skills/ci-speedup/CHANGELOG.md'] },
  { signature: 'b', changed_files: ['skills/ci-speedup/scripts/collect_runs.py', 'skills/ci-speedup/tests/test_b.py', 'skills/ci-speedup/CHANGELOG.md'] },
])
assert.equal(dupGroups.length, 1, 'two fixes on the same script must form one overlap group')
assert.equal(dupGroups[0].file, 'skills/ci-speedup/scripts/collect_runs.py')
assert.deepEqual(dupGroups[0].signatures, ['a', 'b'])
// Different scripts + the shared changelog/tests every fix touches is NOT an overlap.
assert.equal(overlappingScriptGroups([
  { signature: 'a', changed_files: ['skills/ci-speedup/scripts/collect_runs.py', 'skills/ci-speedup/CHANGELOG.md', 'skills/ci-speedup/tests/test_a.py'] },
  { signature: 'b', changed_files: ['skills/ci-speedup/scripts/blocking_path.py', 'skills/ci-speedup/CHANGELOG.md', 'skills/ci-speedup/tests/test_b.py'] },
]).length, 0, 'different scripts + shared changelog/tests is not an overlap')
// Defensive: empty input, missing changed_files, and missing signatures must not throw.
assert.deepEqual(overlappingScriptGroups([]), [])
assert.deepEqual(overlappingScriptGroups([{ signature: 'a' }, { changed_files: ['skills/ci-speedup/scripts/x.py'] }]), [])

// --- isIncoherentPatch: a patch_ready fix must carry BOTH files and diff text ----------------
const ipm = src.match(/const isIncoherentPatch = (p => [^\n]*)/)
assert(ipm, 'could not locate isIncoherentPatch in the workflow source')
const isIncoherentPatch = (0, eval)(ipm[1])
assert(isIncoherentPatch({ changed_files: [], patch: 'x' }), 'empty changed_files is incoherent')
assert(isIncoherentPatch({ changed_files: ['', null], patch: 'x' }),
  'all-falsy changed_files is incoherent (symmetric with the falsy-filtering guards)')
assert(isIncoherentPatch({ patch: 'x' }), 'missing changed_files is incoherent')
assert(isIncoherentPatch({ changed_files: ['skills/ci-speedup/scripts/x.py'], patch: '' }), 'empty patch is incoherent')
assert(isIncoherentPatch({ changed_files: ['skills/ci-speedup/scripts/x.py'], patch: '   \n\t' }),
  'a WHITESPACE-only patch is incoherent (symmetric with routeCommittedReportFailure trimming)')
assert(isIncoherentPatch({ changed_files: ['skills/ci-speedup/scripts/x.py'] }), 'missing patch is incoherent')
assert(!isIncoherentPatch({ changed_files: ['skills/ci-speedup/scripts/x.py'], patch: 'diff --git a b' }),
  'a real patch with files + diff is coherent')

// --- reconcileIntegration: integrated/not_integrated derived from CANONICAL sources, not trust --
const rim = src.match(/function reconcileIntegration[\s\S]*?\n\}/)
assert(rim, 'could not locate reconcileIntegration in the workflow source')
const reconcileIntegration = (0, eval)(`(${rim[0]})`)

// bugs a,b,c; ready=a,b (c was held out as needs_human). Integrator lands a, drops b as a dup, and
// FALSELY names c (held out) plus a hallucinated 'zzz'. c must stay not_integrated; count must be 1.
const recon = reconcileIntegration(
  [{ signature: 'a' }, { signature: 'b' }, { signature: 'c' }],
  [{ signature: 'a', outcome: 'patch_ready', summary: 'fix a' },
   { signature: 'b', outcome: 'patch_ready', summary: 'fix b' },
   { signature: 'c', outcome: 'needs_human', summary: 'held out by guard' }],
  [{ signature: 'a' }, { signature: 'b' }],
  { outcome: 'pr_opened', integrated: ['a', 'c', 'zzz'], dropped: [{ signature: 'b', reason: 'duplicate of a' }] })
assert.equal(recon.integrated, 1, 'only a truly landed (c was held out; zzz is not a bug)')
assert.equal(recon.integrated + recon.not_integrated.length, 3, 'integrated + not_integrated === bugs_found')
const byB = Object.fromEntries(recon.not_integrated.map(x => [x.signature, x]))
assert.equal(byB.b.reason, 'duplicate of a', 'dropped bug carries the integrator reason')
assert(byB.c.reason.includes('held out'), 'held-out bug keeps its own reason, not re-bucketed as landed')
assert.deepEqual(recon.integrator_reported_unknown.sort(), ['c', 'zzz'], 'integrator naming non-candidates is surfaced loudly')

// Integrator FAILED to open a PR → the READY bug lands in not_integrated flagged as an INTEGRATION
// failure, but a bug HELD OUT before integration (needs_human, not a candidate) keeps its own reason
// without the misleading integration-failure prefix.
const reconFail = reconcileIntegration(
  [{ signature: 'a' }, { signature: 'h' }],
  [{ signature: 'a', outcome: 'patch_ready', summary: 'fix a' },
   { signature: 'h', outcome: 'needs_human', summary: 'write-surface violation' }],
  [{ signature: 'a' }],   // only `a` was a ready candidate; `h` was held out
  { outcome: 'failed', integrated: [], dropped: [], summary: 'integrator threw: boom' })
assert.equal(reconFail.integrated, 0)
const reconFailBy = Object.fromEntries(reconFail.not_integrated.map(x => [x.signature, x]))
assert(reconFailBy.a.reason.includes('integration did not open a PR'),
  'a ready candidate that failed integration is flagged as an integration failure')
assert(!reconFailBy.h.reason.includes('integration did not open a PR'),
  'a held-out bug keeps its own reason — not mislabeled as an integration failure')
assert(reconFailBy.h.reason.includes('write-surface violation'), 'held-out bug keeps its own summary')
// Defensive: empty/missing inputs don't throw.
assert.deepEqual(reconcileIntegration([], [], [], {}), { integrated: 0, not_integrated: [], integrator_reported_unknown: [] })

// --- canonicalizeFixes: the fix→reconcile join key must be the bug slug, not the agent's echo ---
// Regression for the "PR opened with 2 fixes but reported integrated:0, all failed" accounting bug.
// The fix agent may echo the bug TITLE as `signature` instead of the slug it was handed; if so the
// fix/ready/integrator space diverges from `bugs`' slug space and reconcileIntegration credits 0.
// canonicalizeFixes re-keys positionally to the bug slug. Extract the live helper and exercise it.
const cfm = src.match(/function canonicalizeFixes[\s\S]*?\n\}/)
assert(cfm, 'could not locate canonicalizeFixes in the workflow source')
const canonicalizeFixes = (0, eval)(`(${cfm[0]})`)

const _bugs = [{ signature: 'slug-a@f.py:x' }, { signature: 'slug-b@f.py:y' }]
// agent ECHOED THE TITLE, not the slug it was handed — the actual failure mode this run hit.
const _canon = canonicalizeFixes(
  [{ signature: 'Some long bug title A', outcome: 'patch_ready', patch: 'd', changed_files: ['z'] },
   { signature: 'Some long bug title B', outcome: 'patch_ready', patch: 'd', changed_files: ['z'] }],
  _bugs)
assert.deepEqual(_canon.map(f => f.signature), ['slug-a@f.py:x', 'slug-b@f.py:y'],
  'canonicalizeFixes must re-key each fix to the positional bug slug, overriding the agent-echoed title')
assert.equal(_canon[0].outcome, 'patch_ready', 'canonicalizeFixes preserves the rest of the fix payload')
// And the re-keyed fixes now JOIN correctly in reconcileIntegration (integrator echoed the slugs the
// patch blocks now carry) — proving the end-to-end accounting is restored, not just the local re-key.
const reconCanon = reconcileIntegration(_bugs, _canon, _canon,
  { outcome: 'pr_opened', integrated: ['slug-a@f.py:x', 'slug-b@f.py:y'], dropped: [] })
assert.equal(reconCanon.integrated, 2, 'with canonical slugs, a 2-fix PR reports integrated:2 (not 0)')
// A null (dead-agent) slot is still filled with the canonical slug + failed outcome.
const _canonNull = canonicalizeFixes([null], [{ signature: 'slug-dead@f.py:z' }])
assert.equal(_canonNull[0].signature, 'slug-dead@f.py:z')
assert.equal(_canonNull[0].outcome, 'failed')
assert(_canonNull[0]._fix_errored, 'null slot is flagged _fix_errored')
// Defensive: empty / mismatched inputs don't throw.
assert.deepEqual(canonicalizeFixes([], []), [])
assert.deepEqual(canonicalizeFixes(undefined, undefined), [])
// The `|| (p && p.signature)` fallback: when there is NO positional bug slug (fixesRaw longer than
// bugs, or bugs[i] missing a signature), the fix KEEPS its own signature rather than nulling it.
// Can't occur in the index-aligned flow, but the branch must do the right thing if it ever does.
assert.equal(canonicalizeFixes([{ signature: 'own-sig', outcome: 'patch_ready' }], [])[0].signature,
  'own-sig', 'with no positional bug slug, canonicalizeFixes falls back to the fix\'s own signature')

// --- Integrate-stage null guard: a null agent() result must not crash result assembly ---------
// Regression for the "connection closed mid-response" crash. The integrate agent() can RESOLVE to
// null (a skip, or terminal API death after retries) — which the `.catch` on the integrate call
// never sees, because `.catch` only fires on a *rejection*. Without a null-coalesce, `integration`
// stayed null and the result assembly dereferenced `integration.outcome`, crashing the whole
// workflow and discarding every completed audit + fix. Extract the live coalesceIntegration helper
// and exercise its BEHAVIOR (not just that the source pattern exists): a null input must yield a
// non-null object whose `.outcome` is safely readable.
const ci2m = src.match(/function coalesceIntegration[\s\S]*?\n\}/)
assert(ci2m, 'could not locate coalesceIntegration in the workflow source')
const coalesceIntegration = (0, eval)(`(${ci2m[0]})`)

// The exact crash input: agent() resolved to null. The helper must return a readable object.
const coNull = coalesceIntegration(null)
assert(coNull && typeof coNull === 'object', 'coalesceIntegration(null) must return an object, never null')
assert.equal(coNull.outcome, 'failed', 'resolved-null integrator → outcome "failed"')
assert.equal(coNull._integrate_errored, true, 'resolved-null integrator is flagged _integrate_errored')
assert.deepEqual([coNull.integrated, coNull.dropped], [[], []], 'fallback carries empty integrated/dropped arrays')
// Reading `integration.outcome` (the exact line that crashed) must not throw and must not look like a PR.
assert.doesNotThrow(() => { const _ = coNull.outcome === 'pr_opened' ? coNull.pr_url : null },
  'result-assembly deref of integration.outcome must be safe after coalesce')
// A real integrator result passes through UNCHANGED (the coalesce must not clobber a success).
const coReal = { outcome: 'pr_opened', pr_url: 'https://x/pr/1', branch: 'b', integrated: ['s'], dropped: [], summary: 'ok' }
assert.equal(coalesceIntegration(coReal), coReal, 'a non-null integrator result passes through unchanged')

// --- PR-B: class-wide synthesis routing (loop-self-improvement-upgrades.md §2, Item 1) ---------
// Fixture-replay per spec §3 item #1: a clusterable >= 2-distinct-repo same-class set, an
// all-singletons set, a covered-class-leaked set, and a cluster whose sketch verdict must be
// flagged not-landable → assert the routing (tighten vs new-sketch vs point-fix-only) and the
// sketch-assembly (never silently drops a real cluster's outcome). Extract-and-exercise, same
// drift-proof pattern as the rest of this file.
const bcem = src.match(/const BUG_CLASS_ENUM = (\[[\s\S]*?\n\])/)
assert(bcem, 'could not locate BUG_CLASS_ENUM in the workflow source')
const BUG_CLASS_ENUM = (0, eval)(bcem[1])
assert(Array.isArray(BUG_CLASS_ENUM) && BUG_CLASS_ENUM.length > 0, 'BUG_CLASS_ENUM must be non-empty')
assert(BUG_CLASS_ENUM.includes('other'), 'BUG_CLASS_ENUM must carry an "other" catch-all')
// The AUDIT bug schema's `class` property must be enum-pinned to THIS exact array (source-text
// containment check — a full nested-brace extraction of AUDIT isn't needed to catch the one thing
// that matters: the schema referencing the same constant, not a hand-duplicated list that could drift).
assert(/class:\s*\{\s*type:\s*'string',\s*enum:\s*BUG_CLASS_ENUM\s*\}/.test(src),
  'AUDIT.bugs[].class must be enum-pinned to BUG_CLASS_ENUM (not a hand-duplicated literal)')

const gim = src.match(/const GUARD_INVENTORY = (\[[\s\S]*?\n\])/)
assert(gim, 'could not locate GUARD_INVENTORY in the workflow source')
const GUARD_INVENTORY = (0, eval)(gim[1])
assert(GUARD_INVENTORY.length >= 4, 'GUARD_INVENTORY must name at least the four §1 class guards')

const mglm = src.match(/function matchGuardByLocus[\s\S]*?\n\}/)
assert(mglm, 'could not locate matchGuardByLocus in the workflow source')
const matchGuardByLocus = new Function('GUARD_INVENTORY', `return (${mglm[0]})`)(GUARD_INVENTORY)
assert.equal(matchGuardByLocus('skills/ci-speedup/tests/verify_report.py:check_x'), GUARD_INVENTORY[0].guard)
assert.equal(matchGuardByLocus('some evidence mentioning skills/ci-speedup/tests/verify_report.py inline'),
  GUARD_INVENTORY[0].guard, 'a locus that merely NAMES the guard file still matches (coarse, by design)')
assert.equal(matchGuardByLocus('skills/ci-speedup/scripts/collect_runs.py:foo'), null,
  'an engine script with NO guard test in its name is NOT covered')
assert.equal(matchGuardByLocus(null), null)
assert.equal(matchGuardByLocus(undefined), null)

const gbcm = src.match(/function groupByClass[\s\S]*?\n\}/)
assert(gbcm, 'could not locate groupByClass in the workflow source')
const groupByClass = (0, eval)(`(${gbcm[0]})`)

const drm = src.match(/function distinctRepos[\s\S]*?\n\}/)
assert(drm, 'could not locate distinctRepos in the workflow source')
const distinctRepos = (0, eval)(`(${drm[0]})`)

const ccmrm = src.match(/const CLASS_CLUSTER_MIN_REPOS = (\d+)/)
assert(ccmrm, 'could not locate CLASS_CLUSTER_MIN_REPOS in the workflow source')
const CLASS_CLUSTER_MIN_REPOS = Number(ccmrm[1])
assert.equal(CLASS_CLUSTER_MIN_REPOS, 2, 'spec §2-B: the cluster threshold must be >= 2 DISTINCT repos/orgs')

const icgsm = src.match(/function isCoveredGraderSeed[\s\S]*?\n\}/)
assert(icgsm, 'could not locate isCoveredGraderSeed in the workflow source')
const isCoveredGraderSeed = (0, eval)(`(${icgsm[0]})`)
assert.equal(isCoveredGraderSeed({ signature: 'grader-seed@check:x' }), true)
assert.equal(isCoveredGraderSeed({ source: 'grader-seed' }), true)
assert.equal(isCoveredGraderSeed({ signature: 'slug@file.py:sym' }), false)
assert.equal(isCoveredGraderSeed(null), false)

const rccm = src.match(/function routeClassCluster[\s\S]*?\n\}/)
assert(rccm, 'could not locate routeClassCluster in the workflow source')
const routeClassCluster = new Function('distinctRepos', 'matchGuardByLocus', 'CLASS_CLUSTER_MIN_REPOS', 'isCoveredGraderSeed',
  `return (${rccm[0]})`)(distinctRepos, matchGuardByLocus, CLASS_CLUSTER_MIN_REPOS, isCoveredGraderSeed)

const carm = src.match(/function classifyAndRoute[\s\S]*?\n\}/)
assert(carm, 'could not locate classifyAndRoute in the workflow source')
const classifyAndRoute = new Function('groupByClass', 'routeClassCluster',
  `return (${carm[0]})`)(groupByClass, routeClassCluster)

const bcsem = src.match(/function buildClassSketchEntry[\s\S]*?\n\}/)
assert(bcsem, 'could not locate buildClassSketchEntry in the workflow source')
const buildClassSketchEntry = (0, eval)(`(${bcsem[0]})`)

// Fixture #1 — a clusterable, UNCOVERED, >= 2-distinct-repo same-class set → 'novel-sketch'.
const clusterableNovel = [
  { signature: 'sig-a', class: 'estimated-not-measured',
    suspected_location: 'skills/ci-speedup/scripts/collect_runs.py:foo', repos: ['org-a/repo1'] },
  { signature: 'sig-b', class: 'estimated-not-measured',
    suspected_location: 'skills/ci-speedup/scripts/blocking_path.py:bar', repos: ['org-b/repo2'] },
]
const routedNovel = classifyAndRoute(clusterableNovel)
assert.equal(routedNovel.routes['estimated-not-measured'].route, 'novel-sketch')
assert.equal(routedNovel.routes['estimated-not-measured'].distinctRepoCount, 2)
assert.equal(routedNovel.routes['estimated-not-measured'].matchedGuard, null)
assert.deepEqual(routedNovel.unclassified, [])

// Fixture #2 — all-singletons (a lone bug, AND two bugs of one class repeating the SAME repo) →
// 'point-fix-only' both ways — never synthesize a guard from under-threshold evidence.
const singleton = [{ signature: 'sig-c', class: 'other',
  suspected_location: 'x.py:f', repos: ['org-a/repo1'] }]
assert.equal(classifyAndRoute(singleton).routes['other'].route, 'point-fix-only')
assert.equal(classifyAndRoute(singleton).routes['other'].distinctRepoCount, 1)
const sameRepoTwice = [
  { signature: 'sig-d1', class: 'other', suspected_location: 'x.py', repos: ['org-a/repo1'] },
  { signature: 'sig-d2', class: 'other', suspected_location: 'y.py', repos: ['org-a/repo1'] },
]
const routedSameRepo = classifyAndRoute(sameRepoTwice)
assert.equal(routedSameRepo.routes['other'].route, 'point-fix-only',
  'two bugs of one class in the SAME repo is N=1 distinct repo, must not cluster')
assert.equal(routedSameRepo.routes['other'].distinctRepoCount, 1)
// A bug with NO class is never clustered (falls through untouched, not dropped).
assert.deepEqual(classifyAndRoute([{ signature: 'sig-e', suspected_location: 'z.py', repos: ['org-a/repo1'] }])
  .unclassified.map(b => b.signature), ['sig-e'])

// Fixture #3 — covered-class-LEAKED: >= 2 distinct repos AND a locus matching an existing guard →
// 'tighten-existing', carrying WHICH guard.
const coveredLeak = [
  { signature: 'sig-f', class: 'mis-ranked-lever',
    suspected_location: 'skills/ci-speedup/tests/verify_report.py:check_headline_pole_actually_gates',
    repos: ['org-a/repo1'] },
  { signature: 'sig-g', class: 'mis-ranked-lever',
    suspected_location: 'the same check in skills/ci-speedup/tests/verify_report.py missed a scoped variant',
    repos: ['org-b/repo2'] },
]
const routedLeak = classifyAndRoute(coveredLeak)
assert.equal(routedLeak.routes['mis-ranked-lever'].route, 'tighten-existing')
assert.equal(routedLeak.routes['mis-ranked-lever'].matchedGuard, GUARD_INVENTORY[0].guard)
assert.equal(routedLeak.routes['mis-ranked-lever'].distinctRepoCount, 2)

// A mixed set clusters classes INDEPENDENTLY — one class's routing must not leak into another's.
const mixed = classifyAndRoute([...clusterableNovel, ...singleton, ...coveredLeak])
assert.equal(mixed.routes['estimated-not-measured'].route, 'novel-sketch')
assert.equal(mixed.routes['other'].route, 'point-fix-only')
assert.equal(mixed.routes['mis-ranked-lever'].route, 'tighten-existing')

// Fixture — a grader-seed cluster spanning >= 2 distinct repos is COVERED-and-already-caught (an
// existing verify_report check fired to seed it). Its renderer-stub locus matches no guard, so
// PRE-FIX it wrongly routed to 'novel-sketch' (duplicating the check that caught it AND holding the
// loop's most deterministic bugs out of the autonomous fix fan-out). It must route 'point-fix-only'.
const graderSeedCovered = [
  { signature: 'grader-seed@check:check_headline_pole_actually_gates', source: 'grader-seed',
    class: 'headline-mislabel',
    suspected_location: 'skills/ci-speedup/scripts/blocking_path.py (renderer; exact locus to triage - grader-sourced)',
    repos: ['org-a/repo1', 'org-b/repo2'] },
]
const routedSeed = classifyAndRoute(graderSeedCovered)
assert.equal(routedSeed.routes['headline-mislabel'].route, 'point-fix-only',
  'a covered grader-seed at >= 2 repos must NEVER route to novel-sketch (H1)')
assert.equal(routedSeed.routes['headline-mislabel'].distinctRepoCount, 2)
assert.equal(routedSeed.routes['headline-mislabel'].coveredBySeed, true)
// Sanity: an IDENTICAL-shaped cluster WITHOUT the grader-seed marker still routes novel-sketch, so
// the exemption keys strictly on the seed marker, not on the renderer locus.
const nonSeedSameShape = [
  { signature: 'llm@blocking_path.py:foo', class: 'headline-mislabel',
    suspected_location: 'skills/ci-speedup/scripts/blocking_path.py:foo', repos: ['org-a/repo1'] },
  { signature: 'llm@blocking_path.py:bar', class: 'headline-mislabel',
    suspected_location: 'skills/ci-speedup/scripts/blocking_path.py:bar', repos: ['org-b/repo2'] },
]
assert.equal(classifyAndRoute(nonSeedSameShape).routes['headline-mislabel'].route, 'novel-sketch')

// Fixture #4 — a real >= 2-repo uncovered cluster whose DRAFTING AGENT reports it can't cleanly
// sketch (landable:false) → buildClassSketchEntry must surface 'not-landable', never silently drop
// it or misreport it as a clean sketch.
const notLandableVerdict = {
  definition: 'looked similar but is actually two unrelated defects',
  candidate_predicate: '(none — no shared root cause)',
  synthetic_instances: [], negative_cases: [],
  landable: false,
  summary: 'on closer reading the two instances do not share one root cause',
}
const notLandableEntry = buildClassSketchEntry('scope-overreach', { distinctRepoCount: 3 }, notLandableVerdict)
assert.equal(notLandableEntry.status, 'not-landable')
assert(notLandableEntry.summary.includes('FLAGGED NOT LANDABLE'))
assert(notLandableEntry.summary.includes('do not share one root cause'),
  'the drafting agent\'s own reason must be carried into the surfaced summary')
assert.equal(notLandableEntry.distinct_repo_count, 3)

// Control: a normal landable sketch verdict → status 'sketch', fields passed through.
const landableVerdict = {
  definition: 'a class definition', candidate_predicate: 'a candidate predicate',
  synthetic_instances: [{ description: 'scoped @foo/bar variant', includes_scope_or_monorepo_variant: true },
                        { description: 'plain variant', includes_scope_or_monorepo_variant: false }],
  negative_cases: ['a must-not-fire case'],
  maintainer_next_steps: 'author the invariant',
}
const landableEntry = buildClassSketchEntry('other', { distinctRepoCount: 2 }, landableVerdict)
assert.equal(landableEntry.status, 'sketch')
assert.equal(landableEntry.synthetic_instances.length, 2)
assert.equal(landableEntry.negative_cases.length, 1)

// A missing/thrown verdict is a COVERAGE GAP ('errored'), never read as "no cluster here".
assert.equal(buildClassSketchEntry('other', { distinctRepoCount: 2 }, null).status, 'errored')
assert.equal(
  buildClassSketchEntry('other', { distinctRepoCount: 2 }, { _sketch_errored: true, summary: 'threw' }).status,
  'errored')

// --- Hardening: the numeric run caps (--token-budget / --max-fixes) and the opus pin -----------
// Same drift-proof extraction discipline: pull the live regex literals + defaults from the source
// so the shipped parser and this pin can't silently drift apart.

// The flag regexes accept ONLY the single-token `=`-form; the two-token form's stranded halves
// must not parse (the unknown-flag guard is what rejects them loudly at run time).
const tbM = src.match(/TOKEN_BUDGET_RE\s*=\s*(\/.*?\/[a-z]*)/)
const mfM = src.match(/MAX_FIXES_RE\s*=\s*(\/.*?\/[a-z]*)/)
assert(tbM && mfM, 'could not locate the numeric-flag regex literals in the workflow source')
const tbRe = (0, eval)(tbM[1]), mfRe = (0, eval)(mfM[1])
assert(tbRe.test('--token-budget=2000000'), '=-form token budget must parse')
assert.equal('--token-budget=1500000'.match(tbRe)[1], '1500000')
assert(!tbRe.test('--token-budget'), 'bare flag half (two-token misuse) must NOT parse as the flag')
assert(!tbRe.test('2000000'), 'a stranded bare number must NOT parse as the flag')
assert(!tbRe.test('--token-budget=abc'), 'a non-numeric value must NOT parse')
assert(mfRe.test('--max-fixes=4'), '=-form max fixes must parse')
assert.equal('--max-fixes=7'.match(mfRe)[1], '7')
assert(!mfRe.test('--max-fixes'), 'bare flag half must NOT parse')

// isFlag must cover the numeric flags (else they'd be scouted as org slugs), and the unknown-flag
// guard must exist (else the two-token misuse's stranded halves are scouted silently).
assert(/isFlag\s*=\s*t\s*=>.*isNumFlag\(t\)/.test(src), 'isFlag must include isNumFlag')
assert(src.includes('unknownFlags'), 'the unknown-flag guard must exist')
assert(/unknown flag/.test(src), 'the unknown-flag guard must throw a helpful message')

// Defaults: the ceilings ship ON (2M tokens / 4 fixes) — an absent flag must never mean "no cap".
assert(/TOKEN_BUDGET\s*=.*\?\?\s*2_000_000/.test(src), 'token budget must default to 2_000_000')
assert(/MAX_FIXES\s*=.*\?\?\s*4/.test(src), 'max fixes must default to 4')

// The ceiling must be consulted at every fan-out stage — a stage that skips the check can spend
// past the cap unbounded (each string below is the stage tag overBudget() logs + records).
for (const stage of ['run', 'sketch', 'fix', 'review', 'integrate'])
  assert(src.includes(`overBudget('${stage}')`), `overBudget must gate the '${stage}' stage`)

// Every agent() opts object must pin model:'opus' — session-model inheritance is the documented
// footgun (a cheaper session model silently degrades the audit). Zero un-pinned `{ label:` sites.
// NOTE (load-bearing assumption): this pin can only SEE an un-pinned agent() site because every opts
// object in this file leads with `label:` (now `model: 'opus', label:`). A future opts object written
// with a different key order (e.g. `{ schema: X, label: 'y' }`, model omitted) would match NEITHER
// regex and slip past both asserts — keep the `model: 'opus', label:` prefix convention when adding
// an agent() call, or tighten this pin to an `agent(` … `model: 'opus'` proximity match.
assert.equal(src.match(/\{ label:/g), null, 'found an agent-opts site without the model pin')
const opusSites = (src.match(/\{ model: 'opus', label:/g) || []).length
assert(opusSites >= 5, `expected >= 5 opus-pinned agent-opts sites, found ${opusSites}`)

// objNum (object-form ceiling parsing): EXECUTE the real extracted source so its safety invariant
// can't drift. A `number` TYPE only — a boolean must NOT coerce (Number(true)===1 would set a
// 1-token ceiling that halts the whole run at the first stage), and neither may a numeric string;
// 0 / negative / NaN all fall through to null (→ token form → default), never a tiny/0 ceiling.
const objNumM = src.match(/const objNum = k => \{[\s\S]*?\n\}/)
assert(objNumM, 'could not locate objNum in the workflow source')
const objNumWith = a => (new Function('args', `${objNumM[0]}\nreturn objNum`))(a)
assert.equal(objNumWith({ token_budget: true })('token_budget'), null, 'objNum: boolean true must fall through to null (never a 1-token ceiling)')
assert.equal(objNumWith({ token_budget: 0 })('token_budget'), null, 'objNum: 0 must fall through to null')
assert.equal(objNumWith({ token_budget: -5 })('token_budget'), null, 'objNum: a negative must fall through to null')
assert.equal(objNumWith({ token_budget: '1500000' })('token_budget'), null, 'objNum: a numeric STRING must fall through (number type only)')
assert.equal(objNumWith({ max_fixes: 6.9 })('max_fixes'), 6, 'objNum: a real positive number is floored')
assert.equal(objNumWith(['nrwl'])('token_budget'), null, 'objNum: an array args yields null (defers to the token form)')

// numFromTokens (token-form ceiling parsing): the SAME positive-finite guard as objNum — the `\d+`
// regex matches `=0`, but a 0 must NOT become the ceiling (`0 ?? default` keeps the 0). Execute the
// real extracted source over an injected `tokens` list to prove `=0` yields null (→ default), while
// a real value parses.
const numFromM = src.match(/const numFromTokens = re => \{[\s\S]*?return null \}/)
assert(numFromM, 'could not locate numFromTokens in the workflow source')
const numFromWith = (re, toks) => (new Function('tokens', `${numFromM[0]}\nreturn numFromTokens`))(toks)(re)
assert.equal(numFromWith(tbRe, ['--token-budget=0']), null, 'numFromTokens: =0 must yield null (never a 0 ceiling), falling through to the default')
assert.equal(numFromWith(tbRe, ['--token-budget=1500000']), 1500000, 'numFromTokens: a real value parses')
assert.equal(numFromWith(mfRe, ['nrwl', '--max-fixes=7']), 7, 'numFromTokens: scans the token list for the flag')
assert.equal(numFromWith(tbRe, ['nrwl']), null, 'numFromTokens: absent flag yields null')

// spentNow must keep TWO budget failure shapes distinct — a bare `try/catch → 0` would fail the
// ceiling OPEN silently (the exact runaway this guards). A MISSING budget global degrades to spent 0
// via `typeof` (intended, silent); a PRESENT-but-throwing `budget.spent()` is captured loudly and
// surfaced as `probe_error` in the result, so a `run_spent_output_tokens: 0` with a non-null probe_error
// reads as a COVERAGE GAP, not a verified bounded run.
assert(/typeof budget === 'undefined'/.test(src), 'spentNow must treat a missing budget global as spent 0 via typeof (not a bare catch)')
assert(/budgetProbeError/.test(src), 'a present-but-throwing budget.spent() must be captured (budgetProbeError)')
assert(/probe_error:\s*budgetProbeError/.test(src), 'the result payload must surface probe_error so a blind ceiling is not read as a bounded run')

// --- #48: the ceiling is scoped to THIS RUN's spend, not the session-cumulative pool -----------
// budget.spent() is the harness's SESSION-cumulative counter and NEVER resets across turns/user
// messages (MEASURED 2026-07-17: three launches read 2,207,037 → 2,209,699 → 2,213,117), so the
// as-shipped (PR #29) comparison of the RAW pool against the ceiling could never admit a run once a
// session passed 2M. The fix snapshots the pool ONCE at start (RUN_SPEND_BASE) and gates on the delta.

// (1) Snapshot ordering: RUN_SPEND_BASE must be assigned BEFORE the first agent (the per-org run
// fan-out) in source order, so the run's own spend is fully covered by the delta.
const rsbIdx = src.indexOf('const RUN_SPEND_BASE = spentNow()')
assert(rsbIdx !== -1, 'RUN_SPEND_BASE must snapshot spentNow() once at script start')
const firstAgentIdx = src.indexOf('label: org,')
assert(firstAgentIdx !== -1, 'the per-org run fan-out agent call must exist')
assert(rsbIdx < firstAgentIdx,
  'RUN_SPEND_BASE must be snapshotted BEFORE the first agent (the per-org run fan-out) — else the run\'s own early spend escapes the ceiling')

// (2) The comparator gates on the RUN DELTA, and runSpend is that delta.
assert(/const runSpend = \(\) => spentNow\(\) - RUN_SPEND_BASE/.test(src),
  'runSpend must be the delta since the RUN_SPEND_BASE snapshot')
assert(/const overBudget = stage => \{\s*\n\s*const spent = runSpend\(\)/.test(src),
  'overBudget must read the run DELTA (const spent = runSpend()), never the raw session pool (spentNow())')
// A NEGATIVE delta must fail CLOSED, not read as under-budget: only 0 <= delta < ceiling passes. A
// bare `runSpend() < TOKEN_BUDGET` would let a negative delta (a counter that threw after a nonzero
// base — spentNow()→0 below RUN_SPEND_BASE) silently disable the ceiling in-flight (#48 review).
assert(/if \(spent >= 0 && spent < TOKEN_BUDGET\) return false/.test(src),
  'overBudget must pass only a NON-NEGATIVE delta below the ceiling — a negative delta (mid-run counter failure) must fail CLOSED')

// (3) Both payload fields: the governed run delta AND the raw pool reading (operator context).
assert(/run_spent_output_tokens:\s*runSpend\(\)/.test(src),
  'the token_budget payload must report run_spent_output_tokens (the governed delta)')
assert(/session_spent_output_tokens:\s*spentNow\(\)/.test(src),
  'the token_budget payload must also carry session_spent_output_tokens (the raw pool, for context)')
// The stale field name must be GONE — a spent_output_tokens survivor would misreport the pool as the
// governed number (the exact conflation #48 fixes).
assert(!/[^_]spent_output_tokens:/.test(src),
  'the old (session-pool) spent_output_tokens payload field must be replaced by run_/session_ fields')

// The ceiling's DECISION logic — the marquee safety property — EXECUTED, not just its call sites:
// a flipped comparator (`<` → `>=`), a broken latch, OR a regression to gating on the raw pool would
// leave the call-site pins green while breaking the ceiling. Extract spentNow + RUN_SPEND_BASE +
// runSpend + overBudget from source and drive them with a MUTABLE stub counter so we can model a
// session BASE and the run's own DELTA independently — the whole point of #48.
const spentM = src.match(/const spentNow = \(\) => \{[\s\S]*?\n\}/)
const rsbSrcM = src.match(/const RUN_SPEND_BASE = spentNow\(\)/)
const rsM = src.match(/const runSpend = \(\) => [^\n]*/)
const overM = src.match(/const overBudget = stage => \{[\s\S]*?\n\}/)
assert(spentM && rsbSrcM && rsM && overM, 'could not locate spentNow/RUN_SPEND_BASE/runSpend/overBudget in the workflow source')
// base = the SESSION pool reading at launch (what RUN_SPEND_BASE captures); the stub is mutable so a
// later setSpent(base + delta) models the run adding `delta` output tokens on top of that base.
// `setThrow(true)` flips budget.spent() to THROW (models a counter that worked at launch — so a
// nonzero RUN_SPEND_BASE was captured — then faults mid-run): spentNow() catches → 0, runSpend()
// goes negative. Existing cases never flip it, so their behavior is unchanged.
const mkCeiling = (base, ceiling = 100, budgetPresent = true) => {
  const preamble = 'let budgetStoppedAt = null; let budgetProbeError = null; const TOKEN_BUDGET = ' + ceiling + '; '
    + 'const log = () => {}; const errMsg = e => String((e && e.message) || e); let __spent = ' + base + '; let __throw = false; '
  const budgetDecl = budgetPresent
    ? 'const budget = { spent: () => { if (__throw) throw new Error("probe boom"); return __spent } };' : ''
  return (new Function(preamble + budgetDecl
    + spentM[0] + '\n' + rsbSrcM[0] + '\n' + rsM[0] + '\n' + overM[0]
    + '\nreturn { overBudget, spentNow, runSpend, setSpent: v => { __spent = v }, setThrow: v => { __throw = v },'
    + ' stopped: () => budgetStoppedAt, probeError: () => budgetProbeError }'))()
}
{
  // Delta-based >= semantics + latch (base 0, so delta == raw spend), TOKEN_BUDGET 100.
  const under = mkCeiling(0); under.setSpent(50)
  assert.equal(under.overBudget('run'), false, 'overBudget: run delta below ceiling must NOT trip')
  assert.equal(under.stopped(), null, 'overBudget: no stage recorded while under ceiling')
  const at = mkCeiling(0); at.setSpent(100)
  assert.equal(at.overBudget('run'), true, 'overBudget: run delta == ceiling must trip (>= semantics)')
  const over = mkCeiling(0); over.setSpent(150)
  assert.equal(over.overBudget('run'), true, 'overBudget: run delta above ceiling must trip')
  assert.equal(over.stopped(), 'run', 'overBudget: records the FIRST halting stage')
  assert.equal(over.overBudget('sketch'), true, 'overBudget: a later stage still reads tripped')
  assert.equal(over.stopped(), 'run', 'overBudget: latch is once-only — stays the first stage')

  // (4) THE #48 REGRESSION PROOF, with the MEASURED numbers: a session already at ~2.2M must NOT
  // refuse a fresh run at delta 0, and MUST stop it once the run itself adds >= the 2M ceiling.
  const BASE = 2_200_000, CEILING = 2_000_000
  const fresh = mkCeiling(BASE, CEILING)
  assert.equal(fresh.runSpend(), 0, 'at launch the run delta is 0 even though the session pool is ~2.2M')
  assert.equal(fresh.overBudget('run'), false,
    'base 2.2M + delta 0 must NOT be exhausted — the run has spent nothing yet (the #48 bug: the session pool alone tripped it)')
  fresh.setSpent(BASE + CEILING)   // the run itself spends the full 2M ceiling
  assert.equal(fresh.runSpend(), CEILING, 'runSpend tracks the delta the run added, not the session total')
  assert.equal(fresh.overBudget('run'), true,
    'base 2.2M + delta 2.0M must be exhausted — a genuine runaway WITHIN this run still stops')

  // (5) MID-RUN COUNTER FAILURE must fail CLOSED, not fail OPEN. A counter that WORKED at launch
  // (nonzero RUN_SPEND_BASE captured) and then THROWS makes spentNow() read 0, so runSpend() goes
  // NEGATIVE — the one in-flight fail-OPEN the raw `runSpend() < TOKEN_BUDGET` gate would leave (a
  // negative delta reads as under-budget, silently disabling the ceiling). overBudget must HALT.
  const faulted = mkCeiling(2_200_000, 2_000_000)
  assert.equal(faulted.overBudget('run'), false, 'sanity: before the fault the run delta is 0 and does not trip')
  faulted.setThrow(true)                            // budget.spent() now throws — spentNow() → 0
  assert.equal(faulted.spentNow(), 0, 'a throwing counter reads 0 (loud probe_error), so the delta drops below base')
  assert.ok(faulted.runSpend() < 0, 'runSpend() goes negative once the pool reads below the launch snapshot')
  assert.equal(faulted.overBudget('fix'), true,
    'a NEGATIVE run delta (mid-run counter failure) must FAIL CLOSED — halt, never read as under-budget')
  assert.equal(faulted.stopped(), 'fix', 'the fail-closed halt records the stage it stopped at')
  assert.notEqual(faulted.probeError(), null, 'the throwing counter is surfaced loudly as a probe_error (coverage gap)')

  const noBudget = mkCeiling(0, 100, false)
  assert.equal(noBudget.spentNow(), 0, 'spentNow: a missing budget global degrades to 0')
  assert.equal(noBudget.overBudget('run'), false, 'overBudget: never trips on an older harness (no budget global)')
  assert.equal(noBudget.probeError(), null, 'a missing budget global is NOT a probe error (silent, intended degrade)')
}

console.log(
  `ok — isTransient pins ${TRANSIENT.length} transient markers + rejects ${NON_TRANSIENT.length} ordinary errors; `
  + `isForceFlag recognizes --force/--fresh/-f; isAllowedFixPath accepts ${ALLOWED.length} / rejects ${REJECTED.length} paths; `
  + `fixWriteViolations (+ ARCHITECTURE.md co-occurrence guard) + overlappingScriptGroups + isIncoherentPatch + `
  + `reconcileIntegration + canonicalizeFixes + coalesceIntegration + routeCommittedReportFailure + `
  + `reviewVerdictRoutesToHuman + applyReviewVerdict + combineReviewVerdicts (+ prompt-integrity & agentType pins) + `
  + `matchGuardByLocus + classifyAndRoute (tighten/novel-sketch/point-fix-only routing) + `
  + `buildClassSketchEntry (incl. not-landable + errored) pinned; `
  + `hardening caps pinned (--token-budget=/--max-fixes= parsing, 2M/4 defaults, `
  + `5-stage overBudget gating, ${opusSites} opus-pinned agent sites, `
  + `objNum number-type-only safety executed, spentNow missing-vs-throwing budget + probe_error, `
  + `#48 run-scoped ceiling: RUN_SPEND_BASE snapshot-before-first-agent + runSpend delta comparator + `
  + `run_/session_ payload fields + base-2.2M/delta-0-vs-2M regression proof + `
  + `negative-delta fail-CLOSED on a mid-run counter throw)`)
