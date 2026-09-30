# ci-speedup — Maintainer Loops Runbook

**Maintainers only. Every loop runs locally via Claude Code, never as a public
GitHub Action.**

ci-speedup has **three** maintainer loops. This runbook covers the **two
self-improvement loops** — the ones that edit the skill in place:

- **Gap → catalog loop** (this first half) — turns *missing-detector gaps* (where
  the LLM fallback fired) into deterministic `_parse_log` detectors. Narrow:
  catalog coverage only.
- **Transcript self-improvement loop** (["The transcript self-improvement loop"](#the-transcript-self-improvement-loop-general)
  below) — reads a whole session and turns *operator steering* into durable
  `SKILL.md` / reference-doc / `evals` edits. General: how the skill *works*.

The **third** is the automated **dogfood loop** (`workflows/ci-speedup-dogfood.js`,
driven by its own `workflows/ci-speedup-dogfood.command.md` slash command): it runs
the real skill against each org's top repo, audits each run for skill bugs — both an LLM
audit AND a deterministic **structured-grader seed** pass (`scripts/grader_seeds.py` maps
`verify_report` + the consumer-divergence probe into seed bugs via a committed triage
allowlist; spec `specs/loop-self-improvement-upgrades.md` §2-A) — drafts a fix patch per
distinct skill bug, and integrates all clean patches into **one consolidated PR**. A single integration stage
reconciles duplicate/conflicting fixes (the symptom-signature dedup can't catch two fixes
that are secretly the same edit — only their diffs can), runs the suite after each patch,
drops + surfaces any that won't apply, and commits one-per-fix so the single PR stays
granularly revertable. It's documented in that command doc, not here.

**The fix default is a CLASS fix, not an instance patch.** When a faithfulness bug
surfaces (a wrong sizing/ceiling, a silently-dropped spine check, a mislabeled pole, a
fabricated structural lever), the fix agent's *default* is to add a deterministic
invariant to [`skills/ci-speedup/tests/verify_report.py`](../../skills/ci-speedup/tests/verify_report.py)
that RE-DERIVES the truth from the findings JSON (`pr_critical_path`, esp. the per-PR
`populations`) and asserts the rendered report matches — so the whole class is caught on
*every* future report — then fix the engine to green. The new check is classified in
[`scripts/grader_seeds.py`](scripts/grader_seeds.py) `TRIAGE_ALLOWLIST`, which wires it
straight back into this loop's bug list (self-reinforcing). Only a bug that genuinely
can't be expressed as a re-derivation property falls back to a one-off code patch + a
bespoke repro test. This is the anti-whack-a-mole rule: drain the class, don't patch the
instance. (Mechanics + the signature branch live in `workflows/ci-speedup-dogfood.js`
step 3; the write-surface allowlist permits the `grader_seeds.py` classification edit.) Install or refresh the
local slash command (the `.claude/` copy is gitignored, so it can drift from the committed
canonical body) with the deterministic installer — never hand-copy:

```bash
python3 maintainers/ci-speedup/scripts/install_dogfood_command.py          # (re)install
python3 maintainers/ci-speedup/scripts/install_dogfood_command.py --check  # detect drift (exit 1 if stale)
```

#### The committed reports are DOCUMENTATION, but they may no longer lag the renderer

`skills/ci-speedup/reports/<repo>/blocking-path-speed.md` (maintained in the
pre-public development archive, not shipped here) are illustrative worked
examples — open and read them. They are **not** the test input.
`test_committed_reports.py` runs verify_report's invariants against a **FRESH render**
of each committed `findings.json` (real data + the current renderer), so:

- **A `verify_report` class invariant does NOT redden a stale snapshot.** It runs against a
  freshly-rendered report, which reflects the CURRENT engine — so if the invariant catches a
  bug, the **engine** has it, and fixing the engine fixes the fresh render.

> **CHANGED 2026-07-08 (PR-G2 / OD10). If you touch `skills/ci-speedup/scripts/`, you
> must re-render the committed reports in the same PR.**
>
> ```bash
> # commit your scripts/ change FIRST (the stamp reads the committed tree), then:
> for d in skills/ci-speedup/reports/*/; do
>   python3 skills/ci-speedup/scripts/blocking_path.py \
>     --in "$d/findings.json" --out "$d/blocking-path-speed.md"
> done
> ```
>
> This makes **zero GitHub calls** — it re-renders from the committed `findings.json`.
> Two tests enforce it: the committed bytes must equal a fresh render, and each
> committed report must carry a `scripts tree` provenance token matching HEAD.
> Rendering with uncommitted `scripts/` edits stamps `-dirty` and fails on purpose.
> Why: this repo squash-merges, so the `skill commit` a report records is discarded at
> merge and can never be verified on `main`; the `scripts/` tree survives the squash.
> The earlier note that reports "are allowed to lag the renderer" is withdrawn — it was
> unenforced, and `langfuse` and `mastra` had silently drifted for weeks.
- `test_measured_evidence.py` still gates the committed `findings.json` **data** shape (not
  renderer-stale). `mastra` is excluded from the fresh-render verify for a known pre-existing
  renderer↔verify spine drift (tracked separately); its `findings.json` is still data-tested.

**Refreshing the docs** (optional, cosmetic — never a CI gate): re-render a committed
`findings.json` with `blocking_path.py --in reports/<repo>/findings.json --out <same>.md` when you
want the readable example to match the current renderer. To refresh the underlying data, re-run the
pipeline pinned (`collect_runs.py --created-before <the example's committed data_sources scanned_at>`).

NOTE: `routeCommittedReportFailure` in `workflows/ci-speedup-dogfood.js` remains as a harmless
fallback, but with fresh-render verify it should rarely fire — a committed-report guard failure now
means the engine output is wrong (fix it), not that a snapshot is stale.

> **Coverage seam — apply the L1-L9 checklist by hand here.** A committed-report-regen fix is routed
> to `needs_human` by `routeCommittedReportFailure` *before* the S3 review stage, and the `reviewable`
> filter only sees `patch_ready` fixes — so the **mechanized L1-L9 reviewer never runs on it**. Yet
> these are the fixes *most* likely to carry a freshly-authored invariant (a new invariant reddening
> the stale examples is exactly what triggers regen). So when you do the regen, **hand-apply the
> [L1-L9 checklist](#the-l1-l9-invariant-authoring-checklist-the-review-stages-contract) below to the
> fix's invariant yourself** — the automated panel didn't.

#### The L1-L9 invariant-authoring checklist (the review stage's contract)

The 17-repo corpus does **not** catch the ways a class fix gets *authored wrong* — the "Class A"
sub-bugs were caught only by adversarial / silent-failure **review** *after* the corpus was green,
and the same modes recurred. So the dogfood loop runs an **independent reviewer PANEL per drafted fix** (between
draft and integration — `reviewerPrompt` + the review stage in `workflows/ci-speedup-dogfood.js`): a
2-agent panel of `pr-review-toolkit:silent-failure-hunter` (the silent-drop lessons L2/L8) **∪**
`pr-review-toolkit:code-reviewer` (the re-derivation lessons L1/L3/L4/L5/L6), **OR-combined** — EITHER
reviewer confirming a defect holds the fix (a false negative defeats the stage; a false positive is
merely a `needs_human`). Each reviewer is worktree-isolated + told read-only, and is handed the
original bug's audit evidence (the concrete false result the fix must eliminate). Its explicit
contract is the checklist below. A **confirmed** defect downgrades the fix to `needs_human` and holds
it out of the consolidated PR (surfaced under `review_flagged`); a panel that returns **no** verdict
(all reviewers threw/skipped) is a **coverage gap** (`review_errored`), surfaced — not silently
treated as clean, but not auto-held either (the consolidated PR is human-reviewed before merge). The
same checklist is referenced by the step-3 drafter prompt, so the drafter authors *to* it and the
panel checks *against* it.

| # | Lesson | Class A evidence |
|---|---|---|
| **L1** | Locate the claim in rendered text (it's what's under test), but **source the ground-truth comparison value from `findings.json`**, never from collapsed/truncated rendered text. Strip render artifacts via `_strip_render_artifacts`. | #5 read the `**Where:**` line that collapses a multi-job finding to one segment → false positive (fivetran-airflow). |
| **L2** | Suppress **only the exact contradiction**; preserve anything with real value on any axis. | #5's first cut excluded ANY pole-job finding → dropped credited OPT73/33/45 levers (~17k min/mo), violating `_is_pole_structural` + no-silent-drops. |
| **L3** | Mirror the engine's exact **KEYING** (raw vs scope-normalized) when re-deriving a count/match; add a monorepo/scoped-name discriminator. | #2 used `_cmp_name` where the engine used the raw name → monorepo double-count false positive. |
| **L4** | Mirror the engine's exact **METRIC** on both sides of a comparison (don't compare a global-p50 pole against a gating-median floor). *Distinct from L3.* | #4 stripe-go false negative from mixing global-p50 with gating-median. |
| **L5** | Mirror the engine's **SELECTION** aggregation (e.g. `_eff_floor_s = max(p50, bimodal-high)`) when choosing *which* item is the floor/pole. *Distinct from L3/L4.* | #6 false FAIL until the invariant selected the floor with the bimodal-high effective value. |
| **L6** | **Choose the assertion shape to the data:** EXACT for a deterministic integer (a count); a DIRECTIONAL upper-bound + tolerance when the engine's aggregation can't be cheaply reproduced. Mirror the engine's metric for *selection*; a looser metric for the *bound* is OK to stay false-positive-free. | #2 exact count vs #4 `rendered ≤ pole_p50 − floor + tol`. |
| **L7** | A **text-keyed** invariant must pin its renderer literals (and engine constants/predicates) so a reword breaks a coupling test, not the check silently. | #4/#7; now Stream 1's drift tests + the renderer-literal coupling test. |
| **L8** | Surface **every coverage skip** in `Check.detail` — a SKIP that reads clean is a false negative. | All 5; now Stream 1's `Check.__post_init__` non-empty-detail invariant. |
| **L9** | **Corpus discipline:** "green across all reports" can be faked by OVER-suppression. Hand-check that every remaining RED is a true positive of a *different* class, not silenced by your fix. | #4 lightdash stayed RED (its cause is class #6) — correctly, not suppressed. |

**Governance (why this is encoded directly).** This checklist lives in `MAINTAINERS.md`, which is
**not** in the transcript self-improvement loop's sanctioned `target_file` set (`SKILL.md` /
`evals/evals.json` / `references/*.md` / `ARCHITECTURE.md`; see `loops/loop-summary.schema.json`). So
it is **outside** that loop's ≥2-distinct-session recurrence gate — there is no gate to bypass, and it
is encoded directly here. Each lesson is backed by a committed `verify_report` invariant (stronger
than a transcript anecdote). The ≥2-session gate still governs any future *skill-behavior* rule added
to a gated file; we add none here.

