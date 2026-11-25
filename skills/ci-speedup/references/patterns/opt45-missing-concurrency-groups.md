---
pattern: OPT45
impact: HIGH
class: static
detector: yaml-path-absent
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml'
fix_strategy: missing-concurrency-groups
title_template: 'Missing Concurrency Groups'
category: 9
---

# OPT45 — Missing Concurrency Groups

**TL;DR**: Nothing cancels superseded runs, so a branch can have several runs of the same workflow going at once after each push — whether the runs come from `pull_request` or from `push`.

**Anti-pattern**: No concurrency group on a `pull_request`- **or** `push`-triggered workflow, allowing multiple runs for the same branch to pile up.

**Detection heuristic**:

```bash
# Flag workflows triggered by pull_request OR push that declare no
# `concurrency:` block (top-level or per-job).
```

**Fix**: Add a concurrency group and scope the *cancellation* with an expression — a bare `cancel-in-progress: true` also cancels in-flight runs on `main` and on release tags, killing a half-finished deploy, publish, or migration. The detector fires on `push`-triggered workflows too, so the recipe must be safe on them:

```yaml
concurrency:
  group: ${{ github.workflow }}-${{ (github.ref == format('refs/heads/{0}', github.event.repository.default_branch) || startsWith(github.ref, 'refs/tags/')) && github.run_id || format('{0}/{1}', github.event.pull_request.head.repo.full_name || github.repository, github.head_ref || github.ref_name) }}
  cancel-in-progress: >-
    ${{ github.event_name == 'pull_request'
    && github.head_ref != github.event.repository.default_branch }}
```

`cancel-in-progress` accepts an expression, so the predicate is re-evaluated per event: `push`, `merge_group`, `schedule`, and tag builds all evaluate to `false`, so none of them is cancelled; only superseded PR runs are cancelled.

**Why default-branch and tag runs are keyed on `github.run_id`.** By default (`queue: single`), GitHub keeps at most one running and one pending run in a concurrency group. If a third run joins while one is running and another is pending, the new run replaces the older pending run even when `cancel-in-progress` is false. A group shared by every push to the default branch would therefore drop the middle of three quick merges, and a scheduled run on the default branch could replace a waiting push run. Giving each default-branch and tag run its own `run_id` group means every one of them runs; this is the same combined key as OPT46.

**The group key and the cancel predicate pull against each other — this is the whole design tension, state it before you touch either.** On every other branch the key pairs the repository identity (`pull_request.head.repo.full_name` on a PR, `github.repository` on a push) with the short branch name (`head_ref` on a PR, `ref_name` on a push). For an in-repository branch those values match across the `push` and `pull_request` events, which preserves the push+PR double-trigger dedup (OPT47). For a fork, the head repository differs: two contributors can both use GitHub's common `patch-1` branch name without sharing a concurrency group and cancelling one another. `github.head_ref` alone is therefore unsafe on repositories that accept fork contributions. Writing `github.head_ref || github.ref` would instead produce different branch strings for the same in-repository branch, so those runs would never share a group and that saving would be zero.

GitHub decides cancellation from the **incoming** run's `cancel-in-progress`, and it cancels every in-progress run in the group *regardless of their settings*. The repository-qualified key prevents a fork PR from colliding with an upstream push merely because their branches share a name. The `&& github.head_ref != github.event.repository.default_branch` term still protects in-repository back-merge PRs whose head is the default branch. `github.event.repository.default_branch` is present on **both** the `push` and the `pull_request` payloads, so it needs no per-repo substitution. If pushes to another long-lived branch (`release/1.x`, `develop`) must never be cancelled, add an explicit `&& github.head_ref != '<branch>'` term per branch (a general protected-branch set-membership test is not expressible in a substitution-free one-liner).

**Routing (mechanical — check the trigger set the finding's evidence line reports)**: if the workflow has **no `pull_request` trigger** (the detector also fires on `push` alone), `${{ github.event_name == 'pull_request' }}` is never true, the block cancels nothing, and the runner-minute saving is **zero**. In that case copy OPT46's **widened** block: it combines the same group key, including `github.run_id` for default-branch and tag runs, with a predicate that cancels superseded feature-branch pushes.

**Folded-scalar discipline**: keep the `>-` continuation line at the **same indent** as `${{` (as above) so YAML folds it into one space-joined line; a more-indented line keeps its newline, and an expression carrying a literal newline is not a documented expression — see OPT46's note below for why a mis-folded predicate degrades to a truthy *string* and cancels on **every** event. Equivalently, write the predicate on one line.

---
