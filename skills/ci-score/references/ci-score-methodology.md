# CI Score v0.1.3 — methodology

> **CI Score gauges best-practice adherence on CI speed and gate hygiene: a
> straightforward pass/fail rubric of best-practice checks computed from a
> repo's workflow configuration.**

This document is the human form of the registry; the machine form and
single source of truth is `ci-score-spec.json`, linked from SKILL.md. If the two disagree, the JSON governs.

## Contents

- How it works (formula, bands, applicability, the one refusal)
- The eleven checks
- Each check, explained (one anchored section per check)
- What a high score means — and what it does not (score vs. speed, measured)
- Evidence, and the measured report beside the score
- Who publishes this, and the conflict stated plainly
- Data handling
- Change control

## How it works

Every check is a **configuration fact** — a thing present or absent in the
repo's own workflow files, verifiable by the maintainer in under a minute.
Pass or fail; a check whose subject doesn't exist in the repo (no build tool,
no dependency manifest to cache, no test job to shard, no OIDC use, no
third-party actions) is **not applicable** and leaves the denominator.

```
score = round-half-up( 100 × checks passed ÷ checks applicable )
grades: A ≥ 85 · B ≥ 70 · C ≥ 55 · D ≥ 40 · F < 40  (± thirds; F unsuffixed)
```

All surfaces use the **numeric score only** (owner decision, 2026-07-28,
extended the same day): the letter bands above remain defined in the spec and
stamped beside every value as internal registry data, but are **never
rendered on any surface and never written into records** (reports, cards,
calibration tables, changelogs) — with 8-12 applicable checks the instrument
cannot distinguish adjacent values, and the number states that honestly
without a report-card letter. The bands block above documents the frozen
internal bands; it is the one place a letter is written, on purpose.

Configuration facts need no run history, no branch-protection access, and no
merge-queue calibration, so **every repo with real CI scores** — the score
refuses in only three cases, all "nothing to score honestly," never a guessed
number:

- **no workflow files found**, so there is nothing to check;
- **automation-only** (OD-CS20): workflows exist but none build or test the
  project — what is visible is bots, releases, and triage, not the project's
  CI. Grading pure automation produces a technically-honest-but-absurd number
  that would corrode trust in every real score, so the repo is refused. The
  test is conservative: a repo is automation-only ONLY when it has no test
  signal (a test-like job **or** a test-runner command like `pytest` / `go
  test` / `npm test` — so a real test-only repo whose job is named `ci` still
  scores), no build signal (build tool, a build-or-lint job or command, a
  container-build action), AND no dependency-install command in any workflow —
  any one of those scores it, so a small-but-real gate (a lint-only job, a
  single build job) is never falsely refused. A setup-node/setup-bun action
  does not count as CI here (issue-triage bots use those to run their own
  scripts), and a repo that delegates its CI to a cross-repo reusable workflow
  is never refused (invisible, not absent).
- a **findings document from before the facts stamp existed** also refuses,
  with "re-run the skill" — a data-vintage note.

## The eleven checks