## Gap → catalog loop

ci-speedup is deterministic everywhere except one spot: when a drilled long
pole's job log matches **no catalog detector** (`_parse_log` returns `None`),
the agent running the skill fills the gap with a log-grounded **LLM root-cause
analysis** (SKILL.md phase 4a). That fallback is the safety net — but every time
it fires, it means the deterministic catalog is missing a detector for that
stack. Per `ARCHITECTURE.md` §12.7 the goal is to **extend the catalog first**:
this loop turns the accumulated gap captures into proposed deterministic
detectors, so the same stack drills measured + auditable next time and the LLM
fallback shrinks to genuinely-novel cases.

The captured material can include third-party job logs (which can carry repo
internals or tokens) and derived bill-side workflow evidence, so all of it stays
in the gitignored `.ci-speedup-gaps/` directory and is **never committed**. Only
the human-reviewed detector + test + `_FIX_META` PR the loop proposes reaches
the public repo.

## Contents

**Gap → catalog loop** (missing detectors → catalog entries):
- Why local-only
- Where gap captures come from — and how to get them back
- The UX: it runs itself; you only approve the PR (incl. how it detects a maintainer)
- What the background subagent does
- Before you approve (the review gate + manual batch option)

**Transcript self-improvement loop** (operator steering → SKILL.md / evals edits):
- The transcript self-improvement loop (general)

