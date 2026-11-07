---
pattern: OPT79
impact: MEDIUM
class: data-driven
detector: actions-job-net-negative-cache
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml'
fix_strategy: cache-costs-more-than-it-saves
title_template: 'A Cache That Costs More Than It Saves'
category: 1
---

# OPT79 — A Cache That Costs More Than It Saves

**Anti-pattern**: A job restores a dependency cache and then installs
dependencies, and restoring the cache takes longer than the install it was meant
to shorten. The archive is large, it comes off a network store, it has to be
decompressed onto the runner's disk, and the install it replaces would have
resolved most of its work from a warm local store anyway. Every other caching
entry in this catalog says *add a cache*; this is the one that says the cache you
already have is costing you time, and it only ever says so from measurement.

```
before (cache hit):   restore 28s  →  install 3s   →  post-save 0s   = 31s
before (cache miss):  restore  1s  →  install 7s   →  post-save 4s   = 12s
after  (no cache):                    install 7s                     =  7s
```

*(Illustrative shape only. The rendered finding always quotes this repo's own
measured numbers.)* The one public write-up this pattern was drawn from reports a
`node_modules` cache hit restoring in about 28 seconds against about 7.5 seconds
for a filtered install, and the cache being removed as a result —
<https://linear.app/now/ci-bottleneck-reworked>. That is cited as somebody else's
result, never as this skill's sizing: no OPT79 finding ever renders a number that
did not come from the audited repository's own runs.

**Detection heuristic**: measured, and every gate fails closed. The constants
named here are the detector's own, not restatements of them — with one
inheritance called out where it appears: the 0.25 hit-share floor in step 6 is
the shared cache tail floor (`_CACHE_TAIL_MIN_FRAC`), which the whole cache
family reads, so retuning it there moves this gate too.

