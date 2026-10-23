---
pattern: OPT46
impact: MEDIUM
class: data-driven
detector: manual
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml'
fix_strategy: superseded-runs-not-cancelled-missing-concurrency-or-cancel-
title_template: 'Superseded Runs Not Cancelled (Missing Concurrency or `cancel-in-progress: false`)'
category: 9
---

# OPT46 — Superseded Runs Not Cancelled (Missing Concurrency or `cancel-in-progress: false`)

**Anti-pattern**: A workflow that triggers on `push` (or `pull_request`) for branches where developers commonly push multiple times in quick succession, but the workflow either (a) has **no top-level `concurrency:` block at all**, or (b) has a concurrency group with `cancel-in-progress: false`. In either case, every superseded push run continues to occupy a runner — wasting the entire wall-clock duration of the run for the obsolete commit.

This pattern has two flavors that share the same fix mechanism:

- **Flavor A — no concurrency block**: workflow triggers on `push` (often with `branches-ignore: [main]` or similar) but has no `concurrency:` block. Common because authors use `concurrency:` for `pull_request` workflows but forget that `push` workflows on branches need it just as much.
- **Flavor B — `cancel-in-progress: false`**: concurrency group is set but explicitly does not cancel. Stale runs aren't cancelled when a new push arrives.

**Detection heuristic**:

1. **Structural scan** — for every workflow file in `.github/workflows/`:

   ```bash
   # Workflows that trigger on push without a top-level concurrency block
   for f in .github/workflows/*.yml; do
     if yq '.on' "$f" | grep -q 'push' && ! yq '.concurrency' "$f" | grep -q 'group'; then
       echo "$f: push trigger without concurrency"
     fi
   done

   # Workflows with concurrency but cancel-in-progress: false
   grep -rn 'cancel-in-progress: *false' .github/workflows/
   ```

2. **Quantify wasted compute** — a structural match alone is not enough, and neither is "a branch had ≥2 runs" (sequential commits on a long-lived / default branch each test a distinct commit and were never superseded — cancelling them saves nothing). Measure the runs that ACTUALLY RACED:
   - List runs (all statuses) via `gh api repos/{owner}/{repo}/actions/workflows/{wf}/runs?created=<30d-window>`; group by `head_branch`; within each branch, count a run as **superseded** iff a later-created run **started before it finished** (timestamp overlap of `run_started_at`…`updated_at`). Sequential, non-overlapping runs count 0.

