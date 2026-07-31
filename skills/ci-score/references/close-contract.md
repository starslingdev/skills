# CI Score — close contract (the executable close/kickoff protocol)

This is the full close and kickoff protocol for ci-score: how to open the
close on the collector's banner, present recommendations, ask the kickoff
question, apply one fix per approval, re-score, and handle ship, post-merge,
refusal, and PR-request rounds. `SKILL.md` keeps a terse
**"Close contract — invariants"** section at point of use — the
non-negotiables that live runs regressed on when they were buried — and
points here. Read this doc in full before composing any close; the
invariants in SKILL.md are what you hold in mind while following it.

## Contents

- **Open with the CI Score** — copy the collector's banner verbatim; only a
  scored stamp gets a banner; never fabricate a score.
- **When the offer applies** — recommendations only when a check failed;
  clean repos and refusals close without an offer.
- **Deliberate-absence judgment** — a quick look for concrete evidence a
  failing practice is intentional.
- **Top recommendations and the stake kind** — one plain word per finding
  (security / reliability-cost / speed-cost), qualitative never measured.
- **End the close BY ASKING the question** — the offer IS the question; the
  three live misses that prove the failure modes.
- **The kickoff menu shapes** — first close, post-apply re-offer (ship
  leads), the reported-merge fresh round, and the completion rule for every
  re-offer.
- **Cleanest-clean-win** — which slots the menu offers when the top-ranked
  fix is not a clean apply.
- **Kickoff protocol** — informed consent, protecting the user's work,
  apply, honest bounded confirmation, stop-at-the-diff.
- **PR requests** — opening a PR is never part of an apply.

## Close contract

**Open with the CI Score** — lead the close with the score banner, which
the collector **pre-draws from the stamp and prints to stdout** right after
its summary line. **Copy that banner verbatim into the close — never redraw,
re-pad, or adjust it freehand.** A hand-drawn 30-block bar was mis-counted
in a live run (29 blocks, misaligned box); the collector now owns the
drawing so the numbers can't drift. (For reference, the collector fills
round-half-up(value × 30 / 100) of a 30-block bar, prints the stamp's own
pass / fail / not-applicable tallies, and ends with `<owner/repo> @ <7-hex-sha>` (local directory name when there is no linked GitHub remote)
keeping any `-dirty` suffix — but you read the finished box, you don't
compute it.) What the collector's output looks like (illustrative — the
real box carries this run's values):

```
┌────────────────────────────────────────────┐
│  CI SCORE                        63 / 100  │
│  ███████████████████░░░░░░░░░░░            │
│  5 pass · 3 fail · 2 not applicable        │
│  <owner/repo> @ <short-sha[-dirty]>        │
└────────────────────────────────────────────┘
```

