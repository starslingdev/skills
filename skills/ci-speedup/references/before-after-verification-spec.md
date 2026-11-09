# Before/After speedup verification — methodology (v1)

**Status:** implemented. The six design decisions are **locked** (see
"Decisions" below); this is the methodology contract for ci-speedup's built-in
post-fix verification phase.

**Amended 2026-09-08** with the workload-state vocabulary, the confounded-workload
reporting rule, the trigger/resume contract, the gate-migration rule, the
bootstrap clarification, and the optional descriptive baseline spread (historical
context only — it never replaces the locked adaptive threshold). Those amendments
narrow what may be *claimed* and define *when* the phase runs. **They change none of the six locked decisions**; where an
amendment and a locked decision could be read as interacting, the locked decision
governs.

**Measurement amendment 2026-11-02.** Phase 7 compares push-wide wall clock:
first job created to last job finished across **all** workflows triggered by the
push. This supersedes the former single-gating-check metric and sampling scope.
The N=2/adaptive-to-4 after-sample rule, variance threshold, exact-head binding,
rerun/dispatch mechanisms, opt-out, and workload-attribution safeguards remain.
A sample now requires a complete matched cohort, not one selected workflow.
The interim report protocol in `wall-clock-methodology.md` §7 additionally calls
for at least two before samples and one unchanged rerun as a noise control;
disclose that control separately, including its added cost. A missing complete
cohort makes repository wall-clock change unavailable.

## Problem

ci-speedup is a *speedup* tool, but today it never tells the user how much
speedup a fix actually delivered — the user has to ask. In the real
internal-dev-repo run, the operator applied the fix, CI went green,
and then had to prompt *"what's the before/after?"* — which is the whole point of
the skill. The payoff number should be surfaced **automatically**, once it's
measurable, with the disclaimers that keep it honest.

## Goal

After a selected fix is applied to a branch and its CI has run, **automatically**
report the measured before → after change in push-wide CI wall-clock, as a
range with a sample size and the disclaimers below — **without the user asking**.

Explicit non-goals (v1):
- Not a controlled A/B (we do **not** re-measure the baseline fresh — see
  "Asymmetry" below; we disclose it instead).
- Not post-merge / production tracking over time.
- Not a runner-minute saving — v1 sizes **wall-clock** merge-wait only. It
  discloses total job duration and billed compute beside that result, without
  treating either as a sized saving.
- Built-in and native to ci-speedup — no dependency on any external
  benchmarking tooling.

## Where it fits — a new phase 7 (post-fix verification)

