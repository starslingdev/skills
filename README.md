<div align="center">

# StarSling Skills

Agent skills for faster, safer, and easier-to-debug GitHub Actions.

[![skills.sh](https://skills.sh/b/starslingdev/skills)](https://skills.sh/starslingdev/skills)

[Website](https://starsling.dev/skills)

</div>

```bash
npx skills add starslingdev/skills
```

Choose the skills and agent you want when prompted. Add `-g` to install them
for every project instead of only the current one, or use a skill-specific
command from the sections below.

## Choose a skill

| Skill | Use it when you want to… | Coverage | Reads | Skill file |
|---|---|---|---|---|
| 🏎️ [**ci&#8209;speedup**](#ci-speedup) | Find what is slowing CI down or wasting runner-minutes | 90+ optimization patterns | Local workflows and GitHub run history | [`SKILL.md`](skills/ci-speedup/SKILL.md) |
| 📋 [**ci&#8209;score**](#ci-score) | Grade CI configuration against best practices | 10+ configuration checks | Local checkout | [`SKILL.md`](skills/ci-score/SKILL.md) |
| 🔒 [**ci&#8209;secure**](#ci-secure) | Find critical, exploitable GitHub Actions attack paths | 10+ critical attack vectors | Local workflows; optional GitHub API checks | [`SKILL.md`](skills/ci-secure/SKILL.md) |
| 🛰️ [**sling**](#sling) | Understand usage, performance, billing, and CI activity across your StarSling account | 15+ account and CI commands | Your StarSling account | [`SKILL.md`](skills/sling/SKILL.md) |

The first three audit a repository. They produce a report, then offer to fix
the findings you select or save the report as Markdown. Fixes stay in your
working tree for review: the skills do not commit, push, or open a pull request
unless you explicitly ask.

`sling` is different: it requires a StarSling account and gives you the
operational view of that account—usage, performance, billing, runs, failures,
runner capacity, and more. It reads the CI data StarSling already collects
rather than auditing workflow files in a checkout. The CLI is read-only;
requests that change CI state are routed to the GitHub CLI.

## Requirements and data handling

| Skill | Requirements | Network and data handling |
|---|---|---|
| <code>ci&#8209;speedup</code> | Python 3.9+, PyYAML, authenticated `gh` | Reads GitHub run history through the GitHub API |
| <code>ci&#8209;score</code> | Python 3.9+, PyYAML | Fully offline |
| <code>ci&#8209;secure</code> | Python 3.9+, PyYAML; `gh` optional | Runs locally; `gh` enables four additional checks |
| `sling` | StarSling account, `sling` CLI, login, and StarSling GitHub App | Queries your account through StarSling's control plane |

The repository-audit skills do not send audit results or repository contents
to StarSling. `sling` sends the organization, repository, and run identifiers
you ask about and stores its credential at `~/.config/sling/credentials`. See
the full [data-handling policy](SECURITY.md).

---

## ci-speedup

🏎️ **Measure what actually makes CI slow, using your own run history.**

**[Open the ci-speedup skill file →](skills/ci-speedup/SKILL.md)**

```bash
npx skills add starslingdev/skills --skill ci-speedup
```

Invoke it as `/ci-speedup` (`$ci-speedup` in Codex), or ask: *“Why is my CI
slow?”*

`ci-speedup` reports two separate measures:

- **Developer wall-clock wait:** the merge-gating critical path, which findings
  are ranked by.
- **Runner-minutes:** the compute bill and capacity consumed.

The numbers come from real runs. The skill samples per-job P50/P95 timing via
the GitHub API, reconstructs the actual job graph and critical path, and checks
the result against a catalog of [90+ optimization
patterns](skills/ci-speedup/references/optimization-patterns.md) spanning
hygiene, data-driven detection, and structural critical-path analysis.

It diagnoses rather than prescribing a generic fix. Each finding includes a
ready-to-paste prompt for an agent that can inspect the relevant logs, history,
and repository context before changing anything.

**Examples:** [pallets/flask](examples/pallets-flask/ci-speedup-findings-report.md)
· [microsoft/playwright](examples/microsoft-playwright/ci-speedup-findings-report.md)

**Reference:** [Architecture](skills/ci-speedup/ARCHITECTURE.md) ·
[Catalog](skills/ci-speedup/references/optimization-patterns.md) ·
[Wall-clock methodology](skills/ci-speedup/references/wall-clock-methodology.md) ·
[Savings methodology](skills/ci-speedup/references/savings-methodology.md) ·
[Changelog](skills/ci-speedup/CHANGELOG.md) ·
[All files](skills/ci-speedup/) ·
[Website](https://starsling.dev/ci-speedup)

---

## ci-score

📋 **Grade CI configuration against best practices and get a ranked fix for
every gap.**

**[Open the ci-score skill file →](skills/ci-score/SKILL.md)**

```bash
npx skills add starslingdev/skills --skill ci-score
```

Invoke it as `/ci-score` (`$ci-score` in Codex), or ask: *“Grade my CI.”* It
runs fully offline.

The score covers 10+ configuration checks, including caching, shallow
checkout, sharding, concurrency cancellation, path filters, timeouts, action
pinning, and OIDC scoping. The score is checks passed divided by checks
applicable; every failed check gets one ranked fix.

It measures **best-practice adherence, not speed**, and it is **not a security
audit**. A small subset of checks is security-adjacent, but the score makes no broader
security claim.

**Example:** [pallets/flask](examples/pallets-flask/ci-score-report.md), scoring
89/100.

**Reference:** [Methodology](skills/ci-score/references/ci-score-methodology.md)
· [Machine-readable rubric](skills/ci-score/references/ci-score-spec.json) ·
[Changelog](skills/ci-score/CHANGELOG.md) · [All files](skills/ci-score/) ·
[Website](https://starsling.dev/ci-score)

---

## ci-secure

🔒 **Find 10+ critical GitHub Actions attack vectors and fix the findings you
select.**

**[Open the ci-secure skill file →](skills/ci-secure/SKILL.md)**

```bash
npx skills add starslingdev/skills --skill ci-secure
```

Invoke it as `/ci-secure` (`$ci-secure` in Codex), or ask: *“Is my CI
secure?”*

The scanner checks for [10+ critical attack
vectors](skills/ci-secure/references/security-patterns.md): template injection,
privileged execution of fork code, cache poisoning, impostor action SHAs,
secret dumps, `$GITHUB_ENV` hijacking, write tokens on untrusted triggers,
credentials in caches and artifacts, unverified remote-code execution, and
dependency install scripts running beside secrets.

Each finding names the file and line, quotes the workflow, and explains what an
attacker could do. Zero findings is a first-class result. A check that could
not run is reported as *did not run*, never as a pass.

This is deliberately **not a comprehensive security audit** and does not
produce a security score. The closed set contains exploit chains with a real
incident behind each one; see [how the vectors are selected](skills/ci-secure/references/why-these-ten.md).

**Examples:** the same `snowflakedb/snowflake-connector-net` workflow at two
commits — [3 findings before the fix](examples/snowflakedb-snowflake-connector-net/ci-secure-report-4a1b8ce.md)
and [0 findings after](examples/snowflakedb-snowflake-connector-net/ci-secure-report-1dc7766.md).

### ci-secure CI check

The optional CI check runs the same engine on every pull request so fixed
findings do not return. Ask your agent to install it:

```text
Run `npx skills add starslingdev/skills --skill ci-secure` to install or
update the ci-secure skill, then install ci-secure as a CI check in this repo.
```

The check is vendored, not fetched at runtime. Its engine, gate, and license
are copied into the repository, making the code that judges a pull request
readable and stable. The setup starts in advisory mode; remove `--advisory` and
make the check required when you are ready for it to block merges.

**CI-check implementation:** [Gate](skills/ci-secure/scaffold/gate.py) ·
[Workflow](skills/ci-secure/scaffold/ci-secure.yml) ·
[Vendor script](skills/ci-secure/scripts/vendor.py) ·
[Setup guide](skills/ci-secure/references/ci-gate.md)

**Reference:** [Attack vectors](skills/ci-secure/references/security-patterns.md)
· [Selection criteria](skills/ci-secure/references/why-these-ten.md) ·
[Configuration checks](skills/ci-secure/references/security-facts.md) ·
[Troubleshooting](skills/ci-secure/references/troubleshooting.md) ·
[Changelog](skills/ci-secure/CHANGELOG.md) · [All files](skills/ci-secure/) ·
[Website](https://starsling.dev/ci-secure)

---

## sling

🛰️ **Understand usage, performance, billing, and CI activity across your
StarSling account.**

**[Open the sling skill file →](skills/sling/SKILL.md)**

```bash
npx skills add starslingdev/skills --skill sling
```

Invoke it as `/sling` (`$sling` in Codex), or ask: *“What is driving our
StarSling bill?”*, *“How is runner performance trending?”*, or *“Why did this
job fail?”*

Unlike the repository-audit skills, `sling` requires a StarSling account. The
skill drives StarSling's read-only [`sling`
CLI](https://docs.starsling.dev/sling-cli), the agent-first interface to that
account. Its 15+ commands cover:

- **Usage:** attribute runner-minutes and cost by repository, workflow, job,
  label, or day.
- **Performance:** decompose wall-clock time, compare lifecycle phases, and
  rank time or cost hotspots with trends.
- **Billing:** inspect the current period, month-end projection, credits,
  per-label line items, and finalized invoice history.
- **CI activity:** find runs and jobs, diagnose failures, and retrieve only the
  relevant log lines.
- **Account and capacity:** work across your organizations and compare the
  runner sizes, specifications, and prices available to you.

The routing is part of the skill:

- Account reads—usage, billing, runner performance and capacity, runs, jobs,
  and failures—go to `sling`.
- State changes—rerun, cancel, trigger, enable, disable, or artifact
  download—go to `gh`.
- Repository-wide workflow audits go to `ci-speedup`, `ci-score`, or
  `ci-secure`.

Before answering, the skill checks that the CLI is installed, you are signed
in to your StarSling account, and the [StarSling GitHub
App](https://github.com/apps/starslingdev) is installed for the organization
whose data you want to inspect. The service currently supports organizations,
not personal repositories.

**Reference:** [Verified command reference](skills/sling/references/command-reference.md)
· [GitHub CLI fallback](skills/sling/references/gh-fallback.md) ·
[Machine-readable command surface](skills/sling/references/command-surface.json)
· [Changelog](skills/sling/CHANGELOG.md) · [All files](skills/sling/) ·
[CLI documentation](https://docs.starsling.dev/sling-cli)

---

## For maintainers

Maintainer-only infrastructure lives outside the installable skill trees, so
the `skills` CLI never copies it into an installation:

- [`maintainers/ci-speedup/`](maintainers/ci-speedup/) — self-improvement loops
  that run locally; see its [maintainer guide](maintainers/ci-speedup/MAINTAINERS.md).
- [`maintainers/ci-score/`](maintainers/ci-score/) — maintainer notes and
  deliberately excluded checks; see its [maintainer guide](maintainers/ci-score/MAINTAINERS.md).
- [`maintainers/ci-secure/`](maintainers/ci-secure/) — disclosure policy and
  enforcement for scans of third-party repositories; see its [maintainer
  guide](maintainers/ci-secure/MAINTAINERS.md).
- [`maintainers/skills-registry-security/`](maintainers/skills-registry-security/)
  — triage for registry security-audit failures; see its
  [README](maintainers/skills-registry-security/README.md).

This repository's internal CI runs on [StarSling
Runners](https://starsling.dev/). Fork pull requests run the same suite on
GitHub-hosted runners, so untrusted code does not execute on self-hosted
runners. See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE)
