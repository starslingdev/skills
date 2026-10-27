---
pattern: OPT22
impact: MEDIUM
class: static
detector: yaml-on-trigger
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml'
fix_strategy: sequential-workflows-via-workflow-run
title_template: 'Sequential Workflows via `workflow_run`'
category: 4
---

# OPT22 — Sequential Workflows via `workflow_run`

**Anti-pattern**: Chaining workflows with `workflow_run` when they could run in parallel as jobs within a single workflow.

**Detection heuristic**:

```bash
grep -rn 'workflow_run' .github/workflows/
```

**Fix**: Consolidate into a single workflow with parallel jobs, or use `workflow_call` for reusable workflows that can run concurrently.

**Required-checks caveat**: consolidating workflows renames the checks (the old `workflow_run` check name disappears). If the old check was a required status check, add the new job's check name to branch protection as a required check (or the ruleset equivalent), or the consolidated work silently stops gating merges until that admin-only step is done. If the consolidation routes the old check's work behind a `needs:` edge and an aggregator, see OPT75's [dependency-failure skip caveat](#opt75--long-pole-optimize-or-relocate-the-dominant-step) — a dependent skipped by a failed dependency reports skipped, not failed, so the aggregator must run with `always()` (not `!cancelled()`, which a cancelled run skips) and propagate every `needs.<job>.result`, and keeping the required check name on that aggregator re-gates the merge without an admin.

---