1. From the **workflow file** (never from the observed step list), the job must
   declare exactly **one** cache-restore step — `actions/cache`,
   `actions/cache/restore`, or an `owner/setup-*` action whose cache input
   (`cache:`, or `enable-cache` / `bundler-cache` where that is the switch) is
   set to anything other than empty / `false` / `no` / `off`; `cache: ${{ … }}` is an
   unevaluated expression whose value the static parse cannot know and withholds
   rather than reading as a yes — and **that cache step must be saved by its own
   post phase**. A separate `actions/cache/save` step withholds
   (`cache_is_saved_by_a_separate_step`): the save runs on the miss path but is
   not the restore's post phase, so the three-step block cannot measure it.
   Caches that are **on by default** count toward "exactly one": `setup-go` v4+
   caches unless it says `cache: false`, and `astral-sh/setup-uv` v5+ defaults
   `enable-cache` to `auto` (on GitHub-hosted runners, off on self-hosted ones;
   OPT79 counts it as on everywhere, which can only withhold), so a job pairing
   either with an `actions/cache` step is a multi-cache job and withholds. So do
   actions that restore a cache of their own (`Swatinem/rust-cache`,
   `gradle/actions/setup-gradle`, `gradle/gradle-build-action`,
   `bahmutov/npm-install`, sccache and ccache actions): beside a cache OPT79 prices they make two caches, and on
   their own they withhold as `cache_action_is_not_one_this_pattern_measures`.
   A ref that is not a version tag (a commit SHA, a branch) names no major
   version, even when the SHA starts with digits.

   `actions/setup-node` **v5+ with no `cache:` input** (or an empty one, which
   setup-node reads the same way) caches automatically when
   the repository's `package.json` names the package manager, so OPT79 reads that
   file the way setup-node does and counts the cache **only when it is really
   on**. setup-node reads `$GITHUB_WORKSPACE/package.json` (the repository root,
   whatever `working-directory` says) and switches the automatic cache off when
   `package-manager-cache` is anything but `true` (default `true`). The field
   rule depends on the major version: **v5** reads the top-level
   `packageManager` and auto-caches `npm@…`, `yarn@…` and `pnpm@…` (a bare `npm`
   with no `@` does not match); **v6 and later** read
   `devEngines.packageManager` (an object, or an array of objects, by `name`)
   and then `packageManager`, and auto-cache **npm only** (`npm`, `npm@…`,
   `^npm@…`). `cache-dependency-path` changes which lockfile is hashed, not
   whether the cache is on. The outcomes:
   - **on** — it is a real cache. Beside an `actions/cache` step the job is a
     two-cache job and withholds; on its own it is the cache OPT79 prices, read
     from setup-node's own log lines, and the fix names
     `package-manager-cache: false` because there is no `cache:` input to
     remove.
   - **off** — it is not a cache, so a job running setup-node v5 next to its own
     `actions/cache` step is measured like any other.
   - **unknown** — `package.json` is missing, a 404, a failed fetch, invalid
     JSON or not a JSON object; the job has no checkout before the setup step,
     or checks out into a `path:` or another `repository:`, so the root file is
     not the one OPT79 read; or the ref is a SHA or branch on which the v5 and
     v6 rules disagree. These fail closed as
     `setup_action_cache_default_depends_on_repository_files`, counted in the
     per-gate tally.

   The file is read **once per repository**, and only when the cache count of a
   job in a sampled workflow depends on it: from the local checkout when `--root` has it, else one
   `contents/package.json` call against the default branch — the same sources,
   in the same order, as the workflow YAML (`data_sources.setup_node_package_json`
   records which). Like the YAML, it is the audited commit's copy, not each
   run's own. The cache is followed by an **install**
   step, and **no unrecognised `run:` step may sit between the cache and that
   install** (`first_step_after_cache_is_not_a_recognised_install` withholds
   otherwise — the unrecognised step, `cd web && npm ci` say, may be the real
   install, and pricing a later one pairs the cache with the wrong step). The
   cache must name a **known package store** — a `setup-*` action's ecosystem, or
   an `actions/cache` path naming exactly one store
   (`cache_path_names_no_known_package_store` withholds otherwise: a browser or
   build-output cache pays off in a later step outside the measured block, so
   its cost would read as pure waste) — and the install must belong to the
   **same ecosystem** (`install_package_manager_does_not_match_cache` withholds a
   `node_modules` cache followed by `pip install`, for example). An install step is recognised by **what it runs**, not by what it was
   called, so `name: Install dependencies` over `run: npm ci` is found like any
   other — and a step *named* `npm ci` that *runs* `npm run build` is not one. A
   multi-line `run:` block must be installs **all the way down**: `pip install
   -e .` followed by `pytest -q` withholds, because pricing it would charge the
   whole test suite to both sides of the comparison. The install verbs are the
   install alternatives of the shared setup
   classifier and nothing else: `npm|pnpm|yarn|bun ci|install|i`,
   `pip|pip3|pipenv|poetry|uv install|sync`, `uv pip install`,
   `python -m pip install`, `bundle install`, `composer install`,
   `mix deps.get`, `go mod download`, `cargo fetch`, `mvn dependency:`. Checkout,
   configure and the cache step itself are setup but are not installs, and
   pairing a cache with one of them would price an unrelated block. Two cache
   steps in the job withholds (counting the on-by-default caches above): the
   log's hit line could belong to either.
2. The job must resolve to exactly **one** job in the workflow YAML by name (an
   interpolated matrix leg resolves to none; a name carried by more than one job
   in a single run is not one job), and to one known per-minute-billed runner
   label.
