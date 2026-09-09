# Before/After speedup verification — spec / plan (v0 draft, for review)

**Status:** approved plan, not yet implemented. The six design decisions are
**locked** (see "Decisions" below); nothing in this PR changes skill behavior.
This becomes the methodology doc for a new, lightweight, **built-in** ci-speedup
phase once the implementation lands in a follow-up PR.

**Amended 2026-09-08** with the workload-state vocabulary, the confounded-workload
reporting rule, the trigger/resume contract, the gate-migration rule, and the
bootstrap clarification. Those amendments narrow what may be *claimed* and define
*when* the phase runs. **They change none of the six locked decisions**; where an
amendment and a locked decision could be read as interacting, the locked decision
governs.

## Problem

ci-speedup is a *speedup* tool, but today it never tells the user how much
speedup a fix actually delivered — the user has to ask. In the real
internal-dev-repo run, the operator applied the fix, CI went green,
and then had to prompt *"what's the before/after?"* — which is the whole point of
the skill. The payoff number should be surfaced **automatically**, once it's
measurable, with the disclaimers that keep it honest.

## Goal

After a selected fix is applied to a branch and its CI has run, **automatically**
report the measured before → after change in the gating check's merge-wait, as a
range with a sample size and the disclaimers below — **without the user asking**.

Explicit non-goals (v1):
- Not a controlled A/B (we do **not** re-measure the baseline fresh — see
  "Asymmetry" below; we disclose it instead).
- Not post-merge / production tracking over time.
- Not a runner-minute (billing) before/after — v1 measures **wall-clock**
  merge-wait only; a billing delta can follow later.
- Built-in and native to ci-speedup — no dependency on any external
  benchmarking tooling.

## Where it fits — a new phase 7 (post-fix verification)