3. **Bounded savings estimate** — size the **cancellable remainder**, not the whole run, and report a range:
   - **Lower (credited)**: `cancel-in-progress` cancels a superseded run the moment its successor **starts**, so only the compute it would have burned AFTER that moment is reclaimable — the *remainder*. For each superseded run *i*, `remainder_i = end_i − (earliest later start < end_i)`; credit `mean-per-run compute × Σ(remainder_i / duration_i)` over the superseded runs. Compute spent *before* supersession is spent either way, so charging a run superseded 30s before its natural finish its whole cost over-states the reclaimable amount (the gap grows the later runs get superseded). Per-second compute is unknowable (a run's jobs run in parallel), so this pro-rates the **mean** per-run compute by each run's wall-clock remainder fraction — say so in the basis note.
   - **Upper**: the naive `Σ(runs - 1)` over multi-run branches priced at the **whole** run (the loose bound if every non-final run were wasted end-to-end). The old "overlap-confirmed × whole-run" figure is now **neither** bound — the whole-run price only survives as this upper bound.
   Per-run compute is the **mean** job-minutes of the sampled successful runs (needs ≥3 timed runs to be stable). Extrapolate the sampled count to the 30-day volume by `monthly_volume / sampled_n` in **both** directions (a low-frequency workflow's recent slice spans >30 days and must scale down); compute the remainder ratio on the sampled window and apply it before scaling. Skip dormant workflows (`monthly_volume` 0); a run missing either timestamp contributes nothing and is disclosed as a skip. Report as a range; the superseded attribution is **inference** (the API marks no run "cancelled-by-concurrency").

**Fix**: Add a top-level concurrency block, and make the *cancellation* conditional — **never** a bare `cancel-in-progress: true`. This pattern fires on workflows that trigger on `push`, and a bare `true` cancels the in-flight run on `main` or on a release tag the moment the next commit lands — a deploy, a publish, or a migration killed halfway. `cancel-in-progress` accepts an expression ([workflow syntax: `concurrency`](https://docs.github.com/en/actions/reference/workflow-syntax-for-github-actions#concurrency)), so scope it to the events where a superseded run is genuinely worthless.

**Default (PR-scoped)** — correct whenever the racing runs are PR runs:

```yaml
concurrency:
  group: ${{ github.workflow }}-${{ github.head_ref || github.ref_name }}
  cancel-in-progress: >-
    ${{ github.event_name == 'pull_request'
    && github.head_ref != github.event.repository.default_branch }}
```

`push`, `merge_group`, `schedule`, and tag builds evaluate to `false` and run to completion. `merge_group` needs no separate carve-out here — cancelling a queued merge-group run can eject the PR from the merge queue, and this predicate already excludes it.

**The tension you must not "simplify" away.** The group key uses `github.head_ref || github.ref_name` — both are the **short** branch name (`head_ref` is set only on `pull_request`; `ref_name` is the short form on `push`, where `github.ref` would be the fully-formed `refs/heads/…`). That is deliberate: it lands a branch's `push` and `pull_request` runs in the **same** group, which is the whole point of the push+PR double-trigger dedup (OPT47). Writing `github.head_ref || github.ref` instead yields two different strings for one branch, the runs never share a group, and that saving is zero.

**And that unification is exactly what makes the cancel predicate dangerous.** Cancellation is decided by the **incoming** run's `cancel-in-progress`, and it kills every in-progress run in the group *regardless of their settings*. A fork contributor commits on **their fork's `main`** and opens a PR (the most common fork workflow; a gitflow `main → develop` back-merge PR does the same): that PR run has `head_ref == "main"` → group `CI-main` — the **same group** the upstream repo's own `push: [main]` run sits in (`ref_name == "main"`). Without a guard its predicate is `true`, so it **cancels the in-flight push-to-`main` run** — the very harm this recipe exists to prevent, through a different door. The `&& github.head_ref != github.event.repository.default_branch` term closes it **for the default branch**: a PR whose head branch *is* the default branch never cancels. `github.event.repository.default_branch` is present on **both** the `push` and the `pull_request` payloads (and on `pull_request` it is the **base**/upstream repo's default branch, which is the one that matters), so it needs no per-repo substitution. Residual: the same name-collision applies to any *other* long-lived branch that both receives pushes and can appear as a PR head name (`release/1.x`, `develop`, `production`) — if pushes to such a branch must never be cancelled, add an explicit `&& github.head_ref != '<branch>'` term per branch, exactly as the widened form's release-branch note below prescribes for `github.ref`.

**Widened (Flavor A — waste is on feature-branch pushes)** — a `push`-triggered workflow (e.g. `branches-ignore: [main]`) gets *zero* benefit from the PR-scoped predicate, because it never sees a `pull_request` event. Cancel on every ref *except* the protected ones:

```yaml
concurrency:
  group: ${{ github.workflow }}-${{ github.head_ref || github.ref_name }}
  cancel-in-progress: >-
    ${{ github.event_name != 'merge_group'
    && github.ref != format('refs/heads/{0}', github.event.repository.default_branch)
    && github.head_ref != github.event.repository.default_branch
    && !startsWith(github.ref, 'refs/tags/') }}
```

Substitution-free by construction — `format()` is available anywhere expressions are, and `github.event.repository.default_branch` is on every one of these payloads, so a repo whose default branch is `master` or `develop` copy-pastes this **unchanged**. (Do **not** hardcode `'refs/heads/main'`: on such a repo the term never matches, and the block degenerates to `cancel-in-progress: true` on its own default branch — the original bug, restored.)

Two terms that look redundant and are not:
- `github.event_name != 'merge_group'` — a merge-group run's `github.ref` is a `gh-readonly-queue/...` ref, so the branch and tag terms alone would **not** exclude it, and cancelling it can eject the PR from the queue.
- `github.head_ref != github.event.repository.default_branch` — if this workflow ever also sees `pull_request` events, a PR's `github.ref` is `refs/pull/N/merge`, which passes the branch test and the tag test. That is the same fork-PR-from-`main` hole described above; this term is its guard here.

If the repo also protects long-lived **release** branches that must never be cancelled, add an explicit term (e.g. `&& !startsWith(github.ref, 'refs/heads/release/')`). `github.ref_protected` looks like a shortcut for that, but it is unreliable in **both** directions and this catalog does not recommend it: an *unprotected* default branch reads `false` (→ it gets cancelled — the bug), and repo **rulesets** increasingly target `~ALL` branches, which makes `ref_protected` `true` on ordinary feature branches (→ the predicate never fires and the saving is **zero**).

Keep the continuation lines at the **same indent** as `${{` (as above). YAML folds a `>-` block into one space-joined line only for lines at the base indent; a *more-indented* line keeps its newline, and an expression carrying literal newlines relies on undocumented lexer behavior — if it were ever read as a plain string, a non-empty string is truthy and the workflow would cancel on **every** event, which is exactly the failure this recipe exists to prevent. (`>-` also chomps the trailing newline; `|` / `>` would leave one, and `"false\n"` is truthy.) Equivalently, write the whole predicate on one line.

**When NOT to cancel at all**: release/deploy/publish workflows where partial completion is unsafe (artifacts uploaded, tags pushed, deployments in flight). Keep `cancel-in-progress: false` (or no concurrency) for those — the wasted-compute cost is justified. The scanner already suppresses OPT45/OPT46 on release-like workflows, but confirm it against the workflow's actual jobs before shipping.

**Real-world example (2026-05)**: a `Pre-merge` workflow triggered on `push` with `branches-ignore: [main]` and had no top-level concurrency block. 62% of branches had ≥2 runs in a 30-day window. Adding the **widened** block above (this is a Flavor A / push-triggered case) saved an estimated 70-125 min/mo of runner compute.

**Tier-2 render note**: When the detector can prove overlap from real run timestamps, OPT46 is eligible for the first-class runner-minute section only if the finding is stamped `sizing_basis=measured` and carries a `tier2_neutrality` certificate. Modeled or uncertified instances stay in the residual "Also noticed" appendix.

---