3. Across the sampled runs, each occurrence's log is classified **HIT** or
   **MISS** by the **verbatim cache line** — the matchers the rest of the cache
   family reads, plus the `setup-*` family's own miss wording (those actions
   print `<package manager> cache is not found`, which the cache action's
   phrasing does not match) and `astral-sh/setup-uv`'s pair (`uv cache restored
   from GitHub Actions cache with key` / `No GitHub Actions cache found for
   key`) — never by a duration.

   That scan is **scoped to the restore step's own log group**: it starts at
   the restore step's `##[group]Run <step>` header and ends **only** at the next
   `##[group]Run ` or `##[group]Post ` header. The action's own inner groups and
   every `##[endgroup]` are skipped, because `actions/cache` prints its hit/miss
   line after closing its group but still inside its own step. It has to be: Turborepo
   prints `cache miss, executing <task>`, Gradle prints `Build cache miss for
   task …`, and both land in the *test* step. Read unscoped, every genuine cache
   hit in a JavaScript monorepo looked like a two-cache job. Each row stamps the
   group its line came from, and a log with no such group withholds the
   occurrence rather than guessing.

   Inside that group, a log showing **both** a miss and a hit line is
   **excluded and counted** (`run_log_shows_both_a_hit_and_a_miss_line`), never
   guessed, in either order. A `restore-keys` fallback prints no miss line: the
   `@actions/cache` toolkit prints `Cache hit for restore-key: <key>` and the
   action then `Cache restored from key: <key>`, so it is read as a **partial
   hit** (`run_log_shows_a_partial_restore_keys_hit`) on that toolkit line, when
   the restored key differs from the primary `key:` the step echoed, or when the
   post step saved a new cache (`Cache saved with key` / `Cache saved with the
   key`), which an exact hit never does. A partial hit is not a clean hit — its
   restore size and time are not the exact-key restore this comparison prices —
   so it is excluded from both paths rather than counted as a hit, and still
   counts in the hit share's denominator. When excluded runs leave too few hits
   or misses, the job withholds as `population_truncated_by_excluded_runs`, not
   as a thin sample, or as `population_truncated_by_unmeasurable_step_times`
   when runs set aside because a block step's time did not measure outnumber
   the rest. The runs set aside are stamped per gate (`excluded_runs_by_gate`).

   Only **successful** job runs are classified: a failed or cancelled run's
   step timings are truncated and its post save does not run, so it is withheld
   as `occurrence_did_not_succeed`. An occurrence GitHub skipped never ran and
   is counted as `occurrence_was_skipped`.

   An occurrence whose log was never fetched is **counted as unread**, not folded
   into the populations; an occurrence past the per-job log cap of 8 is tallied
   separately as `beyond_the_per_job_log_probe_cap`, not as unread. When unread
   occurrences leave either population short, the withhold says so rather than
   reporting a thin sample. At least **3 hits
   and 3 misses** are required; a comparison with one side unmeasured is the
   shape-assumption the evidence guards forbid.
4. Every credited occurrence must have run on the **same runner label**. A hit on
   a slow runner against a miss on a fast one is not a cache comparison.
5. Both paths measure the **same three steps** — restore + install + the post
   save — identified in step 1 and summed per run. A step the run **rendered**
   stamped with no measurable length (start and end in the same second), or a
   skipped step with parseable timestamps, counts as **0s**: GitHub stamps step
   timestamps at one-second granularity and a zero-length span is dropped from
   the timed set, so reading the step set from what happened to be timed would
   let that noise change *which* steps are being compared.

   A step that was **never rendered at all** is a different fact, and it fails
   open on the term that matters most: on `actions/cache` the save runs on a
   MISS, so silently zeroing it removes the biggest miss-side term and
   manufactures the excess. A post step that started and never completed
   withholds the occurrence; a block step whose timestamps are missing, do not
   parse or run backwards (end before start) did not measure 0s, it did not
   measure, and withholds the occurrence as
   `step_timestamps_unparseable_in_this_occurrence`; a block step that GitHub
   did **not** report as skipped but whose timestamps parse and run forward to
   no in-window time (a `started_at` in 1970 or earlier, the year-1 placeholder
   included, or a start more than 1s before its own job or after it ended) did
   not measure either, and withholds the occurrence as
   `step_has_no_in_window_time`; a post label
   that matched **no** occurrence
   withholds the job; and `actions/cache/restore`, which has no post phase at
   all, records that there is no save rather than inventing a step name. That
   is only true when no *separate* `actions/cache/save` step exists in the job;
   one that does withholds in step 1, because its save cannot be measured here.

   The block's p50 over the hit runs must exceed its p50 over the
   miss runs by at least **max(5s, 20% of the miss path)** — a named floor, so a
   one-second "loss" never renders as a finding.