- NEVER (applies to both)

## Why local-only

Gap captures are gitignored, so CI can't see them and a public workflow would
have nothing to read. Proposing a `_parse_log` detector also needs a maintainer's
judgement about the catalog's shape and the false-positive risk (a "high miss
rate" can be legitimately-changed packages). Maintainers-only is inherent: you
need the local captures plus repo write access.

## Where gap captures come from — and how to get them back

There are now two local capture shapes under `.ci-speedup-gaps/`.

A **log-backed catalog gap** is written automatically by SKILL.md **phase 4b**
whenever the phase-4a LLM fallback fires during a run. Each lives at:

```
.ci-speedup-gaps/<repo-slug>__<job-slug>/
  <job>.log        # the captured job log — the raw evidence (gitignored)
  analysis.json    # the phase-4a gap-fill the agent wrote {cause,breakdown,evidence,prompt}
  meta.json        # {repo, workflow_file, job, dominant_step, skill_commit_sha, scanned_at, run_url}
```

This is the only capture shape `draft_detector.py` promotes today.

A **bill-workflow discovery capture** is written by the `collect_runs.py` CLI
in maintainer source checkouts whenever the final `runner_minute_spine` is
render-ready. It ranks workflows by summed billable-equivalent minutes/month,
skips any workflow already covered by a source-backed Tier-2 finding, and writes:

