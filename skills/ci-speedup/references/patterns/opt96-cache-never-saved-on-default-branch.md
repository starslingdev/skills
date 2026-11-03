---
pattern: OPT96
impact: MEDIUM
class: static
detector: yaml-workflow-correlated
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml'
fix_strategy: cache-never-saved-on-default-branch
title_template: 'Cache Never Saved on the Default Branch'
category: 1
---

# OPT96 — Cache Never Saved on the Default Branch

**TL;DR**: A dependency cache saved only by PR runs cannot seed the next PR's
branch. Save the same cache on the default branch so new branches can restore it.

**Anti-pattern**: A PR job restores a dependency cache, but every save point
is in a PR-only workflow or behind a condition excluding the default branch.
GitHub cache scoping permits reads from the current branch and the base branch;
one PR's cache cannot warm a different PR. Short-lived branches repeatedly pay
for a cold install.

**Detection heuristic**: Correlate `actions/cache`, its separate `restore` and
`save` actions, and the `cache:` inputs of `setup-node`, `setup-python`,
`setup-go` (cached by default from v4), and `setup-java`, including those
inside local composite actions. Normalize expression-valued platform and lockfile
hash components to a stable key prefix, including `restore-keys` fallbacks.
A save is reachable through a default-branch push (including branch globs)
or `schedule` unless a literal job or step condition excludes
it; each event's conditions are evaluated for that event. A `merge_group` save
does not count: it lands on the temporary `gh-readonly-queue/…` ref, which a
new PR branch cannot restore from. Unknown conditions
count as reachable. Follow local reusable workflow calls and composite
actions; unresolved ones suppress the finding and appear in `scan_incomplete`.
Use repository metadata for the default branch when available; otherwise
assume `main`/`master` and disclose the assumption in the evidence.

Confirm the static candidate using the existing cache-distribution sampling,
run only for a job a static finding names (matched by job id or its `name:`):
dependency-cache restore hit/miss lines on PR runs versus the existing bounded
push probe. Confirmation requires a median PR miss rate of at least 50% and
push logs showing neither a restore nor a save, or a job absent from push
runs. A default-branch restore proves the cache is already seeded. Below the 25% miss
tail, present it as **check this**, not a savings lever. Missing log evidence
is an uncertainty, not proof that a cache was never saved.

**Fix recipe** — pick the first suitable option:

1. Add `push: branches: [<default>]` to the existing restore-and-save workflow
   if running that workflow after merge is acceptable.
2. Otherwise add a small `ci-warm-caches`-style workflow on default-branch
   `push` plus `workflow_dispatch`. Run only the dependency install and save
   using the same key template as the PR workflow.
3. Add `actions/cache/save` inside an existing push-triggered job, guarded by
   `if: github.ref == 'refs/heads/<default>'`.

**Guardrail**: Keep the lockfile's `hashFiles(...)` in the key. Widening the
key to force a hit risks stale dependencies. The default-branch save must use
the **same key template** as the PR restore. Fork PRs can read base-branch
caches too, so default-branch seeding benefits them without granting write
access or secrets. Check OPT79's net-negative cache economics before adding a
warmer for a cheap install.

**Relations**: OPT37 concerns writer/reader races within the same event;
OPT74 covers cold work at the fork trust boundary; OPT6 covers unstable keys;
OPT79 measures caches whose restore and save cost exceeds their benefit.

**Sizing**: Use the measured install time on a cache miss minus its time on a
hit, multiplied by the PR miss share. Cap that saving by the existing
within-workflow and cross-workflow floors. With no hit sample, leave savings
uncredited: name the install step and its miss time without a saving number.
