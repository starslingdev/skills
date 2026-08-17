# pallets/flask — how does your CI configuration score?

| Repository | [`pallets/flask`](https://github.com/pallets/flask) — local checkout at `/local/flask` |
| :--- | :--- |
| **Scored commit** | [`d318b68`](https://github.com/pallets/flask/commit/d318b683471101618febed18996405ad26462110) |
| **Workflows scanned** | 5 workflow file(s) under `.github/workflows/` |
| **Rubric** | `ci-score-v0.1.3` · 11 pass/fail configuration checks |
| **Scored** | 2026-08-17 (UTC) · local checkout only — no CI runs fetched |

```
CI Score  89/100  ▏██████████████████████░░░▕  8 of 9 checks pass · 2 n/a
```

## CI Score: **89/100** — 8 of 9 applicable checks

> CI Score gauges best-practice adherence on CI speed and gate hygiene: a straightforward pass/fail rubric of best-practice checks computed from a repo's workflow configuration.

| | Check | Evidence |
|---|---|---|
| ✅ | [Dependency caching](#dependency-caching) | dependency caching configured in 2 workflow/action file(s) |
| n/a | [Build caching](#build-caching) | no build tool with a cache detected in the repo tree |
| ✅ | [Shallow checkout](#shallow-checkout) | no PR-gating workflow checks out full history (post-merge automation like backport/changelog jobs is exempt - it needs history and gates nobody) |
| ✅ | [Test sharding / matrix](#test-sharding--matrix) | shard/matrix strategy on test job(s) in 1 workflow(s) |
| n/a | [Change-scoped builds](#change-scoped-builds) | no turbo/nx task graph detected (change-scoping is not checkable here) |
| ✅ | [Concurrency groups](#concurrency-groups) | concurrency group declared on 3 of 3 PR workflow(s) |
| ✅ | [Superseded runs cancelled](#superseded-runs-cancelled) | concurrency + cancel-in-progress on 3 of 3 PR workflow(s) |
| ✅ | [Path filters](#path-filters) | paths filters on 2 of 3 PR workflow(s) |
| ❌ | [Job timeouts](#job-timeouts) | no job sets timeout-minutes (GitHub's default is 360 minutes) |
| ✅ | [Scoped OIDC id-token](#scoped-oidc-id-token) | id-token: write scoped at job level in 1 workflow(s) |
| ✅ | [Pinned action SHAs](#pinned-action-shas) | 21 of 21 remote action references SHA-pinned (workflow + local composite action files; passes at >=95%) |

> This grade measures configuration adherence to CI best practices; it does not predict CI speed: in our measured calibration, faster repos can hold lower grades.

## Recommendations — ranked by impact × risk

Highest-impact, lowest-risk first. Each carries a concrete fix and a paste-able agent prompt; risk notes state plainly what the change could break.

> If this repo's primary CI runs outside GitHub Actions (e.g. Buildkite, Jenkins) or distributes work via Nx Cloud agents, some failing checks may reflect what this scan cannot see — weigh each recommendation against mechanisms that live outside the workflow YAML.

### 1. Job timeouts — impact: medium, risk: low

**A hung job bills the full 6-hour GitHub default before dying - a timeout caps the damage at minutes.**

- **Why:** without timeout-minutes a hung job bills the default 360 minutes
- **Risk of the change:** low: set generously (2-3x normal runtime); too tight kills legitimate slow runs
- **Finding:** no job sets timeout-minutes (GitHub's default is 360 minutes)
- **Files:** `.github/workflows/lock.yaml`, `.github/workflows/pre-commit.yaml`, `.github/workflows/publish.yaml`
- **Guide:** https://starsling.dev/best-practices/github-actions/bound-job-timeouts

**Fix:**

```yaml
set on every job:
jobs:
  test:
    timeout-minutes: 20
(pick 2-3x the job's normal runtime)
```

<details><summary>Agent handoff prompt</summary>

```text
Fix one CI best-practice gap in this repository (ci-score recommendation #1: Job timeouts).
Repo state when scored: commit d318b683471101618febed18996405ad26462110.
Finding: no job sets timeout-minutes (GitHub's default is 360 minutes)
Example files (up to three; the Finding above states the full scope): .github/workflows/lock.yaml, .github/workflows/pre-commit.yaml, .github/workflows/publish.yaml
Task: set on every job:
jobs:
  test:
    timeout-minutes: 20
(pick 2-3x the job's normal runtime)
Reference: https://starsling.dev/best-practices/github-actions/bound-job-timeouts
Constraints: apply the fix everywhere the practice is missing — the files listed are up to three examples, so more offenders may exist; the Finding above states the full scope. Change nothing else; preserve workflow behavior apart from the practice being added; do not reformat unrelated YAML. Then re-run `python3 <ci-score>/scripts/collect_config.py --repo . --out /tmp/rescore.json`: the re-scored check is the oracle — you are done only when it reads pass (if it still fails, offenders remain — fix them and re-run).
```

</details>

## What each check means

### Dependency caching

**The check:** at least one workflow or local composite action configures dependency caching (a cache action - actions/cache incl. restore/save, rust-cache, buildjet - or a setup-* action's cache input). Caching housed in cross-repo reusable workflows is not visible; run the score in that repo.

**Not applicable when:** the repo shows NO dependency-install signal at all - no dependency manifest at the repo root (package.json / lockfiles / requirements*.txt / pyproject.toml / go.mod / Cargo.toml / build.gradle / pom.xml / *.csproj / mix.exs / build.sbt / Gemfile / composer.json / pubspec.yaml and the like), no dependency-install command in any workflow or composite step (pip / npm / pnpm / yarn / bun / poetry / uv / composer / bundle / gem / conda / mvn / gradle / dotnet / mix / cabal / stack with an install-style verb, or `go mod download` / `cargo fetch`), and no language setup action (actions/setup-node / setup-python / setup-java / setup-dotnet / setup-go / setup-ruby and the like) - and no cache is already configured; with nothing installed there is nothing to cache, so a missing cache is never a fail (OD-CS19). Any one of those three signals makes the check applicable. OR CI is delegated to cross-repo reusable workflows and no local caching is visible - a mechanism this fact cannot see is never failed

**Why it matters:** Every run re-downloads all your dependencies from scratch - caching reuses the last install instead of fetching them again.

**Guide:** https://starsling.dev/best-practices/github-actions/cache-dependencies

**Sources:** [GitHub Actions — Caching dependencies](https://docs.github.com/en/actions/reference/workflows-and-actions/dependency-caching), [actions/cache](https://github.com/actions/cache)

### Build caching

**The check:** a build-tool cache is configured (turbo/nx/gradle/sccache/bazel remote or actions-cache-backed)

**Not applicable when:** no detectable build tool configuration in the repo tree (turbo.json / nx.json / settings.gradle(.kts) / .bazelrc)

**Why it matters:** CI rebuilds work that didn't change - a build cache reuses the previous build instead of redoing it.

**Sources:** [Turborepo — Remote caching](https://turbo.build/repo/docs/core-concepts/remote-caching), [Nx — Remote cache](https://nx.dev/ci/features/remote-cache), [Gradle — Build cache](https://docs.gradle.org/current/userguide/build_cache.html)

### Shallow checkout

**The check:** no checkout step on a PR-GATING workflow sets fetch-depth: 0 (structure-walked; comments never count; post-merge automation - a pull_request trigger whose types are only [closed], e.g. backport/changelog jobs - is exempt: it needs history and gates nobody)

**Why it matters:** CI downloads your repo's entire history when it only needs today's code.

**Guide:** https://starsling.dev/best-practices/github-actions/shallow-checkout

**Sources:** [actions/checkout — fetch-depth](https://github.com/actions/checkout)

### Test sharding / matrix

**The check:** a test-like job (test/spec/e2e/integration/unit in its id or name) runs a matrix, or any job's matrix axis is shard-like (shard/chunk/split/partition)

**Not applicable when:** no test-like job (test/spec/e2e/integration/unit in an id or name) and no shard-like matrix axis exists in any parsed workflow - you cannot shard tests you do not have, so a missing shard is never a fail (OD-CS19); OR CI is delegated to cross-repo reusable workflows and no local test job is visible

**Why it matters:** One long test job sets the floor for every PR - splitting it across N runners runs the slices in parallel instead of one after another.

**Guide:** https://starsling.dev/best-practices/github-actions/shard-tests

**Sources:** [Playwright — Sharding](https://playwright.dev/docs/test-sharding), [Jest — --shard](https://jestjs.io/docs/cli#--shard), [pytest-xdist — Distribution modes](https://pytest-xdist.readthedocs.io/en/stable/distribution.html)

### Change-scoped builds

**The check:** CI scopes work to what changed: --filter=...[base], nx affected, --changed/--onlyChanged/--affected run modes, turbo-ignore, or a changed-files step (dorny/paths-filter, tj-actions/changed-files)

**Not applicable when:** no turbo/nx task graph is detected - gradle/bazel scoping is not checkable from workflow YAML and is never failed for a mechanism this fact cannot see

**Why it matters:** A docs typo shouldn't rebuild and retest the world - scope CI to what the change actually touched.

**Guide:** https://starsling.dev/best-practices/github-actions/build-only-affected

**Sources:** [Nx — Affected](https://nx.dev/ci/features/affected), [Turborepo — Running tasks](https://turbo.build/repo/docs/crafting-your-repository/running-tasks)

### Concurrency groups

**The check:** at least one PR-triggered workflow declares a concurrency group (the config lever against self-stampede queue time)

**Why it matters:** Without a group, every push while CI is busy just stacks another full run on the pile - you wait longer and pay for runs nobody will read.

**Guide:** https://starsling.dev/best-practices/github-actions/cut-queue-time

**Sources:** [GitHub Actions — Control workflow concurrency](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency)

### Superseded runs cancelled

**The check:** at least one PR-triggered workflow sets concurrency with cancel-in-progress (true or a templated expression - conditional cancellation is the practice too; an explicit false is not)

**Why it matters:** When you push again, the old run keeps burning paid minutes on code that no longer exists - this kills it the moment it's obsolete.

**Guide:** https://starsling.dev/best-practices/github-actions/cancel-superseded-runs

**Sources:** [GitHub Actions — Control workflow concurrency](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency)

### Path filters

**The check:** at least one PR-triggered workflow scopes itself with paths / paths-ignore

**Why it matters:** Workflows run even for changes that can't possibly affect them - filters skip CI that has nothing to check.

**Guide:** https://starsling.dev/best-practices/github-actions/path-filter-workflows

**Sources:** [GitHub Actions — Workflow syntax (`paths` / `paths-ignore`)](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax)

### Job timeouts

**The check:** workflow jobs set timeout-minutes (any usage; GitHub's default is 360 minutes)

**Why it matters:** A hung job bills the full 6-hour GitHub default before dying - a timeout caps the damage at minutes.

**Guide:** https://starsling.dev/best-practices/github-actions/bound-job-timeouts

**Sources:** [GitHub Actions — Workflow syntax (`jobs.<job_id>.timeout-minutes`)](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax)

### Scoped OIDC id-token

**The check:** id-token: write is granted at job level, never workflow-wide (an explicit grant or a workflow-level permissions: write-all both count as workflow-wide)

**Not applicable when:** no workflow requests an OIDC id-token

**Why it matters:** A workflow-wide cloud credential hands every job the keys - scoping gives them only to the one job that actually deploys.

**Guide:** https://starsling.dev/best-practices/github-actions/scope-id-token-per-job

**Sources:** [GitHub Actions — About security hardening with OpenID Connect](https://docs.github.com/en/actions/concepts/security/openid-connect), [GitHub Actions — Secure use reference](https://docs.github.com/en/actions/reference/security/secure-use)

### Pinned action SHAs

**The check:** at least 95% of remote action references are pinned to a full commit SHA, across workflow files AND local composite actions (first-party actions/* included - the pinning practice does not exempt them; the evidence always shows pinned-of-total). The threshold asks 'is pinning an adopted, automated practice?': >=95% admits Renovate-style stragglers (at most 1 in 20) while rejecting partial adoption, and is gaming-resistant - reaching it from a low base means actually pinning the fleet (OD-CS17)

**Why it matters:** A tag like @v4 can be silently repointed by whoever owns the action - pinning the exact commit means CI only ever runs code you chose.

**Guide:** https://starsling.dev/best-practices/github-actions/pin-action-shas

**Sources:** [GitHub Actions — Secure use reference (using third-party actions)](https://docs.github.com/en/actions/reference/security/secure-use)