```
.ci-speedup-gaps/bill-workflows/<repo-slug>__<workflow-slug>/
  bill-gap.json    # schema, repo/commit provenance, workflow totals, top cost-spine job rows
  README.md        # local-only warning
```

These captures do **not** contain raw job logs and do **not** prove a detector.
Use them to choose high-value bill-side drill targets; promotion still needs a
later human-reviewed detector/test PR grounded in real logs or equivalent
deterministic evidence. They are intentionally namespaced under
`bill-workflows/` so the log-gap `draft_detector.py` loop ignores them.

**The loop dir is gitignored and never committed, so git cannot restore it** —
there is no branch, stash, or history copy. If `.ci-speedup-gaps/` is deleted,
it is gone. To repopulate: re-run ci-speedup against the repos that tripped the
fallback (the log gaps re-capture), re-run bill-side audits to re-create
`bill-workflows/`, or copy captures from another maintainer's machine. Treat
staged captures as precious-but-reproducible; do not point bulk `git clean` or
parallel agents at the working tree while they're staged.

> **Note on storage location.** Captures root at the **repo root** of your checkout
> (`<checkout>/.ci-speedup-gaps/`, `<checkout>/.ci-speedup-loop/`) — **not** under
> `skills/ci-speedup/`. The `skills` installer copies the skill dir recursively excluding only
> `{.git, __pycache__, __pypackages__}` (no dotfile exclusion), so a capture dir under the skill
> would ship MBs of third-party job logs to end users; `tests/test_skill_install_surface.py`
> guards against that regrowing. The loop is maintainer-only and runs from the git **source
> checkout**; an **installed** end-user copy does **not** capture at all — `blocking_path.py`
> `_gaps_root_default()` returns `None` off a tracked-source checkout, so there's nothing to
> consume and nothing to ship. The durable copy is the maintainer's source-checkout
> `.ci-speedup-gaps/` at the repo root.

## The UX: it runs itself; you only approve the PR

