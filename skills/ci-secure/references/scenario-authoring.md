# Attacker-scenario authoring guide

Detailed guidance for [SKILL.md](../SKILL.md)'s scenario phase. For each
finding **group** (one pattern) the orchestrator writes one repo-grounded
`attacker_scenario` onto **every member** of the group in the findings JSON.
`severity` stays catalog-authored — the unfixed attack's potency is a property
of the pattern — but the scenario is not: it depends on the specific repo and
is the report's comprehension mechanism ("What an attacker could do"). Under
the critical-only contract EVERY group renders, so every group gets a
scenario.

## Writing the attacker_scenario

This is the report's "What an attacker could do" row, and it drives
how seriously a reader takes the finding. Write it for a reader who
knows this codebase and how GitHub and GitHub Actions work, **but
nothing about security**. Your job is to make the attack concrete and
understandable to that person — not to sound like a security report.

- **Write only the attack — don't restate the finding.** The TL;DR
  already says what the gap is. This row says who the attacker is, how
  they get in, and what they get. Don't re-explain the vulnerability
  ("NPM_TOKEN is a long-lived credential…", "secrets are at repo
  level…") — the reader just read that.
- **Lead with the access an attacker needs** — the barrier to entry,
  the single most useful thing for triage. Be explicit about where the
  attacker starts: anyone with a GitHub account (a fork PR or a
  comment, zero prior access), someone who can get a pull request
  merged, someone who has taken over a maintainer's account or laptop.
  "Any stranger can reach this" and "needs a maintainer account first"
  are very different — the reader must tell them apart at a glance.
- **Spell out the concrete mechanic, in plain words.** Don't write
  "code execution in a job" or "a foothold" or "exfiltrate" — say
  *how* the attacker's code actually ends up running: they open a fork
  pull request these workflows run; they slip in a dependency (or a
  dependency update) whose install step runs during the job; they get
  a workflow edit merged; they make a job save a cache key another job
  later restores. Then say what that code can then read or do, in
  ordinary language ("read your secrets", "publish to npm as you",
  "push commits to the repo").
- **No unexplained security jargon.** Avoid foothold, code execution,
  exfiltrate, blast radius, supply chain, lateral movement, threat
  actor. If a concept is needed, describe it in GitHub/GHA terms the
  reader already has.
- **Self-contained — never reference another finding.** No "same entry
  as P14.10", no "see the untrusted-trigger finding". Each row must
  stand on its own even if it repeats a sentence of setup.
- **Never soften a vector into a nuisance.** Under the critical-only
  contract every one of the ten patterns *is* an outsider → compromise
  chain, so "there's no real attack here" is never the right scenario —
  if it feels true, the detector fired on something the catalog says it
  shouldn't, and that is a detector bug to surface, not prose to hedge.
  (Honest hedging belongs in the *conditions*: see the next bullet.)
- **Don't treat runner type as a risk factor.** A self-hosted runner
  is not inherently riskier, and ephemeral runners grant no persistent
  foothold. Reason about what the *workflow* exposes, not where it runs.
- **Don't assert repo specifics you didn't verify.** If you didn't
  confirm a job checks out fork code, phrase it as a condition ("*if* a
  step runs the pull request's code…") rather than a fact.
- Keep it to 2–3 sentences, one paragraph (it renders in a table cell).
  Generate one for **every** group — every group renders, always.

## One inline example

For a P14.9 finding (an untrusted trigger checking out the pull request's
code and then running commands in it):

> Anyone can open a pull request from a fork, which starts the test run
> this workflow waits on. When it fires, `secrets.test-workspaces.yml`
> checks out the pull request's own code and then runs commands in that
> directory — install scripts, test setup, whatever the branch contains —
> while holding the repository's token and this workflow's secrets.

Note the shape: who (anyone, no access) → how they get in (a fork pull
request) → what actually runs (commands in the checked-out branch) → what
that reaches (the token and secrets). No jargon, no restating the TL;DR.

More worked examples in this style: the `attacker_scenario` fields in
the `findings.json` a scan writes (worked examples are not shipped with the
skill — live third-party findings never ship).
