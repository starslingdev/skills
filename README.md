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

| Skill | What it answers |
|---|---|
| 🏎️ [**ci-speedup**](#ci-speedup) | Why is my CI slow? |
| 📋 [**ci-score**](#ci-score) | Is my CI config following best practices? |
| 🔒 [**ci-secure**](#ci-secure) | Can someone attack me through my CI? |

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

Learn more: [starsling.dev/ci-speedup](https://starsling.dev/ci-speedup)

---

## ci-score

📋 **Grades your CI configuration against best practices, and hands back the
fixes.**

```bash
npx skills add starslingdev/skills --skill ci-score
```

Then invoke it by name (**`/ci-score`**, or `$ci-score` in Codex), or just ask
*"grade my CI"*. Runs fully offline.

Eleven configuration facts (caching, shallow checkout, sharding, concurrency
cancellation, path filters, timeouts, action pinning, OIDC scoping and more),
each checkable in your own workflow YAML in under a minute. The score is checks
passed over checks applicable; every failed check gets one ranked fix.

**It measures adherence, not speed**, and it is **not a security audit**: two
of the eleven checks are security-adjacent, and it claims nothing further.

Learn more: [starsling.dev/ci-score](https://starsling.dev/ci-score)

---

## ci-secure

🔒 **Finds ten critical attack vectors an outsider could exploit, and fixes
them.**

```bash
npx skills add starslingdev/skills --skill ci-secure
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

Learn more: [starsling.dev/ci-secure](https://starsling.dev/ci-secure)

---

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
