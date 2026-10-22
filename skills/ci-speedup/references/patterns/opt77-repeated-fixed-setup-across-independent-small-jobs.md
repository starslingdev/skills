---
pattern: OPT77
impact: MEDIUM
class: data-driven
detector: actions-job-setup-consolidation
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml'
fix_strategy: repeated-fixed-setup-across-independent-small-jobs
title_template: 'Repeated Fixed Setup Across Independent Small Jobs'
category: 2
---

# OPT77 — Repeated Fixed Setup Across Independent Small Jobs

**Anti-pattern**: A workflow declares several small, independent checks — lint,
typecheck, a licence audit, a formatting check, a spellcheck — and every one of
them starts a runner, checks out the repository and installs dependencies before
doing work that takes no longer than the setup did. The same fixed setup prefix is
paid N times for one commit. The jobs are not tiny in billed terms (each is
comfortably over a minute once checkout and install are paid), so nothing about
per-job billing round-up describes this; what is wasted is repeated setup
*runtime*. (The gate is "setup is at least as large as the useful work", not
"the work takes seconds" — the finding's evidence quotes the measured maximum.)

Consolidating N such jobs into one, and running their tasks concurrently inside
it, pays the setup once instead of N times.

```
before:  N jobs  →  N x (start runner + checkout + install)  +  N x seconds of work
after:   1 job   →  1 x (start runner + checkout + install)  +  the same work, concurrent
saved:   (N - 1) x setup, every run
```

**Detection heuristic**:

1. For every sampled job, split its measured step timeline (jobs API `steps[]`
   timestamps) into the **leading run of setup steps** — the implicit `Set up
   job`, `actions/checkout`, `setup-*` actions, dependency installs — and the
   remaining **useful work**. Take the p50 of each across the sampled runs, and
   record the prefix's **signature**: the ordered identities of those setup
   steps. An identity is the step's name, whitespace-collapsed and casefolded,
   with the `Run ` prefix GitHub renders in front of an unnamed step dropped
   (that last one is load-bearing: it is what makes `Run npm ci` and a step
   *named* `npm ci` the same identity), and two kinds of churn removed — an
   **action's** version ref (`actions/setup-node@v4` and `@v3` are one identity)
   and **bare boolean** flag tokens (`npm ci` and `npm ci --prefer-offline` are
   one identity).

   Everything else is kept, so a different toolchain (`pip install` vs `npm ci`),
   a different thing installed (`npm ci frontend/package.json`) and an extra step
   all remain different setup. In particular an install *target* is not churn:
   `--filter=api` / `--filter api`, `-w frontend` and `--target build` all name
   what is being installed and keep their groups apart, and an `@` that is not an
   `owner/repo` action reference (a pinned image digest) is left intact.
   Comparing the names exactly would be correct but unusable: one pinned-version
   bump anywhere in the group would silence the finding, and a lever that never
   fires on a real repo cannot be told apart from a broken one.

   The signature's **shape** comes from every step the job declares; only its
   duration comes from the steps that measured above zero. GitHub stamps step
   timestamps at one-second granularity, so a sub-second step is 0s in one run and
   1s in the next on identical YAML; reading the shape from the timed steps alone
   made that noise change the signature's length and silently drop the job.
2. Keep a job as a candidate only if it resolves to exactly **one** job in the
   workflow YAML by name (an interpolated matrix leg resolves to none and is
   excluded; a name carried by more than one job in a single run is not one job
   and is excluded too, which covers a matrix declaring a static `name:`), runs
   on a single known
   per-minute-billed runner label, shows the **same setup signature in every
   sampled occurrence**, and its setup p50 is **at least as large as its
   useful-work p50**.
3. Group the candidates by resolved runner label **and by setup signature** —
   never by runner label alone. The saving model removes `(N-1)` payments of
   *one* prefix and projects the consolidated job at `max(setup_p50)` rather than
   the sum of the setups, and both steps are only valid for jobs re-paying the
   **same setup work**. Three checks that each spend 80s installing *different*
   toolchains (a Node one, a Python one, a Go one) share a runner and a duration
   but not a prefix: consolidating them removes only the one shared checkout,
   while every distinct install still has to run. Grouping on the signature keeps
   them in separate groups, each sized on its own. Emit only when:

   * the group has at least **three** jobs;
   * **no `needs:` edge**, direct or transitive, links any two of them — a chain
     is not a consolidatable set;
   * **no job outside the group** waits on a member, directly or transitively.
     The projected consolidated job is always at least as tall as the tallest
     member, so anything downstream of a member starts later and the change is
     wall-clock *negative* — which a bill-only finding must never be. (A `needs:`
     parent the group *shares*, such as `lint`/`typecheck`/`audit` all waiting on
     one `build`, is fine and is the commonest real layout for this shape.) A
     dangling `needs:` naming a job the workflow does not declare withholds too:
     dropping an unresolvable parent would strengthen the independence claim
     rather than question it;
   * the shared prefix contains at least one **recognizable** piece of shared
     work — a real `owner/repo` action or a dependency install. A prefix of
     purely human-authored names is a naming coincidence, and `Set up job` alone
     is only the runner booting;
   * the credited setup prefix clears an absolute floor (10s), below which the
     removed payments are noise against the cost of coupling the checks;
   * the **workflow file** agrees, for the members it can be read for. Two jobs
     can render the same step name while doing different work: a different
     `working-directory`, a different `run:` body, or the same unnamed action
     with different `with:` inputs (`python-version: 3.9` vs `3.12`).
     Consolidating those removes neither install, so any such disagreement splits
     the group.
4. Project the consolidated job at `max(setup_p50) + max(useful_work_p50)` (the
   collapsed tasks run concurrently inside it) and compare it against **the
   tallest job that REMAINS after the consolidation** — the tallest job in the
   workflow that is not a member of the credited group. If the projection reaches
   that job, **withhold the finding**; if nothing outside the group is taller,
   withhold it too. Consolidation must never lengthen the merge gate.

   **When the group is every job the workflow DECLARES** there is no remaining
   job to compare against — but the group still sets a gate today, because its
   members run in parallel: `max_i(job_p50_i)`, **the slowest member's measured
   duration**. That is literally what the workflow makes a pull request wait
   for today. Fire when `max(setup) + max(useful)` is at or below it (the merge
   wait does not lengthen, so the runner-minute saving is free); hold the group
   back when the projection is above it, and say so
   (`collapsing_the_whole_workflow_would_lengthen_the_merge_gate`), with the
   two durations recorded on the held-back entry.

   **When this fires in practice — rarely, and that is honest.** A member's
   useful work is its measured duration minus its setup, read off one span, so
   `setup_i + useful_i` is exactly `job_p50_i` and the collapsed job's
   `max(setup) + max(useful)` is never *below* the slowest member's measured
   duration. Collapsing a whole workflow can therefore never shorten the merge
   wait. The saving is credited only when the two are **equal** — one member
   holding both the longest setup and the longest task, so the wait is
   unchanged — and otherwise the group is held back with what the collapse
   would cost. On real repositories the hold-back is the common outcome and the
   credit is the exception; disclosing the hold-back is the point of this
   branch, not the credit. The equality is only as stable as a one-second
   median, and a member's p50 moving by a second flips a credit into a
   hold-back — the safe direction, since the flip can never invent a credit.
   The gate itself was previously re-assembled from each member's step
   components, capped by the measurement, which described three members each
   measuring 400s as taking 90s today.

   "Whole workflow" is read off the workflow's **declared** jobs, never off the
   sampled runs: a declared job that simply never ran in the sampled window — a
   main-gated `release`, a job behind an `if:` — is still a job outside the
   group, and reading the sample instead classified such workflows as having
   nothing outside the group and dropped them silently. It also requires one
   declared job key per member, so several observed jobs resolving to the same
   declared job cannot satisfy it.

   That job has to be a job that actually runs. Only jobs present in a **majority
   of the sampled runs in which the whole group ran** are eligible to carry the
   proof — a conditional job seen once in ten samples cannot show the gate is
   unchanged, because on the other nine the group's own members are the tallest
   thing left. The eligible set, and every job left out of it with its reason, are
   stamped so the check can re-derive the same maximum.

   The comparison is deliberately *not* against the workflow's cluster floor. The
   question the gate answers is "does the merge gate get longer if these jobs are
   collapsed?", and the gate afterwards is set by the jobs that were not touched.
   A floor the group's own members help define measures the fix against something
   the fix removes — which is a modelling error, not conservatism, and it silenced
   the pattern on its own motivating shape (one long test job beside a flat row of
   equally-sized small checks, where the second-tallest job is itself a member).
5. Credit `occurrences x (N - 1) x setup_p50` runner-seconds across the sample,
   where `setup_p50` is the **smallest** measured setup p50 in the group and
   `occurrences` is the number of sampled runs in which **every** credited job
   produced a clean measurement (it ran, its duration and its setup prefix both
   measured) — a run where one of them was skipped paid no duplicate setup for
   it and so has none to remove. Scale that to a month by the monthly volume for
   the sampled event scope divided by sampled successful runs. This credits
   removed setup runtime only — no wall-clock speedup is ever claimed.

**Groups it could not decide are named in the report.** Once a candidate group
has formed (at least three jobs on one runner sharing one setup prefix), every
exit is either a *verdict* — the members depend on each other, the prefix is not
shared work, the YAML steps differ, the setup is too small, consolidating
would reach the tallest remaining job, or the credited saving rounds to zero —
or a *could not tell*: the workflow file could not be read (`no_yaml_jobs`), the
`needs:` graph
is undecidable, the group never ran complete in one sampled run, jobs outside
it exist but none runs often enough to measure against, every job the workflow
file declares is in the group but the sampled runs also carried one it does not
declare, or the group is every job the workflow declares and collapsing it
would lengthen the merge gate.
Each could-not-tell group
is listed on the findings document (`opt77_withheld_candidates`), and the
report's Data sources table carries a `repeated-setup: held back` row — "N
candidate job group(s) held back (lint + test + typecheck in ci.yml): the
sampled runs never had every job in the group run together, so the saving could
not be measured." The groups are named (workflow-qualified, at most five, then
"and K more"), the reason is a plain-English phrase for the commonest gate, and
`verify_report.py` re-derives the whole line, so an undecided group never reads
as "measured, nothing found". Verdicts are not counted there.