You do **not** invoke this loop. When you run ci-speedup from inside your
`starslingdev/skills` checkout and a pole trips the LLM fallback, the skill
(SKILL.md phase 4c) detects the maintainer context and **automatically launches a
background subagent** to draft a detector, while it finishes your report. Your
experience is:

1. **You just use the skill.** Run ci-speedup on some repo as normal. It
   produces the report (with the LLM fallback section if a stack was unknown) and
   captures the gap to `.ci-speedup-gaps/`. Nothing for you to do.
2. **A background subagent drafts the fix.** Because you're in the skills repo, the
   render's gap signal points at [`scripts/draft_detector.py`](scripts/draft_detector.py):
   `draft_detector.py prepare` deterministically assembles the drafting task (the
   pending captures + [`loops/gap-to-catalog-prompt.md`](loops/gap-to-catalog-prompt.md)
   + the verify command), handed to a subagent. You keep working; it runs in the
   background. The draft is gated by `draft_detector.py verify <slug>` — the gap
   must now fire a detector whose `fix_key` has a `_FIX_META`, and the detector
   tests must pass — so a draft that doesn't actually close the gap can't reach you.
3. **It pings you when done.** "I found a catalog gap for a `turbo run build` that
   prints `cache bypass` — drafted a `_parse_log` detector + `_FIX_META` + a test
   (all existing detector tests still pass). **Add it to the catalog?**"
4. **You approve (or not).** On **yes**, the agent creates a new branch, applies
   the detector + test, regenerates any worked example the detector now covers (so
   it drills deterministically instead of via the fallback), runs `pytest`, and
   opens a PR for your normal review. On **no**, nothing changes — the capture
   stays local for later.

So the whole maintainer flow is: **use the skill → get pinged with a drafted
detector → say yes → review the PR.** You never hunt for what the catalog is
missing; the tool tells you, with the fix written.

### How it knows you're a maintainer

It checks whether the running skill **is this repo's tracked source** rather than
an installed copy: `git ls-files --error-unmatch` on its own
`skills/ci-speedup/scripts/blocking_path.py` succeeds, and the checkout has the skills-monorepo
layout (sibling `skills/*/` + the catalog under `skills/ci-speedup/references/`). That's true when
you run from a `starslingdev/skills` clone (or a fork — the remote name isn't
used, so fork maintainers count); it's false for an installed skill under
`~/.claude/` or a single-skill copy vendored into another repo, so **end users
never trigger it**. The check is deliberately loose — it only decides whether to
*bother* drafting; nothing lands without your explicit yes at the PR gate, so a
false positive just means you say "no" and a false negative just means you run the
loop manually. An installed-skill / end-user run stops at the capture (phase 4b) —
no subagent, no branch, no PR.

## What the background subagent does

1. Reads the new capture (and any sibling `.ci-speedup-gaps/*/` captures of the
   same tool — recurrence strengthens the case) — log + analysis + meta.
2. **Clusters** by stack signature (the tool + the log shape the existing
   detectors key on — a turbo `Cached:`/`cache (miss|bypass)` summary, a vitest
   `Duration (transform … import … tests …)` line, a jest `Ran all test suites`,
   …). A true one-off stays an LLM-fallback case rather than over-fitting the
   catalog to one repo.
3. Proposes a **deterministic detector** grounded in the captured logs: a new (or
   widened) `_parse_log` branch, a `_FIX_META` entry, and a unit test — matching
   the existing conventions (`{fix_key, unit_label, deeper, evidence, magnitude}`;
   the prompt hands off, never prescribes), with a **false-fire boundary** and a
   confirmation it doesn't reclassify any existing detector.

The worked precedent (the loop done by hand): the **langfuse `e2e-tests`** gap (a
`turbo run build` printing `cache bypass, force executing` + two `Cached:`
summaries) was first caught by the phase-4a fallback, then promoted into the
summary-driven `turbo-remote-cache` widening (commit `a04d9b4`). The subagent
automates that discovery + draft.

## Before you approve

