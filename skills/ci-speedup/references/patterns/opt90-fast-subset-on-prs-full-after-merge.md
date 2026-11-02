---
pattern: OPT90
impact: HIGH
class: data-driven
detector: manual
risk: MEDIUM
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml'
fix_strategy: fast-subset-on-prs-full-after-merge
title_template: 'Every pull request pays for coverage that could run after merge'
category: 7
---

# OPT90 — Every Pull Request Pays for Coverage That Could Run After Merge

**Not auto-detected yet.** Like OPT86 and OPT88, no pass emits OPT90, so the audit never reports it on its own and never claims to have checked it. No audit phase applies it; a reader applies it by hand, reading the pull-request workflows and their run history against the heuristic below.

**TL;DR**: Every pull request waits for the full matrix (every OS, every runtime version, every heavyweight configuration) even though a small subset catches almost every failure. Run the subset that catches what matters before merge, and move the rest to a run after merge or on a schedule, where it still gates releases and pages someone when it fails.

**Anti-pattern**: A `pull_request` workflow on the long pole runs a wide matrix or a heavyweight suite on every push to every PR. Most legs only fail when another, cheaper leg fails too, so they add wait and runner minutes to every PR without changing the merge decision.

**Relation to other patterns**: [OPT32](#opt32--missing-pathspaths-ignore-on-expensive-workflows), [OPT39](#opt39--multi-language-matrix-without-path-filter) and [OPT70](#opt70--scope-the-buildtest-to-only-what-changed) skip work a PR's changes cannot affect. OPT90 keeps all the work and changes *when* part of it runs. [OPT35](#opt35--missing-fail-fast-on-non-diagnostic-matrix-dimensions) stops a failing matrix early; OPT90 shrinks the matrix a PR waits on.

**Detection heuristic** (manual; read, never inferred):

1. A `pull_request`-triggered workflow on the merge path runs a matrix of several OS, runtime or configuration legs, or a heavyweight suite, and it is on the long pole or a large share of the PR's runner minutes. A PR path that already runs a small subset while the wide matrix runs after merge or on a schedule is the recommended shape, not an instance.
2. In run history, count each leg's leg-unique failures: runs where that leg failed and every other leg passed, ignoring skipped legs and the aggregator job that only reports the others' results. Use a window long enough to cover normal PR traffic, not the last few hours. A leg with no leg-unique failures is a candidate to move. A leg that does fail alone is catching something no other leg catches (a subset on one OS cannot stand in for another OS); moving it means finding those failures after merge instead, which is the team's call to make with the count in hand.
3. The repo can host the rest after merge: a `push` trigger on the default branch, a merge queue, or a `schedule`.

**Fix recipe**:

1. Choose the PR subset from the data in step 2: typically the oldest and newest supported runtime on the main OS, plus any leg the team keeps because it fails alone. The subset can be empty for a class of check another required workflow already covers on PRs.
2. Move the remaining legs to a workflow that runs on `push` to the default branch (and `workflow_dispatch`), on a merge queue, or on a `schedule`, so they run against the merged tree.
3. Keep one required status check on the PR path so branch protection stays simple, and do not leave a moved job listed as required, or PRs will wait on a check that never runs.
4. Make a red post-merge run impossible to miss: it notifies an owner, and it blocks a release.

**Guardrail**: moving coverage after merge means a regression can land on the default branch before it is caught. Keep that window short and owned: every merged commit must be covered (a post-merge or merge-queue run starts on every merge; a scheduled run tests the latest merged tree, so it must run often enough to keep the window short), its failures must reach someone, and a release must not ship a commit the full matrix has not passed. Never move a leg whose failures are often unique to it.

**Sizing: uncredited**. No `_SIZING` model. The ceiling is the measured share of the PR's wait spent on the legs that move; what remains is the subset's time.

**Real-world example (OpenAI)**, their numbers, not a forecast for your repo:

- openai/codex [#16072](https://github.com/openai/codex/pull/16072) (2026-03-28) split the Rust checks into a PR-time workflow and "a full post-merge Cargo workflow", because the old one "made every PR pay for the full Cargo matrix even though most of that coverage is better treated as post-merge verification". The PR-time workflow kept no build or test legs for the main crates (only formatting, `cargo shear`, a custom lint, and that lint's own package tests when it changes), because a separate workflow already ran build and test on every PR. No timing was published.
- openai/openai-python [#3537](https://github.com/openai/openai-python/pull/3537) (2026-07-24) adopted this shape when it added version coverage: "test the minimum/current runtimes on normal CI and the full supported matrix on a schedule". The full matrix was new, so this is the recommended shape rather than a measured saving; no timing was published.
- openai/codex [#20585](https://github.com/openai/codex/pull/20585) (2026-05-01): "pull requests get the fast Windows cross-compiled Bazel test leg, while post-merge pushes to `main` run both that fast cross leg and a fully native Windows Bazel test leg". The PR reports a native Windows test "in about 28m12s" against the cross-compiled PR leg's "Windows test 9m16s". That gain comes from two changes the PR does not separate (cross-compiling, [OPT89](#opt89--the-slowest-platform-compiles-its-own-tests), and keeping the V8 and code-mode unit tests out of the PR leg), so none of it is a number for this pattern alone.

**Risk**: **MEDIUM**. A regression can reach the default branch before the post-merge run catches it; the guardrail keeps that visible and short.
