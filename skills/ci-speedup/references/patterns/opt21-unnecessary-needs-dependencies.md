---
pattern: OPT21
impact: MEDIUM
class: static
detector: yaml-job-correlated
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml'
fix_strategy: unnecessary-needs-dependencies
title_template: 'Unnecessary `needs:` Dependencies'
category: 4
---

# OPT21 — Unnecessary `needs:` Dependencies

**TL;DR**: A job waits on another job it doesn't actually depend on, delaying it for no reason.

**Anti-pattern**: Jobs declaring `needs:` on another job when they don't actually consume its outputs, artificially serializing the workflow.

**Detection heuristic**:

```bash
# Parse workflow YAML for needs: declarations
# Check if the dependent job uses any outputs/artifacts from the dependency
```

**Fix**: Remove `needs:` unless the job genuinely requires outputs from the dependency, or the dependency is a gate (e.g., lint must pass before deploy).

**When a `needs:` edge is the right call**: a cheap check job in front of expensive jobs (lint, pre-commit, a format or type check, a lockfile check) trades the check's own duration in wall clock for the runner minutes saved whenever it fails, because the expensive jobs never start. OPT21 does not flag a wait on such a check-only gate:

- **What counts as a check-only gate.** Every step is setup (checkout, `setup-*`, cache or a dependency install) or a recognised check command, and at least one check runs unconditionally. A step named as a check (Check…, Verify…, Lint…) also counts when its body is a short inline check script; the name never makes anything that builds, tests, deploys or publishes a check.
- **What disqualifies a gate.** A check that can pass on failure (`|| true`, `--exit-zero`) or is piped into other work; a job with outputs, artifacts, services, a container, an environment, `defaults`, a custom `shell`, a reusable-workflow `uses:` or `continue-on-error`; a gate that itself waits on another job; any step the detector cannot read. A check that only runs under an `if:` does not count as the gate's check.
- **When the wait is still flagged.** Any job the waiting job needs is not a check-only gate, or the waiting job runs whatever the gate's result (`always()`, `failure()`, `cancelled()` in its `if:`). Anything the detector cannot classify is still flagged.
- **"Cheap" is judged by what the gate runs, not by how long it takes**, so a slow gate is not an OPT21 finding. To make a gate cheaper, keep it short: it adds its full duration to every PR's wait.

---
