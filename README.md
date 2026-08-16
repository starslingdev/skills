# StarSling Skills

Public agent skills from StarSling. Each skill lives in its own self-contained
directory under `skills/<name>/` and installs individually.

This repo ships three skills: **`ci-speedup`**
([overview](https://starsling.dev/ci-speedup)), **`ci-score`**, and
**`ci-secure`**.

## ci-speedup: measured CI audits for GitHub Actions

`ci-speedup` audits a repository's GitHub Actions workflows and produces a
**root-cause-analysis report** of what actually makes your CI slow, on two
measured axes:

- **Developer wall-clock wait**: the merge-gating critical path, the slowest
  checks a pull request waits on before it can go green. This is the ranking
  axis, the thing engineers feel.
- **Runner-minutes**: the cloud bill. Sized per finding and reported as a
  secondary axis (a fix can cut the bill while adding developer wait, so the two
  are never blended into one number).

It works from **real run history**, not estimates: per-job P50/P95 timings
sampled over the `gh` API, the critical path re-derived from the actual job
graph. Findings come from a
[catalog](skills/ci-speedup/references/optimization-patterns.md) of **70+
optimization patterns across 14 categories** (missing caches, redundant setup,
sleep-based readiness, unsharded test jobs, full-history checkout, dead env
vars, build-cache misconfig, queue time, and more) plus a structural track
routed from the measured long pole.

**ci-speedup diagnoses; it does not prescribe the fix.** A generic tool can't
see a file's intent or whether a "waste" is deliberate, so instead of a
baked-in diff, every finding ships a ready-to-paste **agent prompt** that hands
the measured root cause to *your own* coding agent, which reads the real logs and
the file's git history before shaping a safe change. Detection, ranking, and
every measured number are deterministic; the only place an LLM steps in is a
log-grounded gap-fill when a drilled pole matches no catalog detector.

## ci-score: a best-practice grade for your CI config

`ci-score` grades a repository's GitHub Actions **configuration** against CI best
practices and hands back concrete fixes for every gap. The **CI Score** is a
plain pass/fail rubric — eleven configuration facts (dependency caching,
shallow checkout, test sharding, concurrency cancellation, path filters, job
timeouts, action pinning, OIDC token scoping, and more), each self-verifiable in
the repo's own workflow YAML in under a minute. The score is checks passed over
checks applicable; the report ranks one fix per failed check by impact × risk,
each with a fix recipe and a ready-to-paste **agent prompt**.

**It measures adherence, not speed.** A faster repo can hold a lower score, and
the report says so beside the card — never read the score as a speed verdict.
It is also **not a security audit**: exactly two of the eleven checks (action
pinning, job-scoped OIDC tokens) happen to be security-related, and it claims
nothing further. Everything runs locally from a checkout — **no network access,
nothing sent anywhere.** For measured wall-clock and runner-minute audits, that
is a different question — use `ci-speedup`.

## ci-secure: the ten critical CI/CD attack vectors

`ci-secure` scans a repository's GitHub Actions workflows for the **ten critical
CI/CD attack vectors** — template injection in `run:` blocks, fork code executed
with privileges (pwn requests), `pull_request_target` jobs that poison the shared
cache, impostor action SHAs, whole-context secret dumps, `$GITHUB_ENV` /
`$GITHUB_PATH` hijack, `pull-requests: write` granted to untrusted triggers,
credential files swept into caches and artifacts, unverified remote code
execution (an installer piped straight into a shell, or a git tree fetched at
a branch or tag and then run), and dependency install scripts running in a job
that holds secrets.
Every finding comes with the exact file and line, evidence taken from your
own workflow, and a plain-English **"what an attacker could do"** scenario —
then the skill offers to fix the ones you pick, dispatching a subagent per
finding group that applies the
[catalog](skills/ci-secure/references/security-patterns.md) recipe
and leaves the diff in your working tree to review. It never commits, pushes, or
opens a PR on its own. Alongside the findings it reports a short set of pass/fail **config hygiene
checks** (declared `permissions:`, CODEOWNERS coverage of `.github/workflows/`,
`secrets: inherit`, credential-persisting checkouts, and more).

**It is deliberately not comprehensive, and it renders no security score.** The
catalog is a closed set of ten vectors, each a complete outsider → compromise
chain with a real incident behind it (the admission test and the rejection record
are in
[references/why-these-ten.md](skills/ci-secure/references/why-these-ten.md));
every report repeats *"critical exploit-chain checks only — this is not a
comprehensive audit."* There is no grade, ratio, or `N/100` anywhere a reader
sees: findings are open doors and hygiene checks are armor, they move
independently, and neither is a score. **Zero findings is a first-class result**
— a clean run says so plainly instead of padding with lesser observations, and a
check that could not run is always reported as *did not run*, never as a pass.
The scan runs locally in seconds from your checkout; `gh` is optional and used
only for the impostor-SHA check and for noting which findings sit in dormant
workflows.

### Keeping it fixed: ci-secure as a CI check

A scan tells you what is wrong today. It does nothing about next week, which is
when the workflow gets edited. Ask for the check and ci-secure sets itself up to
run on every pull request:

> install ci-secure as a CI check

1. It **copies** the scanner into your repository — under `ci-secure/`, plus one
   workflow — and hands you a pull request to review and merge. Nothing is
   downloaded at build time, so the code judging your pull requests is code you
   can read, and it changes only when you ask for an update.
2. The **first run reports without blocking**. A repository that has never been
   scanned usually has two or three findings, and the setup pull request should
   not brick your merge path on day one.
3. When you have fixed those, **you** make it blocking: delete the `--advisory`
   flag from the workflow and add `ci-secure` to your required checks. From then
   on a failed security check stops the merge.
4. If you ever need out, remove `ci-secure` from your required checks — one
   settings change, reversible. Not deleting the workflow, which would leave you
   believing you have a check you do not.

Your CI re-checks the copied files against a manifest of hashes on every run, so
a local edit somebody made while debugging and never removed shows up instead of
quietly weakening the check. Asking for an update re-copies the code and leaves
your workflow file alone — the runner, the triggers and the flag you deleted are
yours.

## Install

```bash
npx skills add starslingdev/skills
```

Select the skill, your agent (Claude Code, Codex, Cursor, …), and an install
scope. The [`skills`](https://github.com/vercel-labs/skills) CLI (built by
Vercel) is fetched fresh via `npx`, so you always get its latest version.

Then invoke it with your agent:

```bash
# Claude Code
/ci-speedup      # or /ci-score, /ci-secure

# Codex
$ci-speedup      # or $ci-score, $ci-secure
```

## First run

**Requirements:**

- An authenticated **GitHub CLI** (`gh auth login`) — **required by `ci-speedup`
  only.** Its run-history data pass reads the audited repo's Actions
  run/job/log data through read-only `gh` API calls; a token with read access to
  the repo's Actions is enough. `ci-score` never uses the network at all, and
  `ci-secure` treats `gh` as optional: with it, the impostor-SHA check, the
  dormancy notes, and the two config facts read over the API run; without it,
  the scan still runs and the report says which checks went unmeasured.
- **`python3` (3.9 or newer)** and **PyYAML**: the bundled scripts are
  stdlib-only apart from one third-party dependency, **PyYAML**, which the
  scanner uses to parse your workflow YAML. Install it with `pip install pyyaml`
  (or `python3 -m pip install pyyaml`).

**What `ci-speedup` does:** it defaults to the repo you're in (and confirms the
target with you first), samples your recent CI runs over the `gh` API, and closes by leading
with the biggest measured lever, the slowest check gating your merge and how much
developer wait it costs, then lets you pick what to fix. The full markdown report
is **opt-in**: it is rendered and integrity-checked internally on every run, and
**"Save the full report"** is one of the offered options. Pick it and the
verified report is written to `./ci-speedup-findings-report.md` in your working
directory (a generated artifact you can gitignore or delete).

**Cost / timing (measured on a mid-size OSS repo):** the data pass made **314
`gh` API calls in ~51s** (the committed `pallets/flask` example). Larger repos
sample more (the `microsoft/playwright` example: 824 calls in ~3m 07s; a
very-high-volume monorepo like next.js measured ~1,800 calls in ~8m); the pass
is frugal by design
(one workflow list, one total-count per workflow, one log per pole, and an
adaptive two-pass job-list sample). It sends **nothing** to StarSling or any
third party. See [SECURITY.md](SECURITY.md) for the data-handling model.

## Learn more

**ci-secure:**

- [`skills/ci-secure/SKILL.md`](skills/ci-secure/SKILL.md): the full skill
  contract.
- [`skills/ci-secure/references/security-patterns.md`](skills/ci-secure/references/security-patterns.md):
  the ten-vector catalog — what each vector is, how it is detected, and its fix
  recipe. Every finding in a report links back into it.
- [`skills/ci-secure/references/why-these-ten.md`](skills/ci-secure/references/why-these-ten.md):
  the admission test for the catalog, and the record of what was rejected.

**ci-score:**

- [`skills/ci-score/SKILL.md`](skills/ci-score/SKILL.md): the full skill
  contract.
- [`skills/ci-score/references/ci-score-methodology.md`](skills/ci-score/references/ci-score-methodology.md):
  the rubric write-up — what each of the eleven checks means and why it is
  graded.
- [`skills/ci-score/references/ci-score-spec.json`](skills/ci-score/references/ci-score-spec.json):
  the frozen CI Score registry (v0.1.3).

**ci-speedup:**

- [`docs/methodology.md`](docs/methodology.md): how the numbers are measured
  (P50 over sampled runs, the merge-gating critical path, measured vs modeled,
  adaptive sampling, why it diagnoses instead of prescribing).
- [`skills/ci-speedup/SKILL.md`](skills/ci-speedup/SKILL.md): the full skill
  contract.
- [`skills/ci-speedup/references/optimization-patterns.md`](skills/ci-speedup/references/optimization-patterns.md):
  the pattern catalog.
- [`skills/ci-speedup/references/wall-clock-methodology.md`](skills/ci-speedup/references/wall-clock-methodology.md)
  and [`savings-methodology.md`](skills/ci-speedup/references/savings-methodology.md):
  the in-skill methodology references.
- [`PROVENANCE.md`](PROVENANCE.md): how the detection catalog was developed and
  validated.
- [`examples/`](examples/): a sanitized sample report showing the output shape.
- [starsling.dev/ci-speedup](https://starsling.dev/ci-speedup): the skill's
  landing page — what a run does, walked through end to end, without cloning
  anything.

## For maintainers

The skill's self-improvement loop infrastructure (the gap→catalog, transcript,
and dogfood loops) lives **outside** the installable skill, under
[`maintainers/ci-speedup/`](maintainers/ci-speedup/), so the `skills` CLI never
copies it into an install. It runs locally via Claude Code only, never as a
GitHub Action. See [`maintainers/ci-speedup/MAINTAINERS.md`](maintainers/ci-speedup/MAINTAINERS.md).

This repo's CI runs on [StarSling Runners](https://starsling.dev/). Fork PRs
run the identical suite on GitHub-hosted runners (the `test (fork)` job in
[`ci.yml`](.github/workflows/ci.yml)); untrusted code never executes on the
self-hosted runners. See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE)
