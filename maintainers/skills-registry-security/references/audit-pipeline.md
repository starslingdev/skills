# How skills.sh audits actually work

The mental model that makes registry audits legible, the timings observed for
each stage, and — for every load-bearing claim — how it was established. Read
this when an audit disagrees with the repository and you need to know whether
to wait, fix, or escalate.

The short version: **there are two independent pipelines on two different
slow cadences, and neither one is triggered by anything you do.** Almost every
confusing symptom follows from that.

## Contents

- The pipeline
- What each timestamp actually means
- What the vendors say
- Observed timings
- Worked example: a complete, successful cycle (ci-speedup)
- Worked example: a fix that has not landed yet (ci-secure)
- Reading a disagreement
- Preventing it: scan before you publish
- Evidence log: how each claim was established
- Claims deliberately NOT made

## The pipeline

```
   GitHub default branch  (your merge lands here, and nothing happens)
             |
             |   (1) INDEX — irregular. Observed ~2 days after a change.
             |       Not triggered by: merging, pushing, or installing.
             v
   +---------------------------------------------+
   |  STORED SNAPSHOT                             |
   |    { files: [{path, contents}], hash }       |   <-- inspect this
   |  GET /api/download/{owner}/{repo}/{skill}    |
   +---------------------------------------------+
             |
             |   (2) AUDIT — separate cadence. Ran 11 days after the
             |       index in the one fully observed case. Re-runs the
             |       scanners over WHATEVER the snapshot holds.
             v
   +---------------------------------------------+
   |  PER-PROVIDER VERDICTS                       |
   |    Gen Agent Trust Hub | Socket | Snyk       |
   +---------------------------------------------+
             |
             +--> GET /api/v1/skills/audit/{o}/{r}/{s}
             |      status + riskLevel + auditedAt. NO finding codes.
             |
             +--> GET /{o}/{r}/{s}/security/{provider-slug}
             |      the codes (E005, W011) and the literals they quote.
             |      HTML only. This is the only place findings exist.
             |
             +--> GET add-skill.vercel.sh/audit?source=..&skills=..
             |      the install-path cache. What users see. Lags.
             |
             +--> the rendered skill page badges (third cache)


   npx skills add  ── clones the CURRENT default branch ──> user's disk
             |
             +── fires a read-only GET at the audit cache for display
                 (owner/repo + skill names only; no SHA, no trigger)
```

The install arrow is drawn separately on purpose. It touches neither stage.
A clean freshly-installed tree and a failing audit are entirely compatible,
and neither is evidence about the other.

## What each timestamp actually means

| Field | Where | Means | Does NOT mean |
|---|---|---|---|
| `auditedAt` | audit API, detail page | when the **scanners last ran** | that current code was read |
| `analyzedAt` | install-path cache | same value, different key name | — |
| `hash` | snapshot API | identity of the **stored content** | anything about scan time |
| "First Seen" | skill page | when the skill was first indexed | last index |

The single most expensive misreading in this whole system: **a fresh
`auditedAt` on a stale snapshot.** It makes a finding about deleted content
look current, which reads as "your fix did not work." The snapshot `hash` is
the only field that tracks content, so it is the one to watch.

## What the vendors say

Two published statements corroborate the model and explain provider behavior
that otherwise looks like a bug.

On the trigger, from Snyk's write-up of the partnership:

> "When a **new** agent skill is installed using the npx skills installer,
> Vercel's infrastructure triggers a call to Snyk's high-throughput scanning
> API. This happens automatically, with no action required from the skill
> author or the end user."

Note the word *new*. This is the first-index trigger, not a per-install one,
which is exactly what the observations show: repeat installs of an already
known skill do nothing.

On the engine:

> "built on the **agent-scan** scanning engine (also known as mcp-scan), which
> combines multiple customized **LLM-based judges with deterministic rules**."

That hybrid is why one finding can reproduce byte-for-byte across two scans of
the same content while another flips. A deterministic rule (a download-URL
pattern like E005) returns the same answer every time it sees the same bytes;
a judge can reach a different conclusion on identical input. So **a verdict
changing is not evidence that the content changed** — and conversely, a
deterministic finding surviving a rescan after its literal was deleted is
strong evidence the input did not change.

Source: <https://snyk.io/blog/snyk-vercel-securing-agent-skill-ecosystem/>
(the domain may be unreachable from restricted networks; the quotes above were
recovered via search excerpts).

## Observed timings

