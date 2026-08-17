<div align="center">

# StarSling Skills

Agent skills from StarSling, for optimizing your GitHub Actions.

[![skills.sh](https://skills.sh/b/starslingdev/skills)](https://skills.sh/starslingdev/skills)

[Website](https://starsling.dev/skills)

</div>

```bash
npx skills add starslingdev/skills
```

Pick the skills you want and your agent (Claude Code, Codex, Cursor, …). Add
`-g` to install for every project instead of just this one. Each section below
gives the one-line command for that skill on its own.

| Skill | What it answers | Source |
|---|---|---|
| 🏎️ [**ci-speedup**](#ci-speedup) | Why is my CI slow? | [`skills/ci-speedup/`](skills/ci-speedup/) |
| 📋 [**ci-score**](#ci-score) | Is my CI config following best practices? | [`skills/ci-score/`](skills/ci-score/) |
| 🔒 [**ci-secure**](#ci-secure) | Can someone attack me through my CI? | [`skills/ci-secure/`](skills/ci-secure/) |

Each run ends the same way: your agent offers to fix the findings you pick, or
to just save the full report as markdown. Fixes land in your working tree for
you to review, and nothing is ever committed, pushed, or opened as a PR.

All three run from a local checkout and need **`python3` 3.9+** and **PyYAML**
(`pip install pyyaml`). `ci-speedup` also needs **`gh auth login`**, because it
reads your run history over the GitHub API. That data stays on your machine:
**nothing is sent to StarSling**, and nothing is sent to any third party
([data handling](SECURITY.md)).

---

## ci-speedup

🏎️ **Measures what actually makes your CI slow, from your own run history.**

```bash
npx skills add starslingdev/skills --skill ci-speedup
```

Or paste this into your agent:

```text
Run `npx skills add starslingdev/skills --skill ci-speedup` to install or
update the ci-speedup skill.
```

Then invoke it by name (**`/ci-speedup`**, or `$ci-speedup` in Codex), or just
ask *"why is my CI slow?"*

It reports two axes, never blended: **developer wall-clock wait** (the
merge-gating critical path, and the axis findings rank on, because it is what
engineers feel) and **runner-minutes** (the bill). The numbers come from real
runs: per-job P50/P95 sampled over the `gh` API, with the critical path
re-derived from your actual job graph, then matched against a
[catalog](skills/ci-speedup/references/optimization-patterns.md) of 70+ patterns.

**It diagnoses; it does not prescribe.** A generic tool can't tell deliberate
from wasteful, so each finding ships a ready-to-paste prompt handing the root
cause to your own agent, which reads the logs and git history before changing
anything.

Example reports from real runs:
[`pallets/flask`](examples/pallets-flask/ci-speedup-findings-report.md) and
[`microsoft/playwright`](examples/microsoft-playwright/ci-speedup-findings-report.md).

Source: [`skills/ci-speedup/`](skills/ci-speedup/) · [SKILL.md](skills/ci-speedup/SKILL.md) · Learn more: [starsling.dev/ci-speedup](https://starsling.dev/ci-speedup)

---

## ci-score

📋 **Grades your CI configuration against best practices, and hands back the
fixes.**

```bash
npx skills add starslingdev/skills --skill ci-score
```

Or paste this into your agent:

```text
Run `npx skills add starslingdev/skills --skill ci-score` to install or
update the ci-score skill.
```

Then invoke it by name (**`/ci-score`**, or `$ci-score` in Codex), or just ask
*"grade my CI"*. Runs fully offline.

Eleven configuration facts (caching, shallow checkout, sharding, concurrency
cancellation, path filters, timeouts, action pinning, OIDC scoping and more),
each checkable in your own workflow YAML in under a minute. The score is checks
passed over checks applicable; every failed check gets one ranked fix.

**It measures adherence, not speed**, and it is **not a security audit**: two
of the eleven checks are security-adjacent, and it claims nothing further.

Example report from a real run:
[`pallets/flask`](examples/pallets-flask/ci-score-report.md), scoring 89/100.

Source: [`skills/ci-score/`](skills/ci-score/) · [SKILL.md](skills/ci-score/SKILL.md) · Learn more: [starsling.dev/ci-score](https://starsling.dev/ci-score)

---

## ci-secure

🔒 **Finds ten critical attack vectors an outsider could exploit, and fixes
them.**

```bash
npx skills add starslingdev/skills --skill ci-secure
```

Or paste this into your agent:

```text
Run `npx skills add starslingdev/skills --skill ci-secure` to install or
update the ci-secure skill.
```

Then invoke it by name (**`/ci-secure`**, or `$ci-secure` in Codex), or just
ask *"is my CI secure?"*

It scans for the [ten critical attack
vectors](skills/ci-secure/references/security-patterns.md): template injection,
fork code run with privileges, cache poisoning, impostor action SHAs, secret
dumps, `$GITHUB_ENV` hijack, write tokens on untrusted triggers, credentials in
caches and artifacts, unverified remote code execution, and install scripts
running beside secrets. Each finding names the file and line, quotes your own
workflow, and says in plain English **what an attacker could do**. It then
offers to fix the ones you pick, leaving the diff in your tree. It never
commits, pushes, or opens a PR.

**Deliberately not comprehensive, and no security score.** Ten closed-set
vectors, each a full outsider → compromise chain with a real incident behind it
([why these ten](skills/ci-secure/references/why-these-ten.md)). **Zero findings
is a first-class result**, and a check that could not run says *did not run*,
never "pass".

Example reports from real runs over one workflow file in
`snowflakedb/snowflake-connector-net`, scanned at two commits:
[3 findings before the vendor's
fix](examples/snowflakedb-snowflake-connector-net/ci-secure-report-4a1b8ce.md),
and [0 findings
after](examples/snowflakedb-snowflake-connector-net/ci-secure-report-1dc7766.md).

### ci-secure CI check

A scan says what is wrong today. The CI check runs the same engine on every
pull request, so the findings do not come back. Paste this:

```text
Run `npx skills add starslingdev/skills --skill ci-secure` to install or
update the ci-secure skill, then install ci-secure as a CI check in this repo.
```

It is **vendored, never fetched**: the engine, the gate and the licence are
copied into your repository, so the code judging your PRs is code you can read
and it cannot change underneath you. It is two files plus the engine:
[`scaffold/gate.py`](skills/ci-secure/scaffold/gate.py), which decides pass or
fail, and [`scaffold/ci-secure.yml`](skills/ci-secure/scaffold/ci-secure.yml),
the workflow that runs it. [`scripts/vendor.py`](skills/ci-secure/scripts/vendor.py)
is what copies them in. Because that writes into your working
tree, the skill says exactly what it will write and asks for a yes first, works
on a branch as one setup PR, and never pushes without asking.

It ships in `--advisory` mode, so it does not affect your merge path on day
one. It becomes blocking when you drop `--advisory` and add the check to your
repository's required checks.

Source: [`skills/ci-secure/`](skills/ci-secure/) · [SKILL.md](skills/ci-secure/SKILL.md) · Learn more: [starsling.dev/ci-secure](https://starsling.dev/ci-secure)

---

## For maintainers

Maintainer-only infrastructure lives **outside** the installable skill trees,
under `maintainers/`, so the `skills` CLI never copies it into an install:

- [`maintainers/ci-speedup/`](maintainers/ci-speedup/): the self-improvement
  loops, which run locally via Claude Code and never as a GitHub Action.
  See [MAINTAINERS.md](maintainers/ci-speedup/MAINTAINERS.md).
- [`maintainers/ci-score/`](maintainers/ci-score/): maintainer notes for
  ci-score, including what was deliberately left out of the shipped skill.
  See [MAINTAINERS.md](maintainers/ci-score/MAINTAINERS.md).
- [`maintainers/ci-secure/`](maintainers/ci-secure/): the disclosure rule for
  findings from a scan of a repository you do not own, and the guard that
  enforces it. See [MAINTAINERS.md](maintainers/ci-secure/MAINTAINERS.md).
- [`maintainers/skills-registry-security/`](maintainers/skills-registry-security/):
  triage for a failing registry security audit, answering whether a finding
  describes the skill as it is today or a stale copy.
  See [README.md](maintainers/skills-registry-security/README.md).

This repo's CI runs on [StarSling Runners](https://starsling.dev/). Fork PRs run
the identical suite on GitHub-hosted runners, so untrusted code never executes
on the self-hosted ones. See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE)
