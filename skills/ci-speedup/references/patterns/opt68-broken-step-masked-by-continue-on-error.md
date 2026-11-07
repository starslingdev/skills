---
pattern: OPT68
impact: MEDIUM
class: static
detector: yaml-path
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml'
fix_strategy: broken-step-masked-by-continue-on-error
title_template: 'Broken Step Masked by `continue-on-error`'
category: 13
---

# OPT68 — Broken Step Masked by `continue-on-error`

**Anti-pattern**: A step has `continue-on-error: true` (or uses an action with built-in retry/upload semantics that silently fails) AND has been failing on every run for an extended period. The job stays green, the failure never surfaces in the dashboard, and the step continues consuming runner time for zero value. Three flavors are common:

1. **`continue-on-error` covering a real bug**: the step's command exits non-zero on every run (wrong arg, missing tool, deprecated API), but `continue-on-error: true` masks it.
2. **`codecov/codecov-action@v4` without a token**: emits `Token required - not valid tokenless upload` and fails, but `fail_ci_if_error` defaults to false. Coverage uploads simply don't happen.
3. **`github/codeql-action/upload-sarif` (or `dependency-review-action`) without GHAS enabled**: API returns `Code Security must be enabled for this repository`. The action exits 1, but workflow doesn't fail (continue-on-error or upload built into analyze step).

**Detection heuristic**:

1. Read per-job logs from `gh api repos/{owner}/{repo}/actions/jobs/{job_id}/logs` (downloaded by `collect_runs.py --with-logs`) for recent successful job runs.
2. For each step in each job, scan the log section for that step looking for error tokens: `Error:`, `error:`, `FAILED`, `failed`, `Token required`, `Code Security must be enabled`, `not valid`, `Permission denied`, `404`, `Unauthorized`, `command not found`.
3. Cross-reference the step's `continue-on-error` setting in the workflow YAML. If the step both has `continue-on-error: true` AND the log shows an error token, flag it.
4. ALSO flag actions with built-in silent-failure modes when the prerequisite is missing:
   - `codecov/codecov-action@v4` step + no `secrets.CODECOV_TOKEN` reference in the workflow → likely silent failure
   - `github/codeql-action/upload-sarif` (or any action that uploads to GHAS) + repo doesn't have `security_and_analysis.advanced_security` enabled
   - `actions/dependency-review-action` on a non-GHAS repo

**Recommendation pattern**: "Step `<name>` has been silently failing in the last N runs (cite log lines). Either fix the underlying issue or remove the step. Currently consuming `<seconds>`s/run for zero value."

**Fix strategies**:

- If the step is genuinely useful: fix the root cause (add the missing token, enable GHAS, fix the broken command).
- If the step is dead weight (token won't be added, GHAS not on the roadmap): delete the step.
- Never recommend "just turn off `continue-on-error`" without addressing the failure — that just turns silent failure into loud failure on every run.

**Real-world example (blen-starter-kit deep-scan)**: `ci.yml` has 3 separate `codecov/codecov-action@v4` steps. None have `secrets.CODECOV_TOKEN` referenced anywhere. Logs show `Token required - not valid tokenless upload` on every run. Each step costs ~3-5s × 3 jobs = ~12s/run × workflow run frequency. Easy delete.

**Risk**: LOW (deletion) or MEDIUM (rewrite). Always check whether the failing step is the only thing producing a downstream artifact (e.g., a coverage badge that the README uses). Most of the time, "silently failing for months" means nobody downstream noticed — safe to remove.

---
