# How skills.sh audits actually work

The mental model that makes registry audits legible, the timings observed for
each stage, and — for every load-bearing claim — how it was established. Read
this when an audit disagrees with the repository and you need to know whether
to wait, act, or escalate.

## The whole thing in twenty seconds

```
  YOUR MERGE ──────────────X   nothing happens. ever.

  SOMEONE INSTALLS (with working telemetry)
        │
        └─> beacon ──> INGEST ──> SNAPSHOT ──> AUDIT ──> badge
                       "photocopy   frozen     runs
                        of your     copy       later, on
                        repo RIGHT             whatever the
                        NOW"                   snapshot holds
```

**The snapshot is a photocopy.** Scanners read the photocopy, never the
repository. The only thing that takes a new photocopy is an install whose
telemetry beacon actually reaches the service.

Three consequences follow, and they cover nearly every confusing symptom:

1. **Merging a fix changes nothing.** No install, no new photocopy.
2. **A fix that lands after the last photocopy is invisible** until the next
   one, no matter how many times the audit re-runs.
3. **Re-audits keep reporting the old finding with a fresh timestamp**, which
   reads as "your fix did not work" when it actually means "the photocopy
   predates your fix."

The rest of this document is the detail behind that picture, the timings, and
the evidence for each claim.

## Contents

- The whole thing in twenty seconds
- A real timeline, annotated
- The pipeline
- The precondition that invalidates most experiments
- What each timestamp actually means
- What the vendors say
- When a re-audit happens
- Observed timings
- Worked example: one repo-wide ingest, and a fix that missed it
- Dating a snapshot exactly
- Reading a disagreement
- Dead ends, already tested
- Preventing it: scan before you publish
- Evidence log: how each claim was established
- Claims deliberately NOT made

## A real timeline, annotated

The incident this skill was written from. Three skills in one repository; only
one of them was new. All times UTC.

```
Aug 10
 14:04  commit 3f5df92 - a new skill is added to the repo
 17:13  commit 27d9ca9 - two SIBLING skills edited
          |
 17:54  * someone installs the new skill  -->  INGEST FIRES
          |    photocopy taken of the WHOLE REPO as of 27d9ca9
          |    all three snapshots rebuilt at this instant
          |
 19:09  commit f5c51a2 - THE FIX (removes the flagged URL)
          |    ^^^ 75 minutes too late. Photocopy already taken.
          |
 19:11  audits run against that photocopy - the two siblings pass
 19:49  commit 063d560 - no install, so nothing happens

Aug 11
 19:44  the audit RE-RUNS, still against the 17:54 photocopy
          |    -> the deterministic finding repeats, with a NEW timestamp
          |    -> looks like "the fix did not work". It is not what happened.
 22:50  * a real install finally happens (beacon NOT suppressed)
          |
Aug 12
 ~02:00  --> RE-INDEX CONFIRMED. New photocopy, ~3h after the install.
          |     hash changed; the flagged URL is gone from the scan input;
          |     the post-fix placeholder is present.
          |     Audit still stamped 19:44 - it drains behind ingest.
          |
          |   ... 22 hours of nothing. Audit still stamped Aug 11 19:44.
          |       Polled ~20 times; byte-identical payload every time.
Aug 13
 00:01  --> RE-AUDIT FIRES ON ITS OWN. No install, no push, no request.
          |     Snyk  fail/CRITICAL/2 issues -> warn/MEDIUM/1 issue
          |     E005 (the flagged URL) GONE; W011 remains.
          |     Socket +22s, Gen Agent Trust Hub +38s - one batched sweep.
```

That last line closes the loop the rest of this document only inferred:
**install → ingest → audit → badge, all four links observed end to end on one
skill.** The remedy is real, it is unattended, and the only thing it needed
was time.

That last step is the whole remedy, and it was confirmed by experiment rather
than inferred: **one install whose beacon actually fires rebuilds the
snapshot.** Nine installs with the beacon suppressed did nothing; one without
the suppression did everything.

Two things this makes obvious that prose does not:

- **The fix missed the bus by 75 minutes.** Everything downstream follows from
  that single gap, not from anything being broken.
