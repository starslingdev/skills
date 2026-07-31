# StarSling Skills

Public agent skills from StarSling. Each skill lives in its own self-contained
directory under `skills/<name>/` and installs individually.

This repo ships two skills: **`ci-speedup`**
([overview](https://starsling.dev/ci-speedup)) and **`ci-score`**.

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
/ci-speedup

# Codex
$ci-speedup
```

## First run

**Requirements:**

- An authenticated **GitHub CLI** (`gh auth login`): the run-history data pass
  reads the audited repo's Actions run/job/log data through read-only `gh` API
  calls. A token with read access to the repo's Actions is enough.
- **`python3` (3.9 or newer)** and **PyYAML**: the bundled scripts are
  stdlib-only apart from one third-party dependency, **PyYAML**, which the
  scanner uses to parse your workflow YAML. Install it with `pip install pyyaml`
  (or `python3 -m pip install pyyaml`).

**What it does:** it defaults to the repo you're in (and confirms the target with
you first), samples your recent CI runs over the `gh` API, and closes by leading
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
run the identical suite on GitHub-hosted runners
([`ci-fork.yml`](.github/workflows/ci-fork.yml)); untrusted code never executes
on the self-hosted runners. See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE)
