---
pattern: OPT47
impact: MEDIUM
class: data-driven
detector: manual
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml'
fix_strategy: redundant-push-pull_request-double-trigger
title_template: 'Redundant push + pull_request Double-Trigger'
category: 9
aliases:
  - opt47--redundant-push-pull_request-double-trigger
alias_note: 'Alias anchor: findings files stamped before the GFM anchor fix link this older slug; keep it so those deep links still land.'
---

# OPT47 — Redundant push + pull_request Double-Trigger

**Anti-pattern**: A workflow triggers on **both** `push` and `pull_request`, and the `push` trigger is **not restricted to the default branch**. Every commit on a PR branch then runs the workflow **twice** — once for the `push` event and once for the `pull_request` event — building the same `head_sha` on two runners. The `pull_request` run is the one that gates the merge; the `push` run is redundant compute.

Not a finding on structure alone — a repo may legitimately want push builds. It requires **measured** duplication.

**Detection heuristic**:

1. **Structural scan** — the workflow's `on:` includes both `pull_request` and a `push` whose `branches:` filter is absent (or lists branches beyond the default). A `push` scoped to `branches: [main]` does **not** double-fire on PR branches and is excluded.
2. **Positive instance evidence** — in the run history (all statuses, last 30 days), find commits where the **same `head_sha`** produced both a `pull_request`-family run and a `push` run **on a non-default branch**. Only non-default-branch pushes count: a push on the default branch sharing a PR sha is a post-merge (rebase/FF) validation run, which the fix keeps — counting it would be a false saving.
3. **Bounded savings** — size the redundant (`push`-event) runs' compute: `count(duplicated commits) × mean-per-run job-minutes`, extrapolated to the 30-day volume. Report the sample size (`n` runs) and the duplicated fraction.

**Fix**: Add a `branches:` filter to the `push:` trigger so push builds only run where you actually want them (typically the default branch); PRs continue to run via `pull_request`:

```yaml
on:
  push:
    branches: [main]
  pull_request:
```

**GUARDRAILS before removing push:**
1. **Confirm which check the merge requires.** If branch protection requires the **push**-triggered check (not the `pull_request` one), filtering push out leaves the required check unsatisfied and blocks merges. Verify the required status check is the `pull_request` run first. (This is why ci-speedup emits OPT47 as a measured bill finding but does **not** yet certify it wall-clock-neutral.)
2. **Confirm the push run has no SIDE EFFECT the PR run lacks.** A per-commit preview deploy, a sha-tagged image/artifact, or a cache warm makes the push run **not redundant** — filtering it would break that. The run list can't see side effects, so ci-speedup carves out release/deploy/publish-named workflows and warns; you must verify the rest.

**Tier-2 render note**: OPT47 remains a measured residual bill finding until its required-check and side-effect neutrality can be certified. Do not promote it to the neutral runner-minute section by pattern alone.

**When NOT to apply**: workflows that intentionally build both a branch's push and its PRs for different purposes (e.g. push publishes a preview, PR runs tests) — the two runs aren't redundant.

---
