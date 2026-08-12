---
name: skills-registry-security
description: >-
  Checks the third-party security audit status of a skill published to a skills
  registry (skills.sh), across every cached surface at once, and decides whether
  a failing finding is real or points at content that was already removed. Runs
  a real install to capture what users actually see, reads the per-provider
  finding codes the JSON API does not expose, and greps each flagged literal
  against the repository and the shipped tree. Use when a published skill shows
  a FAIL or CRITICAL security audit, when an audit looks stale or predates a fix,
  when someone asks why a registry badge disagrees with a merged change, when
  deciding whether to request a re-index, or before or after publishing a skill
  that has been flagged. Also use when a Snyk, Socket, or Gen Agent Trust Hub
  verdict on a published skill needs to be explained or disputed.
---

# Registry security status for a published skill

A registry stores a skill's audit in several places that drift apart, and the
verdict a user sees at install time is not the one the JSON API returns. Worse,
a re-audit can run against a stored snapshot rather than the current default
branch, so a finding can survive long after the code it describes is deleted.
Answering "is this failure real?" therefore means reading four surfaces and then
proving, by grep, whether the flagged string still exists.

`scripts/registry_audit.py` does all of that concurrently. Run it first; read
the rest of this file only when the output needs interpreting.

## Quick start

```bash
python3 maintainers/skills-registry-security/scripts/registry_audit.py \
    starslingdev/skills/ci-secure
```

Roughly 40 seconds, most of it the install. Add `--no-install` for a ~3 second
check when only the cached verdicts matter. `--json` emits the same data
machine-readably; `--ref` picks the git ref to verify literals against
(default `origin/main`).

The script is read-only: HTTP GETs, a throwaway install into a temp directory,
and greps over a local checkout. It never POSTs to the registry and never
writes to the repository.

## What it reports, and how to read it

The output has six parts. Read them in this order, because each one narrows
the question:

1. **Provider status** — status, risk level, and `auditedAt` per provider from
   the JSON API. A fresh `auditedAt` proves only that scanners ran, not that
   current code was read; keep going.
2. **Install-path cache** — the separate endpoint the CLI reads. This is what
   users see. It has lagged the API by a full day.
3. **Fresh install output** — the verbatim risk table the CLI prints.
4. **Stored snapshot** — the `skillsComputedHash` and file count of the content
   the registry has on hand. This is the scanners' actual input.
5. **Findings** — the codes (`E005`, `W011`, …) scraped from the per-provider
   pages, plus every literal each finding quotes, classified against all three
   corpora.
6. **Verdict** — which of the cases below applies, with the evidence to quote.

## Deciding what to do

Each literal is classified by comparing what the skill *is* (the git ref and the
installed tree) against what the scanner was *handed* (the stored snapshot).
Those two questions have opposite remedies, so the classification is the whole
decision:

**`REAL` — present in current content.** The scanner is describing today's
skill. Fix it, or accept it with a written rationale; a re-scan will keep
reporting it, correctly. Warning-class codes are frequently accurate
descriptions of what a skill legitimately does, and accepting one is a valid
outcome. Staleness is not the explanation here.

**`STALE_INPUT` — gone from the repository, still in the stored snapshot.**
Nothing to patch. The scanner is right about its input and the input is old.
Requesting a plain re-audit is the trap: re-auditing the same snapshot
reproduces the finding. Ask for a **re-index**, and say the word, because these
are different operations and only one helps. The script prints the snapshot
hash and the stale file paths — quote both, since they let a maintainer confirm
the claim with a single request against their own service and without access to
your repository.

**`PHANTOM` — gone from the repository *and* the snapshot.** The scanner is
likely serving a cached result of its own, so a re-index alone may not clear it.
Say that explicitly rather than asking for the wrong remedy.

**Surfaces disagree.** The reconciliation section fires. The audit may be fine
while users still see a stale badge. This resolves as caches catch up; confirm
by re-running rather than by acting.

If the project runs the same scanner in its own CI, a passing run on the current
default branch is a strong supporting exhibit — same scanner, opposite verdict,
which isolates the input as the variable.

## Forcing a refresh when the input is stale

Confirmed by experiment, not inference: **one install that reaches the telemetry
service rebuilds the snapshot.** There is no button, no API, and no ticket
required — but there is a precondition, and getting it wrong is what makes this
look impossible.