6. The **hit share** — exact hits over every run read and kept, the excluded
   partial-restore and two-verdict runs included — must be at least **0.25**. A
   cache that almost never hits has a key-entropy problem, which OPT6 and OPT8
   own; route there rather than report the same cache twice.
7. How the measured excess is **credited** depends on where the job's p50
   sits: strictly shorter than the workflow's long pole is a runner-minute
   finding (against the cluster floor when it is below it, else against the long
   pole), the workflow's long pole on a pull-request workflow is a wall-clock
   finding, and what is left is reported uncredited. (Counted by
   `cache_net_negative.kind`: three tags, four emitting arms — the two
   runner-minute certificates, the pole finding, and the uncredited row with its
   sub-reasons.) This is a crediting gate,
   not a candidate gate: a job at or above the floor is still measured by steps
   1-6.
   See *Three outcomes* below.

Job logs are the expensive call in this engine, so the probe is capped three
times: at most **8** sampled occurrences of one job, at most **2** candidate jobs
per workflow, and at most **24** log fetches across the whole repository — the
first two are per workflow, and without the third a monorepo with thirty workflow
files would multiply them into hundreds of calls. Candidates are ranked by
measured job p50, which is a **proxy** for what a cache can cost and not a
measurement of it: the longest-running cached jobs are probed first, and what the
cache actually costs is only known once its logs are read. The repo-wide ceiling
is applied to the whole plan **after** that ranking, so the budget reaches the
costliest candidates in the repository rather than whichever workflow file was
walked first, and every occurrence it cuts is counted. The report's Data sources
row states both what was planned and what was read. This probe runs during
collection and does **not** need `--with-logs`.

**Held-back candidates are disclosed.** A candidate that a gate held back does
not vanish, whether that happened after its logs were probed or before any log was
read (two caches, an unreadable `package.json`, a cache that does not serve the
install, a first step that is not a recognised install, a separate save step): each
one is recorded as `{workflow_file, job, gate}` in `opt79_withheld_candidates`, and
the report's Data sources table carries a **`cache hit/miss verdicts`** row — "N
candidate cache(s) held back (<job>, <job>, ...): <plain-English reason for the
most common gate>." (ties go to the alphabetically first gate; at most five jobs
then "and K more", workflow-qualified where two workflows share a job name) —
which `verify_report.py` re-derives from that list.
So a repository with a withheld cache reads differently from one with no cache
at all. A cache measured healthy, or one hitting too rarely to judge (step 6), is
a verdict, not a withhold, and is not listed. The full per-gate tally, including
every shape gate that cost no log fetch, stays in `opt79_withheld_by_gate`.

Every gate about a job's SHAPE — no cache, two caches, a separate save step, no
recognised install right after the cache, a package-manager mismatch — is
answered from data already in hand, so those jobs cost no log fetch. That is
not the same as "no log is fetched for a job that could not produce a finding":
the workflow's slowest job is probed too, and what it produces is a credited
wall-clock finding (on a workflow that runs on pull requests) or the uncredited
line below; every job at least 1s shorter than it (a job within 1s of it counts
as tied and is uncredited) is a credited runner-minute finding, given a measured
net-negative cache and a known monthly volume, except a job at or above the floor
that is `needs:`-chained to the long pole of a pull-request workflow, which is
uncredited too.

**Sizing (measured)**:

```
waste_s     = p50(cache block | HIT runs) − p50(cache block | MISS runs)
hit_share   = hits / (hits + misses + ambiguous)           [every run read and kept]
runner_min  = waste_s × hit_share × effective_monthly / 60
```

`effective_monthly` is the workflow's 30-day volume for the sampled event scope,
scaled by how often this job actually ran in the sample, so a conditional job is
not billed at the whole workflow's frequency. `sizing_basis = "measured"`. A
runner-minute job (below the floor or below the long pole) on a workflow with no
measured 30-day volume cannot be credited and is withheld as
`no_monthly_volume`, after it is measured (the
uncredited row and the wall-clock pole finding below need no volume and may stamp
it, and the effective volume, as null; the pole finding's `runner_min_saving`
is null too).