- **The two skills the fix did NOT touch look "fresh" while the one it DID
  touch looks "stale."** That inversion is purely an artifact of when the one
  ingest fired. It is not per-skill logic, and chasing it wastes hours.

## The pipeline

```
   GitHub default branch   (merging a fix triggers NOTHING on its own)
             |
             |  read by the indexer, but only when something asks it to
             v
   +--------------------------------------------------------------+
   |  INGEST                                                        |
   |    triggered by an install that REACHES the telemetry service: |
   |    GET add-skill.vercel.sh/t?event=install&source={o}/{r}      |
   |        &skills=<names>&skillFiles={"name":"path/SKILL.md"}     |
   |    skillFiles is the map an indexer needs to locate each skill |
   |    Appears to be REPO-SCOPED: one install rebuilt all three    |
   |    sibling snapshots at the same instant.                      |
   +--------------------------------------------------------------+
             |
             v
   +--------------------------------------------------------------+
   |  STORED SNAPSHOT          <-- inspect this; it is the scan input
   |    { files: [{path, contents}], hash }                         |
   |  GET www.skills.sh/api/download/{owner}/{repo}/{skill}         |
   |  NOT a faithful copy: files above ~500-600 KB are dropped.     |
   +--------------------------------------------------------------+
             |
             |  (2) AUDIT — separate cadence, drains behind ingest.
             |      Re-runs scanners over WHATEVER the snapshot holds.
             v
   +--------------------------------------------------------------+
   |  PER-PROVIDER VERDICTS                                         |
   |    Gen Agent Trust Hub | Socket | Snyk                         |
   +--------------------------------------------------------------+
             |
             +--> GET /api/v1/skills/audit/{o}/{r}/{s}
             |      status + riskLevel + auditedAt. NO finding codes.
             +--> GET /{o}/{r}/{s}/security/{provider-slug}
             |      the codes (E005, W011) and the literals they quote.
             +--> GET add-skill.vercel.sh/audit?source=..&skills=..
             |      install-path cache. What users see. Lags.
             +--> rendered page badges (a third cache)


   npx skills add ── clones the CURRENT default branch ──> user's disk
        |             (unless the owner is on the blob allowlist)
        +── fires the install beacon above, IF it is not suppressed
        +── fires a read-only audit GET purely for display
```

Installing delivers current content to the user while the audit reads the
snapshot. A clean installed tree and a failing audit are therefore entirely
compatible, and that gap is cosmetic-versus-real: **users are not receiving the
flagged content; only the scanners are.** Establish which case you are in
before escalating, because it changes the urgency completely.

## The precondition that invalidates most experiments

Before concluding anything from an install, verify the beacon could actually
have been sent. It is dropped silently — no warning, no non-zero exit — when:

- **`api.github.com/repos/{owner}/{repo}` does not answer 200.** The CLI calls
  it unauthenticated to decide whether the repo is private, and returns `null`
  on any non-OK response. The beacon is sent only when the result is exactly
  `false`, so `null` suppresses it. This bites in sandboxes and CI containers
  that proxy or block GitHub, and on any IP that has burned the 60-requests/hour
  unauthenticated limit.
- **`DISABLE_TELEMETRY` or `DO_NOT_TRACK` is set.**
- **The repo is genuinely private.**

Check before, not after:

```bash
curl -sS -o /dev/null -w '%{http_code}\n' https://api.github.com/repos/OWNER/REPO   # need 200
env | grep -iE 'DO_NOT_TRACK|DISABLE_TELEMETRY'                                     # need empty
```

This is not a footnote. An entire investigation concluded "installs never
trigger indexing" from nine installs in an environment where that GitHub probe
returned 403 — so the beacon never fired once, and the null result measured
nothing. If the precondition fails, an install tells you nothing at all.

## What each timestamp actually means

| Field | Where | Means | Does NOT mean |
|---|---|---|---|
| `auditedAt` | audit API, detail page | when the **scanners last ran** | that current code was read |
| `analyzedAt` | install-path cache | same value, different key name | — |
| `hash` | snapshot API | identity of the **stored content** | anything about scan time |
| "First Seen" | skill page | when the skill was first indexed | last index |

