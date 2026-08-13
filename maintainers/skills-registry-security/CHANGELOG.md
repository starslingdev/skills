# Changelog — skills-registry-security

Maintainer-only. Not part of any installable skill tree.

## Fixed

- **2026-08-13** — **An unscanned skill is no longer reported as a passing one,
  and the operator loop no longer contradicts itself about installs.** Three
  defects found in adversarial review. (1) `decide()` gated on the status API's
  `http`/`error` fields but not on its contents, so a 200 carrying an empty
  `audits` list — nobody has scanned this skill yet, the normal state of a
  just-published one, which is precisely when this skill gets run — produced
  zero failing providers and resolved to `RESOLVED`, stopping an unattended
  watch before the first scan ever landed. It now returns `UNVERIFIED` saying
  no provider has reported. (2) The Gotchas claim "Installing never triggers a
  re-scan … polling on a timer is wasted effort — check once, then act"
  contradicted both *Forcing a refresh* (one beacon-capable install rebuilds the
  snapshot) and the operator loop (watch on a durable Routine; on `MONITOR` with
  a live beacon, run one install). It is the pre-beacon-experiment belief the
  skill exists to refute, and an agent executing the loop would have hit it as
  authoritative guidance. Reworded to split the install's cached audit *lookup*
  (changes nothing) from its telemetry *beacon* (enqueues a re-index when it can
  fire). (3) The decision table described `UNVERIFIED` as only the no-local-corpus
  case and prescribed `--repo-root`, though `decide()` also emits it for an
  unreachable API, an API with no records, and an unreachable finding-detail
  page — for which that instruction is a no-op. The row now covers all four and
  says never to treat the verdict as green.

## Added