The credited figure is a **lower bound** on what removing the cache would save:
the miss path it is measured against still pays the restore step and the post
save today, and both disappear with the cache.

**Three outcomes, by which job it is.** The floor is the *second-ranked* job's
p50 in the workflow and the long pole is the *first*, so a job's place relative
to them decides what the audit may claim. Neither gates which jobs are measured;
they gate how the measured excess is credited.

1. **Strictly shorter than the long pole: runner-minutes only**, on any
   workflow (the bill does not depend on pull requests). `wall_clock_p50_s` is
   0, the conservative lower bound: shrinking a job that is shorter
   than the slowest one cannot make the workflow take longer (the slowest job is
   longer than it); a saving there may still
   shorten the gate through a `needs:` chain, which is not claimed. The finding carries one of two neutrality certificates:
   - **strictly below the cluster floor** → `below_cluster_floor`, margin
     `floor_p50 − job_p50`;
   - **at or above the floor, strictly below the long pole** →
     `below_long_pole`, margin `long_pole_p50 − job_p50` (each p50 rounded to
     0.1s first; within 1s of the slowest job counts as tied, the same cutoff
     the slowest-job arm uses, and a tied job is not credited; a job under the
     floor whose floor margin rounds to 0.0 lands here too), with a
     `ref` naming the long-pole job and both p50s. The comparison is against the
     actual slowest job's p50, not the second-slowest job's, against which such
     a job's margin is not positive. The finding says so in plain words:
     "runner-minutes only; this job (Ns) is shorter than the workflow's
     slowest job (`<pole>`, Ms), so making it faster cannot make the workflow
     take longer". It claims no merge gate: the workflow may gate no pull
     request, and on a pull-request workflow a job chained to the slowest one is not
     credited at all. (Owner
     decision 2026-10-06; the same move OPT77's whole-workflow arm makes against
     its slowest member.)

   This is the credited Tier-2 finding described above.
2. **The workflow's long pole, on a workflow that runs on pull requests: a
   credited wall-clock finding** (`kind: opt79_pole_net_negative_cache`). Shrinking
   the slowest job shortens the merge wait, so the finding is sized in
   wall-clock time, not runner-minutes. Its raw wall-clock is the measured
   `waste_s`, first capped by the existing within-workflow bound,
   `min(waste_s, long_pole_p50 − floor_p50)` (credit the pole only as far as the
   next-tallest job's duration; the cap compares durations, so it is
   conservative when that job runs alongside the pole or directly after it,
   and it does not model a longer `needs:` chain of shorter jobs, which a
   chain-aware cap in a follow-up would), and stamped
   under `cache_net_negative.pole_sizing` as `waste_s`, `long_pole_job`, `long_pole_p50_s`, `next_tallest_job`,
   `floor_p50_s`, `headroom_s`, `raw_wall_clock_s` and `capped_by_next_tallest_job`.
   It then goes through the same cross-cutting
   cascade every wall-clock finding gets (the developer-facing gate, the measured
   critical-path floor and the cross-workflow floor), and any further shrink is
   recorded in `wall_clock_derivation`. A long pole TIED with the next-tallest
   job (under 1s of headroom) is not credited here: shrinking it moves no merge
   wait, because the gap to the next job is under a second, so it falls through
   to outcome 3 (so does a job TIED with the long pole, which is a co-pole).
   There is one tie constant, 1.0s, for both arms: the pole arm declines
   headroom under 1.0s, and a job counts as tied with the long pole when its
   lead over it is under 1.0s (each p50 rounded to 0.1s first).
   The finding renders inside that job's long-pole section of the report, opened
   by an `<!-- opt79-pole:<id> -->` marker, as
   `💾 Measured cache cost - OPT79 · <title> (<id>) - up to **Ns** off the merge wait on the P% of sampled runs where the cache hit (H of R runs read)`
   (the figure is the extra time PER CACHE HIT, so the measured hit rate, the
   stamped `hit_share`, is stated beside it, in the evidence sentence too),
   with `risk: LOW`, the guardrail below (re-key or
   narrow before removing; valid for this runner class only; never narrow what
   the install installs) and a rollout line. It needs that placement because its
   capped figure is often below the 30s long-pole floor, which would otherwise
   drop it from the "Also noticed" appendix as a valueless pole-job row; when its
   figure clears the 30s long-pole floor it also counts as a catalog match for
   that pole, so the pole gets no gap-fill, and (unless a leaf, structural or
   data-driven match fired first) the pole's waterfall and agent prompt name
   OPT79 and point at its block. Under 30s the block still renders and the
   prompt only adds "ALSO MEASURED ON THIS JOB". When the
   job is not a drilled long pole (for example, the slowest job of another
   pull-request workflow), the same marked block renders in a short section
   before "Also noticed", never as an appendix row. When a cross-check bound
   lowered the figure, the block prints each step and its reason. Every number
   is from the audited
   repository's own p50s and runs. Runner-minutes are deliberately not stated: the
   runner-minute section requires proof that the job is shorter than the
   workflow's slowest job, which the slowest job cannot have. The neutrality certificate is only required to promote
   a finding into the runner-minute section; a finding that saves merge-wait time
   does not need one, because the wall-clock bound cascade already handles
   "shorten only as far as the next job".