The skill's phases end at **6 (Present & hand off)**, where — on the user's pick —
it applies the fix and (with the #234 close change, in flight) verifies it and
pauses before committing / opening a PR. Phase 7 presupposes the user then
**pushes the fix to a branch** and all workflows triggered by that push complete; it runs
**after** that:

```
6. Present & hand off → user picks a pole → apply fix → verify → (pause) → commit / push to branch
7. Verify the speedup (NEW)  ← branch CI has run → measure after → report before/after
```

Trigger condition: the fix is bound to an exact remote head SHA AND that head's
initial complete CI cohort has completed (see "Sampling", and "Trigger and resume
contract" for the precise eligibility, binding, and resume rules). The user
reaches phase 7 by authorizing and pushing the fix, then resuming the original
scratch findings artifact with `run.py --verify-branch`.

## Measurement design

### "Before" — reuse, don't re-measure
Reuse historical push cohorts with complete job creation/completion timestamps
across all triggered workflows. For each push compute
`max(job.completed_at) - min(job.created_at)`, then take the median over at least
two comparable pushes. Do not substitute the report's gating-check p50: it is a
different metric. Missing cross-workflow timestamps make the baseline unavailable.
The unchanged rerun control must keep its bound commit and workload identical;
report it separately rather than treating it as a changed-code sample.

### "After" — fresh runs on the fix branch
- **Sampling: N = 2, adaptive up to 4** (Decision 1). Collect the complete push-cohort timing on the fix branch from N samples — the automatic push/PR run plus
  reruns (see "Triggering"). CI wall-clock is noisy — in this repo's own data a
  single step swung 5m20s → 7m35s (±20%) across three runs, so **one run is not
  evidence** and even two can both land high or low by luck. If the runs disagree
  by more than the **variance threshold (default 20%, Decision 2)**, run one more,
  up to a **cap of 4**. Two is the minimum that can detect disagreement at all;
  the adaptive extra run is spent ONLY when the noise warrants it.
- **Report median + range over the collected runs, never a single "clean"
  figure.** e.g. `6m10s median of 3 runs (5m50s–6m30s)`. A point estimate over
  noisy CI is the thing that misleads.
- **Cold vs. warm cache — disclose, don't cherry-pick.** Classify a sample from
  its logs when they name a dependency, build, or task-cache miss or hit. State
  why a cold sample was cold when the log identifies key miss, branch-scope
  visibility, or eviction. Run position is descriptive context, never a
  substitute for log evidence. Keep every complete sample in the aggregate
  median and range, and also report cold and warm samples on separate lines;
  an unclassified sample stays unknown. A cold-vs-warm gap that trips the
  variance threshold is cache sensitivity, not jitter, so the adaptive run
  still fires and the range is shown.
- **Sampling costs wall-clock on the rerun path — it is not free.** `gh run rerun`
  produces *sequential* attempts of one run (see "Triggering"), so N samples cost
  ≈ N × one run of wall-clock — real time on already-slow CI. Only where the
  workflow declares `workflow_dispatch` can the N runs fire as distinct runs in
  parallel (≈ one run of wall-clock); that is the non-primary path. Either way the
  cost is disclosed (Decision 4), never hidden behind a "free" claim.

### What is compared
The **push-wide CI wall-clock**, from first job created to last job finished
across all workflows triggered by each push:
before-p50 vs after-median, plus the delta as a percentage and absolute time.
If the fix targeted a specific step, the report must show that same step's
before/after beside the push-cohort wall clock (the report already has the step
timeline). This line is mandatory so unrelated checkout or setup noise cannot
hide or imitate the changed step's result.

## Comparability and disclosure contract

This table is normative and closed-world: every row is evaluated for both arms.
Anything that cannot establish its evidence takes the state shown here rather
than being guessed from job counts or row position.

| Rule | Evidence | State | Report |
| --- | --- | --- | --- |
| aggregate | Retain every complete sample, whether cold, warm, or unknown. | All complete samples contribute to the locked aggregate. | Show the median and range over all complete samples, then show cold and warm samples on separate lines. |
| queue-work | Derive `queue_delta_s` and `work_delta_s` separately from job timestamps. | A runner-pool change is not attributable to the code change. | Show both deltas and name the changed runner pool. |
| compute | Compare the sum of job durations in both arms. | Disclosure only. | Show both totals beside wall clock; this is not a sized saving. |
| in-flight | An in-flight run is never a sample. | Excluded. | Name the in-flight run and its exclusion. |
| tests | Compare test totals or names by field name, never position. | Missing tests make the workload `reduced`. | Say "coverage change, not a speedup" and state the missing work. |
| lint-format-types | Compare files checked by lint, format, and type-check steps. | A lower checked set is `reduced`. | State the work parity evidence and the changed file count. |
| build-output | Compare artifact file list and hashes; use sizes where builds are not reproducible, or a printed compile-unit count. | Without either arm's printed fingerprint, use `unknown`. | Show the differing files, hashes, sizes, or compile-unit counts. |
| coverage | Compare reported coverage figures. | A coverage difference is `changed`. | State the coverage delta. |
| cache-replay | Count `(cached)`, `FROM-CACHE`, and `cache hit, replaying` as not run. | An after-arm replay share more than 10 percentage points above before is `changed`. | State both replay shares and the work counted as not run. |
| missing-totals | Check whether both arms print workload totals or names. | When neither arm prints them, use `unknown`, never `same`. | State that parity could not be established. |
| red-before | Check each arm's conclusion before comparing totals. | When the before arm is red, use never `same`. | State that the before workload failed. |
| runner-class | Compare runner label and image where the log names it. | A mismatch or runner pool change is not attributable. | Name both runner classes. |
| changed-step | Match the step changed by the fix across both arms. | This line is mandatory. | Show the same step before and after beside push-wide wall clock. |
| cold-evidence | Read dependency, build, and task-cache miss evidence from each log. | A logged cache miss makes that sample cold wherever it occurs. | State why it was cold when the log names key miss, branch scope, or eviction. |
| task-cache | Record Turbo, Nx, and Gradle hit share per sample. | A difference of more than 20 percentage points is not attributable. | Show each arm's hit share on separate warm and cold lines. |
| suspicious-warm | Find cache hits one arm lacked, including `FULL TURBO`, `CACHED`, and Go `(cached)`. | On persistent-disk runners this is `changed`. | Warn that the hits can come from unrelated jobs. |
| triggered-set | Compare the same workflows and jobs in both arms. | A different triggered set is `changed`. | List the added or missing workflows and jobs. |
| per-commit | Pair cohorts only when the collected data carries an explicit shared pair identity; never pair by position or reuse one arm's median as a synthetic pair. | Only explicit comparable pairs contribute; without them, this row is unavailable and requires no extra run. | Show the median of per-commit ratios and the worst explicit pair beside the ratio of medians, or state why pairwise ratios are unavailable. |
| billed-compute | Each job is rounded up to the minute and grouped by runner OS and runner ownership. | Disclosure only. | Show GitHub-hosted billed compute by runner OS; show self-hosted and third-party rounded compute separately, never folded into billed minutes. |
| runner-carry-over | Bind every measured multiple to its runner label and size. | The multiple is restricted to that class. | State the runner label and size with every multiple. |
| exclusions | Identify infrastructure stalls, bot-triggered runs, and jobs red on both arms. | Exclude them only from a filtered diagnostic; the complete triggered group remains the primary headline. | List exclusions and show wall clock with and without jobs red on both arms, with the complete-group value first. |
| lane | Bind each verdict to its workflow lane. | Required for every verdict. | Name the lane and its 30-day run count. |
| negative-control | Remove one known unit of work and rerun parity classification. | Required; the parity check must fail. | Show the negative control and the guard that rejected parity. |

## Disclaimers — mandatory, auto-included

Every before/after MUST carry these; they are the difference between an honest
measurement and a misleading one:

1. **Sample size** — "N fresh runs" stated explicitly.
2. **Asymmetry** — before = historical p50 of *M complete historical push cohorts* (many
   different commits, on `push` events); after = *N complete fresh branch cohorts* of a
   *single commit* (rerun attempts, on `pull_request`/rerun events), under
   possibly-different runner load, cache state, and queue conditions. So the
   "after" captures one commit's run-to-run noise, not the commit-to-commit
   variation the "before" spans, and the two sides may even select different jobs
   if the trigger events do (verify the after job set matches the before's). This
   is **not** a same-window controlled experiment.
3. **CI-only-scope precondition (guarded, see below)** — the delta is
   attributable to the CI change *only if* the branch changes nothing but CI
   config. If it touches product code, the timing conflates the two. The
   **workload state** (`same` / `reduced` / `increased` / `changed` / `unknown` —
   see "Workload state" below) is stated too; only `same` supports a clean
   "same work, faster" claim, and any other state is reported with its confound.
4. **Variance** — the range is shown; if runs disagree beyond the threshold even
   after the cap, say the result is noisy and give the range, not a point.
5. **Cache state** — whether the "after" reflects warm or cold caches, and that
   warm reruns of one commit are the optimistic, best-cache end (a real future
   branch's first push hits cold caches and can be slower).
6. **Environment** — this is CI wall-clock; a user's merge-queue experience can
   differ with different runner tiers / concurrency settings.

### The CI-only-scope guard
Before claiming a clean attribution, run two cheap config-fact checks on the fix
branch:

1. **No product code changed.** Diff the branch against its base. "CI config" is
   broader than `.github/workflows/`: `.github/actions/` (composite/local
   actions), CI shell scripts the workflow calls (e.g. `scripts/ci/`), and
   Dockerfiles/compose the runner builds all count as CI. If the changed files are
   only those, no product code moved; if **any product/source file** changed,
   downgrade — report the number but flag that it conflates the CI change with the
   code change, and don't headline a clean "X% faster from the CI fix." (The exact
   CI-path set is a judgment call at the boundary; enumerate it in the
   implementation and make it configurable.)
2. **The same work ran.** A pure-workflow edit can still change *what* runs — path
   filters, `if:` skips, test sharding, or a reduced matrix (all fixes ci-speedup
   itself produces) can make the "after" faster because **different CI executed**,
   not because the same CI got faster. Classify the workload with the states
   below and do not headline a clean speedup unless the state is `same`.

These establish that no *product code* changed and the *same work* ran — they do
not neutralize the cache/commit/event asymmetries above, so "cleanly attributable"
means "attributable to the CI change," not "controlled."

### Workload state — evidence-backed, not one universal label

*(2026-09-08 amendment. This replaces the earlier universal "less work run"
label, which asserted a direction — less — that the evidence often does not
support. It narrows what may be claimed; it changes none of the six locked
decisions.)*

Compare the after run's workload identity against the before's and record exactly
one state:

- **`same`** — the same jobs/checks and the same units of work are identified in
  both. Only this state permits a clean "same work, faster" attribution.
- **`reduced`** — positive evidence that work present before is absent after (a
  named job/check/test target no longer executed, a matrix leg dropped, a path
  filter that demonstrably skipped identified work).
- **`increased`** — positive evidence of work present after that was not present
  before (added jobs, added matrix legs, added steps).
- **`changed`** — the workload differs in identity without a defensible net
  direction: work both appeared and disappeared, or was redistributed (the same
  tests re-split across a different number of shards, work relocated between
  jobs).
- **`unknown`** — the evidence needed to compare workload identity is missing,
  ambiguous, or not retained. `unknown` is the default; a state is only asserted
  when its evidence exists.

Classify a direction **only when workload identity/count evidence supports it**.
Two rules the classifier must obey, because both mistakes are easy:

- **Equal job counts alone do not prove equal work.** The same number of jobs can
  run a different matrix, a different test selection, or a different filtered set.
  Equal counts with no identity evidence is `unknown`, not `same`.
- **Sharding alone does not prove coverage reduction.** Re-sharding a suite
  changes how work is divided, not necessarily how much of it runs: re-splitting
  the same suite across more or fewer shards moves the job count while coverage
  is unchanged. Absent evidence about the work each shard actually ran, that is
  `changed`, not `reduced`.

The conservative same-work attribution gate is preserved exactly as written
above: it is `same` that unlocks a clean attribution, and every other state
withholds it.

### Reporting a confounded workload

For **`increased`**, **`changed`**, or **`unknown`**, report the observed timing
change *together with the confound*, in the same breath, and stop there.
Specifically:

- State the measured delta as observed, and state what about the workload
  differed (or that it could not be established).
- **The observed delta is never a lower bound on the fix's benefit** when the
  workload is `increased`, `changed`, or `unknown`; do not claim that added or
  altered work makes it one. "It got faster even though it did more work, so the real win is
  at least this big" is not supported: the added work may be cheap, concurrent,
  off the critical path, or cache-warming, and the altered work may have moved
  time rather than removed it. There is no floor to quote.
- `reduced` is likewise not a clean speedup: the delta includes work that simply
  did not run.
- Product-code changes continue to disallow clean CI-only attribution regardless
  of workload state; guard 1 and guard 2 are independent and both must pass.

### Optional descriptive baseline context

Where a descriptive spread over the baseline's own observations is available
(minimum/median/maximum and sample count of the before-side observations), it MAY
be shown as historical context alongside the before figure. It **must not replace
the locked adaptive threshold**, must **not become a significance test** or an
"outside the noise" verdict, and must not alter sample selection or the adaptive
sampling decision — where the two could interact, **the locked decision governs**
(Decisions 1 and 2). Its absence never blocks this phase.

## Triggering the runs (Decision 3)

- The fix is pushed to a branch; its complete `pull_request`/`push` CI cohort
  runs automatically. Once every triggered workflow finishes, that is sample 1 (cold cache). **Bootstrap (2026-09-08
  amendment):** that completed automatic branch cohort **supplies the first sample** — this
  phase then collects only the remaining attempts up to N. Requiring two
  pre-existing samples before the component responsible for obtaining sample two
  may start would **deadlock**, since nothing else produces sample two. The
  locked default (N = 2, adaptive to 4, Decision 1) counts the automatic run
  among the N; the amendment names where sample one comes from, it does not raise
  or lower N.
- For samples 2..N, **`gh run rerun <run-id>` is the primary mechanism** — it
  re-runs each required member of the complete cohort on the same branch head, works for ANY workflow
  (no config needed), pollutes no history, and re-uses the identical commit so it
  isolates CI variance from code. It also hits the warm cache from sample 1 (the
  steady-state end — see "Cold vs. warm cache").
- **Reruns are sequential, not parallel.** `gh run rerun` creates the next
  *attempt* of one run-id, and a run has one in-flight attempt at a time — you
  cannot run attempts 2 and 3 at once. So samples 2..N are collected serially, and
  N samples cost ≈ N × one run of wall-clock on this path. (Same attempt model
  `collect_runs.py` already handles for rerun/attempt waste.)
- **`workflow_dispatch` where every required cohort member supports it** — an optional
  transport extension beyond the implemented v1 serial-rerun path, and the only path
  that yields *distinct run-ids* and can therefore fire N runs **truly in
  parallel** (≈ one run of wall-clock). But many workflows are `push`/`pull_request`
  only (the website's `ci.yml` is), so dispatch cannot be the primary path; use it
  for parallel sampling when available, otherwise accept serial reruns.
  **Dispatch takes a ref, not a SHA, so it does not satisfy the exact-head
  binding on its own:** a dispatched run builds whatever the ref points at when
  it starts, so a follow-up commit silently swaps the commit under the sample.
  Check each dispatched run's `head_sha` against the bound fix head and **discard
  any sample that does not match** (the changed-head rule below then applies). A
  rerun cannot drift this way — it re-uses the identical commit by construction —
  which is the second reason it is the primary path.
- **Never re-push** (empty commits pollute history) — explicitly rejected.
- Guard: enumerate all workflows triggered by the push, including delayed or
  chained workflows, and bind their run/attempt identities to that same cohort.
  Only complete cohorts contribute a repository wall-clock sample. A rerun of
  the gating workflow alone remains a check-level diagnostic, not a new push
  sample; never pair its fresh timing with stale sibling timings. Additional
  sampling must reproduce the full relevant cohort with the same head, event,
  workload and dependency semantics, or withhold the push-wide comparison.
  Price the cost of that full cohort and the unchanged control before sampling.

## Trigger and resume contract (2026-09-08 amendment)

This section makes "once the fix is on a branch" precise. It defines *when* the
phase may start, *what* it is bound to, and *what happens when it is invoked
again*. It changes none of the six locked decisions — sampling defaults, the
variance threshold, the rerun mechanism, automatic-with-disclosed-cost, the env
knobs, and wall-clock-only all continue to govern once the phase is eligible.

### Eligibility

Phase 6 still stops at a verified, **uncommitted** change and the existing user
checkpoint before commit / PR. This work **does not authorize bypassing that
checkpoint**: no commit, push, or branch creation happens on the user's behalf
to make verification possible.

Phase 7 becomes eligible when all of the following hold:

1. The **authorized commit/push** has occurred — the user took the phase-6
   checkpoint decision and the fix reached the remote.
2. The fix is bound to an **exact remote head SHA**, not a branch name. A branch
   name is a moving target; the comparison is about one commit.
3. That head's initial complete CI cohort has **completed** (it is sample one — see
   Bootstrap above).

During an ongoing authorized fix session, continue automatically when those facts
become available — that is Decision 4's automatic behavior, with the cost line.

### Resume after the session ends

If the session ends before verification completes, a **later invocation can
resume from the saved scratch context** and finish the comparison. This is a
resume-on-next-invocation, **not a background daemon**: nothing keeps running,
polls, or spends runner minutes between sessions, and the output must never
imply that something is watching in the background. If no one invokes the skill
again, no further samples are ever collected.

Runtime context is persisted beside the run's scratch artifacts, outside tracked
and installable content.

### Context binding

Bind the saved context to all of:

- repository,
- the baseline artifact the "before" came from,
- the **fix head SHA**,
- workflow / check identity being sampled for every member of the complete push cohort,
- the run and attempt IDs already collected.

### Repeat invocation

Repeated invocation reuses completed samples and an existing verdict for that
head. It **must not spend another set of reruns** to re-answer a question already
answered for the same head — the disclosed cost is paid once, not once per
invocation. Reuse is still subject to Decision 5's escape hatch:
`CI_SPEEDUP_VERIFY_RUNS=0` disables phase 7 outright, so a saved verdict is not
rendered either — the knob turns the phase off, not merely its spending.

**A changed head invalidates in-flight comparison.** If the remote head moved
(amended commit, force-push, new commits on the branch), samples collected
against the old head are not mixed with the new one: the in-flight comparison is
discarded and a fresh after-sample set is collected against the new head, under
the same locked N and threshold.

### Gate migration — the after metric must describe the current gate

**The after metric must describe the current merge gate.** A fix that speeds up
or removes the former pole often hands the gate to whatever check is now slowest.
Reporting the former pole's improvement as a merge-wait improvement would then
describe a check that no longer decides when the user can merge.

- Across all workflows triggered by the push, **re-evaluate the gate/chain** on
  the after head; the last completed job sets the end of the measured interval.
- Never report a former pole's improvement as merge-wait improvement after
  another check has taken over. Report the check-level improvement as a
  check-level fact, and state that the gate moved.
- If relevant competing workflows or external checks cannot be compared on the
  same after head, report the measured check-level result and mark the
  repository **merge-wait change unavailable**. Unavailable is an honest answer;
  a number that describes the wrong check is not.
- **Do not expand sampling to unrelated workflows** outside the push cohort.
  All workflows actually triggered by that push are part of the metric, even
  when none of their files changed. Disclose their sampling cost as part of the
  complete cohort; never assume their duration remained unchanged.

## Fetching + computing "after"

For each complete cohort, compute `max(job.completed_at) - min(job.created_at)`
across every job of every workflow triggered by the push. Aggregate those cohort
intervals as median + range; never sum workflow durations or average per-job
ratios. Bind every run and attempt to the exact head and trigger cohort, including
workflow chains. A shared SHA alone does not prove two independent events belong
to the same push. Wait for all relevant workflows to finish; missing timestamps,
unresolved trigger membership, or an incomplete attempt bundle means the
repository merge-wait change is unavailable.

Rerun enumeration is per attempt (`filter=all` / `/runs/{id}/attempts/{n}`),
not the latest-attempt run list. Use fresh job timestamps for that attempt,
never the original run creation time. Dispatch samples must preserve the complete
cohort's event and job-set semantics; otherwise report them as check-level facts.
One former pole getting faster is not evidence of a push-wide speedup.

## Output (rendered in the close, plain-English)

Auto-emitted once phase 7 completes, in the phase-6 plain-English close voice (no
jargon, no machinery — the #234 UX direction):

```
Speedup (measured): push-wide CI now finishes in ~6m10s, down from ~8m36s — about 28% faster.
  • After: median of 3 complete fresh runs on your branch (5m50s–7m05s), including cold and warm samples.
  • Cache: cold 7m05s (cache key miss); warm 5m50s–6m30s.
  • Before: median of 12 complete historical push cohorts.
  • Queue delta: -8s. Work delta: -2m18s. Total job duration: 22m before, 20m after.
  • Changed step: build, 3m12s before and 1m05s after.
  • This branch changes only CI config and runs the same work on the same runner class, so the gain is attributable to the fix.
  • Lane: pull requests, 240 runs in the last 30 days.
  • CI timing varies run-to-run, on whatever runner tier/concurrency you use; this is one branch's runs vs recent history, not a controlled test.
```

That example is the `same`-workload case. If the guard trips (non-CI files
changed), or the workload state is anything but `same`, or the gate moved to
another check, or variance stays high, or CI failed on the branch, the block says
so plainly instead of quoting a clean number — e.g. *"the branch also added a job,
so this timing change and that extra work can't be separated"* rather than any
claim that the fix is worth at least the measured delta.

## Cost, automation, and configuration (Decisions 4 & 5)

N complete push cohorts plus the unchanged control cost runner-minutes **and** wall-clock. On the primary rerun path the runs
are serial, so wall-clock is ≈ N × one run (only `workflow_dispatch` parallelism
collapses it to ≈ one run); billing is N × one run's runner-minutes regardless.

- **Automatic, cost disclosed in the same line, NOT gated (Decision 4).** The
  whole UX goal is that the user never has to ask for the payoff — a confirm
  reintroduces the friction we're removing. So phase 7 runs automatically, and the
  line that kicks it off states the real cost as it happens — **both** the added
  wall-clock and the billing minutes, computed from the report's own per-run job
  data, not the bare run count: e.g. *"Measuring the speedup — rerunning your CI
  fix N× (~M more minutes of wall-clock, ~N×P runner-minutes)."* ("~N billing
  minutes" would be wrong — one run is many runner-minutes, not one.)
  Transparency, not a gate. (Edge refinement: if the report's own data shows the
  gating run is unusually expensive, escalate to a one-line confirm — otherwise
  auto.)
- **Env-configurable** (Decision 5), matching
  the `CI_SPEEDUP_FETCH_CONCURRENCY` / `STARSLING_LOG_LEVEL` convention:
  - `CI_SPEEDUP_VERIFY_RUNS` — default `2`; **`0` disables phase 7 entirely** (the
    clean escape hatch that makes "auto" safe).
  - `CI_SPEEDUP_VERIFY_VARIANCE_PCT` — default `20`; the adaptive-extra-run
    threshold.

## Edge cases

- **CI failed on the branch** → no valid "after"; report the failure, don't
  fabricate a number.
- **Fix didn't change the gating check's identity** → normal case, compare.
- **Fix changed which check gates** (e.g. removed the pole) → report that the
  former gate is gone and what the new merge-wait is, per "Gate migration" above.
  The former pole's own improvement is a check-level fact, never the merge-wait
  delta once another check gates; if the new gate cannot be compared on the same
  after head, mark the repository merge-wait change unavailable.
- **Only 1 run available** (dispatch unsupported, or user won't spend N) → report
  it as a single cold sample with a loud "one run, treat as indicative" caveat.
- **High variance past the cap** → range only, explicitly "noisy."

## Decisions (locked)

All six resolved; these are the contract the implementation follows. Under the
2026-11-02 metric amendment, a "run" sample means a complete matched push cohort.
Single-workflow attempts are implementation units, never substitutes for that
cohort. Cost estimates below use the whole cohort plus the separate noise control.

1. **Default N = 2, adaptive up to 4.** Two is the minimum that can detect
   disagreement; the extra run is spent only when variance warrants it. On the
   primary rerun path the samples are serial, so N runs cost ≈ N × one run of
   wall-clock — real time, disclosed up front (Decision 4), not free; keeping the
   default at two holds that cost down while still detecting disagreement. (Where
   `workflow_dispatch` is available the N runs go parallel at ≈ one run of
   wall-clock.) The reported median + range spans all N runs, including the cold
   first run.
2. **Variance threshold = 20%** (of the median). This repo's natural CI noise is
   ~±20% (calibrated here, shipped as the default, overridable via
   `CI_SPEEDUP_VERIFY_VARIANCE_PCT`); a 15% threshold would fire on ordinary
   jitter and defeat starting at 2. A genuine cold-vs-warm cache gap will exceed
   it — that is real cache-sensitivity, and firing the adaptive run there is
   correct, not waste. The range is always shown, so a 2-sample result is never
   dressed up as cleaner than it is.
3. **Trigger via `gh run rerun` (primary), `workflow_dispatch` where declared,
   never re-push.** Rerun works for any workflow, pollutes no history, reuses the
   identical commit (isolating CI variance), and hits the warm cache — but its
   samples are sequential attempts of one run-id, so they collect serially.
   `workflow_dispatch` is the only path that yields distinct run-ids and can fire
   the samples in parallel; it isn't universally declared, so it can't be primary.
4. **Automatic, with the runner-minute cost disclosed in the same line — not
   gated.** Auto serves the "never ask for the payoff" goal; the disclosed cost
   keeps it honest; `CI_SPEEDUP_VERIFY_RUNS=0` is the opt-out.
5. **Env-configurable** — `CI_SPEEDUP_VERIFY_RUNS` (default 2, `0`=off) and
   `CI_SPEEDUP_VERIFY_VARIANCE_PCT` (default 20), per the existing knob
   convention. The phase documents the safe `0` opt-out; the variance tuning
   knob remains a reference-level detail.
6. **Wall-clock only in v1; runner-minute (billing) before/after is a documented
   fast-follow.** Merge-wait is the payoff the user feels and the skill headlines.
   A billing delta would compare the report's *modeled* before against a *measured*
   after — the modeled-vs-measured mismatch this codebase is disciplined about
   avoiding — so it ships as its own clean increment, not bolted onto v1.

The compute rows required by the comparability contract are disclosures. They
do not size a runner-minute saving, change the wall-clock headline, or alter
Decision 6.

## Implementation map

- SKILL.md: add phase 7 (post-fix verification) + its close-rendering rules.
- `before_after.py` triggers the samples with serial reruns, polls to completion, enumerates
  the run's **attempts** (new per-attempt fetch — the run-list sampler sees only
  one row), compute each complete push cohort's first-created → last-finished interval, run the two-part CI-only-scope
  guard (no product code + same work ran), classify cold/warm from log evidence,
  compare runner class, triggered set, task-cache hit share and work parity, and
  emit the disclosed before/after block (all six disclaimers, real wall-clock +
  billing cost). The dispatch contract above remains the safe shape for a future
  parallel transport; v1 does not claim to dispatch workflows.
- Tests: median/range over all complete samples with cold and warm samples on separate lines, the
  variance-adaptive-N logic, log-backed cold classification, the CI-only-scope guard
  (product code → conflated; each workload state reached only on its own
  evidence, with equal job counts and a pure re-shard NOT classified as
  `reduced`; `increased`/`changed`/`unknown` never headlining a clean
  attribution or a lower bound), the trigger/resume contract (no reruns before a
  remote fix head exists; the automatic run counted as sample one; repeat
  invocation collecting no duplicate samples; a moved head discarding in-flight
  samples), the gate-migration rule (a new gate or an uncomparable competing
  workflow cannot be reported as the old check's merge-wait delta), and the
  "CI failed / 1 run / high variance" degradations — all offline with
  synthesized run timings.
- CHANGELOG entry.
