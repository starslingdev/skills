---
pattern: OPT28
impact: MEDIUM
class: static
detector: yaml-path
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml'
fix_strategy: full-git-history-checkout
title_template: 'Full Git History Checkout'
category: 5
---

# OPT28 — Full Git History Checkout

**TL;DR**: The checkout downloads the project's entire git history when the job only needs the current code — wasted download time on every run.

**Anti-pattern**: Using `fetch-depth: 0` (full history) when only the latest commit is needed. Wastes time downloading the full git history.

**Detection heuristic**:

```bash
# Check for fetch-depth: 0 or missing fetch-depth (default is 1, which is fine)
grep -rn 'fetch-depth' .github/workflows/
```

**Fix**:

- **Delete the checkout step outright** if no step in the job reads a file from the checkout (a job that only calls an API, downloads an artifact, or runs a container image). The cheapest checkout is none. Check first that nothing needs the repo indirectly: a local action (`uses: ./...`) or reusable-workflow path (a self-repository `uses: $/...` action's definition loads without the checkout, but its steps may still read repo files, so open it before deleting), `hashFiles()` in a cache key, a `run:` that calls `git` or a repo script, or a `gh` command relying on the current repo (it then needs `--repo` or `GH_REPO`). A missing file fails the job, so delete only when every step is accounted for.
- Use `fetch-depth: 1` (default) unless the job needs git history (e.g., changelogs, blame). For PR diff detection against the merge commit's parents, `fetch-depth: 2` suffices — but change-scoped runners that diff against the BASE BRANCH (`turbo --filter=...[origin/main]`, `nx affected`, `vitest --changed` — see OPT34/OPT70) need the base ref fetched (`fetch-depth: 0` or a targeted base-ref fetch); do not shallow those jobs.
- **Sparse / blobless checkout** is the option between a full clone and depth 1 for diff-based gates that need history but not every file: `filter: blob:none` (with `fetch-depth: 0`, full history with file contents fetched on demand) or `sparse-checkout:` (newline-separated patterns; only those paths materialized) on `actions/checkout`. Both are real inputs; the action's own docs say `filter` "overrides sparse-checkout if set", so verify a combination on the job rather than assume it. Confirm the job's steps only touch the sparse paths, or a missing file fails the job.

---
