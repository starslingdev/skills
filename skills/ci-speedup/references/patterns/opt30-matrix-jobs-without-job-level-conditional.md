---
pattern: OPT30
impact: MEDIUM
class: static
detector: yaml-path-absent
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml'
fix_strategy: matrix-jobs-without-job-level-conditional
title_template: 'Matrix Jobs Without Job-Level Conditional'
category: 6
---

# OPT30 — Matrix Jobs Without Job-Level Conditional

**Anti-pattern**: Same as OPT29 but for matrix jobs — N runners are provisioned for nothing.

**Detection heuristic**:

- Identify matrix jobs triggered by merge_group
- Check for job-level `if:` conditions

**Fix**: Add job-level `if:` to skip the entire matrix for irrelevant triggers.

**Real-world example (better-auth)**: `adapter-integration` (6 matrix variants) provisions 6 runners in merge queue that all skip.

---
