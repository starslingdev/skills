---
pattern: OPT80
impact: MEDIUM
class: data-driven
detector: actions-checkout-tail-stall
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml'
fix_strategy: checkout-stalls-on-the-tail
title_template: 'Checkout Stalls on the Tail'
category: 5
---

# OPT80 — Checkout Stalls on the Tail

**TL;DR**: The checkout step is a few seconds on a typical run and minutes on a
handful, and the slow runs' own logs show the git fetch standing still. That is
a stalled transfer, not a large one — and a low-speed abort plus a retry caps it.

**Anti-pattern**: A job's `actions/checkout` has a heavy right tail. Most runs
fetch in seconds; every so often one sits at the same `Receiving objects: N%`
for a minute or more and then completes. Nothing in the repository changed
between those runs — the transfer stalled, and `git` has no timeout configured,
so it waits. The cost is paid by whoever happens to catch it, and it is
invisible in a p50: the median run looks fine, and only the average moves.

```
before:  git fetch  ->  [stall, no timeout]  ->  ...  ->  done   (120s)
after:   git fetch  ->  [stall]  -> abort at 30s -> retry -> done ( ~40s)
```

**Why this is not OPT49 revived**: [OPT49](#opt49--slow-setup-step) was cut for
reading a cause out of a duration — "checkout took 61s, therefore the
dependencies are uncached". OPT80 never infers. It proves the stall from the
tail runs' own logs, and the proof is narrow: the percentage transferred must be
the SAME on both sides of the pause. A heavy tail whose logs show a smooth fetch
is **withheld** — and so is one whose fetch was merely slow, or that paused
before any byte moved — because a long fetch with no stop in it is a large
repository (that is [OPT28](#opt28--full-git-history-checkout)'s lever and
[OPT76](#opt76--submodule--git-lfs-checkout-payload)'s), not a stalled one.

**Detection heuristic** (every gate required; all fail closed):

1. The step is `uses: actions/checkout@*`, or a **local composite action** whose
   body checks out (this mirrors OPT76's transitive local-action indexing — it is
   re-implemented against the workflow-YAML vintage the tail was measured on, not
   imported. A local action that is unreadable, or that transitively invokes
   itself, fails closed and the job is skipped). A hand-rolled
   `git clone` in a `run:` block is **not** matched — its fix is flags on the
   user's own command. A job declaring more than one checkout step is skipped:
   which one the timing belongs to is ambiguous.
   The step is located in the **YAML**, never by its timing. GitHub stamps step
   timestamps at one-second granularity, so a warm checkout measures 0s; a
   timing-based search would drop that run from the sample and inflate the p50.
   A step `name:` carrying a `${{ }}` expression is rendered before the jobs API
   reports it, so for those the step is matched on its `uses:` identity instead —
   otherwise a repo that templates its step names is silently unreachable. When
   the identity still matches no observed step, that is counted as
   `checkout_step_identity_never_matched_in_steps` /
   `checkout_step_measured_on_no_sampled_run`, never as "the job ran too rarely".
   A checkout that was skipped, or whose start is a placeholder (1970 or
   earlier) or falls outside its job, did not run in that occurrence and is
   counted as `checkout_step_skipped_or_out_of_window`; one whose timestamps do
   not parse or run backwards (end before start) is counted as
   `checkout_step_duration_unparseable`.
2. At least **6** sampled occurrences of the job, all on **one** known
   per-minute-billed runner label. A "tail" that is really some runs on a
   different runner class is a runner comparison, not a stall.
3. The checkout step's duration has a real tail:
   `p95 ≥ max(3 × p50, p50 + 30s)`, with at least **2** runs at or above that
   threshold. Both parts of the bound are named constants: the multiple keeps a
   uniformly slow checkout out, the absolute floor keeps sub-minute jitter out.
4. No retry or abort is configured already. Four doors, read with two different
   scopes:

   - **anywhere in the job** (the checkout inherits these wherever they are set):
     `GIT_HTTP_LOW_SPEED_LIMIT` / `GIT_HTTP_LOW_SPEED_TIME` in the workflow-,
     job- or step-level `env:` or in a local composite's body; and
     **`git config http.lowSpeedLimit` / `http.lowSpeedTime`** — git's own
     documented equivalent — in a `run:` step or a composite's body. Step order
     is deliberately not checked: the setting is usually made in a setup step.
   - **on the checkout step only**: a retry-wrapper action used *as* the checkout
     step, or the body of the local composite that *is* the checkout step. A
     retry wrapping `npm test` three steps below a bare `actions/checkout@v4`
     retries the tests, not the fetch, and must not read as the fix.

   A `git config` that `--unset`s, `--get`s or `--list`s the setting, and a
   mention inside an `echo`, a `grep` or a `#` comment, never read as applied —
   a repo that just removed the abort is the repo this pattern exists for. If a
   door is open the fix is already applied and the finding is **withheld**. A
   retry configuration that cannot be read is withheld too.
5. For at least **2** of the tail runs, the captured checkout log shows the
   transfer **standing still**: a gap of **≥ 20s** between two consecutive
   `Receiving objects: N%` lines reporting the **same N**. A stall is progress
   that STOPPED, not progress that had not started, so none of these qualifies:

   - the quiet between `Fetching the repository` and the first `remote:` line
     (DNS, auth, negotiation);
   - the quiet across `remote: Enumerating / Counting / Compressing objects`
     (the server building the pack, during which the client legitimately
     receives nothing);
   - two `Receiving objects` lines whose percentage **advanced** (a transfer
     that is slow, not one that stopped);
   - a pair at **100%** (`tail_pause_was_after_the_transfer_completed`) — the
     transfer is already over, and the gap is the runner writing the pack to
     disk. A low-speed network timeout cannot act on it, and the evidence would
     have said the fetch was "stuck at 100%".

   The first three are a large repository — OPT28's lever, not this one — and each
   would be aborted by the low-speed timeout this pattern recommends, so
   reporting them would hand the reader a fix that reds their CI. They are
   withheld under `tail_pause_was_advancing_or_pre_transfer`, so a big-repo tail
   is visible as that rather than as "the logs showed a smooth fetch". The
   non-transfer lines stay in the vocabulary only as window anchors; they never
   bracket a proven pause.

   The two lines bracketing the longest qualifying gap are quoted **verbatim**,
   and only lines inside the checkout step's own time window are read, so a
   later `git submodule` or `git lfs` step cannot supply the proof. Logs are
   fetched for **tail runs only**, newest-first, bounded by a named probe cap
   (4) — never for the whole sample, and never at all for a job with no tail. A
   tail run whose job has no log to serve (in flight, skipped) or whose step
   window is unreadable is dropped **before** the fetch, so the bounded budget is
   never spent on a run the result would be refused for.

   The log is split on newlines only; within one record git's progress animation
   is separated by **carriage returns**, and every fragment is read with the
   record's timestamp. Reading only the first fragment threw the intermediate
   percentages away, which is how a transfer that kept moving could look as
   though it stood still. A log where more than a small named share of records
   carry no parseable timestamp is refused (`log_lines_without_timestamps`)
   rather than proved from what survived.

   Every way a probe can fail to prove a stall is counted **separately**, and
   every distinct reason across the probed runs is counted — not just the most
   common one — because only some of them are evidence about the repository:

   | gate | what it means |
   |---|---|
   | `tail_without_log_gap` | we looked, and the fetch was smooth. The only one that is evidence about the repo |
   | `tail_pause_was_advancing_or_pre_transfer` | a big-repo pause: advancing, or before any byte moved |
   | `tail_pause_was_after_the_transfer_completed` | a pause at 100% — local pack writing, not the network |
   | `log_carries_no_progress_vocabulary` | `show-progress: false`; the checkout printed nothing |
   | `progress_lines_all_outside_step_window` | the log has progress, none of it inside the step's own window (a clock/attempt mismatch) |
   | `log_carries_no_parseable_timestamps` / `log_lines_without_timestamps` | the capture's gaps are not derivable |
   | `tail_run_log_unavailable` | the log was fetched and was not there |
   | `tail_run_has_no_log_to_fetch` | the run is in flight or skipped; no fetch was attempted |
   | `tail_run_step_window_unreadable` | the step timestamps do not parse, so no log could be clamped |
   | `quoted_progress_line_is_credential_shaped` | a stall was found and its proof dropped unquoted |
   | `no_tail_run_log_was_probed` | the fallback when no run was reached at all |

   A count in `opt80_withheld_by_gate` means one thing only: **a finding was
   suppressed**. A poisoned line dropped from a finding that still fired is
   recorded in a separate `opt80_notes` tally instead.

Every one of those exits increments a stamped per-gate counter
(`opt80_withheld_by_gate` on the findings document) and logs at DEBUG, so a
detector that has quietly stopped firing is distinguishable from a repository
with nothing to report.

**Checkouts it could not decide are named in the report.** A job whose checkout
measured a tail is a candidate. Its exit is a *verdict* when the tail runs' logs
were read and show a smooth fetch, a pause that was advancing or came before the
transfer, or one after the transfer completed; when the retry or abort is
already configured; or when the measured excess or credited minutes are nothing.
It is a *could not tell* when there were too few tail runs, the retry
configuration could not be read, or the tail runs whose logs were unavailable,
silent, undecidable or never fetched (only the newest four are) could still have
supplied the missing proof. Each could-not-tell
checkout is listed on the findings document (`opt80_withheld_candidates`, named
by its commonest such reason), and the report's Data sources table carries a
`checkout stall: held back` row — "N candidate checkout(s) held back (build):
too few slow checkouts in the sampled runs to tell a stall from a one-off." The
jobs are named (workflow-qualified when two workflows share a name, at most five,
then "and K more"), the reason is a plain-English phrase for the commonest gate,
and `verify_report.py` re-derives the whole line, so an undecided tail never
reads as "measured, nothing found".

**Log text is untrusted third-party data.** Only lines from the closed progress
vocabulary above are ever read, the two quoted lines are quoted and never acted
on, and a quoted line carrying a credential shape drops that run's proof rather
than being masked — a masked git progress line is no longer evidence of
anything. If that leaves fewer than two clean proofs the finding is withheld
outright. Either way the poisoned line is never quoted.

**Sizing (measured — the tail-excess model)**:

```
tail_excess_s = mean(checkout_s) - p50(checkout_s)        over the sample
runner_min    = tail_excess_s x effective_monthly_volume / 60
wall_clock_p50_s = 0
```

Never the full p95, never the whole step. `effective_monthly_volume` is the
workflow's monthly volume scaled by how often *this job* actually ran in the
sample, so a job behind an `if:` gate is not credited at the workflow's
frequency. The evidence renders p50 / p95 / max so the reader sees the spread.

`wall_clock_p50_s` is **0 by construction**, and this is deliberate: the median
run has no stall, so capping the tail cannot move the p50 merge gate. What does
improve is the tail runs' own wall-clock, bounded above by the longest observed
pause — stamped as `tail_run_longest_pause_s` and named in the rendered evidence,
along with whether the job sits on the critical path — but **not credited**,
because a mean-minus-median quantity is not a p50 saving and must not be rendered
as one. It is an upper bound, not a forecast: the recommended abort fires at 30
seconds and the retry re-fetches, so the realised gain on a stalled run is
smaller than the pause it replaces.

**The tail line (slowest job of a pull-request workflow only).** When the
stalling job is its workflow's slowest job and the workflow runs on pull
requests (and was timed on sampled pull-request runs, not on push runs standing
in for them), and the job is one the merge actually waits on (a required check,
or a job a required job `needs:`; with no readable required-check set the
merge wait is not claimed), a stalled run's checkout sits on that run's merge wait. The report
then states the tail on its own line, next to that job's merge-wait figure in
its long-pole section (or in a short block of its own when the job is not one
of the drilled poles):

```
one run in N spends about X s longer on checkout, and that run's log shows the fetch stalling
N = round(sampled runs / counted slow runs)
counted slow runs = slow runs - logs read and shown clean
X = median checkout of the log-proven tail runs - p50 checkout of the sample
```

A slow run is one at or above the tail threshold. Logs are read for at most
four of them (a cost bound). A slow run whose log was read and shows a clean
fetch is left out of N; a slow run whose log was never read, or could not be
read (gone, unparseable, or its stall dropped as credential-shaped), is not
evidence of a smooth fetch and stays in. The report says how many runs were
slow, how many of their logs were read, and how many of those prove the stall,
were unreadable and were clean.
X uses only the proven runs. X is the median proven run's loss, not an upper
bound (a proven run can lose more), so it reads "about" and applies to roughly
one run in N: it is
not a typical-run number, so it is never added to the headline, a pole's
wall-clock figure, any total or the runner-minute section, and
`wall_clock_p50_s` stays 0. A slowest job with no tail line says why, and
claims no merge wait: the workflow is not shown to run on pull requests
(push-only or unknown trigger), it was timed on all events with no pull-request
sample, branch protection could not be read, or the merge does not wait on that
job. A job that is not the slowest carries no merge-wait sentence at all. The report's self-check
re-derives N and X from the stamped per-run checkout durations and the quoted
progress lines, and no longer wrongly fails a slowest job whose check is named
`<workflow> / <job>`.

**Fix recipe**, in this order, with the caveat that **retry and abort cap the
damage; they do not fix the network**:

1. Set `GIT_HTTP_LOW_SPEED_LIMIT: 1000` and `GIT_HTTP_LOW_SPEED_TIME: 30` in the
   checkout step's `env:` **and** wrap the checkout in a retry (step 2) in the
   same change. Git then aborts a transfer that stays under 1 KB/s for 30
   seconds instead of waiting indefinitely. The abort on its own is not a
   half-measure but a regression: the runs this pattern measured stalled and
   then *succeeded*, and aborting without a retry turns them into failures.
2. Wrap the checkout in a retry with backoff (a retry action, or a local
   composite). The abort from (1) is what makes the retry fast — **never add a
   retry without it**, or the retry inherits the same hang and doubles the worst
   case.
3. Where the job allows it, a shallow / blobless / sparse checkout shrinks what
   can stall at all. This pairs with [OPT28](#opt28--full-git-history-checkout),
   which already knows which jobs read history — check it before narrowing the
   checkout, because a job that needs history fails only later, inside the step
   that reads it.
4. A persistent git mirror on the runner removes the fetch entirely. That is a
   **runner-side capability your runner provider may or may not offer** — it is
   not something this change installs, and this skill does not recommend a
   vendor for it.

Expect the tail to shorten, not to disappear, and re-measure the same step after
the change.

**Risk**: **MEDIUM**. No check is renamed, so branch protection is untouched.
But the abort in step 1 does change how a *succeeding* transfer is handled: the
runs this pattern measures stalled and then completed, so an abort shipped
without the retry converts them into red runs. Shipped together, steps 1 and 2
turn a long stall into a short retry; shipped apart, step 1 alone is a
regression. Step 3 is the one that can break a job outright (a narrowed checkout
the job silently depended on), which is why it is third and gated on OPT28.

**Guardrail**: never present this as a fix for a checkout that is merely large;
never add a retry without the abort, and never add the abort without the retry;
never narrow the checkout without
confirming the job reads no history and no excluded path; never describe the
runner-side mirror as part of the change.

**Published example**: Linear's CI writeup
(<https://linear.app/now/ci-bottleneck-reworked>) replaced `actions/checkout`
with a composite that retries with backoff and sets the same two low-speed
variables so a stalled fetch aborts after about 30 seconds. That is cited as a
real-world instance of the shape; it is **not** this skill's sizing, and no
number from it is ever credited to a user's repository.

**Tier-2 render note**: OPT80 can promote only with measured tail evidence and a
neutrality certificate whose `proof` token is `checkout_tail_excess` — its own
token, because the credited quantity is one step's tail excess rather than a job
duration, so neither the cluster-floor comparison nor the post-completion
argument describes it. The meaning is restated wherever it is read: the
detector, `verify_report.py`'s neutrality arm, `blocking_path.py`'s certificate
summary and this note.

The finding must stamp `wall_clock_p50_s=0`, `sizing_basis=measured`, the
tail-excess model in `measured_signal`, and a structured `checkout_stall` block
that lets `verify_report.py` re-derive the whole claim. The block must carry all
of these keys. The ones marked **re-derived** are recomputed by the neutrality
arm and fail the report when they disagree; the rest are stamped so the claim can
be audited by hand, and the arm reads its own copies of the constants rather than
the stamped ones, so a drifted constant fails the engine/verifier coupling test
instead of being silently trusted:

| key | what it carries |
|---|---|
| `kind` | **re-derived** — `opt80_checkout_tail_stall`; a block without it is not this claim |
| `job` | **re-derived** — the credited job; must equal `affected_jobs` |
| `checkout_step` / `checkout_step_identity` / `checkout_step_source` | which step was measured, and whether it is `actions/checkout` directly or a local composite that wraps one |
| `runner_label` | the one billed label every credited occurrence ran on |
| `per_run_checkout_s` | each sampled occurrence's job id, run URL and checkout seconds — the inputs p50 / p95 / mean / max are recomputed from |
| `p50_s` / `p95_s` / `mean_s` / `max_s` | **re-derived** — the distribution, recomputed from `per_run_checkout_s` |
| `tail_threshold_s` | **re-derived** — the threshold the tail test produced |
| `tail_p95_multiple` / `tail_p95_abs_s` / `min_tail_runs` / `min_gap_s` / `min_proven_tail_runs` / `min_sampled_occurrences` / `log_probe_max` | **re-derived** — the bounds the detector used. The arm re-derives on its OWN `_VR_OPT80_*` copies (a bound read out of the artifact under audit proves nothing) and then compares each stamped value to its copy, failing the claim on any disagreement; an engine/verifier coupling test pins the copies to the engine's constants |
| `tail_run_job_ids` | **re-derived** — which runs were tail runs, against the threshold, bounded by the sampled occurrences |
| `proven_tail_runs` | **re-derived** — per proven run the two quoted lines, their log timestamps and the derived gap. The gap is recomputed from the timestamps, and so is the PREDICATE: the arm re-runs its own copy of the `Receiving objects: N%` regex over both quoted lines and fails the claim unless both match, report the same N, and that N is below 100 — otherwise a stamped `5% → 60%` pair would render as proof of a stall. Both lines are re-scanned for credential shapes here too |
| `logs_fetched` | **re-derived** — against the verifier's PINNED probe cap (never the stamped `log_probe_max`, which is only compared to it), and against the number of tail runs |
| `log_probed_job_ids` | **re-derived** (tail axis) — must be distinct slow runs, as many as `logs_fetched`; every proven run must be one of them, and the slow runs outside it are the unread ones the tail axis keeps in N |
| `tail_excess_s` / `runner_min_saving` | **re-derived** — the credited quantity and the minutes it becomes |
| `tail_run_longest_pause_s` | **re-derived** — must equal the largest gap across the proven runs, because the evidence renders it as the upper bound on what capping the stall recovers |
| `on_critical_path` | **checked one way, against the report's own rendered Long-pole sections** — since this is the exact fact the pole-rule exemption below turns the blanket check off for. A `True` with no rendered pole for the job fails; a `False` on a rendered pole is accepted (the flag marks the workflow's slowest job, and a drilled pole is often another job, such as a chain pole). Three exemptions from the `True` rule: the slowest job's tail line renders in the dedicated Checkout stall tails section and `per_workflow_timing[wf].long_pole_job` names that job; the workflow records no pull-request event; or the workflow has no rendered pole header at all |
| `monthly_volume` / `sampled_successful_run_count` | bounds-checked, not re-derivable — they come from the collection, not from anything in the block. `monthly_volume` must be positive and the sampled count must be at least the occurrences |
| `effective_monthly_volume` / `occurrences` | **re-derived** — the scaling; `occurrences` can never exceed the sampled run count, and the effective volume must be the monthly volume scaled by `occurrences / sampled` |

**The slowest job is not excluded.** Every other Tier-2 proof argues its credited
work is off the merge gate by showing the job is not the long pole; the report
verifier enforces that as a blanket rule. `checkout_tail_excess` is exempt from
it, because it carries the thing that rule stands in for: the credited quantity
is `mean - p50` of one step, and a quantity defined as the distance of the mean
above the median cannot by construction move the median — which the verifier
re-derives from the stamped per-run inputs rather than infers. So a stalling
checkout on the workflow's slowest job **is** reported, and the finding says in
plain words that the effect on the merge wait is measured and not credited in
this version. The `wall_clock_p50_s == 0` check still applies to it unchanged.

It never claims a speedup on the typical run. It credits the tail excess in full,
which assumes the stall is fully capped — the residual the abort and re-fetch
leave behind is not netted out, so treat the credited minutes as the optimistic
end of the range.

**Worked shape**: a `smoke` job checks out in eight seconds on ten of twelve
sampled runs and in two minutes on the other two. Both slow runs' logs sit at
`Receiving objects: 17%` for eighty-five seconds before finishing. The credited
saving is the average's inflation — mean minus p50 — times how often the job
runs, and nothing at all is credited to the ten runs that were already fine.
