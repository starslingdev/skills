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
A save is reachable through a push to the default branch (including branch
globs), `schedule`, `workflow_run`, or `repository_dispatch` unless a literal job
or step condition excludes it; each event's conditions are evaluated for that
event. A tags-only push filter is non-branch only when neither `branches` nor
`branches-ignore` is set. A `merge_group` save does not count: it lands on the
temporary `gh-readonly-queue/…` ref, which a new PR branch cannot restore from.
A `workflow_dispatch`-only save does not seed either, but the finding's evidence
says a manual warmer exists. Unknown conditions count as reachable.
Follow local reusable workflow calls and composite actions. A missing local
reusable workflow file cannot run, so it hides no writer.

Key matching treats every `${{ }}` expression as a wildcard. A writer matches
when either key template can match the other (`restore-keys` act as prefixes).
If a match depends only on a template with no literal text, it is undecidable:
the reader is held back and disclosed, never flagged. A reader key made only of
expressions with no matching writer at all is flagged.

A writer whose saves cannot be read on a seed event (a remote reusable workflow,
a remote action outside a small known-non-caching allowlist of `actions/checkout`,
`actions/upload-artifact`, `actions/download-artifact` and
`actions/github-script`, or an unreadable local composite action) holds back only
readers whose workflow (or caller chain) references the same repository or local
path, or whose literal key prefix appears in that step's `with:` inputs.
Unrelated readers are still flagged. Held-back readers produce no finding and are
listed in the scan output field `opt96_unresolved` (workflow file, job, family,
reason), shown as a "cache seeding: held back" row in the report's Data sources
table. They are never written to `scan_incomplete` and never dropped silently. A
detector error is caught and disclosed there too.

The default branch is read from `--default-branch`; if omitted, from the
checkout's origin/HEAD, else `main`/`master` is assumed. The evidence says which
source was used. The measured pass cross-checks the scan's branch against GitHub's
default branch; on a mismatch every OPT96 finding is demoted to **check this** and
the mismatch is disclosed in `opt96_unresolved`.

Confirm the static candidate using the existing cache-distribution sampling,
run only for a job a static finding names (matched by job id or its `name:`):
dependency-cache restore hit/miss lines on PR runs versus the existing bounded
push probe.
Confirmation requires a median upstream PR miss rate of at least 50% and either (a) every sampled default-branch push log of that job shows only full cache misses and no save, (b) the job did not run on any sampled push run, or (c) the workflow has no push runs at all (the static reachability finding supplies the missing writer). Only completed push jobs count, and the (c) case requires a workflow with no default-branch trigger.
Queued, in-progress, cancelled or similarly named push jobs are unknown and never confirm. A default-branch restore proves the cache is already seeded.
Present it as **check this**, not a savings lever, when the mean PR miss rate is below 25% or the confirmation above is not met. An unconfirmed finding carries a specific reason in its size note: no matching pole, pole log not fetched, another detector took precedence, confirmation failed, push probe unavailable, or default-branch mismatch. Missing log evidence
is an uncertainty, not proof that a cache was never saved.

A reader inside a local reusable workflow carries its callers (caller workflow,
caller job chain, callee display name) so it grounds on the caller's pole
(`caller job / callee job`).

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