The most expensive misreading here: **a fresh `auditedAt` on a stale
snapshot.** It makes a finding about deleted content look current, which reads
as "your fix did not work." The snapshot `hash` is the only field that tracks
content, so it is the signal to watch. Note the `hash` is not reproducible from
the served files by the CLI's own algorithm, so treat it as an opaque identity
token, not a verifiable checksum.

## What the vendors say

On the trigger, from Snyk's write-up of the partnership:

> "When a **new** agent skill is installed using the npx skills installer,
> Vercel's infrastructure triggers a call to Snyk's high-throughput scanning
> API. This happens automatically, with no action required from the skill
> author or the end user."

And from the registry's own docs (`skills.sh/docs/customize`):

> "skills.sh picks up skills.sh.json **after the repository is seen by the
> telemetry service**. In practice, that usually means **after someone installs
> from the repo with the skills CLI**."

Both name the install as the ingest trigger. On the engine:

> "built on the **agent-scan** scanning engine (also known as mcp-scan), which
> combines multiple customized **LLM-based judges with deterministic rules**."

That hybrid explains why one finding reproduces byte-for-byte across two scans
of the same content while another flips. A deterministic rule returns the same
answer on the same bytes; a judge can reach a different conclusion on identical
input. So **a verdict changing is not evidence that content changed** — and a
deterministic finding surviving a rescan after its literal was deleted is
strong evidence the input did not.

Sources: <https://snyk.io/blog/snyk-vercel-securing-agent-skill-ecosystem/>,
<https://www.skills.sh/docs/customize> (the snyk.io domain may be unreachable
from restricted networks; that quote was recovered via search excerpts).

## When a re-audit happens

Ingest has a known trigger — the install beacon. The audit does not appear to
have one you can pull. What has been observed:

- **Audits re-run unattended.** A re-audit fired 22 h after a re-index with no
  install, no push, and no request of any kind in between. Nothing needs to be
  done to make the badge catch up with a fresh snapshot except wait.
- **Ingest is the thing worth chasing; the audit follows.** Since scanners read
  the snapshot, a re-audit is only useful once the snapshot already contains
  the fix. Spending effort trying to trigger an audit before that just re-reads
  the old photocopy — which is exactly how a fix that already landed keeps
  reappearing as a live finding.
- **All three providers move together.** Snyk, Socket and Gen Agent Trust Hub
  re-stamped within 38 s of each other, so this is one batched per-skill sweep
  rather than three independent schedules. A single provider with an old
  `auditedAt` while its siblings are fresh would be the anomaly, not the norm.
- **The cadence is loose.** Two consecutive audits of the same skill were
  28.3 h apart, and an earlier pair ~24 h. Treat "roughly daily, but do not set
  a watch by it" as the model. A gap of 28 h is not evidence of anything stuck.
- **No client-controllable trigger exists.** Every attempt is in *Dead ends,
  already tested* below. The remaining unknown is whether a re-index enqueues
  the audit or whether the sweep is purely periodic; 22 h is consistent with
  either, so the question is open.

The practical consequence: after a beacon-capable install, budget **~3 h for
the snapshot and up to another day for the badge**, and do not poll in between.
Twenty polls over that window produced a byte-identical payload every time and
told nobody anything. Check once, then check the next day.

## Observed timings

From directly observed cycles, not documentation. Re-derive rather than
trusting these numbers forever.

| Stage | Observed | Notes |
|---|---|---|
| install beacon → **first** index | ~50 min | brand-new skill, never seen before |
| install beacon → **re**-index | **~3 h** | measured end to end: install 22:50, snapshot changed by 02:07 |
| ingest → audit | ~77 min in one case | the audit queue drains behind ingest |
| **re-index → re-audit** | **~22 h** | measured: snapshot 02:00, audit 00:01 next day. Unattended — no install or push in between |
| audit → next audit | ~24 h, once 28.3 h | loose cadence; do not read a missed day as stuck |
| provider spread within one sweep | **38 s** | Snyk 00:01:48, Socket 00:02:10, Gen ATH 00:02:26 — one batched sweep |
| audit → all caches agree | up to ~1 day | install-path cache lagged the JSON API by a day |
| merge with no install | **never** | two pushes produced no ingest at all |
| install with beacon suppressed | **never** | nine installs, zero effect — the beacon never fired |