The subagent drafts; **you are the gate**. Read the proposed detector against the
captured logs: does the regex match the real lines? Is the magnitude recomputable?
Could it **false-fire** on a healthy build (the cache-key-churn hedge in
`turbo-partial-cache` is the model — some misses are legitimate)? The PR it opens
runs `pytest` in CI, but sanity-check locally too. Only the detector + test +
`_FIX_META` + regenerated example land; the gap captures stay local.

You can also run the loop **manually** on accumulated captures any time — hand the
prompt to an agent pointed at `.ci-speedup-gaps/` — e.g. to batch several
same-tool gaps into one detector. The auto-trigger is just the zero-effort path.

## The transcript self-improvement loop (general)

The gap → catalog loop only fixes *detector coverage*. The transcript loop fixes
*how the skill works* — it reads a whole session and turns the moments where you
had to **steer the agent** (correct it, repeat yourself, catch a near-miss) into
durable `SKILL.md` / reference-doc / `evals` edits, so a fresh agent doesn't make
the same mistake. It mirrors the proven `ci-secure` loop.

**Inputs (local, gitignored).** A session transcript staged at
`.ci-speedup-loop/transcripts/<slug>/transcript.txt` (or `.jsonl`). Source
sessions live under `~/.claude/projects/`; copy one in. Transcripts embed
third-party job logs (repo internals / tokens), so `.ci-speedup-loop/` is
gitignored and **never committed** — git can't restore it if deleted.

**Run it.** Hand an agent
[`loops/loop-analysis-prompt.md`](loops/loop-analysis-prompt.md) pointed
at one transcript. It emits a schema-valid
`summary.json` ([`loops/loop-summary.schema.json`](loops/loop-summary.schema.json))
into that transcript's dir: the **steering events** (what made you intervene + the
root cause) and **proposed_changes** — matched pairs of a `never-rule`/`phase-check`
(SKILL.md) plus an `eval-case` (evals.json), or a `doc-clarification`. The schema's
`scrub_check` (three `const:true` flags) structurally blocks a summary that admits a
scrub failure. It does **not** propose detectors — that's the gap → catalog loop.

**The recurrence gate (don't encode a one-off).** A single session is not enough
evidence to change the contract — a situational correction in one run can be a quirk
of that repo / that day / that operator, and encoding it over-fits exactly the way
the gap → catalog loop refuses to over-fit the catalog to one repo. So before you
adapt anything, run the **cross-session aggregation** over the batch of staged
summaries:

```
python3 maintainers/ci-speedup/scripts/aggregate_lessons.py            # observe only — prints the report, does NOT touch pending.jsonl
python3 maintainers/ci-speedup/scripts/aggregate_lessons.py --commit   # persist: rewrite pending.jsonl (drop promoted + expired)
```

By default the run is **non-destructive** — it prints the report and leaves
`pending.jsonl` untouched, so you can look before you persist. Pass `--commit` to
rewrite the feedstock (clearing promoted + expired entries). It reads every
`.ci-speedup-loop/transcripts/*/summary.json`, clusters their steering lessons by the
stable `signature` (the `<area>@<file>:<rule-slug>` template the analysis prompt
emits), and counts **distinct sessions** per cluster. It prints:

- **PROMOTED** — clusters that recur across **≥ `RECURRENCE_MIN` distinct sessions**
  (default **2**, a constant in the script). These are the only lessons eligible to
  become a `SKILL.md` / `evals` / doc edit. Adapt *these*. Note PROMOTED reflects
  **whatever transcripts are currently staged** — the script re-reads them all every
  run, so an already-handled cluster keeps re-appearing in PROMOTED while its
  transcripts remain on disk (idempotent, not a re-promotion). After you act on a
  promotion, **remove or archive the transcripts you processed** from
  `.ci-speedup-loop/transcripts/` so the next run only weighs what's left. (Clearing
  a signature from `pending.jsonl` — which needs `--commit` — only governs the
  below-floor feedstock list; it does not stop a staged batch from re-clustering.)
  **Act on PROMOTED before you `--commit`.** A cluster can promote by pairing two
  *pending* entries (one held in an earlier run, one in a later one) with no staged
  transcript behind it; `--commit` clears both from `pending.jsonl`, so a
  pending-only promotion won't re-surface next run. The report is always printed
  before any write, so the signal is never lost silently — but persisting without
  acting consumes it.