Ranges from directly observed cycles, not documentation. Treat as
order-of-magnitude, and re-derive rather than trusting these numbers forever.

| Stage | Observed | Notes |
|---|---|---|
| first index | at first install | "Audits are generated automatically after a skill is installed for the first time" |
| re-index after a change | **~2 days** | one fully observed cycle |
| re-audit after an index | **~11 days** | same cycle; the two are decoupled |
| audit → all caches agree | up to **~1 day** | install-path cache lagged the JSON API by a day |
| install → any effect | **none** | 9 installs over 6 hours produced no index and no audit |

**Escalation threshold.** Below roughly 2–3 days after a fix, nothing is wrong
— the re-index has not come due. Escalating early wastes your time and a
maintainer's. Beyond that window, with the literal still in the snapshot, the
case is real.

## Worked example: a complete, successful cycle (ci-speedup)

The whole loop, start to finish, with no intervention from anyone:

```
 Jul 22            Jul 28 01:12       ~Jul 30             Aug 10 19:11
 first seen        PR #15 merges      snapshot            Snyk re-audits
 + first audit     the W007 fix       RE-INDEXED          that snapshot
     |                  |                  |                   |
     v                  v                  v                   v
 FAIL / HIGH            +---- ~2 days -----+                   |
 W007 + W011                               |                   |
                                           +---- ~11 days -----+
                                           |                   |
                              snapshot now contains       WARN / MEDIUM
                              tests/test_secret_          W011 only
                              redaction.py                (W007 CLEARED)
```

Two things this proves and one it strongly suggests:

- Re-indexing **does** happen, unprompted. The snapshot moved from Jul-22-era
  content to Jul-30-era content on its own.
- Findings **do** clear once the fix is in the snapshot. W007 disappeared.
- It strongly suggests the scanners read the snapshot: W007 cleared precisely
  when the fixing commit's file appeared there. Not proof — the scanners could
  read the branch independently — but the correlation is exact.

## Worked example: a fix that has not landed yet (ci-secure)

```
 Aug 10 17:54       Aug 10 19:09        Aug 11 19:44        Aug 11 22:12
 first index        f5c51a2 removes     audit re-runs on    snapshot still
 + first audit      the flagged URL     the OLD snapshot    holds Aug-09
     |                   |                   |               content
     v                   v                   v                   |
 FAIL / CRITICAL     fix lands 75 min    E005 REPEATS with       v
 E005 + W011         AFTER the index     a NEW timestamp    ~27h elapsed:
                                         (Gen flips to      still inside
                                          SAFE on the       the ~2-day
                                          same input)       window
```

The 75-minute gap is the whole story: the index captured the repository just
before the fix, and the audit a day later re-ran over that capture. Nothing is
stuck; the next index had not come due.

Note Gen flipping fail→pass across those two audits **on unchanged input**.
Model-based providers re-judge; rule-based ones reproduce. A verdict changing
is therefore not evidence that content changed.

## Reading a disagreement

Work down this list; stop at the first match.

1. **Is the quoted literal in the snapshot?** If yes and it is gone from the
   repository, the input is stale. Check elapsed time before escalating.
2. **Is it in the repository too?** Then the finding is real. Fix or accept it.
3. **In neither?** The scanner is likely serving its own cached result; a
   re-index may not clear it. Say so rather than requesting the wrong remedy.
4. **Verdicts differ across surfaces?** Cache propagation. Re-run later.

Corroborate with the project's own scanner run if it has one: same scanner,
current branch, opposite verdict isolates the input as the variable.

## Preventing it: scan before you publish

Everything above is remediation. The cheap fix is upstream: run the registry's
own scanner in CI so a violation fails your build instead of surfacing as a
public FAIL badge days later, on a snapshot you cannot refresh. This repo's
`.github/workflows/registry-scan.yml` is a working implementation; the design
points that matter are portable.

**Gate on the critical class only.** The scanner has no severity threshold, so
the rule is expressed by enumerating the warning class into
`--ignore-issues-codes`. Warning-class findings are usually accurate
descriptions of what a repository-auditing skill legitimately does; blocking on
them trains people to suppress findings. Surface them (annotations, job
summary, uploaded artifact) and let a human judge.

**Never let an E-code into the ignore list.** One there silently disarms the
gate, and nothing else in the system would tell you. Pin it with a test that
asserts every ignored code starts with `W`, and a second that pins the list to
exactly the published warning class — short of it, a warning reddens the build
and someone "fixes" it by guessing; beyond it, a code nobody read is being
suppressed.