Re-indexing an existing skill is markedly slower than first-indexing a new one,
so do not treat a couple of quiet hours as failure. Budget half a day before
concluding an install did not work, and check the precondition again first.

**Before escalating**, confirm a beacon-capable install has actually happened
since the fix. Most "stuck" reports in the wild are missing that step.

## Worked example: one repo-wide ingest, and a fix that missed it

All times UTC. Three skills in one repo; only one was new:

```
 14:04  3f5df92 adds ci-secure to the repo
 17:13  27d9ca9 edits ci-speedup + ci-score SKILL.md (adds `license: MIT`)
 17:54  first install of the new skill registers the repo
          -> REPO-WIDE INGEST: all three snapshots rebuilt from the tree
             as of 27d9ca9
 19:09  f5c51a2 removes the flagged URL from ci-secure
          -> 75 minutes AFTER the ingest. Missed it.
 19:11  ci-speedup + ci-score audits run (queue draining behind ingest)
 19:49  another push. No install -> no ingest.
 next day 19:44  ci-secure re-audited over the UNCHANGED snapshot
          -> the deterministic finding repeats, with a fresh timestamp
```

The lesson is the 75 minutes. The ingest captured the repository just before
the fix, and every later audit re-ran over that capture. Nothing was stuck and
nothing was broken — no beacon-capable install had happened since.

Note also that the two skills the push did *not* touch appear "fresh" while the
one it did appears "stale." That inversion is an artifact of when the single
ingest fired, not evidence of per-skill logic.

## Dating a snapshot exactly

Comparing the newest date in the snapshot's `CHANGELOG.md` against the
repository's is a fast approximation, but it is coarse: a commit that does not
touch the changelog is invisible to it.

The exact method is to byte-compare every snapshot file against each candidate
commit's tree and find the one with zero differences. That pins the snapshot to
a specific commit, which is what makes an escalation concrete: "your snapshot
is commit X; the fix is commit Y; here are the N files that differ."

Remember the indexer drops files above roughly 500–600 KB, so expect large
files to be absent from the snapshot entirely rather than stale. Compare only
the paths the snapshot actually contains.

## Reading a disagreement

Work down this list; stop at the first match.

1. **Has a beacon-capable install happened since the fix?** If not, that is the
   whole answer. Do it, then wait.
2. **Is the quoted literal in the snapshot?** If yes and it is gone from the
   repository, the input is stale — the scanner is right about what it was
   handed.
3. **Is it in the repository too?** The finding is real. Fix or accept it.
4. **In neither?** Do NOT jump to "the scanner cached its own result". First
   ask the discriminating question: **is the finding's `auditedAt` older than
   the snapshot's re-index?** If it is, this is an ordinary stale finding that
   simply has not been re-audited yet — the literal is absent from the current
   snapshot precisely *because* the fix landed, and the audit has not read it.
   Wait for the next sweep (see *When a re-audit happens*). Only once an audit
   has demonstrably run **against the current snapshot** and still cites a
   literal present in neither corpus is a scanner-side cache the explanation.
5. **Verdicts differ across surfaces?** Cache propagation. Re-run later.

Step 4 is the trap this document exists to prevent, in a new costume. The
original mistake was reading a fresh timestamp on stale content as "the fix
did not work". The mirror-image mistake is reading fresh content under a stale
timestamp as "the scanner is broken" — and it was made here, four hours before
the audit re-ran on its own and cleared the finding. Both are the same error:
comparing a timestamp and a hash without asking which one moved first. The
corroborating signal is in `registry-surfaces.md`: warning codes reproducing
while a critical code vanishes is the signature of stale input, not of a
phantom.

Corroborate with the project's own scanner run if it has one: same scanner,
current branch, opposite verdict isolates the input as the variable.

## Dead ends, already tested

Recorded so nobody spends a day re-deriving them.

