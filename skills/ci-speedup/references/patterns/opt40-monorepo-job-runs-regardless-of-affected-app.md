---
pattern: OPT40
impact: MEDIUM
class: static
detector: yaml-workflow-correlated
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml'
fix_strategy: monorepo-job-runs-regardless-of-affected-app
title_template: 'Monorepo Job Runs Regardless of Affected App'
category: 7
---

# OPT40 — Monorepo Job Runs Regardless of Affected App

**TL;DR**: In a monorepo, a job for one app runs on every PR even when only a different app changed.

**Anti-pattern**: Repo is a monorepo (top-level `apps/*`, `packages/*`, or `services/*` directories). Workflow has jobs whose work targets one app (e.g., `web-quality` runs `playwright test` in `apps/web/`), but the workflow's `on.pull_request` doesn't have `paths` filters AND no per-job `dorny/paths-filter` gate exists. Result: every PR runs full Playwright + tests + builds for every app, even when only `apps/api/**` changed.

**Why distinct from OPT32**: OPT32 catches workflows missing `paths`/`paths-ignore` at the WORKFLOW level. That works for single-purpose workflows. OPT40 covers the monorepo case where the workflow is correctly scoped (it should run on every PR — there's at least ONE thing that needs to run) but per-job gating is missing for the apps that weren't touched.

**Detection heuristic**:

1. Detect monorepo layout: presence of `apps/`, `packages/`, `services/`, or `pnpm-workspace.yaml` / `turbo.json` / `nx.json`.
2. For each job in each workflow, identify which app it targets:
   - Steps that `cd apps/<name>`, `pnpm --filter <pkg>`, `turbo run --filter=<pkg>`, `nx run <project>:<target>`, etc.
3. For jobs with a clear single-app target, check whether the workflow `paths` includes ONLY that app's path AND whether a `dorny/paths-filter` precedes the job.
4. Flag if neither gate exists.
5. **Activation fidelity**: only flag when the workflow actually runs on every PR — suppress when the `pull_request:` trigger is `types:`-gated to a non-lifecycle activity (e.g. `types: [labeled]`), since the "targets every PR" claim is then false. (Shared `_pr_trigger_runs_every_pr` in `scan.py`; see OPT33.)

**Fix**: Same shape as OPT39 — add a `changes:` job with `dorny/paths-filter@v3` mapping each app/package to its paths, then gate per-job:

```yaml
jobs:
  changes:
    runs-on: ubuntu-latest
    outputs:
      web: ${{ steps.f.outputs.web }}
      api: ${{ steps.f.outputs.api }}
      mobile: ${{ steps.f.outputs.mobile }}
    steps:
      - uses: actions/checkout@<sha>
      - uses: dorny/paths-filter@v3
        id: f
        with:
          filters: |
            web: ['apps/web/**', 'packages/ui/**', 'package.json', 'bun.lock']
            api: ['apps/api/**', 'packages/db/**', 'pyproject.toml']
            mobile: ['apps/mobile/**']

  web-quality:
    needs: changes
    if: needs.changes.outputs.web == 'true'
    # ...
```

Required-status-check caveat from OPT39 applies here too.

**Real-world example (blen-starter-kit deep-scan)**: `ci.yml` `web-quality` runs `bunx playwright install --with-deps chromium` + smoke e2e on every PR, even API-only or mobile-only PRs. Adding the `changes:` job + `if: needs.changes.outputs.web == 'true'` skips the entire job for ~50-70% of PRs.

**Risk**: MEDIUM. Cross-app changes (e.g., a shared `packages/ui` change that affects both `apps/web` and `apps/mobile`) need to be in the `paths-filter` rules for BOTH apps. List shared packages explicitly in each app's filter to avoid false-skip.
