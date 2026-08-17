# StarSling Skills

Public agent skills from StarSling, for auditing your **GitHub Actions** CI.

```bash
npx skills add starslingdev/skills
```

Pick the skills you want and your agent (Claude Code, Codex, Cursor, …). Add
`-g` to install for every project instead of just this one. Each section below
gives the one-line command for that skill on its own.

| Skill | What it answers | | |
|---|---|---|---|
| [**ci-secure**](#ci-secure) | Can someone attack me through my CI? | [jump ↓](#ci-secure) | [overview](https://starsling.dev/ci-secure) |
| [**ci-speedup**](#ci-speedup) | Why is my CI slow? | [jump ↓](#ci-speedup) | [overview](https://starsling.dev/ci-speedup) |
| [**ci-score**](#ci-score) | Is my CI following best practices? | [jump ↓](#ci-score) | [overview](https://starsling.dev/ci-score) |

All three run from a local checkout and send your code, logs, and findings
nowhere. They need **`python3` 3.9+** and **PyYAML** (`pip install pyyaml`);
`ci-speedup` also needs **`gh auth login`**, because it reads your real run
history.

---

## ci-secure

**Finds the ten ways an outsider can take over your GitHub Actions and steal
your secrets.**

```bash
npx skills add starslingdev/skills --skill ci-secure
```

Then invoke it by name (**`/ci-secure`**, or `$ci-secure` in Codex), or just
ask *"is my CI secure?"*

It scans your workflows for the [ten critical CI/CD attack
vectors](skills/ci-secure/references/security-patterns.md) — template injection
in `run:` blocks, fork code executed with privileges (pwn requests),
`pull_request_target` jobs that poison the shared cache, impostor action SHAs,
whole-context secret dumps, `$GITHUB_ENV` / `$GITHUB_PATH` hijack,
`pull-requests: write` on untrusted triggers, credential files swept into caches
and artifacts, unverified remote code execution (`curl | bash` and mutable
fetch-and-run), and dependency install scripts running in a job that holds
secrets.

Every finding names the file and line, quotes the evidence from your own
workflow, and explains in plain English **what an attacker could do** with it.
Then the skill offers to fix the ones you pick, leaving the diff in your working
tree to review — it never commits, pushes, or opens a PR. It also reports
pass/fail **config hygiene checks** (declared `permissions:`, CODEOWNERS on
`.github/workflows/`, `secrets: inherit`, credential-persisting checkouts).

**It is deliberately not comprehensive, and renders no security score.** The
catalog is a closed set of ten, each a complete outsider → compromise chain with
a real incident behind it — the admission test and what was rejected are in
[why-these-ten.md](skills/ci-secure/references/why-these-ten.md). Findings are
open doors and hygiene checks are armor; they move independently, so neither is
a grade. **Zero findings is a first-class result**, and a check that could not
run is reported as *did not run*, never as a pass. Runs in seconds; `gh` is
optional, used only for the impostor-SHA check and dormant-workflow notes.

What a run looks like, without installing anything:
[starsling.dev/ci-secure](https://starsling.dev/ci-secure).

---

## ci-speedup

**Measures what actually makes your CI slow, from your own run history.**

```bash
npx skills add starslingdev/skills --skill ci-speedup
```

Then invoke it by name (**`/ci-speedup`**, or `$ci-speedup` in Codex), or just
ask *"why is my CI slow?"*

It reports on two measured axes, never blended into one number:

- **Developer wall-clock wait** — the merge-gating critical path, the slowest
  checks a PR waits on. This is the ranking axis, the thing engineers feel.
- **Runner-minutes** — the cloud bill, sized per finding. A fix can cut the bill
  while adding developer wait, so the two stay separate.

The numbers come from **real run history**: per-job P50/P95 sampled over the
`gh` API, the critical path re-derived from your actual job graph. Findings come
from a [catalog](skills/ci-speedup/references/optimization-patterns.md) of 70+
patterns across 14 categories — missing caches, redundant setup, sleep-based
readiness, unsharded test jobs, full-history checkout, queue time, and more.

**It diagnoses; it does not prescribe.** A generic tool can't see a file's
intent or whether a "waste" is deliberate, so every finding ships a
ready-to-paste **agent prompt** that hands the measured root cause to your own
coding agent, which reads the real logs and git history before shaping a change.
Detection, ranking, and every measured number are deterministic.

What a run looks like, without installing anything:
[starsling.dev/ci-speedup](https://starsling.dev/ci-speedup).

---

## ci-score

**Grades your CI configuration against best practices, and hands back the
fixes.**

```bash
npx skills add starslingdev/skills --skill ci-score
```

Then invoke it by name (**`/ci-score`**, or `$ci-score` in Codex), or just ask
*"grade my CI"*. Runs fully offline.

Eleven configuration facts — dependency caching, shallow checkout, test
sharding, concurrency cancellation, path filters, job timeouts, action pinning,
OIDC token scoping, and more — each self-verifiable in your own workflow YAML in
under a minute. The score is checks passed over checks applicable; the report
ranks one fix per failed check by impact × risk, each with a recipe and a
ready-to-paste agent prompt.

**It measures adherence, not speed** — a faster repo can hold a lower score, and
the report says so beside the card. It is **not a security audit** either: two
of the eleven checks happen to be security-related, and it claims nothing
further. For speed use [`ci-speedup`](#ci-speedup); for security,
[`ci-secure`](#ci-secure).

What a run looks like, without installing anything:
[starsling.dev/ci-score](https://starsling.dev/ci-score).

---

## What a ci-speedup run costs

It defaults to the repo you're in, confirms the target first, then samples your
recent runs. Measured: **314 `gh` API calls in ~51s** on `pallets/flask`; 824
calls in ~3m on `microsoft/playwright`; ~1,800 in ~8m on a next.js-sized
monorepo. The full markdown report is **opt-in** — pick *"Save the full report"*
and it writes `./ci-speedup-findings-report.md`. See
[SECURITY.md](SECURITY.md) for the data-handling model.

## Learn more

**ci-secure** — [SKILL.md](skills/ci-secure/SKILL.md) ·
[the ten-vector catalog](skills/ci-secure/references/security-patterns.md) ·
[why these ten](skills/ci-secure/references/why-these-ten.md)

**ci-speedup** — [SKILL.md](skills/ci-speedup/SKILL.md) ·
[methodology](docs/methodology.md) ·
[pattern catalog](skills/ci-speedup/references/optimization-patterns.md) ·
[provenance](PROVENANCE.md) · [sample report](examples/)

**ci-score** — [SKILL.md](skills/ci-score/SKILL.md) ·
[rubric write-up](skills/ci-score/references/ci-score-methodology.md) ·
[frozen registry (v0.1.3)](skills/ci-score/references/ci-score-spec.json)

## For maintainers

The self-improvement loop infrastructure lives **outside** the installable
skill, under [`maintainers/ci-speedup/`](maintainers/ci-speedup/), so the
`skills` CLI never copies it into an install; it runs locally via Claude Code,
never as a GitHub Action. See
[MAINTAINERS.md](maintainers/ci-speedup/MAINTAINERS.md).

This repo's CI runs on [StarSling Runners](https://starsling.dev/). Fork PRs run
the identical suite on GitHub-hosted runners, so untrusted code never executes
on the self-hosted ones. See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE)