**What counts as the setup prefix.** Only the *leading* run of setup-classified
steps, and only steps the classifier recognises. In full, a step name opening
with (optionally after the `Run ` prefix GitHub renders for an unnamed step):

* `set up` / `setup`, `checkout`, `install`, `cache`, `restore`, `fetch`,
  `configure`, `init `, `bootstrap`;
* `docker login`, `docker pull`, `docker compose up`;
* `actions/checkout`, `actions/setup-*`, `actions/cache`, `pnpm/action-setup`,
  and the bare `setup-node` / `setup-python` / `setup-go` / `setup-pnpm` forms;
* the unnamed install commands themselves — `npm|pnpm|yarn|bun ci|install|i`,
  `pip|pip3|pipenv|poetry|uv install|sync`, `uv pip install`,
  `python -m pip install`, `bundle install`, `composer install`, `mix deps.get`,
  `go mod download`, `cargo fetch`, `mvn dependency:`.

Each install alternative pins the verb as the tool's *own subcommand*, so
`mvn install` (which compiles and tests) and `bundle exec rspec spec/get_spec.rb`
are builds and tests, not setup. A setup step the classifier does not recognise
ends the prefix, so its time lands in "useful work" and the group is sized
conservatively or withheld — never inflated.

**Relationship to OPT65 (supersede, not disjoint).** These two were described as
disjoint because a matrix leg never resolves to a single YAML job by name. That
reasoning does not hold: OPT65 never required a *declared* matrix — it groups on a
trailing parenthetical in the **observed** job name, so three ordinary jobs named
`lint (eslint)`, `lint (biome)`, `lint (stylelint)` resolve to one YAML job each
*and* form an OPT65 base. Both then describe the same consolidation. **OPT77
supersedes OPT65 on any job set both claim**: one edit renders as one lever.