| Check | The fact |
|---|---|
| Dependency caching | some workflow configures it (`actions/cache` or a setup action's cache input) *(n/a when the repo installs no dependencies at all — no manifest, no install command, and no language setup action)* |
| Build caching | a build-tool cache is configured *(n/a without a build tool)* |
| Shallow checkout | no PR-triggered workflow uses `fetch-depth: 0` |
| Test sharding / matrix | a test job runs sharded or as a matrix *(n/a when there is no test-like job to shard)* |
| Change-scoped builds | CI scopes work to what changed (task-graph filters, affected/changed modes, or a changed-files step) *(n/a without a task graph)* |
| Concurrency groups | PR workflows declare a `concurrency` group |
| Superseded runs cancelled | ...with `cancel-in-progress` |
| Path filters | some PR workflow scopes itself with `paths` |
| Job timeouts | `timeout-minutes` is set (GitHub's default is 6 hours) |
| Scoped OIDC id-token | `id-token: write` at job level, never workflow-wide *(n/a when unused)* |
| Pinned action SHAs | **≥95%** of remote action references are SHA-pinned — full adoption with room for Renovate-style stragglers (at most 1 in 20); the evidence always shows pinned-of-total. The threshold is a ratio, so a repo with fewer than 20 references has no straggler allowance: one unpinned reference fails it |

Every published best-practice page maps to a check except
*keep-advisory-checks-non-blocking*: whether an advisory check blocks merges
lives in branch-protection settings, not workflow YAML, so no configuration
fact can decide it honestly. It is accounted for and deferred, not silently
missing.

## Each check, explained

One section per check: the exact configuration fact the scorer reads
(verbatim from the registry), when the check is not applicable, and why
the practice matters. The score card links every check name here.

### Dependency caching

**The fact:** at least one workflow or local composite action configures dependency caching (a cache action - actions/cache incl. restore/save, rust-cache, buildjet - or a setup-* action's cache input). Caching housed in cross-repo reusable workflows is not visible; run the score in that repo.

**Not applicable when:** the repo shows NO dependency-install signal at all - no dependency manifest at the repo root (package.json / lockfiles / requirements*.txt / pyproject.toml / go.mod / Cargo.toml / build.gradle / pom.xml / *.csproj / mix.exs / build.sbt / Gemfile / composer.json / pubspec.yaml and the like), no dependency-install command in any workflow or composite step (pip / npm / pnpm / yarn / bun / poetry / uv / composer / bundle / gem / conda / mvn / gradle / dotnet / mix / cabal / stack with an install-style verb, or `go mod download` / `cargo fetch`), and no language setup action (actions/setup-node / setup-python / setup-java / setup-dotnet / setup-go / setup-ruby and the like) - and no cache is already configured; with nothing installed there is nothing to cache, so a missing cache is never a fail (OD-CS19). Any one of those three signals makes the check applicable. OR CI is delegated to cross-repo reusable workflows and no local caching is visible - a mechanism this fact cannot see is never failed

**Why it matters:** Every run re-downloads all your dependencies from scratch - caching reuses the last install instead of fetching them again.

**Guide:** https://starsling.dev/best-practices/github-actions/cache-dependencies

**Sources:** [GitHub Actions — Caching dependencies](https://docs.github.com/en/actions/reference/workflows-and-actions/dependency-caching), [actions/cache](https://github.com/actions/cache)

### Build caching

**The fact:** a build-tool cache is configured (turbo/nx/gradle/sccache/bazel remote or actions-cache-backed)

**Not applicable when:** no detectable build tool configuration in the repo tree (turbo.json / nx.json / settings.gradle(.kts) / .bazelrc)

**Why it matters:** CI rebuilds work that didn't change - a build cache reuses the previous build instead of redoing it. This pays off when builds are long; a trivial build gains nothing from a cache.

**Sources:** [Turborepo — Remote caching](https://turbo.build/repo/docs/core-concepts/remote-caching), [Nx — Remote cache](https://nx.dev/ci/features/remote-cache), [Gradle — Build cache](https://docs.gradle.org/current/userguide/build_cache.html)

### Shallow checkout

**The fact:** no checkout step on a PR-GATING workflow sets fetch-depth: 0 (structure-walked; comments never count; post-merge automation - a pull_request trigger whose types are only [closed], e.g. backport/changelog jobs - is exempt: it needs history and gates nobody)

**Why it matters:** CI downloads your repo's entire history when it only needs today's code.

**Guide:** https://starsling.dev/best-practices/github-actions/shallow-checkout

**Sources:** [actions/checkout — fetch-depth](https://github.com/actions/checkout)

### Test sharding / matrix

**The fact:** a test-like job (test/spec/e2e/integration/unit in its id or name) runs a matrix, or any job's matrix axis is shard-like (shard/chunk/split/partition)

**Not applicable when:** no test-like job (test/spec/e2e/integration/unit in an id or name) and no shard-like matrix axis exists in any parsed workflow - you cannot shard tests you do not have, so a missing shard is never a fail (OD-CS19); OR CI is delegated to cross-repo reusable workflows and no local test job is visible

**Why it matters:** One long test job sets the floor for every PR - splitting it across N runners runs the slices in parallel instead of one after another. This pays off when the test job runs long (the measured catalog's threshold was five-plus minutes); on a quick suite the per-shard setup overhead makes CI slower and costs more, so skip it unless tests are what you wait on.

**Guide:** https://starsling.dev/best-practices/github-actions/shard-tests

**Sources:** [Playwright — Sharding](https://playwright.dev/docs/test-sharding), [Jest — --shard](https://jestjs.io/docs/cli#--shard), [pytest-xdist — Distribution modes](https://pytest-xdist.readthedocs.io/en/stable/distribution.html)

### Change-scoped builds

**The fact:** CI scopes work to what changed: --filter=...[base], nx affected, --changed/--onlyChanged/--affected run modes, turbo-ignore, or a changed-files step (dorny/paths-filter, tj-actions/changed-files)

**Not applicable when:** no turbo/nx task graph is detected - gradle/bazel scoping is not checkable from workflow YAML and is never failed for a mechanism this fact cannot see

**Why it matters:** A docs typo shouldn't rebuild and retest the world - scope CI to what the change actually touched. This pays off in large task graphs; a small repo rebuilds everything quickly anyway.

**Guide:** https://starsling.dev/best-practices/github-actions/build-only-affected

**Sources:** [Nx — Affected](https://nx.dev/ci/features/affected), [Turborepo — Running tasks](https://turbo.build/repo/docs/crafting-your-repository/running-tasks)

### Concurrency groups

**The fact:** at least one PR-triggered workflow declares a concurrency group (the config lever against self-stampede queue time)

**Why it matters:** Without a group, every push while CI is busy just stacks another full run on the pile - you wait longer and pay for runs nobody will read.

**Guide:** https://starsling.dev/best-practices/github-actions/cut-queue-time

**Sources:** [GitHub Actions — Control workflow concurrency](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency)

### Superseded runs cancelled

**The fact:** at least one PR-triggered workflow sets concurrency with cancel-in-progress (true or a templated expression - conditional cancellation is the practice too; an explicit false is not)

**Why it matters:** When you push again, the old run keeps burning paid minutes on code that no longer exists - this kills it the moment it's obsolete.

**Guide:** https://starsling.dev/best-practices/github-actions/cancel-superseded-runs

**Sources:** [GitHub Actions — Control workflow concurrency](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency)

### Path filters

**The fact:** at least one PR-triggered workflow scopes itself with paths / paths-ignore

**Why it matters:** Workflows run even for changes that can't possibly affect them - filters skip CI that has nothing to check.

**Guide:** https://starsling.dev/best-practices/github-actions/path-filter-workflows

**Sources:** [GitHub Actions — Workflow syntax (`paths` / `paths-ignore`)](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax)

### Job timeouts

**The fact:** workflow jobs set timeout-minutes (any usage; GitHub's default is 360 minutes)

**Why it matters:** A hung job bills the full 6-hour GitHub default before dying - a timeout caps the damage at minutes.

**Sources:** [GitHub Actions — Workflow syntax (`jobs.<job_id>.timeout-minutes`)](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax)

### Scoped OIDC id-token

**The fact:** id-token: write is granted at job level, never workflow-wide (an explicit grant or a workflow-level permissions: write-all both count as workflow-wide)

**Not applicable when:** no workflow requests an OIDC id-token

**Why it matters:** A workflow-wide cloud credential hands every job the keys - scoping gives them only to the one job that actually deploys.

**Guide:** https://starsling.dev/best-practices/github-actions/scope-id-token-per-job

**Sources:** [GitHub Actions — About security hardening with OpenID Connect](https://docs.github.com/en/actions/concepts/security/openid-connect), [GitHub Actions — Secure use reference](https://docs.github.com/en/actions/reference/security/secure-use)

### Pinned action SHAs

**The fact:** at least 95% of remote action references are pinned to a full commit SHA, across workflow files AND local composite actions (first-party actions/* included - the pinning practice does not exempt them; the evidence always shows pinned-of-total). The threshold asks 'is pinning an adopted, automated practice?': >=95% admits Renovate-style stragglers (at most 1 in 20) while rejecting partial adoption, and is gaming-resistant - reaching it from a low base means actually pinning the fleet (OD-CS17)

**Why it matters:** A tag like @v4 can be silently repointed by whoever owns the action - pinning the exact commit means CI only ever runs code you chose.

**Guide:** https://starsling.dev/best-practices/github-actions/pin-action-shas

**Sources:** [GitHub Actions — Secure use reference (using third-party actions)](https://docs.github.com/en/actions/reference/security/secure-use)

## What a high score means — and what it does not

The score measures **adherence**: does this repo have these eleven practices
configured? It does not measure engineering depth, and it does not measure
speed. Those are different axes, and conflating them is the most common way
to misread a score — so here is the honest picture, from our own calibration
data:

- A heavily-optimized repo can score lower than a simpler one. mastra-ai/mastra
  runs test sharding, a turbo build cache, and a changed-tests-only gate — and
  scores **82** (9 of 11), because it genuinely has two gaps: full-history
  checkouts on five PR-gating workflows and 19 of 120 action references
  unpinned. Its sophistication earns exactly one checkmark per practice, the
  same as anyone's. 82 means "strong, with two concrete, fixable gaps" — which
  is precisely true of mastra.
- **The score does not predict speed — measured, it runs the other way.**
  Across the eleven repos we measured end-to-end, higher adherence scores
  associate with *slower* merge gates (large monorepos adopt every practice
  *and* have enormous test volume; small repos are fast because there is
  little to run). Among the committed worked examples, the lowest-scoring repo
  has the fastest gate (deepgram's SDK at 33, a typical PR waits ~1¼ minutes)
  and 82-scoring mastra the slowest (~7 minutes) — each number is the headline
  of the report shipped beside that card. That is why
  the measured report — with its headline "a typical PR waits N minutes" —
  renders directly beneath the card in the same document: adherence and
  speed are reported separately, side by side, never blended.
- A repo where a practice's subject doesn't exist (no build tool, no OIDC)
  is scored on fewer checks — "9 of 9" and "10 of 11" can both render as
  high scores. The card always shows the checks-passed-of-applicable count
  so the denominators are never hidden.

Every deduction is a fact the maintainer can verify in their own YAML in
under a minute, and every one is fixable — the grade is a to-do list with a
letter on it, not a judgment of engineering quality.

Keeping speed out of the score is a deliberate decision, not an oversight
(the spec's decision log records it as OD-CS16). Raw gate speed mostly
measures repo size — big monorepos test more and run longer no matter how
well-engineered their CI is — so blending it in would punish scale and
reward smallness. Where speed *will* enter a future score is as size-fair
effectiveness measurements (is the sharding balanced? is the queueing
self-inflicted? how much re-run churn?), which grade the engineering rather
than the repo's size.

## Evidence, and the measured report beside the score

Every state carries an evidence string naming the fact ("101 of 120 remote
action references SHA-pinned", "fetch-depth: 0 on 2 PR-gating workflow(s)")
and the files to look at (third-party offenders listed first, counts always
complete — a capped file list is never phrased as exhaustive). Post-merge
automation (a PR trigger whose types are only `[closed]` — backport and
changelog jobs) is exempt from the PR-path checks: it needs history and gates
nobody. CI delegated to cross-repo reusable workflows lands `not applicable`
on the content checks — a mechanism a fact cannot see is never failed. Where the skill's measured pipeline has sized the practice's cost
on this repo, that cost renders under a failed check ("measured cost on this
repo: ~95s per run") — **display only; measurement never changes a state.**
The full wall-clock report renders beside the score, unchanged.

## Who publishes this, and the conflict stated plainly

StarSling builds CI runners. This score deliberately grades **configuration
hygiene, not speed or runner choice**: no check depends on any StarSling
product, runner-minute economics are reported beside the score and never
graded, and the scorer is a pure local function anyone can re-run from this
repository to reproduce any published number. If a published page about your
repository is wrong, the page carries a contact address; factual errors are
corrected and re-scored.

## Data handling

The facts come from the workflow files the scan already reads; the score is a
pure function computed in-process and stamped into the local `findings.json`.
No network, no clock, no LLM in the scoring path; nothing extra leaves your
machine for scoring, and there is no telemetry. A scoring failure leaves the
document unstamped with a machine-readable `ci_score_error` marker — never a
partial stamp.

## Change control

**The v0.1 registry is frozen.** The Phase-3 calibration dry-run (31 repos —
the full site exemplar pool — under a pinned spec version, committed before
any rubric edit) motivated zero changes, and with it the pre-publication
tuning window closed: the registry now changes only with a `spec_version`
bump recorded in the spec's changelog. The calibration receipt lives in the
spec's `calibration` block.

**v0.1.1 (2026-07-15) is the first such bump:** the pinned-SHAs check now
passes at ≥95% instead of all-or-nothing. The owner-decided rationale, and
the census showing exactly one calibration repo flips (next-js, 99.3%
pinned), are recorded in the spec's `decision_log` as OD-CS17 — the
threshold was chosen from the practice-adoption question, not fitted to
move rows.

**v0.1.2 (2026-07-28, OD-CS18)** removed the *draft-pr-gating* check (12 → 11
checks): draft-PR gating is a contested cost preference, not a consensus
practice — many teams push draft PRs precisely to get CI feedback before
review — and the top-scoring calibration exemplars failed only it. It is also the
one check with no published best-practice page behind it. The rationale and the
recomputed grade movement live in the spec's `decision_log` as OD-CS18.
Pre-v0.1.2 receipts (the collected calibration reports and the v0.1/v0.1.1
dry-run tables) remain v0.1.1 records and are not restamped.

**v0.1.3 (2026-07-28, OD-CS19)** gives two checks the applicability gate the
rubric's own principle already demanded — *a mechanism this fact cannot see is
never failed.* **Dependency caching** is now *not applicable* only when the
repo shows no dependency-install signal at all — none of three signals: a
dependency manifest at the root (`package.json` / lockfile / `requirements*.txt`
/ `pyproject.toml` / `go.mod` / `Cargo.toml` / `build.gradle` / `pom.xml` /
`*.csproj` / `mix.exs` / `build.sbt` / `Gemfile` / `composer.json` /
`pubspec.yaml` and the like), an inline dependency-install command in any
workflow step
(`pip`/`npm`/`pnpm`/`yarn`/`bun`/`poetry`/`uv`/`composer`/`bundle`/`gem`/`conda`
install, `mvn`/`gradle`/`dotnet`/`mix` build, `go mod download`, `cargo fetch`),
or a language setup action (`actions/setup-node`/`setup-python`/`setup-java`/
`setup-dotnet`/… — the general ecosystem signal) — with nothing installed there
is nothing to cache. Any one of the three makes the check applicable. **Test
sharding** is now *not applicable* when no test-like job (and no shard-like
matrix axis) exists anywhere — you cannot shard tests you do not have. A
configured cache still passes regardless of the signal; a test job without a
matrix still fails. The motivating case was live: the public skills repo
installs its dependencies with `pip install pytest pyyaml` and caches none, yet
an early file-only gate handled it for the wrong reason. Round-6 dogfooding
showed a pure file-existence gate both *missed* that repo (it actually carries a
`pyproject.toml`) and would *mask real waste* — a manifest-less repo with an
inline `pip install` would go n/a, dropping a genuine fail and inflating the
grade — so the gate was retargeted to this install-signal rule; the skills repo
is now honestly applicable and failing. A later review caught that a
manifest+install-verb-only gate still masked non-JS/Python/Go/Rust ecosystems
(.NET, Elixir, Scala, monorepos with the manifest in a subdir); the setup-action
signal and the broadened manifest/command lists close that. Recomputed over
the full 31-repo calibration table, **two rows move** — both small/atypical
repos with no test-like job at all, where the sharding gate fires:
*anthropics/claude-code* 44 → 50 and *coollabsio/coolify* 22 → 25 (value only).
*adobe/leonardo* also has no test job but already scored 0, so it holds. The
dependency-cache gate moves no calibration row — every dep-cache-fail repo
installs dependencies. Every other row is unchanged. Then **OD-CS20** (the
automation-only refusal) supersedes the claude-code move entirely: its visible
workflows are all issue-triage bots and release automation, so it is **not
scored** at all — verified by re-collecting the three no-test-job repos, which
resolves to a single refusal: claude-code (no test job, no build signal, no
install command), while *coolify* keeps its value (real build-push jobs) and
*leonardo* keeps its 0 (its `pull_request` ci runs a real `pnpm install`). Net:
one calibration row becomes "not scored." The rationale is OD-CS19 and OD-CS20
in the spec's `decision_log`.

The v1-granular rubric (measured-magnitude gating, tiers, weights,
nine refusal conditions) was **punted before publication by owner decision
OD-CS15** — its record and rationale live in the spec's `decision_log`, and
its machinery remains available to a future v2 in git history.
