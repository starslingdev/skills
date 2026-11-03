---
pattern: OPT98
impact: MEDIUM
class: data-driven
detector: actions-merge-queue-reproving
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml'
fix_strategy: merge-queue-reproving
title_template: 'The Merge Queue Re-proves What Is Already Proven'
category: 9
---

# OPT98 — The Merge Queue Re-proves What Is Already Proven

**TL;DR**: High PR volume makes repeated full validation expensive: a strict
up-to-date rule repeatedly invalidates PR checks, or the merge queue runs the
same full suite already run on the PR.

**Anti-pattern**: Independent agent-authored PRs invalidate each other's base,
or repeat identical work when assembled into the merged result.

**Detection heuristic**:

- **Strict rule at high volume:** reuse the classic required-status-check and
  active, default-branch ruleset responses. The rule is on when any source says
  so; it is off only when classic protection was read (a non-admin cannot read
  it, so a ruleset saying off is not enough). Require no merge queue: no
  merge-queue ruleset, no `merge_group` trigger and no cached `merge_group` run.
  Require at least 30 merged PRs/day: one REST search counts merges on the
  rule's branch in the 30 complete UTC days before the pin. Measure the
  rebase-churn share from at most 30 of the newest merged PRs' timelines: a
  two-parent commit (the base merged in) or a head force-push (a rebase, or an
  amend). Run pages cannot answer this; GitHub empties a run's PR list once the
  PR merges. An unread share says "not measured", never 0%. Unreadable rules,
  incomplete searches and an unparseable date pin withhold this case and say why
  in data sources.
- **Full suite in the queue:** a workflow triggered by both `pull_request` and
  `merge_group` runs the same jobs on both, with no affected-only scoping (the
  OPT70 signals: `nx affected`, `turbo --filter=...[`, `--affected`, `--changed`,
  `--onlyChanged`, a changed-files action) and no build or remote cache
  (`actions/cache` beyond dependency directories, Turbo/Nx Cloud tokens,
  `--remote_cache`, `--build-cache`, sccache). Expressions that don't select
  work (names, concurrency, runner labels, secrets, matrix values) don't count.
  Event-dependent conditions, commands, inputs or matrices, reusable workflows
  and unread local actions make the scope unknown; each such workflow is named
  in data sources. Package scripts and third-party actions are not read, so the
  measured ratio is the second proof: at least 15 successful first-attempt runs
  of each event, and a merge-group median from 0.8 to 1.5 times the PR median
  (run start to completion update in the cached run pages). Above 1.5 the queue
  is doing different work and is reported as such, not as re-proving. The
  finding names the workflow and its jobs.

**Fix recipe**:

1. Replace the high-volume up-to-date rule with a merge queue.
2. In the queue, scope work to what the queued change affects relative to the
   queue's base, following [OPT70](#opt70--scope-the-buildtest-to-only-what-changed).
3. Reuse build and test caches main writes; PRs and queue entries read only.
   Follow [OPT74](#opt74--trust-boundary-forced-cold-work-producerconsumer-split).
4. Use content-addressed remote caches to avoid re-proving an identical tree
   that main already proved green.

**Guardrail**: Keep every required check on the merged result. Scoping work
is allowed; dropping required checks is not. A changed-set resolution error
must run the full suite. Shared inputs must bypass scoping. Compare scoped and
full results in parallel before rollout and preserve the full post-merge safety net.

**Sizing**: Neither case receives wall-clock or runner-minute credit. Rebase
churn is observed, and the queue and PR medians are measured; the affected-only
counterfactual is not modeled.
