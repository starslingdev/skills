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
  group: ${{ github.workflow }}-${{ github.head_ref || github.ref_name }}
  cancel-in-progress: >-
    ${{ github.event_name == 'pull_request'
    && github.head_ref != github.event.repository.default_branch }}
```

`cancel-in-progress` accepts an expression, so the predicate is re-evaluated per event: `push`, `merge_group`, `schedule`, and tag builds all evaluate to `false` and run to completion; only superseded PR runs are cancelled.

**The group key and the cancel predicate pull against each other — this is the whole design tension, state it before you touch either.** The group key uses `github.head_ref || github.ref_name` — both are the **short** branch name (`head_ref` is set only on `pull_request`; `ref_name` is the short form on `push`, where `github.ref` would be the fully-formed `refs/heads/…`). Unifying them is deliberate: it puts a branch's `push` and `pull_request` runs in the **same** group, which is what buys the push+PR double-trigger dedup (OPT47). Writing `github.head_ref || github.ref` instead would produce different strings for the same branch, the runs would never share a group, and that saving would be zero.

But that same unification is what makes the cancel predicate dangerous. GitHub decides cancellation from the **incoming** run's `cancel-in-progress`, and it cancels every in-progress run in the group *regardless of their settings*. A fork contributor who commits on **their fork's `main`** and opens a PR (the most common fork workflow — likewise a gitflow `main → develop` back-merge PR) produces a PR run with `head_ref == "main"`, i.e. group `CI-main` — the **same group** as the upstream repo's own `push: [main]` run (`ref_name == "main"`). Its predicate would be `true`, so it would cancel the in-flight push-to-`main` run: exactly the harm this recipe exists to prevent, through a different door. The `&& github.head_ref != github.event.repository.default_branch` term closes it **for the default branch**. `github.event.repository.default_branch` is present on **both** the `push` and the `pull_request` payloads, so it needs no per-repo substitution. Residual: the same name-collision exists for any *other* long-lived branch that both receives pushes and can appear as a PR head name (`release/1.x`, `develop`) — if pushes to such a branch must never be cancelled, add an explicit `&& github.head_ref != '<branch>'` term per branch (a general protected-branch set-membership test is not expressible in a substitution-free one-liner).

**Routing (mechanical — check the trigger set the finding's evidence line reports)**: if the workflow has **no `pull_request` trigger** (the detector also fires on `push` alone), `${{ github.event_name == 'pull_request' }}` is never true, the block cancels nothing, and the runner-minute saving is **zero**. In that case use OPT46's **widened** predicate instead of this one.

**Folded-scalar discipline**: keep the `>-` continuation line at the **same indent** as `${{` (as above) so YAML folds it into one space-joined line; a more-indented line keeps its newline, and an expression carrying a literal newline is not a documented expression — see OPT46's note below for why a mis-folded predicate degrades to a truthy *string* and cancels on **every** event. Equivalently, write the predicate on one line.

---
