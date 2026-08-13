---
name: skills-registry-security
description: >-
  Checks the third-party security audit status of a skill published to a skills
  registry (skills.sh) across every cached surface, decides whether a failing
  finding is real or points at content already removed, and drives a stale badge
  to green unattended. Runs a real install to capture what users see, reads the
  per-provider finding codes the JSON API omits, and greps each flagged literal
  against the repository and shipped tree; emits a next-action verdict, checks
  whether an install can even fire the re-index beacon in this environment, and
  watches on a durable schedule until the registry's own re-audit clears a stale
  finding. Use when a published skill shows a FAIL or CRITICAL audit, when an
  audit looks stale or predates a fix, when a registry badge disagrees with a
  merged change, when deciding whether to request a re-index, before or after
  publishing a flagged skill, or when a Snyk, Socket, or Gen Agent Trust Hub
  verdict needs to be explained or disputed — so no one has to babysit a rescan
  by hand.
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

### Invoking this skill

**This skill has no automatic trigger.** It lives under `maintainers/`, which is
excluded from every installable skill tree, so its frontmatter is never loaded
and its description never fires on its own. It runs only when a maintainer asks
for it by name, or through the bundled slash command:

```bash
ln -sf "$PWD/maintainers/skills-registry-security/commands/registry-security.md" \
       .claude/commands/registry-security.md    # then: /registry-security <owner/repo/skill>
```

`.claude/` is gitignored, so that symlink is per-checkout; the command source is
tracked under `commands/` here. The unattended loop below is armed the same way
— by a maintainer session — and then runs server-side without one.

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

**Gone from the repository *and* the snapshot — check which of two cases this
is before saying anything.** They look identical and need opposite responses,
and the discriminator is one comparison: **is the finding's `auditedAt` older
than the time the snapshot hash last changed?**

- **`LAGGING` — the audit predates the snapshot.** The ordinary post-fix state:
  the fix landed, the snapshot caught up, the audit has not read it yet. The
  literal is missing from the snapshot *because the fix worked*. Wait for the
  next sweep — measured at ~22 h, unattended — and re-check. Do not escalate,
  do not re-install, nothing is stuck.
- **`PHANTOM` — the audit has run against this snapshot and still cites the
  literal.** Only now is a scanner-side cache the explanation, and a re-index
  alone may not clear it. Escalate with the evidence.
- **`PHANTOM_OR_LAGGING` — you did not pass `--snapshot-changed-at`.** The
  script refuses to guess. Supply the time the hash last changed; until then
  assume the lagging case, which is far more common and costs only patience.

Getting this backwards has already happened once here: a `PHANTOM` verdict sent
an investigation toward an escalation packet four hours before the audit re-ran
on its own and cleared the finding. Corroborate with the code split — warning
codes reproducing while a critical code vanishes is the signature of stale
input, not of a phantom.

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

## Drive it to green unattended (the operator loop)

This is the point of the skill: **you own the outcome, not the human.** A stale
badge takes hours-to-days to clear (re-index ~3h, re-audit ~22h behind it, and
the registry's own sweep runs ~daily), so you must WATCH it yourself and surface
only when something changes — never hand the human a timer to babysit.

The script emits a **`NEXT ACTION` decision** (`--json` puts it under
`decision`) so a watch loop can act without anyone reading the report:

| decision | what it means | what you do |
| --- | --- | --- |
| `RESOLVED` | every provider passes, no outstanding literal | report green, **stop watching** |
| `ACTION_REQUIRED` | a cited literal is still in HEAD — a real finding | surface it with the evidence; fix the code or accept it with a rationale; **stop watching** |
| `MONITOR` | a provider still fails but every cited literal is gone from HEAD | **nothing to fix** — keep watching until the registry re-audits it away |
| `DISAGREEMENT` | cached surfaces disagree | re-check shortly before trusting either |
| `UNVERIFIED` | something could not be read, so nothing can be concluded — the status API was unreachable, it returned no audit records at all (nobody has scanned this skill yet), a finding detail page failed to load, or there is no local corpus to classify literals against | read `why`: supply `--repo-root`/`--ref` if it names the corpus, otherwise **keep watching** and re-run — never treat this as green |

**The watch MUST run on a durable server-side Routine, never an in-session
timer.** In a suspending session, `CronCreate`, `ScheduleWakeup`, and a
background `sleep` loop do **not** fire while the turn is idle — the loop stalls
until a human re-pings (this is the failure that turned one real run into ~31
manual re-pings). Arm `mcp__Claude_Code_Remote__create_trigger` with a cron that
matches the timings (every few hours is plenty) and a prompt that re-runs the
script and acts on `decision`. **Surface to the owner only when the decision
CHANGES** (to `RESOLVED` / `ACTION_REQUIRED` / `DISAGREEMENT`) or a max-elapsed
cap is hit; on an unchanged `MONITOR`, re-arm silently. **Disable the Routine the
moment it resolves.**

On a `MONITOR`, branch on the beacon (the `decision.beacon` block):
- **Beacon can fire AND no beacon-capable install has happened since the fix** →
  run ONE ordinary install to nudge the re-index, then watch the snapshot hash.
- **Beacon suppressed** (the common CI/sandbox case — the `github probe` isn't
  200, e.g. a 403) → do **NOT** loop installs; an install is a no-op here. Just
  watch the registry's own re-audit cadence, which clears a stale finding on its
  own. Looping suppressed installs manufactures a false "installs do nothing"
  record — the exact trap this skill exists to avoid.

**Escalation is a last-resort nudge, not a fix — and it is optional.** A
re-index issue on `vercel-labs/skills` (the registry's repo, not yours) is a
long shot: recent re-scan requests sit open and unactioned, and in the run this
skill came from the finding cleared on the registry's *own* cadence, not from
any issue. So **draft** such an issue for the owner only if they ask, **never
file it automatically**, and never present it as "the fix."

**Rails (non-negotiable):**
- **Any research subagent you spawn is READ-ONLY by default** — say verbatim in
  its prompt: no `add_repo`/attach, no push, no comments, no issues; read via
  public WebFetch/WebSearch for any repo outside scope (e.g. `vercel-labs/skills`).
  Never add the constraint reactively after a push-access prompt appears.
- **Never rename or re-slug a published skill to force a fresh scan.** It breaks
  the skill's identity, installs, and marketing. The artifact's identity is not a
  variable; exclude it from every remediation menu.
- **Compute every elapsed-time and "when did X happen" from a fetched clock**
  (the `auditedAt`/snapshot timestamps and the fix commit time), never a narrated
  estimate.
- **Before concluding, run the evidence checklist:** `git blame`/history on the
  flagged lines, your own CI's scanner output on `main` (same scanner — strong
  corroboration), the finding detail page, and the actual network calls. Don't
  speculate about content you can read.

## Gotchas

These are the traps that make this task take hours instead of a minute. Most of
them look like a bug in your own reasoning when you hit them cold.

- **The install's *audit lookup* never triggers a re-scan — the install's
  *beacon* is what rebuilds the snapshot.** The CLI's audit call is a plain GET
  keyed on `owner/repo` plus the skill slug, with no commit SHA anywhere in it,
  so it reads a cache and changes nothing. The separate telemetry beacon does
  enqueue a re-index — but only when it can fire (see *Forcing a refresh*).
  Hence: one beacon-capable install is worth running once; re-installing after
  it is worth nothing, and installing from a beacon-suppressed environment is
  worth nothing at all. Nine suppressed installs over six hours moved the
  snapshot hash zero times.

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
