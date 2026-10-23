---
pattern: OPT33
impact: MEDIUM
class: static
detector: yaml-job-correlated
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml'
fix_strategy: no-draft-pr-gating-on-expensive-jobs
title_template: 'No Draft PR Gating on Expensive Jobs'
category: 7
---

# OPT33 — No Draft PR Gating on Expensive Jobs

**TL;DR**: Expensive jobs run on draft PRs that are still works-in-progress, burning CI on code that isn't ready for review yet.

**Anti-pattern**: Expensive jobs run on draft PRs where the code is still being worked on.

**Detection heuristic**:

```bash
# Check for draft PR condition in job-level if:
grep -rn 'pull_request' .github/workflows/ -A 10
# Flag expensive jobs missing: if: github.event.pull_request.draft == false
```

**Fix**: Add `if: github.event.pull_request.draft == false` to expensive jobs. Job-level gating is the documented-safe shape — a skipped job reports Success and satisfies a required status check, where narrowing the `pull_request` trigger cannot express draft-ness at all (the default `types:` — opened/synchronize/reopened — fires on drafts too) and a mis-narrowed `types:` list leaves required checks "Pending" and blocks merges. When you add the draft `if:`, also ADD `ready_for_review` to `types:` (it is NOT in the default set): without it, no event fires when the draft flips to ready, and the head commit's required check keeps its draft-time "Success (skipped)" — the PR can merge with the expensive job never having run.

**Required-status-check caveat**: treat required-status UNKNOWN as required — when branch protection/rulesets are unreadable, assume every check this workflow produces may be required, and keep the gating at job level (the "Pending" landmine; see OPT32's caveat for the full mechanism and the community-workaround labeling rule).

**Activation-fidelity gate (only flag jobs that actually run on every PR)**: the detector must confirm the job runs on a normal PR open/update before claiming it "runs on every PR including drafts". A job is NOT every-PR — and must NOT be flagged — when the `pull_request:` trigger is gated by `types:` to a non-lifecycle activity (e.g. `types: [labeled]`, which only fires when a label is added), or the job's own `if:` gates on the activity (`github.event.label` / `contains(github.event.pull_request.labels.*.name, …)` / `github.event.action == '<non-lifecycle>'`). GitHub's default `types:` is exactly `{opened, synchronize, reopened}`; a draft `if:` is a separate concern, not an activity gate. (Shared `_pr_trigger_runs_every_pr` / `_job_runs_on_every_pr` in `scan.py`, also used by OPT39/OPT40.)

---