| Attempt | Result |
|---|---|
| Repeat installs from an environment where the GitHub probe 403s | Nothing. The beacon never fired; the experiment measured nothing. |
| `POST add-skill.vercel.sh/check-updates` with `forceRefresh:true` | Route is live and self-documenting, and accepted a well-formed call — but returned `"No cached hash available (skill may need reinstall)"` and moved nothing. It compares against a **separate** update-check cache, and the CLI stopped sending hashes to the server, so that cache is never populated. A zombie endpoint. |
| `POST www.skills.sh/api/revalidate` | `{"error":"Unauthorized"}`. Secret-gated. Do not attempt to bypass. |
| Cache-busting `/api/download?x=1` | `x-vercel-cache: MISS, age: 0` — a genuine origin hit — returning a **byte-identical** body. The staleness is at origin; no edge trick can help. |
| `?ref=`, `?sha=`, `?version=`, `?commit=` on the snapshot or audit endpoints | Silently ignored. Even a nonexistent branch returns the same snapshot. No client-controllable cache key exists. |
| `npx skills use` | Sends no telemetry at all. Cannot be the ingest signal. |
| Installing with `@ref` / `@sha` | The ref is stripped before anything reaches the registry. |
| The documented v1 API | Read-only, all five routes are GETs. No publish, refresh, or index endpoint. |

## Preventing it: scan before you publish

Everything above is remediation. The cheap fix is upstream: run the registry's
own scanner in CI so a violation fails your build instead of surfacing as a
public FAIL badge days later, on a snapshot you cannot refresh on demand. The
scanner is publicly available — `uvx snyk-agent-scan@latest scan <path>` — and
needs a `SNYK_TOKEN`.

**Gate on the critical class only.** The scanner has no severity threshold, so
the rule is expressed by enumerating the warning class into
`--ignore-issues-codes`. Warning-class findings are usually accurate
descriptions of what a repository-auditing skill legitimately does; blocking on
them trains people to suppress findings. Surface them and let a human judge.

**Never let an E-code into the ignore list.** One there silently disarms the
gate and nothing else would tell you. Pin it with a test asserting every ignored
code starts with `W`, and another pinning the list to exactly the published
warning class.

**Prove the gate can go red, on every run.** A check that cannot fail is not a
check, and the rule catalog changes with no commit of yours. Before the real
scan, build a throwaway skill carrying the exact violation class, scan it **with
the gate's real ignore list**, and fail unless the scanner both reports the
anchor code and exits non-zero. With an empty ignore list you would only prove
the scanner can fail, not that *this gate* can. It fails upward too: if the
anchor rule is renamed, the step goes red and a human re-anchors it.

**Add a cheap offline guard for the shape that burned you.** The scanner needs a
token, network, and minutes; a text rule needs none and blocks the commit rather
than the merge. Assemble test fixtures containing the offending shape at
runtime, or the guard will correctly reject your own tests.

**Schedule it**, so a catalog change that needed no commit of yours is noticed.
**And make it a required status check** — a gate that can be merged past is
advisory.

## Evidence log: how each claim was established

The registry documents almost none of this. Re-derive any claim that stops
matching reality.