3. **What is left: measured, reported, not priced.** The audit carries no
   sizing for these, so they are an uncredited row, rendered as one numberless
   line beside the dropped-unprovable note (the nearest precedent: a measured
   fact deliberately kept out of the numbers and shown anyway). Five ways in,
   and the row's `uncredited_reason` names all but the first:
   - the long pole of a workflow that runs on no pull request (or a job tied
     with it, under 1s) — runner-minutes only, unpriced in this version; that
     workflow's shorter jobs still take outcome 1. When the workflow declares a
     pull-request trigger but none of its sampled runs was a pull request
     (`declares_pull_request` true), the line says "none of its N sampled runs
     was a pull request", never that the workflow does not run on pull
     requests;
   - a long pole of a pull-request workflow tied with the next-tallest job
     (under 1s of headroom, so no merge wait of its own; a job tied with the
     long pole is stamped the same way), `pole_tied_with_next_job`;
   - a pole the cross-checks zeroed, to a figure that rounds to 0s
     (`pole_merge_wait_zeroed_by_cross_check`, with the cascade's steps kept
     as `uncredited_derivation` so the self-check re-derives the zeroing);
   - a pole off the merge-gating spine (`pole_workflow_off_merge_gating_spine`,
     which the self-check cross-checks against the dropped checks); these last
     two are built as pole findings and demoted to uncredited rows in
     `collect()`;
   - a job of a pull-request workflow at or above the floor that is
     `needs:`-chained to the long pole
     (`job_in_a_needs_chain_with_the_long_pole`): the chain's sum is the merge
     wait, so shrinking it may shorten that wait, which this version does not
     size. On a push-only workflow such a job is credited by outcome 1.

   The tied, zeroed and off-spine poles read "`X` is this workflow's slowest
   job, but ..." (a co-pole reads "`X` is tied with `Y`, this workflow's
   slowest job"); the chain job reads "`X` is not this workflow's slowest job".

> a cache on `nightly` in `.github/workflows/nightly.yml` measured net-negative
> by 19s per cache hit (4 hit / 4 miss run(s) sampled); `nightly` runs in a
> workflow that does not run on pull requests, so no pull request waits on it;
> the saving is runner-minutes only and is **not credited** in this version.

A workflow that cannot gate a PR is never told it has a merge wait. The report's
self-check fails an uncredited row with no stated reason whose job is at least
1s shorter than its long pole: that job is outcome 1's `below_long_pole` finding,
and listing it here would under-report a priced saving.

An uncredited row carries no runner-minutes, no wall-clock claim, no certificate,
no Tier-2 row, and no contribution to any total. It carries the same stamped block
as a credited finding, including its per-run measurements, so the report's
self-check re-derives it exactly as it re-derives the credited ones. The fix is
the same one the credited findings hand over; only the size of the win is
unstated.

