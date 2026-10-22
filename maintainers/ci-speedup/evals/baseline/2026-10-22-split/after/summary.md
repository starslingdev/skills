# ci-speedup catalog split: AFTER arm (one file per pattern), 2026-10-22

The skill under test is `skills/ci-speedup/` of branch
`ci-speedup/catalog-split` at `7490804` (cut from `main` at `d6f8c77`):
patterns in `references/patterns/opt<N>-*.md` with `INDEX.md`, `SKILL.md`
routing to them, the compiled `optimization-patterns.md` still present.

- **Model:** `claude-opus-5-5` (`--model claude-opus-5-5`), Claude Code
  2.1.308, run locally on macOS on 2026-10-22, in the same batch as the
  other arm.
- **Harness:** `maintainers/scripts/run_skill_evals.py` from `main` at
  `d6f8c77`, byte-identical in both arms, with `maintainers/scripts/eval_reading.py`
  from this branch (the version that counts `references/patterns/` files as
  catalog) in both arms.
- **Command, per entry:**
  `python3 maintainers/scripts/run_skill_evals.py --skill ci-speedup --id <id> --runs 5 --model claude-opus-5-5`,
  run from an export of the tree named above. Entries 10, 11 and 13 ran one
  after another (scratch paths under `/tmp` could collide), and the two
  arms' 10/11/13 chains never overlapped; entry 12 ran beside them from its
  own export.
- **Files:** one results JSON per invocation, exactly as the runner wrote
  it (no hand edits). The table is the output of
  `python3 maintainers/ci-speedup/scripts/eval_baseline_summary.py maintainers/ci-speedup/evals/baseline/2026-10-22-split/after`.
- **Entry 11, run 3:** its two review subagents ran a reading command that
  named the `references/patterns/` directory and printed about 1.1 KB of
  lines without a file prefix; the accounting splits such output evenly over
  every file the command names, so 12 bytes land on each of the 92 files.
  The session was not kept (it passed), so which command it was is not
  recorded.

| id | case | runs | passed | not run | reference bytes | of which catalog | reference files | catalog opened | input tokens | wall time (s) |
|---|---|---|---|---|---|---|---|---|---|---|
| 10 | `full-run-synthetic` | 5 | 5 | 0 | 0 (0 to 0) | 0 (0 to 0) | 0 (0 to 0) | 0/5 | 320,152 (269,177 to 334,992) | 41 (30 to 45) |
| 11 | `quality-review-opt81` | 5 | 5 | 0 | 22,290 (10,497 to 96,821) | 14,766 (10,497 to 52,241) | 2 (1 to 93) | 5/5 | 406,378 (348,675 to 797,361) | 58 (50 to 126) |
| 12 | `gap-fill-coverage-lookup` | 5 | 5 | 0 | 9,060 (6,869 to 33,843) | 9,060 (6,869 to 32,807) | 2 (2 to 3) | 5/5 | 138,004 (133,856 to 154,439) | 19 (18 to 22) |
| 13 | `no-catalog-read-needed` | 5 | 5 | 0 | 0 (0 to 0) | 0 (0 to 0) | 0 (0 to 0) | 0/5 | 171,911 (171,639 to 176,381) | 29 (20 to 38) |

Pass count per check:

| id | check | passed |
|---|---|---|
| 10 | `ci-speedup-skill-invoked` | 5/5 |
| 10 | `data-pass-ran` | 5/5 |
| 10 | `report-drills-ci-test-as-long-pole-1` | 5/5 |
| 10 | `self-check-passed` | 5/5 |
| 10 | `nothing-committed-or-pushed` | 5/5 |
| 11 | `ci-speedup-skill-invoked` | 5/5 |
| 11 | `data-pass-ran` | 5/5 |
| 11 | `names-the-catalog-only-fork-caveat` | 5/5 |
| 11 | `read-some-reference-material` | 5/5 |
| 11 | `read-the-opt81-catalog-entry` | 5/5 |
| 12 | `ci-speedup-skill-invoked` | 5/5 |
| 12 | `names-opt86` | 5/5 |
| 12 | `read-some-reference-material` | 5/5 |
| 13 | `ci-speedup-skill-invoked` | 5/5 |
| 13 | `data-pass-ran` | 5/5 |
| 13 | `states-the-measured-wait` | 5/5 |
| 13 | `names-the-slowest-job` | 5/5 |