The accepted cost: OPT65's billing round-up minutes are genuine, and they go
unreported whenever it is superseded, so the report slightly **understates** that
group's total. They are deliberately not folded into OPT77's credited number —
OPT77 credits raw removed compute, and mixing a billable-round-up quantity into it
would break its measured basis. Under-reporting a real saving is the safe
direction; inventing a basis is not. Because that cost is real, every superseded
finding is disclosed in the results (`superseded_findings`) with what displaced
it and which of its jobs the surviving finding does *not* cover.

**Fix**: Merge the group into one job that checks out and installs once, then
runs the collapsed tasks **concurrently** inside that job (background processes
joined at the end, or a task runner's own parallel mode). The concurrency is not
a nicety: run sequentially, the consolidated job costs `setup + the SUM of the
tasks` instead of `setup + the slowest task`, which can push it past the merge
gate and make the change wall-clock-negative. Do not consolidate any job that can
sit on the merge gate, and do not drop or narrow any check in the process — the
consolidated job must still run everything the separate jobs ran, and still fail
the build when any of them fails. When the jobs must stay separate (independent
re-runs, distinct required checks), the alternative is to make each copy of the
setup cheap instead of merging: bake the recurring `apt` / toolchain installs into
a base image, as [OPT73](#opt73--shared-sub-step-across-critical-path-jobs-cluster-floor-lever)
describes.

**Prefer GitHub's native `parallel:` steps over shell `&`.** Since 2026-06-25 a
step may be a `- parallel:` group: a list of ordinary `run:`/`uses:` steps that
run at the same time, with an implicit wait at the end of the group
([workflow syntax](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#jobsjob_idstepsparallel),
[changelog](https://github.blog/changelog/2026-06-25-actions-steps-can-now-be-run-in-parallel/)).
For the collapsed tasks this beats shell backgrounding:

```yaml
steps:
  - uses: actions/checkout@v4
  - run: npm ci
  - parallel:
      - name: Lint
        run: npm run lint
      - name: Typecheck
        run: npm run typecheck
      - name: Licence audit
        run: npm run audit:licences
```

- **Separate logs.** Each task keeps its own step and log, where shell `&`
  interleaves them — which also recovers part of the failure-isolation cost below.
- **A failing task fails the job automatically.** A failed background step fails
  the job at the next `wait` / `wait-all` that includes it (the end of a
  `parallel:` group is one), unless it sets `continue-on-error`. That keeps the
  no-weakening rule for free; with shell `&` the job must collect and check every
  task's exit code itself, or a red task silently reads as green.
- **Runner requirement.** GitHub-hosted runners update themselves. Self-hosted
  runners: the runner release notes list the background-step engine in
  `actions/runner` **2.335.0** and a fix that stops a cancelled background step
  from affecting the job result in **2.336.0**, so run **2.336.0** or later.
  GitHub publishes no minimum runner version for the feature itself. The syntax is documented for
  github.com and GitHub Enterprise Cloud; GitHub Enterprise Server's docs do not
  carry it, so do not use it there.
- **Limits.** At most 10 background steps run at once in a job, and
  `parallel:` / `background:` cannot be used inside a composite action.
- **Keep a verifying check waited on.** Run a check that must gate the job in a
  `parallel:` group (or cover it with `wait-all:`); never as a bare
  `background:` step that later steps do not wait on: the job still fails at
  the implicit wait before cleanup, but a deploy or publish after it can run
  before the check finishes. And never with `continue-on-error`.

**What the audit does and does not see in a group (known limits).** The static
detectors read a group's children in declaration order; the order-aware ones
(OPT2, OPT5, OPT12, OPT79) use the group and background tags. OPT77's setup
fingerprint adds up setup seconds that may overlap inside a group, so it can
overstate. Run timing (the step-sum p50 and the step timeline) is unchanged and
still lays steps end to end; fixing that needs a live probe of how GitHub
records overlapping steps. Until that probe, OPT79 holds back a cache whose
restore or install runs in a group or in the background, OPT24 and the
long-pole lever hold back a saving priced from a step that runs side by side
(`dominant_step_runs_inside_a_parallel_group`), and the pole drill says when a
pole job's step times overlap.

**Failure-isolation cost (a real cost, not a footnote)**: N separate checks give
N independently-red checks and N independently re-runnable units. One
consolidated job gives one red check and re-runs everything, and a reviewer
reading the checks list loses the at-a-glance "which one broke". Surface each
task's own result inside the job (per-task step, or an explicit summary) so the
diagnosis is not lost, and weigh the lost re-run granularity against the saved
minutes before consolidating.

**Required-checks caveat**: consolidating jobs renames the checks (the old job
names disappear). If any of the consolidated jobs was a required status check,
add the new consolidated job's check name to branch protection as a required
check (or the ruleset equivalent), or the consolidated work silently stops gating
merges until that admin-only step is done. If the consolidation routes the old
checks' work behind a `needs:` edge and an aggregator, see OPT75's
[dependency-failure skip caveat](#opt75--long-pole-optimize-or-relocate-the-dominant-step)
— a dependent skipped by a failed dependency reports skipped, not failed, so the
aggregator must run with `always()` (or `!cancelled()`) and propagate every
`needs.<job>.result`, and keeping the required check name on that aggregator
re-gates the merge without an admin.

**Tier-2 render note**: OPT77 can promote only with measured setup evidence and a
neutrality certificate whose `proof` token is `below_cluster_floor`. **That token
is historical** for this pattern: the comparison is against the tallest job that
*remains* after the consolidation, not against the workflow's cluster floor. The
token is shared with OPT65 (whose cluster-floor comparison *is* genuine) and kept
as the dispatch key; the meaning is restated wherever it is read — the detector,
`verify_report.py`'s neutrality arm, `blocking_path.py`'s certificate summary and
this note.

The held-back row names every candidate but gives only the **commonest**
gate's reason, so it is hedged: "most commonly," whenever a second gate
contributed, and "equally often," when the top two gates held back the same
number (on an exact split there is no commonest reason, and "most commonly"
would be a false claim about the tie itself). Printed flat, the sentence
asserts one reason of every candidate it names. A hedged row reads:

> 3 candidate job group(s) held back (lint + test + typecheck in ci.yml, build
> + docs + e2e in ci.yml, x + y + z in release.yml): most commonly, the
> sampled runs never had every job in the group run together, so the saving
> could not be measured.

Only a list where one gate accounts for every candidate is unhedged. The count
is the number of distinct candidates named, the same number the "and K more"
overflow is taken from.

The finding must stamp `wall_clock_p50_s=0`, `sizing_basis=measured`, the
`(N-1) x setup_p50` model in `measured_signal`, and a structured
`setup_consolidation` block that lets `verify_report.py` re-derive both the
credited minutes and the margin. Every one of these keys is hard-required by that
re-derivation:

| key | what it carries |
|---|---|
| `credited_jobs` | the jobs the saving is claimed for; must equal `affected_jobs` |
| `removed_setup_payments` | `N - 1` |
| `setup_p50_s` | the smallest per-job setup p50 in the group |
| `shared_setup_steps` | the prefix identities the group was formed on |
| `per_job[job].setup_p50_s` / `.useful_work_p50_s` / `.setup_steps` | each job's own measured split and its own prefix, which is what proves the grouping |
| `projected_consolidated_p50_s` | `max(setup) + max(useful)` |
| `remaining_tallest_job` / `remaining_tallest_p50_s` | the job the projection is measured against; both `null` when the group is every declared job, because there is none |
| `group_is_the_whole_workflow` | true when the group is every job the workflow declares, which is what selects the gate below |
| `workflow_declared_job_keys` | the workflow's declared job keys, checked against the per-workflow record the YAML reader stamps. Every one must be a credited member **when `group_is_the_whole_workflow` is true** — that is the claim the whole-workflow gate rests on; on the ordinary path the key is informational, because a sampled job the file never declares can legitimately be the tallest remaining job |
| `gate_today_p50_s` | the gate the group sets today, `max_i(job_p50_i)` — the slowest member's measured duration; `null` unless the group is the whole workflow, and refused if stamped otherwise |
| `credited_job_keys` | the credited members' YAML job keys, which is what `workflow_declared_job_keys` is compared against |
| `remaining_eligible_jobs` / `remaining_excluded_jobs` | the set that job was chosen from, and every job left out with its reason |
| `occurrences`, `sampled_successful_run_count`, `monthly_volume`, `scale` | the scaling; `occurrences` can never exceed the sampled run count |

It never claims a speedup; it credits only removed setup runtime.

**Worked shape**: seven independent checks each start a runner, check out the
repository and install dependencies before doing only seconds of useful work.
Collapsing them into a single job, with the seven tasks running concurrently
inside it, drops the number of times that setup prefix is paid from seven to one
— `(7 - 1) x setup_p50` of removed setup runtime on every run of the workflow.
Split across two consolidated jobs instead (say, because two of the checks need a
different toolchain), the same arithmetic applies per group: each group of `n`
pays its prefix once instead of `n` times.