**Fix**: in this order, and never "just delete it".

1. **Re-key or narrow, then re-measure.** A restore is usually slow because the
   archive is big. Cache the package manager's **store** (`~/.pnpm-store`,
   `~/.npm`, `~/.cache/uv`, the Go or Cargo module cache) instead of an expanded
   `node_modules` tree for a whole workspace, or scope the cache — and the
   install — to the package this job actually needs (pairs with OPT54's filtered
   install and OPT8's key granularity). Re-run the audit; the comparison above is
   the acceptance test.
2. **Remove the cache step and its post save** — only once a narrowed install is
   already faster than any restore. State plainly what this does: the miss path
   becomes the **only** path, so the miss-path numbers in the evidence are what
   every run will pay from then on. That is the trade the measurement says is
   worth making, and it is worth re-measuring after any change to the dependency
   graph.

**Runner-class caveat**: the comparison is valid for the runner class it was
measured on, and the finding names that label. A runner with slower disk or
faster network can flip the result, so do not carry the conclusion to another job
or another runner without re-measuring there.

**No-weakening caveat**: the saving must never be bought by narrowing what the
install installs, by dropping the step the cache feeds, or by skipping the
install on some runs. Those reduce what CI verifies; this pattern is about paying
less for the same work.

**ci-score interaction**: ci-score's *Dependency caching* check reads
configuration only and has no run history, so a repo that measured its cache,
found it net-negative and removed it will still be docked that point — the
measurement lives here, in the ci-speedup report, and reconciling the two is an
open owner decision rather than an engine behaviour either skill implements
today.

**Tier-2 render note**: the runner-minute finding promotes only with measured
evidence and a neutrality certificate whose `proof` token is
`below_cluster_floor` or `below_long_pole` — each **literal** for this pattern:
the credited job's own p50 is below the workflow's cluster floor (margin: that
difference), or below the workflow's long pole (margin: the long pole's p50
minus the job's, re-derived from `per_workflow_timing`; the self-check fails a
job that is or ties the long pole (within 1s of the slowest job counts as tied,
the same cutoff the slowest-job arm uses), and any other pattern claiming the
token).
A job in a `needs:` chain with the long pole (either direction, any depth) of
a pull-request workflow is never `below_long_pole`: the merge wait is then the
chain's sum, so shrinking it may shorten the wait. It is listed uncredited
(`job_in_a_needs_chain_with_the_long_pole`), and the self-check's "also rendered
as a Long pole" proxy applies to this token like any other. That finding must stamp
`wall_clock_p50_s=0`, `sizing_basis=measured`, the two-path model in
`measured_signal`, and a structured `cache_net_negative` block that lets
`verify_report.py` re-derive the credited minutes and the margin without reading
one number as an answer. The long-pole wall-clock finding is not a Tier-2 row: it
has no `tier2_neutrality`, a null `runner_min_saving`, `risk: LOW`, and its
raw wall-clock is the measured excess capped by `pole_sizing`, then shrunk
further by the generic cascade if it must be. Every key below is hard-required
by that re-derivation:

| key | what it carries |
|---|---|
| `kind` | `opt79_net_negative_cache` for a credited runner-minute finding, `opt79_pole_net_negative_cache` for a credited wall-clock finding on the workflow's long pole, `opt79_uncredited_pole_cache` for an uncredited row (a long pole, or a job tied with one, that no other outcome credits — including a slowest job demoted for being tied, zeroed by the cross-checks or off the merge-gating spine — or a pull-request-workflow job `needs:`-chained to the long pole) — the tag that routes the block to this re-derivation instead of the generic one |
| `job` | the credited job; must be the finding's only `affected_jobs` entry |
| `runner_label` / `cache_ref` | the one runner class every credited run ran on, and the cache action the block was built around |
| `restore_step` / `install_step` / `post_step` | the three steps, as named in the YAML, that both paths measure; `post_step` is null for `actions/cache/restore`, which has no post phase |
| `per_run[]` | one row per credited run: its `status`, the **verbatim** `log_line` that verdict came from, the `log_line_group` it was read in (which must be the restore step), its `runner_label`, and `restore_s` / `install_s` / `post_s` / `block_s` |
| `hits` / `misses` / `classified_runs` / `ambiguous_runs` / `occurrences_on_other_runner` | the populations, the two-verdict and partial-restore runs excluded from them (still counted in the hit share), and the occurrences dropped for running on another runner label |
| `hit_path_p50_s` / `miss_path_p50_s` / `waste_s` / `waste_floor_s` | the two medians, their difference, and the floor it had to clear |
| `hit_share` | `hits / (classified_runs + ambiguous_runs)` |
| `job_runs` / `sampled_successful_run_count` / `monthly_volume` / `effective_monthly_volume` | the scaling; `classified_runs` can never exceed `job_runs`, which can never exceed the sampled run count; the two volumes may be null on the pole finding and the uncredited row |
| `runner_min_saving` | restated inside the block and checked against the finding's own; **null** on an uncredited row and on the wall-clock pole finding (neither carries minutes), where a number would be a failure |

An uncredited row carries every key above plus `workflow_file`, `job_p50_s`,
`floor_p50_s`, `long_pole_job`, `long_pole_p50_s`,
`workflow_gates_pull_requests`, `on_critical_path`, which is always `false`
there, `uncredited_reason` and, for a zeroed pole, `uncredited_reason_detail`.
The wall-clock finding for the long pole
(`opt79_pole_net_negative_cache`) carries the same block and placement keys,
with `on_critical_path` and `workflow_gates_pull_requests` both `true`, plus
`cache_net_negative.pole_sizing` (`waste_s`, `long_pole_job`, `long_pole_p50_s`, `next_tallest_job`,
`floor_p50_s`, `headroom_s`, `raw_wall_clock_s` and `capped_by_next_tallest_job`),
and its `runner_min_saving` is null.

The re-derivation recomputes each row's `block_s` from its three parts, re-reads
every row's quoted line against the hit and miss matchers (a row labelled `hit`
whose line says the cache was not found is a failure, and so is a row quoting
both), recomputes both medians, the waste, the floor, the hit share, the
effective volume and the credited minutes, and re-derives the margin from
`per_workflow_timing`. Uncredited rows go through the same re-derivation with the
credited-minutes and neutrality branches skipped, and one extra check: they must
carry no sizing at all, and every row must carry `on_critical_path` stamped
`false` (a row missing the key, or stamped `true`, fails; the long pole is
credited as a wall-clock finding instead). The wall-clock finding
(`opt79_pole_net_negative_cache`) is re-derived the same way, plus one arm: the
headroom is re-derived as `long_pole_p50_s − floor_p50_s` from the stamped block
(cross-checked against `per_workflow_timing`; headroom under 1s fails),
`round(min(waste_s, headroom), 1)` must agree with every `pole_sizing` key and
equal `wall_clock_uncapped_p50_s` (or `wall_clock_p50_s` when the cascade did
not shrink it), every `wall_clock_derivation` step must go down, give a reason
and end on the credited figure, the evidence sentence is restated from the
block, and the rendered block must appear exactly once with its id, its title
and `<N>s off the merge wait` followed by the hit-rate clause whose percentage
and counts equal the stamped `hit_share`, `hits` and runs read. A finding without `tier2_neutrality` that is not
this kind, or one carrying a non-null `runner_min_saving`, fails. A tampered number anywhere in that chain reddens the
report.

The verifier restates the engine in two different ways, and the tests say which
is which: the stamped key list, the population minimums, the two waste-floor
constants and the shared cache tail fraction are asserted **identical** to the
engine's; the hit and miss matchers are deliberately **independent** re-readings
— a verifier sharing the engine's matcher could not catch a mislabelled row — so
they are only spot-checked, line by line.

**Whole-job validation:** compare the whole job as well as the measured restore/install/post-save block: a cache can shorten one step while lengthening the job; this is manual validation guidance, not a change to OPT79's block-based detector.