The skill's phases end at **6 (Present & hand off)**, where — on the user's pick —
it applies the fix and (with the #234 close change, in flight) verifies it and
pauses before committing / opening a PR. Phase 7 presupposes the user then
**pushes the fix to a branch** and its CI runs the gating workflow; it runs
**after** that:

```
6. Present & hand off → user picks a pole → apply fix → verify → (pause) → commit / push to branch
7. Verify the speedup (NEW)  ← branch CI has run → measure after → report before/after
```

Trigger condition: the fix is bound to an exact remote head SHA AND that head's
gating-workflow run has completed (see "Sampling", and "Trigger and resume
contract" for the precise eligibility, binding, and resume rules). Until a
push-to-branch step exists in the skill (phase 6 today ends at applying the fix,
not pushing it), phase 7 is gated on that landing.

## Measurement design

### "Before" — reuse, don't re-measure
The report already computes the gating check's **p50 over the sampled window**
(the measured critical-path / merge-wait — SKILL.md phase 3, the "Bottom line"
figure). Reuse that exact number as the baseline. Do **not** re-run main.

### "After" — fresh runs on the fix branch
- **Sampling: N = 2, adaptive up to 4** (Decision 1). Collect the gating
  workflow's timing on the fix branch from N runs — the automatic push/PR run plus
  reruns (see "Triggering"). CI wall-clock is noisy — in this repo's own data a
  single step swung 5m20s → 7m35s (±20%) across three runs, so **one run is not
  evidence** and even two can both land high or low by luck. If the runs disagree
  by more than the **variance threshold (default 20%, Decision 2)**, run one more,
  up to a **cap of 4**. Two is the minimum that can detect disagreement at all;
  the adaptive extra run is spent ONLY when the noise warrants it.
- **Report median + range over the collected runs, never a single "clean"
  figure.** e.g. `6m10s median of 3 runs (5m50s–6m30s)`. A point estimate over
  noisy CI is the thing that misleads.
- **Cold vs. warm cache — disclose, don't cherry-pick.** The first run on a fresh
  branch often misses caches (nothing to restore yet), so it sits at the slow,
  cold-start end; the reruns that follow hit the cache that first run populated
  and sit at the warm, steady-state end. Detect this by **run position** — the
  branch's first run is the cold sample, its reruns are warm — not by parsing
  cache-hit log lines (the run-level API doesn't surface hit/miss). Keep **all**
  collected runs in the reported median + range and **disclose the cache state**;
  the cold run pulls the median toward the slow end (a conservative bias that
  under- rather than over-states the win). Do **not** report a warm-only median:
  at the default N = 2 that leaves a single warm sample, which "one run is not
  evidence" forbids. A cold-vs-warm gap that trips the variance threshold is real
  cache-sensitivity, not jitter — the adaptive run fires and the range is shown.
  If only the cold run is available, say so and treat it as a ceiling.
- **Sampling costs wall-clock on the rerun path — it is not free.** `gh run rerun`
  produces *sequential* attempts of one run (see "Triggering"), so N samples cost
  ≈ N × one run of wall-clock — real time on already-slow CI. Only where the
  workflow declares `workflow_dispatch` can the N runs fire as distinct runs in
  parallel (≈ one run of wall-clock); that is the non-primary path. Either way the
  cost is disclosed (Decision 4), never hidden behind a "free" claim.

### What is compared
The **gating check's wall-clock** (the merge-wait the report headlines):
before-p50 vs after-median, plus the delta as a percentage and absolute time.
Optionally, if the fix targeted a specific step, the step-level before/after too
(the report already has the step timeline).

## Disclaimers — mandatory, auto-included

Every before/after MUST carry these; they are the difference between an honest
measurement and a misleading one:

1. **Sample size** — "N fresh runs" stated explicitly.
2. **Asymmetry** — before = historical p50 of *M sampled main runs* (many
   different commits, on `push` events); after = *N fresh branch runs* of a
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

- The fix is pushed to a branch; its `pull_request`/`push` CI runs once
  automatically. That's sample 1 (cold cache). **Bootstrap (2026-09-08
  amendment):** that completed automatic branch run **supplies the first sample** — this
  phase then collects only the remaining attempts up to N. Requiring two
  pre-existing samples before the component responsible for obtaining sample two
  may start would **deadlock**, since nothing else produces sample two. The
  locked default (N = 2, adaptive to 4, Decision 1) counts the automatic run
  among the N; the amendment names where sample one comes from, it does not raise
  or lower N.
- For samples 2..N, **`gh run rerun <run-id>` is the primary mechanism** — it
  re-runs the existing gating run on the same branch head, works for ANY workflow
  (no config needed), pollutes no history, and re-uses the identical commit so it
  isolates CI variance from code. It also hits the warm cache from sample 1 (the
  steady-state end — see "Cold vs. warm cache").
- **Reruns are sequential, not parallel.** `gh run rerun` creates the next
  *attempt* of one run-id, and a run has one in-flight attempt at a time — you
  cannot run attempts 2 and 3 at once. So samples 2..N are collected serially, and
  N samples cost ≈ N × one run of wall-clock on this path. (Same attempt model
  `collect_runs.py` already handles for rerun/attempt waste.)
- **`workflow_dispatch` where the gating workflow declares it** — the only path
  that yields *distinct run-ids* and can therefore fire N runs **truly in
  parallel** (≈ one run of wall-clock). But many workflows are `push`/`pull_request`
  only (the website's `ci.yml` is), so dispatch cannot be the primary path; use it
  for parallel sampling when available, otherwise accept serial reruns.
- **Never re-push** (empty commits pollute history) — explicitly rejected.
- Guard: only the gating workflow needs sampling — don't fan out every workflow.

## Trigger and resume contract (2026-09-08 amendment)

This section makes "once the fix is on a branch" precise. It defines *when* the
phase may start, *what* it is bound to, and *what happens when it is invoked
again*. It changes none of the six locked decisions — sampling defaults, the
variance threshold, the rerun mechanism, automatic-with-disclosed-cost, the env
knobs, and wall-clock-only all continue to govern once the phase is eligible.

### Eligibility

Phase 6 still stops at a verified, **uncommitted** change and the existing user
checkpoint before commit / PR. Nothing here authorizes bypassing that
checkpoint — this work **does not authorize bypassing that checkpoint**, and no
commit, push, or branch creation happens on the user's behalf to make
verification possible.

Phase 7 becomes eligible when all of the following hold:

1. The **authorized commit/push** has occurred — the user took the phase-6
   checkpoint decision and the fix reached the remote.
2. The fix is bound to an **exact remote head SHA**, not a branch name. A branch
   name is a moving target; the comparison is about one commit.
3. That head's initial relevant CI run has **completed** (it is sample one — see
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
- workflow / check identity being sampled,
- the run and attempt IDs already collected.

### Repeat invocation

Repeated invocation reuses completed samples and an existing verdict for that
head. It **must not spend another set of reruns** to re-answer a question already
answered for the same head — the disclosed cost is paid once, not once per
invocation.

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

- Within the sampled workflow, **re-evaluate the gate/chain** on the after head
  and report merge-wait against whatever now gates.
- Never report a former pole's improvement as merge-wait improvement after
  another check has taken over. Report the check-level improvement as a
  check-level fact, and state that the gate moved.
- If relevant competing workflows or external checks cannot be compared on the
  same after head, report the measured check-level result and mark the
  repository **merge-wait change unavailable**. Unavailable is an honest answer;
  a number that describes the wrong check is not.
- **Do not expand sampling to unrelated workflows** to manufacture that number.
  The sampling guard above (only the gating workflow is sampled) still holds, and
  widening the fan-out would spend cost the locked disclosure did not price.

## Fetching + computing "after"

Compute the after with the same *metric* as the before — the gating check's
per-run wall-clock, median + range — but note it needs new *enumeration* logic,
not a like-for-like reuse of the data pass. The data pass samples the **run list**
(one row per run-id, latest attempt only); rerun samples are N **attempts of a
single run-id**, so the run list would show one row, not N. Reading the N attempts
means enumerating them per-attempt (`filter=all` / the `/runs/{id}/attempts/{n}`
endpoint), which the run-list sampler does not do — `collect_runs.py`'s attempt
handling is the closest existing building block. Filter to the gating check, take
each attempt's wall-clock, then median + range. (Where `workflow_dispatch`
produced distinct run-ids, the ordinary run-list path applies.)

## Output (rendered in the close, plain-English)

Auto-emitted once phase 7 completes, in the phase-6 plain-English close voice (no
jargon, no machinery — the #234 UX direction):

```
Speedup (measured): the test check now merges in ~6m10s, down from ~8m36s — about 28% faster.
  • After: median of 3 fresh runs on your branch (5m50s–6m30s), including the cold first run.
  • Before: median of 12 recent main runs.
  • This branch changes only CI config and runs the same jobs, so the gain is attributable to the fix.
  • Cache: the warm reruns are the steady state; a first push hits cold caches and can be slower.
  • CI timing varies run-to-run, on whatever runner tier/concurrency you use; this is one branch's runs vs recent history, not a controlled test.
```

That example is the `same`-workload case. If the guard trips (non-CI files
changed), or the workload state is anything but `same`, or the gate moved to
another check, or variance stays high, or CI failed on the branch, the block says
so plainly instead of quoting a clean number — e.g. *"the branch also added a job,
so this timing change and that extra work can't be separated"* rather than any
claim that the fix is worth at least the measured delta.

## Cost, automation, and configuration (Decisions 4 & 5)

N runs cost runner-minutes **and** wall-clock. On the primary rerun path the runs
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
- **Env-configurable, kept out of user-facing SKILL.md (Decision 5)**, matching
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

All six resolved; these are the contract the implementation follows.

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
5. **Env-configurable, out of user-facing SKILL.md** — `CI_SPEEDUP_VERIFY_RUNS`
   (default 2, `0`=off) and `CI_SPEEDUP_VERIFY_VARIANCE_PCT` (default 20), per the
   existing knob convention.
6. **Wall-clock only in v1; runner-minute (billing) before/after is a documented
   fast-follow.** Merge-wait is the payoff the user feels and the skill headlines.
   A billing delta would compare the report's *modeled* before against a *measured*
   after — the modeled-vs-measured mismatch this codebase is disciplined about
   avoiding — so it ships as its own clean increment, not bolted onto v1.

## Implementation sketch (once approved — NOT in this PR)

- SKILL.md: add phase 7 (post-fix verification) + its close-rendering rules.
- A helper (or extend `collect_runs.py`) to: trigger the samples (serial reruns,
  or parallel `workflow_dispatch` where available), poll to completion, enumerate
  the run's **attempts** (new per-attempt fetch — the run-list sampler sees only
  one row), median the gating check's wall-clock, run the two-part CI-only-scope
  guard (no product code + same work ran), tag each run cold/warm by position, and
  emit the disclosed before/after block (all six disclaimers, real wall-clock +
  billing cost).
- Tests: the median/range math over all runs, the variance-adaptive-N logic, the
  cold(1)+warm(N−1) composition at the default N = 2, the CI-only-scope guard
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