Only a scored stamp gets a banner: a **refusal** or a recorded scoring
error prints no banner (the collector emits none) and keeps its
plain-sentence close — never a banner with an empty or invented bar. The
banner presents the score — its
numeric value (e.g. "CI Score: 75/100";
presentation is number-only, owner 2026-07-28 — the letter band stays in
the stamp but is never rendered) — as the headline, reading it straight
from the `ci_score` stamp in
`findings.json` (the same stamp the score card renders; never invent or
recompute a score). Surfacing the score is about *their CI result*, not
skill machinery. Handle the no-score cases from whatever the stamp and
document actually say: a **refusal** (e.g. no workflows to check → "not
scored: no workflows") states the refusal reason instead of a score; a
recorded `data_sources.ci_score_error` (scoring failed) says the score
was unavailable this run; a pre-score/absent stamp simply omits the score
line. **Never fabricate a score** to fill the gap.

**The recommendations-and-offer block below applies only when the stamp
carries a score AND at least one check failed.** A clean repo closes on
the score plus "every applicable check passes — nothing to apply" (no
offer); any refusal or scoring error closes on the stated reason alone —
no recommendations, no offer, no absurd "apply #1" against nothing.

Before presenting recommendations, **spend a quick look checking whether
the repo shows visible evidence a failing practice is deliberate** — e.g.
a test that walks git history explains a full-depth checkout; a
merge-queue config explains missing PR triggers. When you find such
evidence, present that recommendation as "likely deliberate — here's
why; I'd skip it" instead of a fix to apply. The evidence must be
concrete — a test, a config, or a comment that the absence serves — not
merely that the failing practice is applied consistently (a repo that
skips pinning across every workflow is not thereby deliberate). A quick
scan, not an audit: the config-fact stays honest either way; this is the
judgment layer the scan cannot provide.

After the score: the **top ranked recommendations** (up to three, one
line each — label, **the stake's kind in one plain word**, impact/risk
tier, and the concrete consequence). The stake kind is derived from the
check_id's family, never invented per run: `ci.security.*` → **security**
(e.g. "security — an unpinned tag can be repointed to run someone else's
code"), `ci.hygiene.*` → **reliability/cost** (a hung job bills until the
timeout), all others (cache/checkout/parallel/build/trigger) →
**speed/cost**. A reader must be able to tell a security exposure from a
speed win from the close alone — "high impact / low risk" states a size,
not a kind, and is not sufficient by itself (owner dogfood 2026-07-29:
two security findings and a performance one read as interchangeable). The
stake kind is a **qualitative** category, never a measurement: name the
kind and its mechanism, never a saved-seconds number, a runner-minute
figure, or a dollar amount — the Boundaries ban still holds and a speed or
cost *question* still routes to ci-speedup.
Then a one-line note that the full verified report exists (every finding
with its fix recipe, why the practice matters, and a paste-able agent
prompt) — the SAVE CHOICE for it arrives in the question, not narrated in
your prose. Do NOT write the report into the repo or print a repo path
before that pick, and do NOT narrate the menu in prose AHEAD of the
question — no "the last option below saves it" describing choices the
question has not yet presented: that forward-narration is what made a live
close feel finished before the question was ever asked. This bans menu
NARRATION, not the no-tool fallback's option list — on a platform with no
structured-question tool, the numbered options printed as the message's
FINAL block ARE the question, not narration of one.

**End the close BY ASKING the question — never by finishing a text
message that leaves the choices unasked.** The offer IS the question,
delivered per SKILL.md's interaction contract: a structured-question tool
call where one exists (`AskUserQuestion` on Claude Code); where none does
(most Codex runs), the question is the plain-message fallback whose
numbered option list — SAME fixed options, SAME order, save last and
verbatim — is printed as the FINAL block of your message and the turn ends
on it. On that no-tool platform the printed final list IS the question and
is what completes the close. What never completes it is a message that only
mentions or gestures at the options without putting the fixed choices in
front of the user. Three live misses prove the failure modes: a close that
ended on a freeform "Want me to apply #1?" with no options, and a close
whose text mentioned the options and then simply ended — no question at all
(quasar 2026-07-29, twice); and a Codex run that closed on a bare "Apply
recommendation #4 (job timeouts) now?" with no options — the same
optionless close on the tool-less platform, where the fix is precisely to
print the full numbered option list as the message's final block. All had
perfect content; all left the user without the choices they are owed. So: after the
close text, the close is STILL UNFINISHED until the question is asked —
check before ending the turn. Options, in this order (owner 2026-07-30,
adopting ci-speedup's multi-slot menu; the platform question tool caps a
question at 4 options, so the shape is fixed): **up to TWO fix slots**,
ranked best-first by the report's own ranking with the
cleanest-clean-win rule governing what is offered (a
one-edit-closes-both bundle counts as ONE slot, named with the pair of
checks it closes); each fix slot is a consent gate, so its description
carries that fix's informed-consent scope and any risk note the
protocol below requires — never a bare "apply". Then
"a different recommendation" (its pick or Other names which); then last
and verbatim, `None, just save the report (.md)` — this pick performs
the copy from step 2 (`<workdir>/report.md` → `./ci-score-report.md`)
and is the ONLY pick that writes the report into the working tree.
Picking it ends the close (no re-offer) on the saved report's absolute
path — only this structured pick is terminal; and when a fix was applied
this session but not shipped, this close ALSO names the branch (per the
unshipped-close rule below) so the work is findable. (A user may instead ask to
save the report at some earlier point — an explicit out-of-band request;
honor it, announce where it landed, and keep the loop alive.) Later
approvals inside the kickoff loop (the "next?"
after each applied fix's diff and re-score) change shape (owner
2026-07-30: a real developer lands work before starting more — dropping
straight into the next edit is not how anyone works): the FIRST option
becomes **"Commit this branch and open a PR"**, its description naming
exactly what the pick authorizes — commit the applied fix(es) on the
branch, push it, open a PR whose description follows the PR-request
rules below (CI Score before → after from the stamps, the applied
recommendations, any intermediate-state caveat) — and picking it is the
explicit ask the stop-at-the-diff rule requires (still never merge; the
PR is handed back for the user's own review). A ship pick ENDS the loop:
close on the opened PR — name it and its branch — and do NOT silently
keep committing further fixes onto a branch whose PR is already open; if
fixable checks remain, name them and let the user start a fresh round if
they want one. **When the user later reports the PR merged —
or, after a merge, asks for a fresh/updated score or what to do next —
that IS the fresh round**: re-score the merged base and present it as a
full first close — lead with the collector's fresh banner, updated top
recommendations, then the first-close menu — never a bare prose
"re-scored, N/100" aside that jumps straight to a fix menu (live miss
2026-07-30: the merged re-score was buried in prose and the fresh banner
only appeared when the user manually re-invoked the skill). An incidental
post-merge question — a status check ("did it merge cleanly?"), recalling
the earlier number, explaining a finding — is answered on its own terms;
do not surprise the user with an unrequested re-grade. A re-score of a
real committed/merged base, presented as the current standing, always
gets this banner-led close, not just the very first one — but the in-loop
post-apply re-score against the DIRTY working tree keeps its before → after
delta bar (below) and is never turned into a fresh banner. The menu's other options, for when the user is not ready
to ship, continue the loop: ONE next-fix slot drawn from what still fails,
ranked best-first (a bundle still counts as one slot; omitted when nothing
fixable remains) / a different one / the save option last (copying the
freshest re-rendered report). A user
who wants neither to ship nor continue can still pick save or decline
in free text — and if the session ends unshipped, the close names the
branch so the work is findable. Every re-offer
obeys the SAME completion rule as the first close: it is itself a question
call (or the plain-message fallback), never the next options narrated in
prose, and that step stays UNFINISHED until the question is asked — the
miss-#2 failure (text mentions the next options, then the turn ends with
no question) is just as forbidden one loop in as at the first offer.

The slots default to the top of the report's ranking — but when a
candidate's fix is not a clean apply on THIS repo (a missing prerequisite
like a lockfile the cache would key on, or payoff degraded by repo
reality, with the evidence stated), that slot instead takes the
highest-ranked recommendation that IS a clean apply, saying plainly why
you skipped ahead. The same cleanest-clean-win rule fills each of the (up
to two) slots. Judgment with receipts, never silent reordering; the
report's ranking stays untouched.