**Prove the gate can go red, on every run.** A check that cannot fail is not a
check, and a scanner's rule catalog changes with no commit of yours. Before the
real scan, build a throwaway skill carrying the exact violation class you care
about, scan it **with the gate's real ignore list**, and fail unless the
scanner both reports the anchor code and exits non-zero. Running it with the
real ignore list is the subtle part: with an empty one you would only prove the
scanner can fail, not that *this gate* can. This also fails upward — if the
anchor rule is renamed or retired, the step goes red and a human re-anchors it
rather than the gate quietly protecting nothing.

**Add a cheap offline guard for the shape you already got burned by.** The
scanner needs a token, network, and minutes. A plain text rule needs none and
runs in the normal test suite, so it blocks the commit rather than the merge.
Assemble any test fixture that must contain the offending shape at runtime, or
the guard will correctly reject your own tests.

**Schedule it.** A weekly run is what notices a catalog change that needed no
commit of yours.

**Make it a required status check.** A gate that can be merged past is
advisory. Confirm it is required in branch protection, not merely present.

Two layers is the goal: a text guard that fails in seconds with no
dependencies, and the real scanner as the authoritative check. The first
catches the mistake while you are typing; the second catches everything you did
not anticipate.

## Evidence log: how each claim was established

Nothing here is from documentation; the registry documents almost none of it.
Re-derive any claim that stops matching reality.

| Claim | How it was established |
|---|---|
| Install never triggers an index or audit | Read the CLI source: the audit call is a `GET` keyed on owner/repo + skill display names, gated only on the repo being public, with `blobResult` absent from the expression. Confirmed empirically: 9 installs over 6 hours moved nothing. |
| The audit key carries no commit SHA | Same source read; the request carries owner/repo and names only. |
| The JSON API has no finding codes | Its `summary` is prose plus a count. Codes appear only on the per-provider HTML pages. |
| A stored snapshot exists and is fetchable | Found `/api/download/...` referenced in the CLI's blob module; fetched it directly and got `{files[], hash}`. |
| The snapshot can be stale | Fetched ci-secure's: 80 files, hash `a2f6d76e…`, containing the removed URL in 4 files and zero occurrences of the placeholder that replaced it. |
| The snapshot can be dated | Read `CHANGELOG.md` out of the snapshot: newest entry 2026-08-09 versus 2026-08-10 on the default branch. |
| Re-indexing happens | ci-speedup was first seen Jul 22 but its snapshot contains `tests/test_secret_redaction.py`, added by PR #15 on Jul 28. Content postdating the first index can only arrive by re-indexing. |
| Findings clear once the fix is in the snapshot | ci-speedup went FAIL/HIGH with W007+W011 (issue #12, scanned Jul 22) to WARN/MEDIUM with W011 only, after the fix appeared in the snapshot. |
| Audits re-run over an unchanged snapshot | ci-secure's audit timestamps advanced ~24h while its snapshot hash stayed `a2f6d76e…` and the finding text stayed byte-identical. |
| Caches disagree | The install-path endpoint served 2026-08-10 values while the JSON API served 2026-08-11 for the same skill. |
| Model-based providers re-judge unchanged input | Gen went HIGH/fail to SAFE/pass across two audits of the same snapshot, and its category list grew rather than shrank. |
| The pages expose no other API | A browser network capture was blocked by egress policy, but the page source is an RSC payload plus Vercel Web Analytics (`/_vercel/insights/*`); no audit or index endpoint is called client-side. |

## Claims deliberately NOT made

Recorded so nobody re-derives a dead end or repeats an overreach:

- **"The scanners read the snapshot."** Consistent with every observation and
  strongly supported by the W007 timing, but never verified. The scanners could
  read the branch through a separate path.
- **"Indexing happens once and never repeats."** Tempting after watching one
  skill sit still for a day. It is false — ci-speedup re-indexed.
- **"Self-hosting the snapshot fixes the audit."** The CLI has an allowlist for
  repos serving their own snapshot, but the audit call does not consult it.
  Being added would change installs, not audits.
- **"Renaming the skill forces a fresh audit."** A new name means a new slug
  means a new identity with no cached audit — and it is not an acceptable
  remedy. It discards the skill's URL, install count, and recognition to work
  around someone else's caching. Do not propose it.
- **"Filing an issue reliably works."** At the time of writing, a search for
  re-index and rescan requests returned a dozen matches, all open and
  unanswered. It costs little, but do not present it as a fix.