- **2026-08-13** — **The skill moves from one-shot diagnostic to unattended
  operator: it now emits a next-action verdict and checks whether an install can
  even fire the re-index beacon.** `decide()` reduces a run to one of `RESOLVED`
  / `ACTION_REQUIRED` (a cited literal is still in HEAD — a real finding) /
  `MONITOR` (the finding is stale, nothing to fix, wait for the registry's own
  re-audit) / `DISAGREEMENT` / `UNVERIFIED`, so a watch loop can act without a
  human reading the report. `beacon_precondition()` checks the GitHub-repo probe
  (must be 200) and the telemetry env vars up front, so a suppressed-beacon
  environment (the 403 sandbox/CI case) is never mistaken for "installs don't
  trigger a re-audit" — the false conclusion that turned a prior run into a
  ~two-day manual drive. SKILL.md gains a "Drive it to green unattended" section:
  a durable server-side Routine (never an in-session timer, which stalls when the
  session suspends), surface-only-on-decision-change, the beacon-suppressed
  branch (monitor the registry's own cadence rather than loop no-op installs),
  escalation reframed as an optional long-shot draft (never auto-filed, never
  "the fix"), and rails — read-only research subagents by default, no
  rename/re-slug remediation, fetched-clock timing, and an evidence checklist
  before concluding. A `/registry-security` slash command runs the whole thing
  against a target, since the skill lives in `maintainers/` and is therefore
  never installed or auto-discovered. Tests pin `decide()`'s branches,
  `beacon_precondition()`'s suppression paths, and the rendered NEXT ACTION.

- **2026-08-13** — **An unread surface is no longer reported as a clean badge.**
  Three defects found in review, all the same shape: `decide()` classified from
  provider status and literal verdicts alone and never consulted the `error` /
  `http` fields the fetchers already record, so it could not tell "verified
  clean" from "never fetched". An unreachable provider API returned no
  providers, which read as nothing-failing and resolved to `RESOLVED` — an
  unattended watch stopping on a badge it never actually read. A failing
  provider whose finding-detail page 503'd landed in the benign "keep watching"
  branch instead of being flagged unclassifiable. Both now return `UNVERIFIED`
  naming the unreachable surface. Separately, the risk half of the
  failing-provider test read `riskLevel`, a key the fetchers never write (they
  store `risk`), so it was dead code and a HIGH/CRITICAL provider whose status
  read pass-or-blank scored as clean; the test fixtures carried the same typo,
  so the suite confirmed the bug rather than catching it. Fixtures now use the
  shape `fetch_api` really emits.

- **2026-08-11** — **The stored snapshot is now read directly, which turns the
  central claim from an inference into an artifact.** `GET
  /api/download/{owner}/{repo}/{skill}` returns the pre-built snapshot the
  registry keeps and the scanners read, as real file contents plus a
  `skillsComputedHash`. Searching a finding's quoted literal in those bytes
  answers "what was the scanner actually handed?" instead of inferring it from
  what the repository no longer contains. Literals are now classified three
  ways — `REAL` (still in current content, the finding stands), `STALE_INPUT`
  (gone from the repository but still in the snapshot, so the input needs
  refreshing), and `PHANTOM` (gone from both, so the scanner is likely serving
  its own cached result) — because those three cases have different remedies
  and asking for the wrong one wastes days. A `STALE_INPUT` verdict prints the
  snapshot hash and the offending paths, which is exactly what a maintainer
  needs to confirm the claim against their own service without access to the
  repository.

- **2026-08-11** — **The skill exists.** `scripts/registry_audit.py` answers
  "is this registry security failure real?" in one command by reading all four
  cached surfaces concurrently, running a throwaway install to capture the risk
  table users actually see, scraping the per-provider finding codes that the
  JSON API does not expose, and grepping every literal a finding quotes against
  both a git ref and the freshly installed tree. A literal absent from both is
  reported as `PHANTOM`, which is the signal that the scan input is stale rather
  than the code being wrong. Doing this by hand took an afternoon; the script
  takes about seven seconds with `--no-install` and about forty with it.

- **2026-08-11** — **The gotchas that cost the most time are written down.**
  SKILL.md records that installing never promptly triggers a re-scan, that the
  audit cache key carries no commit SHA so merging a fix invalidates nothing,
  that the JSON API carries no finding codes, that timestamps arrive in two
  formats on one payload, and — the expensive one — that a re-audit can re-run
  scanners against a stored snapshot, so a fresh `auditedAt` is not evidence
  that current code was read. `references/registry-surfaces.md` carries the URL
  shapes, response schemas, and the `E`/`W` finding-code split.

- **2026-08-13** — **The chain is now observed end to end, so "wait" is a
  procedure with a number on it rather than advice.** A watched cycle closed
  every link on one skill: beacon-capable install → snapshot rebuilt ~3 h later
  → **audit re-ran unattended ~22 h after that** → Snyk went fail/CRITICAL with
  E005+W011 to warn/MEDIUM with W011 alone. Nothing was requested at any step
  after the install. Added a *When a re-audit happens* section and three
  measured rows: re-index → re-audit ~22 h, audit-to-audit 24–28 h (loose
  enough that a missed day means nothing), and all three providers re-stamping
  within 38 s, which shows audits are one batched per-skill sweep rather than
  independent provider schedules. The practical rule that falls out: after an
  install, budget ~3 h for the snapshot and up to another day for the badge,
  and do not poll in between — twenty polls across that window returned a
  byte-identical payload every time and established nothing. Left explicitly
  open whether a re-index *enqueues* the audit or the sweep is purely
  periodic; 22 h fits both.

- **2026-08-11** — **The re-index versus re-audit distinction is load-bearing.**
  Asking for a plain re-audit of a stale snapshot reproduces the same finding,
  so the skill names re-indexing explicitly as the thing to request, and lists
  the three artifacts that make such a request verifiable without access to the
  repository: the finding code with its quoted literal, the commit that removed
  that literal, and an `auditedAt` proving the scan ran afterwards.

- **2026-08-11** — **Retracted "installs never trigger indexing", which was the
  costliest wrong conclusion in the original investigation.** The nine-install
  experiment behind it ran in an environment where `api.github.com` answers
  403; the CLI reads a non-OK response there as "cannot determine visibility"
  and silently drops the install beacon, so the experiment exercised a code
  path that never fired. The beacon is what triggers content ingest, and ingest
  appears repo-scoped. Documented the precondition check to run *before*
  concluding anything from an install, since a suppressed beacon looks
  identical to a registry that ignores installs. Added a twenty-second summary
  and an annotated real timeline at the top of the pipeline reference — the
  earlier draft was accurate but too dense to act on. Recorded the dead ends
  with their evidence (a live but orphaned `check-updates` endpoint, an
  auth-gated `revalidate` hook, origin-side rather than edge staleness, ignored
  ref parameters) so they are not re-derived, and the fact that the indexer
  drops files above roughly 500-600 KB, which means a snapshot is not a
  faithful copy of the folder.

- **2026-08-13** — **Retracted "gone from repo and snapshot means the scanner
  cached it", the mirror image of the original mistake.** `verify_literals`
  classified any literal absent from both corpora as `PHANTOM` — scanner at
  fault, re-index will not help, escalate. That verdict was produced for a real
  finding and an escalation packet was drafted on it; four hours later the
  audit re-ran on its own and cleared the finding. Absence from both is equally
  consistent with the ordinary post-fix state: the fix landed, the snapshot
  caught up, and the audit had not read it yet. The original error was reading
  a fresh timestamp over stale content as "the fix did not work"; this is the
  same error with the operands swapped. The verdict now splits three ways on
  whether the audit predates the snapshot — `LAGGING` (wait, do not escalate),
  `PHANTOM` (the scanner has read this snapshot and still cites the literal, so
  escalate), and `PHANTOM_OR_LAGGING` when the new `--snapshot-changed-at` is
  not supplied, which refuses to guess and names the flag that would decide it.
  The oldest provider stamp is used, not the newest: a literal is only phantom
  if *every* scanner citing it has read the current snapshot.

- **2026-08-12** — **The remedy is confirmed by experiment, so the skill now
  states it as a procedure rather than a hypothesis.** One install whose
  telemetry beacon actually reaches the service rebuilds the stored snapshot;
  nine installs from an environment where the beacon was suppressed did
  nothing. Same repository, same skill, the beacon the only difference — which
  makes the precondition check the single most valuable line in the document,
  since a suppressed beacon and an unresponsive registry look identical from
  outside. Recorded the measured latency: re-indexing an already-known skill
  took about three hours, against roughly fifty minutes to first-index a new
  one, so a couple of quiet hours is not evidence of failure and the guidance
  is to budget half a day before concluding an install did not work.