Split catalog files read:

| id | compiled file opened | `patterns/` files read |
|---|---|---|
| 10 | 0/5 | none |
| 11 | 0/5 | `patterns/INDEX.md` 1/5 runs, 12 (12 to 12) bytes; `patterns/README.md` 1/5 runs, 12 (12 to 12) bytes; `patterns/_frame.md` 1/5 runs, 12 (12 to 12) bytes; `patterns/opt1-unnecessary-tool-install.md` 1/5 runs, 12 (12 to 12) bytes; `patterns/opt11-redundant-environment-variables.md` 1/5 runs, 12 (12 to 12) bytes; `patterns/opt12-duplicated-setup-across-jobs.md` 1/5 runs, 12 (12 to 12) bytes; `patterns/opt13-build-step-in-jobs-that-dont-need-it.md` 1/5 runs, 12 (12 to 12) bytes; `patterns/opt14-repeated-checkout-setup-without-artifact-handoff-and-slow-to.md` 1/5 runs, 12 (12 to 12) bytes; `patterns/opt15-cross-workflow-build-redundancy.md` 1/5 runs, 12 (12 to 12) bytes; `patterns/opt16-within-job-duplicate-commands.md` 1/5 runs, 12 (12 to 12) bytes; `patterns/opt17-sleep-based-container-readiness.md` 1/5 runs, 12 (12 to 12) bytes; `patterns/opt18-all-containers-started-for-single-service-tests.md` 1/5 runs, 12 (12 to 12) bytes; `patterns/opt19-test-source-sleep-dominance-includes-playwright-waitfortimeo.md` 1/5 runs, 12 (12 to 12) bytes; `patterns/opt2-uncached-large-downloads.md` 1/5 runs, 12 (12 to 12) bytes; `patterns/opt20-unpinned-docker-image-tags.md` 1/5 runs, 12 (12 to 12) bytes; `patterns/opt21-unnecessary-needs-dependencies.md` 1/5 runs, 12 (12 to 12) bytes; `patterns/opt22-sequential-workflows-via-workflow-run.md` 1/5 runs, 12 (12 to 12) bytes; `patterns/opt23-single-threaded-matrix-max-parallel-1.md` 1/5 runs, 12 (12 to 12) bytes; `patterns/opt24-long-test-job-without-sharding.md` 1/5 runs, 12 (12 to 12) bytes; `patterns/opt25-shard-imbalance.md` 1/5 runs, 12 (12 to 12) bytes; `patterns/opt26-outdated-action-major-versions.md` 1/5 runs, 12 (12 to 12) bytes; `patterns/opt27-duplicate-setup-node-calls-in-same-job.md` 1/5 runs, 12 (12 to 12) bytes; `patterns/opt28-full-git-history-checkout.md` 1/5 runs, 12 (12 to 12) bytes; `patterns/opt29-merge-queue-skip-at-step-level-only.md` 1/5 runs, 12 (12 to 12) bytes; `patterns/opt3-turbo-cache-misconfiguration.md` 1/5 runs, 12 (12 to 12) bytes; `patterns/opt30-matrix-jobs-without-job-level-conditional.md` 1/5 runs, 12 (12 to 12) bytes; `patterns/opt31-conditional-step-with-unconditional-setup.md` 1/5 runs, 12 (12 to 12) bytes; `patterns/opt32-missing-paths-paths-ignore-on-expensive-workflows.md` 1/5 runs, 12 (12 to 12) bytes; `patterns/opt33-no-draft-pr-gating-on-expensive-jobs.md` 1/5 runs, 12 (12 to 12) bytes; `patterns/opt34-no-changed-package-filtering.md` 1/5 runs, 12 (12 to 12) bytes; `patterns/opt35-missing-fail-fast-on-non-diagnostic-matrix-dimensions.md` 1/5 runs, 12 (12 to 12) bytes; `patterns/opt36-cron-schedule-too-frequent.md` 1/5 runs, 12 (12 to 12) bytes; `patterns/opt37-workflow-trigger-dependency-gap-cache-race.md` 1/5 runs, 12 (12 to 12) bytes; `patterns/opt38-non-content-trigger-event-types-pr-edited-etc.md` 1/5 runs, 12 (12 to 12) bytes; `patterns/opt39-multi-language-matrix-without-path-filter.md` 1/5 runs, 12 (12 to 12) bytes; `patterns/opt4-docker-layer-cache-missing.md` 1/5 runs, 12 (12 to 12) bytes; `patterns/opt40-monorepo-job-runs-regardless-of-affected-app.md` 1/5 runs, 12 (12 to 12) bytes; `patterns/opt41-turbo-force-true-disabling-all-caching.md` 1/5 runs, 12 (12 to 12) bytes; `patterns/opt42-turbo-cache-remote-rw-in-release.md` 1/5 runs, 12 (12 to 12) bytes; `patterns/opt43-excessive-queue-time.md` 1/5 runs, 12 (12 to 12) bytes; `patterns/opt44-concurrency-group-too-restrictive.md` 1/5 runs, 12 (12 to 12) bytes; `patterns/opt45-missing-concurrency-groups.md` 1/5 runs, 12 (12 to 12) bytes; `patterns/opt46-superseded-runs-not-cancelled-missing-concurrency-or-cancel-.md` 1/5 runs, 12 (12 to 12) bytes; `patterns/opt47-redundant-push-pull_request-double-trigger.md` 1/5 runs, 12 (12 to 12) bytes; `patterns/opt48-high-job-level-failure-rate-15-over-30d.md` 1/5 runs, 12 (12 to 12) bytes; `patterns/opt49-step-duration-outlier.md` 1/5 runs, 12 (12 to 12) bytes; `patterns/opt5-pnpm-store-not-cached-or-wrong-setup-order.md` 1/5 runs, 12 (12 to 12) bytes; `patterns/opt50-post-steps-taking-too-long.md` 1/5 runs, 12 (12 to 12) bytes; `patterns/opt51-install-to-test-ratio-50.md` 1/5 runs, 12 (12 to 12) bytes; `patterns/opt52-turbo-tasks-missing-outputs-in-turbo-json.md` 1/5 runs, 12 (12 to 12) bytes; `patterns/opt53-unstable-env-vars-invalidating-turbo-cache.md` 1/5 runs, 12 (12 to 12) bytes; `patterns/opt54-full-repo-pnpm-r-where-package-filters-are-possible.md` 1/5 runs, 12 (12 to 12) bytes; `patterns/opt55-vitest-running-in-watch-dev-mode-in-ci.md` 1/5 runs, 12 (12 to 12) bytes; `patterns/opt56-playwright-traces-videos-uploaded-unconditionally.md` 1/5 runs, 12 (12 to 12) bytes; `patterns/opt57-missing-timeout-minutes-on-known-flaky-integration-jobs.md` 1/5 runs, 12 (12 to 12) bytes; `patterns/opt58-turbo-tasks-missing-inputs-in-turbo-json.md` 1/5 runs, 12 (12 to 12) bytes; `patterns/opt59-runtime-only-env-vars-in-turbo-globalenv.md` 1/5 runs, 12 (12 to 12) bytes; `patterns/opt6-cache-key-entropy-too-high-or-unstable.md` 1/5 runs, 12 (12 to 12) bytes; `patterns/opt60-turbo-ci-configuration-missing.md` 1/5 runs, 12 (12 to 12) bytes; `patterns/opt61-missing-dependency-caching.md` 1/5 runs, 12 (12 to 12) bytes; `patterns/opt62-build-artifacts-destroyed-before-every-run.md` 1/5 runs, 12 (12 to 12) bytes; `patterns/opt63-dependency-install-with-cache-disabled.md` 1/5 runs, 12 (12 to 12) bytes; `patterns/opt64-repeated-workflow-attempts-from-same-failing-job.md` 1/5 runs, 12 (12 to 12) bytes; `patterns/opt65-billing-rounding-waste-from-tiny-matrix-legs.md` 1/5 runs, 12 (12 to 12) bytes; `patterns/opt66-sku-arbitrage-ceiling.md` 1/5 runs, 12 (12 to 12) bytes; `patterns/opt68-broken-step-masked-by-continue-on-error.md` 1/5 runs, 12 (12 to 12) bytes; `patterns/opt69-dead-workflow-env-vars-config.md` 1/5 runs, 12 (12 to 12) bytes; `patterns/opt7-pnpm-version-drift-across-workflows.md` 1/5 runs, 12 (12 to 12) bytes; `patterns/opt70-scope-build-test-to-changed.md` 1/5 runs, 12 (12 to 12) bytes; `patterns/opt71-expensive-non-required-check.md` 1/5 runs, 12 (12 to 12) bytes; `patterns/opt72-redundant-work-ratio.md` 1/5 runs, 12 (12 to 12) bytes; `patterns/opt73-shared-substep-floor.md` 1/5 runs, 12 (12 to 12) bytes; `patterns/opt74-trust-boundary-cache-split.md` 1/5 runs, 12 (12 to 12) bytes; `patterns/opt75-decompose-inherent-cost-pole.md` 1/5 runs, 12 (12 to 12) bytes; `patterns/opt76-submodule-lfs-checkout-payload.md` 1/5 runs, 12 (12 to 12) bytes; `patterns/opt77-repeated-fixed-setup-across-independent-small-jobs.md` 1/5 runs, 12 (12 to 12) bytes; `patterns/opt78-test-isolation-shared-module-registry.md` 1/5 runs, 12 (12 to 12) bytes; `patterns/opt79-cache-costs-more-than-it-saves.md` 1/5 runs, 12 (12 to 12) bytes; `patterns/opt8-cache-key-granularity-mismatch.md` 1/5 runs, 12 (12 to 12) bytes; `patterns/opt80-checkout-stalls-on-the-tail.md` 1/5 runs, 12 (12 to 12) bytes; `patterns/opt81-same-job-faster-on-another-runner.md` 5/5 runs, 14,766 (10,497 to 51,149) bytes; `patterns/opt82-lint-builds-the-whole-type-graph.md` 1/5 runs, 12 (12 to 12) bytes; `patterns/opt83-run-independent-pole-steps-in-parallel.md` 1/5 runs, 12 (12 to 12) bytes; `patterns/opt84-slow-tool-on-pole-has-native-drop-in.md` 1/5 runs, 12 (12 to 12) bytes; `patterns/opt85-run-tests-in-fewer-processes.md` 1/5 runs, 12 (12 to 12) bytes; `patterns/opt86-use-prebuilt-dependency-packages.md` 1/5 runs, 12 (12 to 12) bytes; `patterns/opt87-stub-slow-external-dependency-in-tests.md` 1/5 runs, 12 (12 to 12) bytes; `patterns/opt88-measure-test-concurrency.md` 1/5 runs, 12 (12 to 12) bytes; `patterns/opt89-build-once-test-natively.md` 1/5 runs, 12 (12 to 12) bytes; `patterns/opt9-tool-specific-cache-flag-not-enabled.md` 1/5 runs, 12 (12 to 12) bytes; `patterns/opt90-fast-subset-on-prs-full-after-merge.md` 1/5 runs, 12 (12 to 12) bytes; `patterns/opt91-profile-the-compile.md` 1/5 runs, 12 (12 to 12) bytes |
| 12 | 0/5 | `patterns/INDEX.md` 5/5 runs, 2,840 (649 to 26,661) bytes; `patterns/opt86-use-prebuilt-dependency-packages.md` 5/5 runs, 6,220 (3,591 to 6,220) bytes |
| 13 | 0/5 | none |