For the scale-dependent practices (test sharding, build caching,
change-scoped builds), state the payoff condition plainly and never
present them as high-impact on a repo whose suite/build is evidently
quick; the cleanest-clean-win rule applies.

**Kickoff protocol** (one recommendation per explicit approval, never
bulk — a blanket "apply all" is NOT per-recommendation approval: apply #1
only, show its diff and re-score, then ask again for the next. ONE
exception, owner-decided: when a single edit fully closes two checks at
once — e.g. one `concurrency` block with `cancel-in-progress: true`
satisfies both the concurrency-group check and the cancel-superseded
check — that one edit MAY be offered as a single apply, provided the
offer names both checks it closes and the user says yes to that named
pair. This is the one-edit-closes-both case only; two edits that merely
touch the same file (a `concurrency` block plus a `timeout-minutes` key)
stay two separate approvals, never bundled):

- **Informed consent first.** Before applying, state the actual scope
  the apply will touch — prefer FILES over bare occurrences ("14 action
  references" is not a scope; "14 refs across 3 workflow files" is;
  "apply #1" must never be a surprise multi-file rewrite). The finding's
  evidence caps its example list at three, but the offer is NOT capped by
  it: enumerate the real offender set from the checkout — the same scan
  the apply must run to fix every offender — and state that true file
  count. Only if the true scope genuinely can't be enumerated before the
  edit do you fall back to the evidence's occurrence count, and then say
  the apply will touch every offender so the diff — not the count — is
  the authoritative scope; never let the offer imply fewer files than the
  apply will change. And for any recommendation that can skip CI
  (path filters, change-scoped builds) or carries a medium risk tier,
  surface its risk note in the offer itself — never apply a skip-CI
  change on a bare "yes". If the apply needs network lookups (resolving
  action tags to commit SHAs via `git ls-remote`), say so in the offer —
  the same ask-before-network spirit that governs cloning. And more
  broadly, any network READ during an apply or its investigation
  (`git ls-remote` for SHAs, `gh api` reads like branch protection) is
  disclosed — in the offer, or at the moment of need — observed live
  2026-07-30 (a branch-protection read during a path-filters
  investigation was protective but undisclosed).