1. **Verify the beacon can actually fire.** On the machine doing the install:

   ```bash
   curl -sS -o /dev/null -w '%{http_code}\n' https://api.github.com/repos/OWNER/REPO   # need 200
   env | grep -iE 'DO_NOT_TRACK|DISABLE_TELEMETRY'                                     # need empty
   ```

   The CLI decides whether the repo is private with an unauthenticated call to
   that endpoint and treats any non-OK response as "cannot tell", which silently
   suppresses the install event. Sandboxes, CI containers, and rate-limited IPs
   routinely fail this. An install with a suppressed beacon is indistinguishable
   from a registry that ignores installs, which is exactly the wrong conclusion
   to reach.

2. **Run one ordinary install** — `npx skills add OWNER/REPO`. Nothing special,
   no flags. One is enough; repeating it does not speed anything up.

3. **Watch the snapshot hash**, not the audit timestamps. Re-index for an
   already-indexed skill took **~3 hours** in the observed case (a first index
   of a brand-new skill was faster, ~50 minutes). The audit then re-runs behind
   it on its own cadence and the finding clears.

Do not escalate before doing this. Most "the registry is stuck" reports are
really "no beacon-capable install has happened since the fix landed."

## Gotchas

These are the traps that make this task take hours instead of a minute. Most of
them look like a bug in your own reasoning when you hit them cold.

- **Installing never triggers a re-scan.** The CLI's audit call is a plain GET
  keyed on `owner/repo` plus the skill slug, with no commit SHA anywhere in it.
  Installing repeatedly, waiting, and re-installing accomplishes nothing.
  Polling on a timer is wasted effort — check once, then act.

- **Merging a fix does not invalidate anything.** Because the cache key carries
  no SHA, pushing the fix to the default branch leaves the audit untouched.
  Expect no change from a merge alone.

- **The JSON API does not carry finding codes.** It returns only a summary
  string like `Risk: CRITICAL · 2 issues`. The codes and the quoted literals
  live exclusively on the per-provider HTML pages. Any workflow that reads only
  the API cannot tell a real finding from a phantom one.

- **Timestamps come in two formats on the same payload** — some providers emit
  `...Z`, others `...+00:00`. Compare parsed datetimes, never strings.

- **A re-audit is not a re-index.** A refresh can re-run the scanner against the
  stored snapshot, producing a brand-new `auditedAt` on a finding derived from
  old content. A fresh timestamp is therefore not evidence that current code was
  read — fetch the snapshot and check it directly instead of trusting the clock.

- **A rescan can make the case *harder* to argue.** While the audit predated the
  fix, "the scan is older than the commit" was enough. After a rescan on a stale
  snapshot, the finding carries a current timestamp and reads as "your fix did
  not work". Expect to need the snapshot as evidence, not the timestamps.

- **Install and audit are fed by different pipelines.** Installing can clone the
  current default branch while the audit reads the stored snapshot, so a clean
  installed tree is entirely compatible with a failing audit. Neither one proves
  anything about the other.

- **Deterministic and model-based providers fail differently on stale input.** A
  rule-based scanner reproduces its previous finding exactly; a model-based
  reviewer may reverse itself on identical content. If a rule-based critical
  finding survives a rescan even though its literal is gone, the input did not
  change — that pattern is itself evidence.

- **Warning-class and critical-class codes are different animals.** A single
  critical code drives the whole verdict; warning codes usually describe
  inherent behavior and often should be accepted rather than chased.

- **Check the sibling skills too.** Registry sweeps have re-audited the skills
  that did not change while skipping the one that did. Comparing timestamps
  across every skill in the same repository exposes that inversion, and it is
  strong evidence for an escalation.

- **A finding can be about a test fixture.** Vendored third-party workflows and
  deliberately malicious-looking fixtures get scanned like real code. These are
  usually false positives, but they are genuinely present in the tree, so the
  script reports them as `present` — that tag means "really there", not
  "really a problem".

- **A URL whose host ends in `.sh` is not a shell script.** Rules about download
  URLs key on the *path* ending in `.sh`, `.ps1`, or `.bash`. A documentation
  link to a `.sh` domain is a false alarm when triaging by hand.

- **Install counters do not reflect your installs.** They appear deduplicated,
  so a rising count is not confirmation that an install registered.

## Deeper reference

`references/audit-pipeline.md` is the model behind all of this: an ASCII map of
the two pipelines, what each timestamp does and does not mean, observed timings
with an escalation threshold, two fully worked timelines, how to prevent the
problem in CI, and an evidence log recording how every claim was established
plus the overreaches that were ruled out. Read it when deciding whether to wait
or escalate, when a timing question comes up, or before repeating any of the
reverse-engineering.

`references/registry-surfaces.md` documents each surface's exact URL shape and
response schema, and the finding-code classes. Read it when the script's parsing
breaks — for example after a registry redesign changes the page markup — or when
adding a provider.
