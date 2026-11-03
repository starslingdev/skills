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
  active, default-branch ruleset responses. Require a readable strict rule,
  no merge-queue ruleset or declared merge-group workflow, and at least 30
  merged PRs/day. One REST search counts merges in the previous 30 complete
  UTC days; its 100 most recently updated result identities bound the churn sample. Report
  the share with sampled CI runs on different base SHAs, explicitly a lower
  bound because the run history is sampled. Unreadable rules or incomplete
  search results withhold this case and disclose why in data sources.
- **Full suite in the queue:** both events declare the same unconditional
  jobs without affected-only commands or build/remote cache restoration.
  Unknown conditions and reusable calls are withheld. Require the median
  successful first-attempt merge-group run to last at least 80% of the PR
  median, measured from run start to completion update in the cached run pages.
  Report both medians and sample counts; missing event timings are disclosed.

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