| Claim | How it was established |
|---|---|
| The install beacon carries what an indexer needs | Read the CLI source: the install event sends `skillFiles`, a JSON map of skill name to repo-relative `SKILL.md` path. A pure install counter would not need paths. |
| The beacon is suppressed on a non-200 GitHub probe | Read the source (`isRepoPrivate()` returns `null` on any non-OK; the send is gated on `=== false`), then confirmed the probe returns 403 in the environment where the null result was produced. |
| **An install with a live beacon rebuilds the snapshot** | Direct experiment. Nine installs from a beacon-suppressed environment over six hours: hash unchanged. One ordinary install from a normal machine: hash changed within ~3 h, the flagged literal dropped to zero occurrences, the post-fix placeholder appeared, and the snapshot's changelog advanced a day. Same repo, same skill, only the beacon differed. |
| Ingest is repo-scoped | One install of a newly added skill coincided with all three sibling snapshots being rebuilt at the same instant. |
| The audit call is read-only and carries no SHA | Source read: a GET keyed on owner/repo plus display names, gated only on the repo being public. |
| The JSON API has no finding codes | Its `summary` is prose plus a count; codes appear only on the per-provider HTML pages. |
| A stored snapshot exists and is fetchable | Found `/api/download/...` in the CLI's blob module; fetched it directly. |
| The snapshot can be stale | Fetched it: 80 files containing the removed URL in 4 of them, and zero occurrences of the placeholder that replaced it. |
| The snapshot pins to an exact commit | Byte-compared every snapshot file against candidate commit trees; one matched with zero differences. |
| Audits re-run over an unchanged snapshot | Audit timestamps advanced ~24h while the snapshot hash and the finding text stayed byte-identical. |
| Findings clear once the fix is in the snapshot | A sibling skill went FAIL/HIGH with two findings to WARN/MEDIUM with one, after the fixing commit's file appeared in its snapshot. |
| **The whole chain works end to end, unattended** | Watched on one skill across three days: beacon-capable install 22:50 → snapshot rebuilt ~02:00 (+3 h) → audit re-ran 00:01 next day (+22 h) → Snyk fail/CRITICAL/E005+W011 became warn/MEDIUM/W011. Nothing was requested at any step; the only input was one install. |
| A re-audit needs no trigger from you | The re-audit above fired with no install, push, or API call in the preceding 22 h — the tree and every registry surface were untouched apart from read-only GETs. |
| Audits are one batched sweep, not per-provider schedules | Three providers re-stamped within 38 s of each other, and the model-based one returned a freshly written summary naming code it had not previously described. |
| Warning codes reproduce while stale critical codes vanish | Across four audits of the same skill, W011 appeared in every one; E005 disappeared the moment the audit read a snapshot without the literal. |
| Caches disagree | The install-path endpoint served day-old values while the JSON API served current ones for the same skill. |
| Model-based providers re-judge unchanged input | One provider went fail to pass across two audits of the same snapshot, its category list growing rather than shrinking. |
| The indexer drops large files | Two snapshots omit files of ~600 KB and ~900 KB; the largest included file is ~470 KB. |
| The pages expose no other API | Page source is an RSC payload plus Vercel Web Analytics; no audit or index endpoint is called client-side. |

## Claims deliberately NOT made

Recorded so nobody repeats an overreach — several of these were made and
retracted while investigating.

- **"Installs never trigger indexing."** False, and the most costly error made
  here. It came from nine installs in an environment where the beacon was
  suppressed. Always verify the precondition first.
- **"Indexing happens once and never repeats."** False. Snapshots demonstrably
  rebuild.
- **"A literal absent from both the repository and the snapshot means the
  scanner cached its own result."** Asserted here, with an escalation packet
  drafted on the strength of it, and disproven four hours later when the audit
  re-ran unattended and dropped the finding. Absence from both corpora is
  equally consistent with the ordinary case — the fix landed, the snapshot
  caught up, and the audit simply had not run yet. The verdict is only
  supportable once an audit has run *against the current snapshot*; check
  `auditedAt` against the re-index before claiming it.
- **"A re-index is what causes the next audit."** Unknown. The one observed
  re-audit came 22 h after a re-index, which fits both "the re-index enqueued
  it" and "a periodic sweep happened to come round". Nothing distinguishes them
  yet, so neither is claimed.
- **"The scanners read the snapshot."** Consistent with every observation and
  strongly supported by a finding clearing exactly when the fix appeared in the
  snapshot, but never directly verified.
- **"Users are receiving the stale content."** False for repos not on the blob
  allowlist: the CLI clones the current default branch, and the installed tree
  was verified clean. Only the audit reads the snapshot.
- **"Self-hosting the snapshot fixes the audit."** The CLI has an allowlist for
  repos serving their own snapshot, but the audit call does not consult it.
- **"Renaming the skill forces a fresh audit."** A new name means a new slug
  means a new identity with no cached audit — and it is not an acceptable
  remedy. It discards the skill's URL, install count, and recognition to work
  around a cache. Do not propose it.
- **"Filing an issue reliably works."** At the time of writing, a search for
  re-index and rescan requests returned a dozen matches, all open and
  unanswered, and no maintainer has publicly described the mechanism.