- **Protect the user's work.** Check for pre-existing uncommitted
  changes before branching; never stash, reset, or discard work you did
  not create this session — if the tree is dirty, say so and ask before
  proceeding, and scope the diff you present to the files you edited.
- **Apply:** on yes, create a branch and apply the recipe **everywhere
  the practice is missing** — the finding lists up to three example
  files but states the full scope in its evidence, so a check with more
  than three offenders is fixed only when all of them are — and nothing
  else. Show the diff.
- **Confirm honestly, bounded:** when the applied fix leaves a
  meaningful intermediate state — the practice only fully pays off once a
  sibling recommendation is also applied (a concurrency group without
  cancel-in-progress queues, it doesn't cancel) — say so plainly in the
  confirmation, and name the sibling. Re-run step 1 against the working
  tree. When the re-score changes the value, show the before → after as a
  delta bar (both bars 25 blocks, filled = round-half-up(value × 25 / 100)
  — the same scale as the score card's gauge, so the user watches the same
  bar grow), the numeric delta, and the check that flipped:

  ```
  before  38/100  ██████████░░░░░░░░░░░░░░░
  after   50/100  █████████████░░░░░░░░░░░░   +12   <check label> → pass
  ```

  Both values are read from the confirming re-scores' stamps, never
  recomputed. The
  confirmation run's provenance is expected to read `-dirty` (it scores
  the uncommitted fix — correct here, not a defect; don't commit to
  "clean" it, and don't present the dirty score as a new published
  score). A passing re-score confirms the **configuration fact only**,
  not that CI still runs green — never introduce a flag, secret, or step
  the repo's toolchain doesn't support just to satisfy the check. A
  still-failing re-score means offenders remain — the evidence states a
  count but names only up to three examples, so fix every offender in
  that full scope (not just the named examples) and re-run. Only once the
  recipe has reached the full scope and the check still fails do you STOP:
  the recipe may not fit this repo's shape; do not keep mutating YAML to
  force a pass. Report what changed and hand it back. **After every
  re-score, re-run steps 2–3 (render + verify)** — not only when the pass/
  fail stamp flips but whenever the re-score changes `findings.json` at all
  (offender detail moves even when the stamp holds) — so
  `<workdir>/report.md` always matches the current state and the save
  pick's "freshest report" promise holds. A stale
  report breaks its own purpose (its handoff prompts would reference
  already-fixed findings), and a later save pick must never copy a report
  that no longer matches its findings. If the user saved the report
  earlier via such an out-of-band request, refresh that saved copy too so
  it never drifts from the current findings.
- **The apply approval covers the EDIT only — stop at the diff.** The
  apply "yes" authorizes the edit and nothing downstream: on its strength
  alone do NOT commit, push, or open a PR — each is a separate action
  needing its own explicit ask (pushing anywhere is outward-facing). The
  local branch from the Apply step is fine — it just holds the uncommitted
  diff. That separate ask is the post-apply re-offer's "Commit this branch
  and open a PR" option: on THAT pick the agent commits the applied
  fix(es), pushes, and opens the PR (still never merge — handed back for
  the user's own review), or the user may do it themselves. What is
  forbidden is treating the apply "yes" as the commit license — a live
  session did exactly that; the apply "yes" never authorizes commit/push,
  only the ship pick (or the user) does.
  Declined recommendations keep their paste-able handoff prompt in the
  report — that is the hand-to-your-own-agent path, which requires the
  report to outlive the session, so point the user at the close's save
  option (an unsaved report lives only in `<workdir>` and may be cleaned
  up).

**PR requests:** opening a PR is never part of an apply — it happens
only on the user's explicit ask, which includes picking the re-offer's
"Commit this branch and open a PR" option (the option names both
actions, so the pick IS the ask). When asked, the PR description states
the **CI Score before → after** (read from the confirming re-scores'
stamps — never recomputed), names the recommendation(s) the diff
applies, and carries the intermediate-state caveat when one applies
(e.g. "group only queues until cancel-in-progress lands — deliberately
left out of this diff"). The reviewer should understand why the PR
exists from the description alone.

Close with the user's result, then END BY ASKING the kickoff question
(the offer is that question call, never text you write in its place) —
and never narrate phases, internal gates, or skill machinery.
