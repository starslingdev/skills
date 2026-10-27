---
pattern: OPT92
impact: MEDIUM
class: data-driven
detector: linux-benchmark
risk: MEDIUM
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml'
fix_strategy: benchmark-portable-job-on-linux
title_template: 'A job is a candidate for a Linux benchmark'
category: 12
---

# OPT92 — A Job Is a Candidate for a Linux Benchmark

**TL;DR:** benchmark a narrowly supported portable validation job on Linux. Its current runtime is an observation, not a saving, a price ceiling or evidence that Linux will be faster.

**Detection:** positive operation rules, never absence of native-tool names. The operation rules cover committed-content whitespace checks, isolated CPython syntax compilation and dedicated Swift source formatting. Every step must resolve to one of these rules; setup alone is insufficient:

- `git show --check --format= --no-ext-diff --no-textconv HEAD` checks committed content without external diff drivers or text conversion.
- `python -I -S -m compileall -q <relative-path>` compiles syntax without executing project modules. Isolation disables environment-driven Python startup, local module shadowing and site hooks. It requires an exact CPython version and matching explicit architecture from the supported setup action. The audited commit's tree must contain Python input files under that path and no symlinks or submodules; an unread tree holds.
- Checkout and Python setup accept only the selected revisions and restricted inputs documented in `scripts/linux_benchmark.py`. Other revisions, local/composite actions, downloaded tools and wrappers hold. No project script is assumed portable without reading it; v1 has no project-script admission rule.
- Dedicated Swift formatting accepts only the reviewed checkout and Swift setup revisions in `scripts/linux_benchmark.py`, Swift 6.1.3, an explicit static JSON configuration and tracked regular inputs. It recognizes literal `swift format lint` and a bounded copy/format/diff block; application builds, tests, package plugins, unknown shell commands and existing backup destinations hold. The setup action does not fail on a version mismatch, so its own log section must confirm the selected version. Its Linux installer requires x64; the benchmark names Ubuntu 24.04 x64 and records any architecture change. Missing source/configuration or scoped log evidence holds.

The runner must have an explicit supported hosted label and architecture. Runner aliases, expressions, custom runners, native builds/tests, unknown working directories or environments, conditional work, dependency jobs and publishing paths hold. Literal non-OS matrices with explicit unique leg names can qualify: at most four axes and 32 legs, literal string values, and direct `matrix.<axis>` substitutions only. Each leg independently passes the operation rules and matches its own history. Generated default names, include/exclude, unresolved expressions and possible name collisions hold. Matrix expansion never resolves a runner expression. Generic .NET, Cargo, Flutter, Electron, Tauri, React Native and CMake commands never imply portability. The same work appearing on a Linux matrix leg does not establish redundant native coverage.

**Measurement:** require a clean workflow checkout of the audited repository and successful executed occurrences of the exact job, on the same runner label, in the collector's sample window. Whitespace/Python rules require push observations at the audited commit. The dedicated Swift rule also accepts push/PR observations whose executed workflow exactly matches the audited workflow: resolve the checkout commit and action revisions from scoped action logs, read the workflow and input tree at that commit, and independently check the audited input tree. The explicit formatter configuration must match across both commits. Never substitute a PR head SHA for its executed merge SHA. At most 32 additional API requests are allowed per audit; missing, skipped, ambiguous, unreadable or budget-limited evidence holds. Record sample count, median job runtime, observation links and available workflow frequency. Do not extrapolate a saving from frequency.

A manual trigger alone does not establish a release path. A workflow offering `workflow_dispatch` can qualify from its eligible push history when all work satisfies the operation rules; release markers and deployment environments still hold.

**Fix recipe:** benchmark unsigned validation on Linux at the same commit, with equivalent toolchain versions, dependencies and architecture where possible. Record Linux-specific setup changes. Compare collected and executed tests, outputs and whole-job duration. Retain the native job until representative runs demonstrate equivalent coverage. Keep Apple builds, native platform tests and Windows packaging native.

**Sizing: uncredited.** `wall_clock_p50_s=None`, `runner_min_saving=None`, `realization="none"`, `sizing_basis="uncredited"`. Render once at the matching pole or in the dedicated Linux benchmark section. Never include the card in totals, Tier 2 or Also noticed. Held candidates use the shared disclosure with their actual reason. No prices, weighted minutes or predicted savings.

**Risk: MEDIUM.** Syntax or whitespace equivalence is a scoped claim. It does not establish equivalent application behavior across operating systems.

The Swift rule follows the selected [setup action bundle](https://github.com/swift-actions/setup-swift/blob/7ca6abe6b3b0e8b5421b88be48feee39cbf52c6a/dist/index.js) and [formatter release](https://github.com/swiftlang/swift-format/tree/4c2421c14ce852fd455c257b4e8b9cae156302f1). Log provenance reads runner preparation and the initial checkout/setup steps; later project output cannot establish action revisions, the executed commit or toolchain version.

Runner architectures follow the [GitHub-hosted runner reference](https://docs.github.com/en/actions/reference/runners/github-hosted-runners). Action inputs are restricted to the selected [checkout revision](https://github.com/actions/checkout/blob/11d5960a326750d5838078e36cf38b85af677262/action.yml) and [Python setup revision](https://github.com/actions/setup-python/blob/a26af69be951a213d495a4c3e4e4022e16d87065/action.yml). Updating these supported revisions requires another contract review and regression check.
