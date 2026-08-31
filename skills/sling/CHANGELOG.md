# Changelog

All notable changes to the `sling` skill. Unversioned; dated (UTC).

## [Unreleased]

### Added

- **2026-08-26** — **The launch-announcement prompts are pinned in the routing
  eval.** "why did CI fail on my last push", "what's breaking the nightly
  build", and "is this test actually broken or just flaky" are published as
  example prompts, so they join `prompt-routing.json` as required rows a
  description edit cannot silently drop (30 rows; the floor and the required
  pins in the guard moved with them).

- **2026-08-25** — **Initial skill.** Routes GitHub Actions questions between
  `sling` (StarSling's read-only, agent-first CLI) and `gh`, so an agent stops
  defaulting to whichever tool it already knows. Ships the routing table, the
  `sling doctor` / `sling login` preflight, the `--agent` machine-mode
  contract, the exit-code recovery table, and the handoff rule that sends
  repo-wide audits to `ci-score`, `ci-speedup` and `ci-secure` instead of
  looping `sling` over every run.
- **2026-08-25** — **A command reference read off the binary, not the docs**
  ([`references/command-reference.md`](references/command-reference.md)). Every
  flag list and JSON shape was captured by running `sling` v0.1.2 with
  `--agent` against a live control plane. Three places where the published
  documentation and the binary disagree are recorded with the binary as the
  answer. (An entry here previously called `sling logs` "the largest"
  disagreement, claiming the docs describe it as streaming raw text in both
  modes — that was wrong, corrected 2026-08-26: the docs document the
  agent-mode JSON envelope. The real, smaller discrepancy is the docs calling
  the envelope one-object-per-line when it is pretty-printed.)
- **2026-08-25** — **A pinned capture of the command surface**
  ([`references/command-surface.json`](references/command-surface.json)) and
  the tests that hold the skill to it. The skill exists to stop an agent
  inventing a `sling` subcommand, so its own routing table is guarded against
  doing the same; the guard has teeth because a wrong command name really does
  survive being printed by the tool — `sling doctor` on v0.1.2 suggests
  `sling update` as its fix, and the binary rejects `sling update` as unknown.
  The reference says so rather than passing the suggestion along.

- **2026-08-25** — **A Gotchas section, an eval set, and a contents block**,
  from a pass against the official skill-authoring best practices and the
  internal authoring guide. The Gotchas section collects the places where
  `sling` behaves differently from what its own `--help`, its output, or its
  documentation implies — the highest-signal content in a skill about driving
  a CLI, and previously scattered through the body.
  [`evals/evals.json`](evals/evals.json) adds five behavioral scenarios,
  including the two the skill must DECLINE, so a later change cannot quietly
  turn the handoff rule into an approximation. The 300-line command reference
  gained a contents block, since long reference files get previewed with
  partial reads.

### Changed

- **2026-08-31** — **Re-pinned every version-pinned fact against `sling`
  v0.1.8** (what the installer serves since 2026-08-31) and **documented
  `doctor`'s new `agent_skill` check**: advisory in all three shapes
  (installed / agent-without-skill / no agent), never a health failure, and
  a row an agent following this skill must report rather than act on — its
  own copy of the skill demonstrably exists. Also corrected two stale
  preflight claims from the 0.1.2 era: an upgrade notice now reads
  `warn: true` (not `skipped: true`), and the `version` `fix_command` is the
  real installer one-liner (a shell pipeline to surface to the user), no
  longer a fake subcommand. All prior probes re-verified unchanged on
  0.1.8: `--compact` absent, `sling update` unknown, `logs` rejects
  `--org`, empty scope-flag exits `2`, `exit-codes` lists only `0`–`5`.


- **2026-08-27** — **Re-pinned every version-pinned fact against `sling`
  v0.1.5** (the version the installer now serves; 0.1.3, 0.1.4 and 0.1.5
  shipped across 2026-08-26/27, all release- or test-plumbing only). Re-verified on the new binary:
  `--compact` still absent, `sling logs` still rejects `--org`/`--repo`
  (exit `2`), an empty scope-flag value still exits `2`, `sling exit-codes`
  still lists only `0`–`5`, `sling update` still does not exist.
  `command-surface.json` now records `cli_version: "0.1.5"`.
- **2026-08-26** — **The `doctor` `fix_command` gotcha is rewritten around
  the surviving lesson.** The 0.1.2 bug it described is fixed upstream: the
  `version` check now emits the installer one-liner (shown as a placeholder —
  this skill ships no literal `curl | sh`), plus `warn: true` on an
  advisory upgrade notice, documented in the reference's doctor shape. The
  rule stays: never run a `fix_command` unchecked — other checks emit
  templates with unfilled placeholders (`git remote add origin
  <github-url>`).

### Fixed

- **2026-08-26** — **The parse contract was falsified by the binary, in the
  direction that loses data.** SKILL.md said a genuine error writes nothing to
  stdout and told an agent to fall back to stderr only when stdout was empty.
  Verified against v0.1.2: `sling logs` on a job that stores no logs exits `3`
  and still prints `{"lines": [], "has_more": false, …}`, and `sling resolve`
  on an ambiguous id exits `2` and still prints `{"candidates": […]}`. An agent
  following the old rule parses that empty `lines` array as "nothing in the
  log" and reports a clean run — the silent false negative the skill exists to
  prevent. The rule is now "branch on the exit code, never on whether stdout
  looks empty", stated once, and the Exit-codes preamble no longer contradicts
  it.
- **2026-08-26** — **Exit `4` was routed to `sling login` before the app gate
  was reached.** The preflight's auth step took *any* exit `4` as a stale
  credential, but the missing-GitHub-App shape (`You don't have access to org
  "<name>"`) exits `4` too — and step 5, which handles it, comes later. A user
  whose app was never installed was sent round a login loop that cannot change
  the outcome. The auth step now excludes the shape whose stderr names an org.
- **2026-08-26** — **The `--org` gotcha described the opposite failure.** A
  scope flag with no value is rejected, not swallowed: `sling usage --org
  --window 7d` exits `2` with `--org needs a slug (got an empty value).` The
  reference had it exiting `0` with a wrongly-scoped answer that looks fine.
- **2026-08-26** — **A nonexistent subcommand exits `1`, not `2`.** The
  exit-`1` row read only "a crash … a bug report, not a routing decision",
  which is where the skill's own `sling doctor` `fix_command` gotcha lands. It
  now names the `unknown command` shape and says to correct the name.
- **2026-08-26** — **`sling resolve` on a run covering several jobs is
  ambiguous** — exit `2`, with the candidate list on stdout and on stderr.
  Undocumented, on the command the routing table reaches for whenever a user
  pastes an Actions URL.
- **2026-08-26** — **`--compact` has no observable effect on v0.1.2**, so
  `--agent` output is indented JSON. Described as exact flag equivalence
  before, which invited a line-oriented parser.
- **2026-08-26** — **A private repository name shipped in the skill.**
  A private test repository's name appeared in an eval prompt and in the
  `resolve` sample payload; both now use placeholders.
- **2026-08-26** — **`--agent` was described as belonging on "every
  invocation"** in the reference, contradicting SKILL.md's rule that it must
  never reach `sling login` and needs an explicit slug on `sling org switch`.

- **2026-08-25** — **The output-stream rule was wrong for the two exit codes
  that are not errors, and it broke the skill's own preflight.** The `--agent`
  section said a non-zero exit means stdout is empty and stderr carries the
  message. Verified against the binary: `sling doctor --agent` on an
  unreachable control plane exits `10` and writes every check to stdout with
  stderr empty. An agent following the old rule would have skipped that JSON
  and been unable to do what the preflight asks two paragraphs later — act on
  the check that is `ok: false` — and would have discarded every partial
  `why` / `time` result at exit `6`. The rule is now: parse stdout first
  whatever the exit code, and fall back to stderr only when stdout is empty.
- **2026-08-25** — **"Pass `--agent` on every invocation" made authentication
  unreachable.** `--agent` implies `--no-input`, so it turns a prompt into
  exit `2`. Following the auth recovery — "`sling login`, then retry once" —
  with `--agent` attached refuses the device-code prompt, lands on the usage
  row, and loops without ever signing in. `--agent` is now scoped to data
  commands, with the two interactive ones called out.
- **2026-08-25** — **Signing in is a human step, and the skill now says so
  everywhere.** `sling login` prints a device code, opens a browser approval
  page, and blocks until a person approves it. An agent cannot do that: as a
  subprocess the output arrives only once the command has finished or timed
  out, so the code is never visible while it is still usable, and `--agent`
  (which carries `--no-input`) refuses the prompt outright with exit `2`.
  The preflight now asks the user to run it in their own terminal and
  confirms with `doctor` afterwards; the exit-`4` recovery row and the
  command reference say the same, and no shipped example pairs `sling login`
  with `--agent`. Two tests pin it, because this is the failure that leaves a
  user signed out and the session hung.
- **2026-08-25** — **The invention guard missed invented subcommands under
  real commands, and never looked at the description.** It reported only the
  first word when a pair was unknown, so `sling bill export` and `sling logs
  tail` passed because `bill` and `logs` are real — the exact class it exists
  to catch. It also scanned only SKILL.md's body, and the frontmatter
  description was at that moment shipping `sling runs`, which is not a command
  (only `runs list` / `runs show` are). The detector now reads command
  mentions out of code spans rather than prose, and the description and every
  shipped reference are scanned alongside the body.
- **2026-08-25** — **`gh run delete` was in the fallback reference but missing
  from the routing table**, and the two mutating-verb guards disagreed about
  whether `trigger` counts as a state change. Both now derive from one list.
- **2026-08-25** — **`--org` was documented as both taking and not taking a
  value.** `sling <cmd> --help` renders it as `[--org | --repo <r>]`, which
  reads as a bare switch; it is not, and `sling usage --org --window 7d` is a
  bad invocation.

- **2026-08-25** — **The `sling update` gotcha is now dated to the release it
  applies to.** It is real on 0.1.2, which is what the installer serves, and
  already fixed on the CLI's main branch — where the same check emits the
  installer one-liner. Saying so keeps the note from outliving the bug and
  teaching users to distrust a `fix_command` that has become correct.

- **2026-08-25** — **The precondition the skill never checked: the StarSling
  GitHub App.** `sling` reports CI that the app collected, so a perfectly valid
  login sees nothing for an org the app was never installed on. It surfaces as
  exit `4` with `You don't have access to org "<name>"`, and the CLI's own
  message ends `Run \`sling login\`` — which cannot work, because the
  credential was never the problem. The skill's exit-`4` row said the same
  thing, so an agent following it would have looped on login exactly as the
  misleading message invites. It now reads the stderr before acting, and the
  preflight carries a **StarSling gate** in the shape ci-speedup's gh gate
  already uses: stop, say what is unavailable in the user's own terms, give the
  install path, and degrade only if they say so. Personal repositories are
  named as unsupported, since the app installs there but StarSling never picks
  up the jobs.
- **2026-08-25** — **An empty listing is a coverage hole, not a finding.**
  `sling runs list --repo <name>` returns `{"runs": []}` at exit `0` both for a
  repo with no runs in the window and for one StarSling is not watching, and
  the payload cannot distinguish them — so reporting "you have no CI runs"
  turns a missing installation into a false statement about the user's repo.
  Adopts ci-secure's rule for a check that could not run: an absent result is a
  coverage hole, never a pass.

- **2026-08-25** — **The `gh` half of the routing table gets the same gate the
  `sling` half has**, reusing ci-speedup's rather than a thinner re-derivation.
  It previously carried a one-line aside about `gh auth status`, which missed
  three things that pattern already encodes: whether `gh` is **installed** at
  all (a different failure with the same consequence), the concrete path
  (https://cli.github.com, then `gh auth login`), and the caution ci-speedup
  recorded from a live miss — a sandboxed agent shell cannot reach keyring
  credentials, so one failed probe is not proof and must never be reported as
  an expired login. The degraded path when `sling` is unavailable now runs that
  gate before promising `gh`, since with neither CLI there is nothing left to
  fall back to.

- **2026-08-25** — **The most ordinary first-run state was unhandled, and the
  step next to it gave wrong advice.** Someone who installs `sling` and signs in
  before installing the GitHub App has no orgs at all: login succeeds, `whoami`
  succeeds, and then every data command exits `2` with `You don't belong to any
  orgs yet.` The skill had no account of that message, so it fell through to the
  exit-`2` row and read as a bad flag. Worse, the preflight's wrong-org step was
  keyed on `doctor`'s `org` check — which fails for *both* "pick one of several"
  and "you have none" — so a brand-new user was sent to `sling org switch
  <slug>` to choose from an empty list. The gate now recognises both shapes of a
  missing installation (exit `4` for an org you cannot reach, exit `2` for
  having none), gives them the same answer, and the wrong-org step hands the
  empty case off instead of claiming it.

- **2026-08-26** — **Four claims the binary contradicts, and the guards that
  let them through.** A review pass run against `sling` itself rather than its
  docs found: `--org`/`--repo` documented as global on every subcommand when
  `sling logs` rejects both with exit `2` — and `logs` is the command the
  routing table reaches for most, so the wrong-org recovery turned a working
  read into a usage error; `--agent` described as an alias for
  `--json --compact --no-input --no-color --yes`, which no help page states and
  whose `--compact` does not exist in this binary at all; `--target` offered as
  the way to resolve an ambiguous id, when it biases the kind and returns the
  same ambiguity; and exit `4`'s missing-app reading applied to a message a
  mistyped slug returns byte-identically, which would have sent someone to
  install a GitHub App on an organization that does not exist.
- **2026-08-26** — **The guards were scoped to the whole document, so the
  centrepiece could be deleted silently.** Removing the entire StarSling gate —
  both failure shapes, the stop, the install path, the organizations-only rule —
  left every test green, because each phrase they searched for also occurs in
  the exit-code table or a reference. Inverting the empty-listing rule into the
  false finding it exists to prevent also passed, as did stripping the gh gate's
  install check and sandbox caution from both files. Each guard now reads the
  section it protects, and each of those three mutations now fails.

- **2026-08-26** — **The description is now the one that measured best, not the
  one that read best.** Two live dogfoods asked "why did this job fail?" with an
  Actions URL and the skill never fired — the agent grepped a whole job log with
  `gh`, the exact behaviour this skill replaces. Rewrites to the documented
  gentle shape (capability clause + short "Use when") still measured 0% in
  direct `claude -p` probes, while an imperative claim over the default path —
  invoke this BEFORE `gh`, before fetching logs — fired 4/4 with the skill as
  the first tool call, and both near-miss negatives (a repo-wide grade, a
  workflow-YAML question) still routed away correctly. The shipped description
  is the tested string verbatim, and the guard pins its measured properties.
  One measurement caveat is recorded with it: the skill-creator eval harness's
  positive control failed (an explicit "use the sling skill" showed 0%), so its
  numbers were discarded and every figure above comes from direct probes.

- **2026-08-26** — **A pasted URL from a third-party org fails with exit `3`,
  not exit `4` — found in live dogfood.** The resolver searches only orgs the
  user belongs to, so a foreign Actions URL reports not-found rather than
  no-access; the exit-4 app-install remedy never applies there, and following
  it would tell the user to install a GitHub App on someone else's
  organization. The exit-3 row and the app gate now name the shape and route it
  to the `gh` read fallback, which is what the live session correctly
  improvised.

- **2026-08-26** — **A pr-review-toolkit round (four agents) over the full
  diff.** The comment analyzer, running ~40 read-only probes against the
  binary, REFUTED the skill's flagship claim: the published docs do document
  `sling logs`' agent-mode JSON envelope, so the "largest docs-vs-binary
  disagreement" never existed — corrected here and in the earlier entry, with
  the real (smaller) discrepancy recorded. Also fixed from that round: the
  `--target` advice the changelog claimed fixed but the files never received;
  a second private-repo leak (a product repo's name and the org's live spend
  in sample payloads — cloaked, denylisted, red-proven); the reference
  contradicting SKILL.md on the `logs` strict parser; `skipped: true`'s
  second meaning (not-checked, under an outage); and README/SECURITY data-
  handling text that still said "nothing is sent to StarSling" above a skill
  whose design is to query StarSling.
- **2026-08-26** — **The silent-failure hunter walked the undefined states.**
  Verified live and now documented: `why`/`time` on a bare multi-job run id
  exit `2` with candidates on stdout and NOTHING on stderr (the exit-2 row
  said "read the stderr first"); `why` on a run URL can diagnose one selected
  job at exit `0` on a cancelled three-job run — confirm the job set before
  reporting it as the cause; compound `doctor` failures need control_plane
  precedence, or an outage reads as "please sign in"; a third party's org via
  `--repo` reaches the exit-4 path the URL carve-out missed; exits `4` and
  `7` now have terminal branches instead of dangling retries; a misconfigured
  `SLING_HOST` exits `1` and is an environment fix, not a bug report; there
  is no client-side timeout, so a black-holed connection hangs — wrap and
  report the timeout; and the empty-listing coverage hole covers `jobs list`,
  `usage` and `time --repo` (which exits `6` even on full data).
- **2026-08-26** — **The mutation sweep's gaps closed.** 13 of 20 probes had
  passed green: step 2, step 6, the exit-row semantics (row 10 could be
  inverted to "file a bug"), the handoff section, the compound-ask rule, ten
  of eleven gotchas, the platform claim, the casing rule, and the routing
  artifact (24 of 27 rows inert) were all unguarded prose. Each now has a
  section-scoped guard; three vacuous assertions (`"gh"` satisfied by
  "GitHub", a `not`-anywhere exemption on the login pairing, a bare
  `"before"`) were replaced with shape-anchored ones, and a skip-instead-of-
  fail became a fail.

### Notes

- **The install command is described, not printed.** `sling` installs by
  fetching and running a shell script, and a literal of that shape in shipped
  skill text is what two registry security scanners rate CRITICAL — the reason
  the repo carries a guard against it. The preflight points at the
  installation page instead of pasting the command.
- **No Windows build exists** (Apple Silicon macOS and x64 glibc Linux only),
  so the preflight says so plainly and falls back to the `gh` read path there.