- **HELD** — below-floor lessons (seen in one session so far). They are **recorded**
  as un-promoted feedstock in `.ci-speedup-loop/pending.jsonl`, not encoded, so a
  later session can recur one and cross the floor. They are **surfaced, never
  dropped** — a genuinely-urgent one-off (a clear contract bug seen once) shows up
  here so you can still promote it **by hand**; the gate governs *automatic*
  promotion only.
- **EXPIRED** — pending entries discarded this run because they aged out. Each
  pending entry is stamped — at the run that first parks it — with `recorded_at` +
  the current `skill_commit_sha` (the **full** HEAD sha; `run.py` records a *short*
  sha for its report footer, but this gate needs the full sha because it feeds
  `merge-base --is-ancestor`, where an ambiguous abbreviation would be misread as a
  non-ancestor). An entry is dropped when it is older than `PENDING_TTL_DAYS`
  (default **90**) **or** its `skill_commit_sha` is no longer an ancestor of the
  current `SKILL.md` — so a fossil lesson can't "confirm" a phase/rule that has since
  been rewritten. Note this gate bounds entries that have **aged in `pending.jsonl`**
  across runs; a transcript staged for the first time is stamped with the *current*
  provenance no matter when its session ran, so a months-old transcript freshly
  staged today is caught not by this gate but by the "still reproduces against the
  current `SKILL.md`?" check at **Review & land** below. Expiry is **logged, not
  silent**; if an expired lesson is still relevant, re-confirm it by hand. (A pending
  entry with no usable provenance at all — neither a parseable `recorded_at` nor a
  sha — is also expired here, since it could otherwise never age out.)
- **INPUT ISSUES** — inputs the run could not count and so surfaces loudly instead of
  dropping silently: an unreadable/invalid `summary.json`, a summary with no
  `transcript_id` (unattributable to a session), or a malformed `pending.jsonl` line
  (kept **verbatim** on `--commit`, never destroyed, since the feedstock is gitignored
  and unrecoverable). A note also prints when the skill sha is unavailable (the
  ancestry gate was skipped that run).

`aggregate_lessons.py` reads/writes only inside the gitignored `.ci-speedup-loop/`
(the transcripts + `pending.jsonl`) and **never** commits.

**Review & land.** Take the **PROMOTED** lessons (and any HELD one-off you judge
urgent enough to promote by hand). For each, confirm the failure mode still
reproduces against the *current* `SKILL.md`/scripts (a transcript can describe
already-fixed behaviour), then adapt the generalized text into the target file and
add the paired eval. Only the SKILL.md / doc / evals edits land; the transcripts +
summaries + `pending.jsonl` stay local. Keep it to the few highest-leverage lessons.

## Verifying a pipeline change (maintainer discipline)

When you change anything in the **deterministic pipeline** — spine scoping,
required-check resolution/filter, scoring, the renderer — the committed-report
invariant tests (`skills/ci-speedup/tests/test_measured_evidence.py`, the structural guards) are
**not** sufficient evidence of no regression: they validate the *static committed
artifacts*, not the code you just changed. A change can pass every committed test
and still break on a repo shape the artifacts don't represent (this is how a
required-scope / PR-floor-fallback interaction bug slipped past green tests and was
only caught by re-running across repos).

- **Re-run end-to-end through the new code**, not from reasoning. Sweep the
  worked-example repos **plus one repo per gating branch** the change can touch —
  a file-backed required set, no required set, a partial/unreadable set, an
  unsatisfiable set, and an all-external/fileless set. Diff the new pole / drop /
  gate-kind against the prior behavior; a fresh window changes magnitudes, so judge
  *behavior* (which check headlines, what's dropped, no contradiction between the
  spine and the dropped set), not numeric equality.
