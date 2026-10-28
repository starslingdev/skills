# CI Score v0.1.6 — methodology

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
Steps inside a GitHub Actions `parallel:` group are read exactly like any
other step (the `wait:` / `wait-all:` / `cancel:` steps only wait for or
cancel background steps, run no code of their own, and are skipped), and the
report header states how many steps were read from parallel groups. It names,
by file, job, step position and reason, every `parallel:` in a shape GitHub
does not document whose steps were read anyway (inside a composite action, a
group inside a group, a `parallel:` beside `run:` / `uses:` on the same step,
or a single step where a list belongs); every `parallel:` that could not be
read, so its steps were not checked (not a list of steps, empty, a group that
contains itself, nested more than 64 deep, or past the 10,000-entry read
limit per job); and every entry inside a group that is not a step. These
unread groups are disclosed, not scored around: like a workflow file that
fails to parse (recorded in the findings document, the repo still scored from
what was readable), the verdicts are computed from the steps that could be
read.

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
| Dependency caching | some workflow configures it (`actions/cache`, a setup action's cache input, or a setup action whose cache is on by default) *(n/a when the repo installs no dependencies at all — no manifest, no install command, and no language setup action)* |
| Build caching | a build-tool cache is configured *(n/a without a build tool)* |
| Shallow checkout | no PR-triggered workflow, or same-repository reusable workflow it calls, uses `fetch-depth: 0` on the pull-request path |
| Test sharding / matrix | a test job runs sharded or as a matrix, including one generated at runtime *(n/a when there is no test-like job to shard)* |
| Change-scoped builds | CI scopes work to what changed (task-graph filters, affected/changed modes, or a changed-files step) *(n/a without a task graph)* |
| Concurrency groups | PR workflows declare a `concurrency` group |
| Superseded runs cancelled | ...with `cancel-in-progress` |
| Path filters | some PR workflow scopes itself with `paths` |
| Job timeouts | `timeout-minutes` is set (GitHub's default is 6 hours) *(n/a when every job delegates to a reusable workflow and some of them live in another repo)* |
| Scoped OIDC id-token | `id-token: write` at job level, never workflow-wide *(n/a when unused)* |
| Pinned action SHAs | **≥95%** of remote action references (steps, composite actions, and calls to other repositories' reusable workflows) are pinned to a commit SHA or by GitHub's workflow lockfile — full adoption with room for Renovate-style stragglers (at most 1 in 20); the evidence always shows pinned-of-total. The threshold is a ratio, so a repo with fewer than 20 references has no straggler allowance: one unpinned reference fails it |

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

**The fact:** at least one workflow or local composite action (any local action a step's uses: names - ./ or $/, wherever it lives - plus everything under .github/actions/) configures dependency caching (a cache action - actions/cache incl. restore/save, rust-cache, buildjet - or a setup-* action's explicit cache or enable-cache input set to any value other than false, credited on any runner as before (a cache switch set by an expression - the input the action itself reads: bundler-cache on ruby/setup-ruby, enable-cache on astral-sh/setup-uv, cache-disabled on the Gradle actions and bahmutov/npm-install, cache on any other setup-* action, action and input names in any case - counts only when it resolves to a value that turns caching on: ${{ inputs.<name> }} inside a local action takes each caller's with: value, else the action's input default, and inside a reusable workflow each calling job's with: value, else the workflow_call input default (an input the workflow declares nowhere has no value); each resolved value is graded exactly as if written in place, by the rules below, and the step is credited when any of them caches; any other expression - env, matrix, or a compound one such as ${{ inputs.x || 'npm' }} - cannot be resolved, earns nothing and is named in the evidence and in data_sources.unresolved_cache_expressions, and such a switch decides its step on its own, never falling back to the action's default below; an expression on a key the action does not read leaves its default to decide; a local action that only an unread called workflow runs earns nothing); or a setup action whose cache is ON BY DEFAULT or switched by a differently-named input, using the speed engine's own table: actions/setup-go v4+ unless cache is set off, and only once a checkout of this repository to the workspace root has finished earlier in the same job and a go.sum exists at the repo root or at the step's cache-dependency-path; astral-sh/setup-uv v5+ unless enable-cache is set off (its default, enable-cache: auto, caches only on GitHub-hosted runners, so with no enable-cache input it counts only when the job's runs-on is one of GitHub's published hosted-runner image labels - ubuntu-latest, ubuntu-24.04, ubuntu-24.04-arm, ubuntu-slim, windows-2025, windows-11-arm, macos-15, macos-15-intel, macos-15-xlarge and the like - as a string or a one-item list, and never inside a local composite action, whose runner is its caller's; on any other runner, including a larger runner whose label its owner names, enable-cache: true earns it); ruby/setup-ruby with bundler-cache; actions/setup-node v5+ when the repo-root package.json names the package manager (v5: npm/yarn/pnpm in packageManager; v6+: npm only, in devEngines.packageManager or packageManager) unless package-manager-cache is set off (an expression there is read as its default, true), and only once a checkout of this repository to the workspace root has finished earlier in the same job; gradle/actions/setup-gradle and gradle/gradle-build-action unless cache-disabled is true; and bahmutov/npm-install. A checkout has finished when it ran earlier in sequence or in an earlier parallel: group, not when it shares the step's group or is an earlier background: true checkout. with: input names are read case-insensitively in both directions, so Cache: npm opts in exactly as cache: npm does and Cache: false opts out exactly as cache: false does. A SHA- or branch-pinned ref names no version, so a default-on setup action counts as caching unless a version comment beside the SHA (# vN, # tag=vN, # ratchet:owner/action@vN, # pin@vN, # owner/action@vN, # @vN) names a release from before the default turned on, or names no release at all (a date such as 2024-05-01, a range such as 20.x, or a major the action has never released is not a release); gradle's and npm-install's default-on actions always cache, so their comments are not read; setup-node counts only when its v5 and v6 rules agree, or when every release its version comment names caches. A default this scan cannot decide earns nothing and is named in the evidence (could not be resolved) and in data_sources.unresolved_setup_defaults: a package.json that cannot be read, is not JSON or is not an object; a SHA or branch ref the setup-node rules disagree on; a version comment that names no release; a runs-on expression or runner group for setup-uv's default; a cache-dependency-path that is an expression, an absolute path, outside the repository (..) or only exclusions (!); a checkout that may still be running, or that went to a sub-path or another repository. A package.json setup-node cannot use because it starts with a byte-order mark (setup-node's own parse rejects it) is decided off, and named and recorded the same way (earns no default cache). An opt-out, a pre-default release (including one a version comment names), no package manager in package.json, and no checkout at all are plain offs and name nothing). Caching housed in cross-repo reusable workflows is not visible; run the score in that repo. A local uses: path must be a spelling git tracks (git's file list decides, so every disk gives the same answer): a wrong-case path does not exist on a Linux runner, so it is never read, and an action under .github/actions/ reached only through one earns nothing. A same-repository reusable workflow that a call on the PR path names but cannot read (it repeats a key, or sits more than 16 calls deep) and that no PR path reads is graded by neither this check nor the other card that names it, so the note is true. Content the scan could not read (a local action, a workflow that does not parse, a step list that is not a list, a reusable-workflow call it could not follow) is named in the card evidence, not only in data_sources.

**Not applicable when:** the repo shows NO dependency-install signal at all - no dependency manifest at the repo root (package.json / lockfiles / requirements*.txt / pyproject.toml / go.mod / Cargo.toml / build.gradle / pom.xml / *.csproj / mix.exs / build.sbt / Gemfile / composer.json / pubspec.yaml and the like), no dependency-install command in any workflow or composite step (pip / npm / pnpm / yarn / bun / poetry / uv / composer / bundle / gem / conda / mvn / gradle / dotnet / mix / cabal / stack with an install-style verb, or `go mod download` / `cargo fetch`), and no language setup action (actions/setup-node / setup-python / setup-java / setup-dotnet / setup-go / setup-ruby and the like) - and no cache is already configured; with nothing installed there is nothing to cache, so a missing cache is never a fail (OD-CS19). Any one of those three signals makes the check applicable. OR CI is delegated to cross-repo reusable workflows and no local caching is visible - a mechanism this fact cannot see is never failed. OR (owner decision 2026-10-27, OD-CS24) a local action that is not in the checkout is the ONLY thing that could explain the missing cache: nothing readable shows a cache; no readable step is a setup-* action or carries a cache input (the same test that grants credit); the unread action has no action.yml in the checkout (an action fetched at run time - a wrong-case path, a path outside the repository or a file that does not load does not count), runs in a job on the PR path before an install, build or test step (readable local actions are followed in step order), and its own if: is not literally false; and no install anywhere runs without such an action before it in its job - the evidence says so and the not-read note names the action; anything else fails

**Why it matters:** Every run re-downloads all your dependencies from scratch - caching reuses the last install instead of fetching them again. A cache that measurably costs more than it saves should be removed, and this check reads configuration only, so it cannot see that measurement; a repo that removed such a cache still loses this point.

**Guide:** https://starsling.dev/best-practices/github-actions/cache-dependencies

**Sources:** [GitHub Actions — Caching dependencies](https://docs.github.com/en/actions/reference/workflows-and-actions/dependency-caching), [actions/cache](https://github.com/actions/cache)

### Build caching

**The fact:** a build-tool cache is configured (turbo/nx/gradle/sccache/bazel remote or actions-cache-backed)

**Not applicable when:** no detectable build tool configuration in the repo tree (turbo.json / nx.json / settings.gradle(.kts) / .bazelrc)

**Why it matters:** CI rebuilds work that didn't change - a build cache reuses the previous build instead of redoing it. This pays off when builds are long; a trivial build gains nothing from a cache.

**Sources:** [Turborepo — Remote caching](https://turbo.build/repo/docs/core-concepts/remote-caching), [Nx — Remote cache](https://nx.dev/ci/features/remote-cache), [Gradle — Build cache](https://docs.gradle.org/current/userguide/build_cache.html)

### Shallow checkout

**The fact:** no checkout step on a PR-GATING workflow sets fetch-depth: 0 (structure-walked; comments never count; a JOB THAT WALKS GIT HISTORY - changelog generation, a merge-base or base-SHA diff, tag/describe, changeset versioning - is exempt: full history is load-bearing there and shallowing it would break the job; post-merge automation - a pull_request trigger whose types are only [closed], e.g. backport/changelog jobs - is exempt too: it needs history and gates nobody; a job whose own if: can never be true for a pull-request event - e.g. github.event_name == 'push', or a job scoped to the merge queue with github.event_name == 'merge_group', which is not a pull-request event - is exempt too: no pull request waits for it; and the same rule reads a checkout STEP's own if: - a job whose every fetch-depth: 0 checkout step carries an if: that can never be true for a pull-request event takes no full history on the PR path and is exempt. Only simple, fully resolvable event_name conditions are read; anything else keeps the job on the PR path, including a bare literal if: false, which the grammar does not read and which is therefore still graded - a known miss in the fail-closed direction, costing at most a finding that was already reported; a same-repository reusable workflow (on: workflow_call) that a PR-reachable job calls - via ./ or $/, directly or through nested calls - is on the PR path and graded the same way, its jobs' if: judged against the caller's pull-request events, while a call from a job that can never run on a pull request reaches nothing; a call that cannot be followed (a path outside the repository, a file that is missing, does not parse or repeats a key, or a chain more than 16 deep) is never graded either way and is named in the evidence; a called workflow's fetch-depth: ${{ inputs.<name> }} takes each calling job's with: value, else the input's default, and a depth set by any expression that cannot be resolved leaves the job ungraded and named in the evidence; a job exempt only because a local action it uses, or one that action reaches, could not be read (missing, outside the repository, in the wrong case, or more than 16 deep) stays exempt, since it cannot be proven history-free, and the evidence says the exemption rests on that action, naming it). Content the scan could not read (a local action, a workflow that does not parse, a step list that is not a list, a reusable-workflow call it could not follow) is named in the card evidence, not only in data_sources.

**Why it matters:** CI downloads your repo's entire history when it only needs today's code.

**Guide:** https://starsling.dev/best-practices/github-actions/shallow-checkout

**Sources:** [actions/checkout — fetch-depth](https://github.com/actions/checkout)

### Test sharding / matrix

**The fact:** a test-like job (test/spec/e2e/integration/unit in its id or name) runs a matrix, or any job's matrix axis is shard-like (shard/chunk/split/partition). A matrix generated at runtime (matrix: ${{ fromJSON(...) }}) is a matrix: on a test-like job it passes; on any other job no axis is visible to call shard-like, so the job-name rule decides, exactly as for a literal matrix

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

**The fact:** workflow jobs set timeout-minutes (any usage; GitHub's default is 360 minutes). A job that calls a reusable workflow cannot set timeout-minutes; a local reusable workflow's own jobs are graded where they are defined

**Not applicable when:** no job sets timeout-minutes, every job in every parsed workflow calls a reusable workflow (none runs steps of its own), and at least one of those calls is cross-repo - the jobs that could carry a timeout live where this scan cannot see, and a mechanism this fact cannot see is never failed. A single job that runs its own steps keeps the check applicable

**Why it matters:** A hung job bills the full 6-hour GitHub default before dying - a timeout caps the damage at minutes.

**Sources:** [GitHub Actions — Workflow syntax (`jobs.<job_id>.timeout-minutes`)](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax)

### Scoped OIDC id-token

**The fact:** id-token: write is granted at job level, never workflow-wide (an explicit grant or a workflow-level permissions: write-all both count as workflow-wide)

**Not applicable when:** no workflow requests an OIDC id-token

**Why it matters:** A workflow-wide cloud credential hands every job the keys - scoping gives them only to the one job that actually deploys.

**Guide:** https://starsling.dev/best-practices/github-actions/scope-id-token-per-job

**Sources:** [GitHub Actions — About security hardening with OpenID Connect](https://docs.github.com/en/actions/concepts/security/openid-connect), [GitHub Actions — Secure use reference](https://docs.github.com/en/actions/reference/security/secure-use)

### Pinned action SHAs

**The fact:** at least 95% of remote action references are pinned - to a full commit SHA, or by a valid entry in GitHub's workflow lockfile (.github/workflows/actions.lock) - across workflow steps, job-level calls to another repository's reusable workflow, AND local composite actions wherever a uses: path places them (first-party actions/* included - the pinning practice does not exempt them; the evidence always shows pinned-of-total, and how many of those the lockfile pins). A tag-referenced action counts as lockfile-pinned only when the lockfile lists it under its own workflow file and carries a full-commit entry for it, or, inside a local action every reference reaches through $/, when the lockfile lists it with a full-commit entry under every workflow file that reaches that action; a ./ action runs whatever is in the workspace, so the lockfile cannot cover what it uses. A job-level call to another repository's reusable workflow counts as pinned only with a full commit SHA: the gh actions-lock README states that calls to another repository's reusable workflow are not locked (github/gh-actions-lock#179, open), even though GitHub's docs page says reusable workflows are pinned; until the tool locks them, a lockfile entry for one enforces nothing, so only a full SHA counts. A lockfile that cannot be read, is malformed anywhere (including an entry whose ref contradicts its key, a listed reference with no entry, or a workflow key that is not exactly .github/workflows/<file>), sits at a path whose case differs from .github/workflows/actions.lock, or carries a format version other than the two GitHub documents (v0.0.2, v0.0.3) grants no credit at all (OD-CS24). A local uses: path must be a spelling git tracks (git's file list decides, so every disk gives the same answer): a wrong-case path does not exist on a Linux runner, so it is never read, and an action under .github/actions/ reached only through one earns nothing. A same-repository reusable workflow that a call on the PR path names but cannot read (it repeats a key, or sits more than 16 calls deep) and that no PR path reads is graded by neither this check nor the other card that names it, so the note is true. Content the scan could not read (a local action, a workflow that does not parse, a step list that is not a list, a reusable-workflow call it could not follow) is named in the card evidence, not only in data_sources. The threshold asks 'is pinning an adopted, automated practice?': >=95% admits Renovate-style stragglers (at most 1 in 20) while rejecting partial adoption, and is gaming-resistant - reaching it from a low base means actually pinning the fleet (OD-CS17)

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
  scores **91** (10 of 11), because it genuinely has one gap: 19 of 120 action
  references unpinned. Its sophistication earns exactly one checkmark per
  practice, the same as anyone's. 91 means "strong, with one concrete, fixable
  gap" — which is precisely true of mastra. Its full-history checkouts are not
  the second gap they once looked like: every one of them sits on a job that
  walks git history, and the shallow-clone check now spares such a job rather
  than telling a maintainer to break it.
- **The score does not predict speed — measured, it runs the other way.**
  Across the eleven repos we measured end-to-end, higher adherence scores
  associate with *slower* merge gates (large monorepos adopt every practice
  *and* have enormous test volume; small repos are fast because there is
  little to run). Among the committed worked examples, the lowest-scoring repo
  has the fastest gate (deepgram's SDK at 33, a typical PR waits ~1¼ minutes)
  and 91-scoring mastra the slowest (~7 minutes). The wait times are the
  headline of the report shipped beside each card. The adherence numbers are
  not always: a card is a frozen snapshot that also carries live run data no
  offline re-run can reproduce, so a card is not re-stamped when the engine
  gets more precise. Mastra's shipped card still reads 82 for that reason —
  91 is what the current engine computes from the same workflow YAML. That is
  why
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

**v0.1.4 (2026-09-02, OD-CS22)** narrows **shallow clone** twice, both times
to the *job* rather than the workflow file. First, a job that walks git history
is no longer an offender: `fetch-depth: 0` is load-bearing for changelog
generation, merge-base diffs, `git describe`, and changeset publishing, so
telling a maintainer to shallow such a job is not an optimization, it is a
broken build. The predicate is the speed engine's, character for character, so
the two shipped engines can no longer reach opposite verdicts on the same line
of YAML. Second, a job whose own `if:` can never be true for any pull-request
event the workflow is triggered by is not on the pull-request path, so it is
not graded there; the expression parser recognises only `github.event_name`
compared with a string literal, and anything it cannot fully resolve leaves the
job graded — it fails closed. Two consequences are worth naming. A job scoped
to the merge queue alone (`github.event_name == 'merge_group'`) is off the
pull-request path by this rule, because a merge-queue run is not a
pull-request event and no pull request waits for it. And a job switched off
with a bare literal — `if: false` or `if: ${{ false }}` — is still graded,
because the grammar reads no literal: a known miss, left deliberately, since
it errs toward reporting a finding rather than dropping one. Recomputed over the frozen calibration controls,
one moves 82 → 91 (all nine of its PR-gating full-history jobs walk history)
and the other holds at 82. The rationale is OD-CS22 in the spec's
`decision_log`. Committed corpus stamps carry the new `spec_version` and
nothing else — their facts and scores are unchanged — and the published worked
example keeps its own frozen `v0.1.3` stamp, since a snapshot records what the
engine said on the day it ran.

**v0.1.5 (2026-10-27, OD-CS23)** stops four checks failing repositories that
follow the practice. **Dependency caching** credits a setup action whose cache
is on by default or switched by an input other than `cache:` (setup-go v4+,
setup-uv v5+, setup-ruby's `bundler-cache`, setup-node v5+ when `package.json`
names the package manager, `gradle/actions/setup-gradle`,
`gradle/gradle-build-action`, `bahmutov/npm-install`); the rules are the speed
engine's own, copied and pinned to it by a test, and an explicit opt-out such as
`cache: false` or `package-manager-cache: false` still fails. Two owner rules
narrow that credit in ci-score only. A SHA-pinned action names no version, so a
default-on action pinned by SHA counts as caching unless its version comment
(`# v3.5.0`, `# tag=v3.5.0`, `# ratchet:owner/action@v3`, `# pin@v3`) names a
release from before the default turned on, or names no release at all. And
setup-uv's default (`enable-cache: auto`) caches only on GitHub-hosted runners,
so with no input it counts only on one of GitHub's published hosted-runner
labels, and never inside a local composite action; `enable-cache: true` earns
it on any runner. The credit also waits for what the action actually reads:
setup-node and setup-go count only after a checkout of this repository to the
workspace root has finished earlier in the job (not one still running in the
same `parallel:` group or in the background), setup-go needs a `go.sum`, and a
`package-manager-cache` written as an expression is read as its default.
Input names are read case-insensitively in both directions (`Cache: npm` opts
in, `Cache: false` opts out). A date, a range such as `20.x` or an implausible
major in a version comment is not a release. Owner decision 3 adds one credit: a
SHA-pinned setup-node whose v5 and v6 rules disagree counts when every release
its version comment names caches. A default this scan cannot decide (an
unreadable or invalid `package.json`, an undecided SHA or branch ref, a version
comment naming no release, an unreadable `runs-on` for setup-uv, an uncheckable
`cache-dependency-path`, a checkout still running or moved) earns nothing and
is named in the evidence and in `data_sources.unresolved_setup_defaults`; a
`package.json` with a byte-order mark is decided off and named the same way. An explicit
cache input other than `false` is credited on any runner, as before. **Test
sharding** reads a matrix generated at runtime (`matrix: ${{ fromJSON(...) }}`)
as a matrix: it passes on a test-like job, and on any other job the job-name
rule decides, as for a literal matrix. Pass, not "not applicable", because the
fact as written is satisfied. **Job timeouts** is not applicable when every job
calls a reusable workflow and some call is cross-repo, since a calling job
cannot set `timeout-minutes`; one job with its own steps keeps it graded.
**Shallow checkout** applies the v0.1.4 reachability rule to a checkout step's
own `if:`, with the same fail-closed grammar. Every change moves a verdict
toward credit (fail to pass, fail to not applicable, or not applicable to
pass), so a check that newly becomes applicable arrives passing; the
withholding rules only narrow credit this change adds, since v0.1.4 credited no
default-on cache at all. Run against the v0.1.4 engine over the frozen
calibration controls, all eleven facts were identical on both, state and
evidence, when the controls' facts were pinned; a test pins those facts, and
re-checks the controls and an explicit list of OD-CS23 cases against the
v0.1.4 engine for any move away from credit (later changes that drop a
verdict on purpose are listed in an allowlist); committed corpus stamps carry
the new `spec_version` and nothing else. One edge: a repository whose only
applicable check was job timeouts (every job delegated cross-repo) moves from a
0 grade to the all-not-applicable refusal.

**v0.1.6 (2026-10-27, OD-CS24)**, on top of v0.1.5, closes places where a check passed, or failed, on
workflow content it never read, and credits GitHub's workflow lockfile. A
reusable workflow in the same repository that a pull-request job calls runs on
every pull request, so **shallow checkout** now grades it with the workflow
that calls it (a calling job that can never run on a pull request reaches
nothing, and a call that cannot be followed is named rather than graded).
Local composite actions are read from every `uses:` path — `./ci/setup` and
`$/tools/x`, not only `.github/actions/` — so their cache steps and unpinned
actions count. Whatever the scan could not read (a local action, a workflow
that does not parse, a step list that is not a list, a call it could not
follow) is named in the caching, pinning and shallow-checkout card evidence,
not only in `data_sources`. When a local action that is not in the checkout is the only thing
that could explain a missing dependency cache — nothing readable shows one,
no readable step is a `setup-*` action or carries a cache input, the action
runs on the PR path before an install, build or test step, and no install
runs without such an action before it — **dependency caching** is not
applicable and the card names the action (owner decision, 2026-10-27). A
wrong-case path, a path outside the repository or a file that does not
load is not such an action. A cache switch set by an expression — the input
the action itself reads: `bundler-cache` on setup-ruby, `enable-cache` on
setup-uv, `cache-disabled` on Gradle's actions and npm-install, `cache` on
any other `setup-*` action, action names in any case — earns credit only
when it resolves: each value it resolves to (a caller's `with:` value or the
input's default) is graded exactly as if written in place, and the step is
credited when any of them caches. One that does not resolve earns nothing,
is named, and never falls back to v0.1.5's default-on credit; an expression
on a key the action does not read leaves the action's default to decide. A
local action only an unread workflow runs earns no caching credit. A `uses:` path must be a spelling git tracks, since
a wrong-case path does not exist on a Linux runner. **Pinned action SHAs** now counts a job-level call to another
repository's reusable workflow, and counts a tag reference as pinned when
`.github/workflows/actions.lock` (public preview 2026-10-14, written by the
`gh actions-lock` CLI extension) lists it under its own workflow with a
full-commit entry; the evidence says how many were pinned that way. Inside a
local action, lockfile credit needs every reference to reach it through `$/`, because GitHub's
`gh actions-lock` documentation says a `./` action is never followed or
locked, and an entry under every workflow that reaches the action. A call to
another repository's reusable workflow is pinned only by a full SHA: the
`gh actions-lock` README states that such calls are not locked
(github/gh-actions-lock#179, open), even though GitHub's docs page says
reusable workflows are pinned, so until the tool locks them a lockfile entry
for one enforces nothing. A lockfile that cannot be read, is
malformed anywhere, sits at a wrong-case path, or carries a format version
other than the two GitHub documents (v0.0.2, v0.0.3) grants nothing. Recomputed over the
frozen calibration controls, no fact moves (91 and 82 hold). The rationale is
OD-CS24 in the spec's `decision_log`.

The v1-granular rubric (measured-magnitude gating, tiers, weights,
nine refusal conditions) was **punted before publication by owner decision
OD-CS15** — its record and rationale live in the spec's `decision_log`, and
its machinery remains available to a future v2 in git history.