- **Report validation honestly.** In the PR body, changelog, and your hand-off,
  state **which repos the pipeline actually ran against this session**, and
  distinguish **unit/synthetic coverage** from **live end-to-end** runs. Never cite
  a repo as validated by copying its name from the PR description or a committed
  worked example — name only what you executed against this session. If a path is
  unit-tested but not exercised e2e (e.g. a degraded-input branch that can't occur
  on a fresh run), **say so loudly** rather than implying full coverage. Re-measure
  any counts (test totals, dropped-check counts, validation tables) at write time —
  don't carry a stale number forward.

## NEVER (applies to all three loops)

- Never commit `.ci-speedup-gaps/`, `.ci-speedup-loop/`, or `.ci-speedup-dogfood/`
  — third-party job logs / transcripts / screen data / run payloads, gitignored,
  unrecoverable if deleted. Protect them from bulk `git clean` / parallel agents.
- Never run any loop as a GitHub Action — they need the local
  data + maintainer judgement; a public workflow can't see the gitignored inputs.
- Never accept a proposed detector whose regex/magnitude isn't grounded in a
  captured log, or that changes an existing detector's classification.
- Never encode a SKILL.md/eval lesson from a transcript without confirming the
  failure still reproduces against the current skill (don't re-fix what's fixed).

## Pricing infrastructure (mothballed 2026-07-20)

The dollar/pricing axis was **mothballed on 2026-07-20** (owner decision: report
runner-**minutes**, punt on pricing). The report now emits minutes only, with the
one-sentence pricing story "multiply by your runner's per-minute rate to get
dollars." The whole pricing surface left the tree:

- `skills/ci-speedup/scripts/billing.py` (SKU-weight / dollar derivation) — deleted.
- `skills/ci-speedup/references/runner-rates.json` (the `usd_per_min` rate table) — deleted.
- `maintainers/ci-speedup/scripts/check_rates_freshness.py` (the rates-freshness
  check) and its tests / dogfood wiring — deleted.
- The `propose_rates_refresh` refresh runbook and the dogfood loop's
  `rates_freshness` step — retired.

**Git history preserves all of it** (internal PRs #98/#100) if the dollar axis is
ever re-introduced; restore from there rather than re-authoring. There is no
runner-rates file to refresh and no freshness check to run.

## Adding a pattern to the catalog (manual)

Adding a new pattern is a two-part edit: author the catalog entry (METADATA +
body) in `skills/ci-speedup/references/optimization-patterns.md`, then register its detector in
`skills/ci-speedup/scripts/scan.py` (a per-file / cross-workflow / repo-file handler or, for
single-condition patterns, just `match:` / `yaml_path:` METADATA params) or
`skills/ci-speedup/scripts/collect_runs.py` (for a `data-driven` pattern, or a `structural`
pattern routed from the measured critical path).

Coverage bookkeeping: a `static` catalog entry with no detector is reported in
`catalog_patterns_without_detector`; `data-driven` entries are detected in
`collect_runs.py` (and the coverage list only tracks `static` entries). The one
asymmetry to know — a `data-driven` pattern that is **intentionally cut**
(OPT49/slow-setup, OPT50/post-step, OPT51/install-ratio, whose detectors are
retained but not dispatched) is NOT surfaced in any without-detector list; its
cut is documented in the catalog body and this runbook. A `structural` entry
with no router is NOT silently assumed covered — `scan.py` lists it in
`catalog_structural_patterns_without_detector` (currently OPT74 and OPT78).
OPT74 is the trust-boundary cache split, catalogued for human application but
needing fork-PR cache signals the scanner doesn't sample; OPT78 is the vitest
test-isolation lever, routed by a drill-time log leaf in `blocking_path.py`
rather than by the critical-path router.
