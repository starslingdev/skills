---
pattern: OPT32
impact: HIGH
class: static
detector: yaml-on-trigger
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml'
fix_strategy: missing-paths-paths-ignore-on-expensive-workflows
title_template: 'Missing `paths`/`paths-ignore` on Expensive Workflows'
category: 7
aliases:
  - opt32--missing-paths-paths-ignore-on-expensive-workflows
alias_note: 'Alias anchor: findings files stamped before the GFM anchor fix link this older slug; keep it so those deep links still land.'
---

# OPT32 — Missing `paths`/`paths-ignore` on Expensive Workflows

**TL;DR**: Expensive workflows run on every PR even when the change can't affect them — e.g. a docs-only edit triggers the full test suite.

**Anti-pattern**: Expensive CI workflows (E2E tests, full integration suites) run on every push/PR regardless of what changed. A docs-only or README change triggers the full CI suite.

**Detection heuristic**:

```bash
# Check on: block for paths/paths-ignore filters
# Flag workflows with >3 jobs and no path filtering
```

**Fix**: Add `paths-ignore` for documentation, markdown files, and other non-code changes. Or use `paths` to restrict to relevant source directories.

**Fail closed:** when a scoped workflow or job is a required check, a job-level `if:` alone is not safe: a failed detector skips the suite, and a skipped required check reads green. Add an always-running verdict job (`if: always()`) that `needs:` the detector, every intermediate job whose failure can suppress a suite, and the suites, and make that verdict the required check. Listing jobs in `needs:` is not an assertion: the verdict must fail unless the detector succeeded AND each suite either succeeded or was skipped because the detector validly reported unchanged (its output exactly `'false'`). Reject failures and cancellations, including an unchanged output written before failure. A strict "every suite succeeded" assertion is right only when a suite is never skipped on purpose; with intentional skips it turns every unchanged PR red. ci-secure's required-checks-skippable fact lists the assertion forms it can verify.

**Required-status-check caveat (the "Pending" landmine)**: if any check this workflow produces is a required status check, do NOT skip it with `paths`/`paths-ignore` (or `branches:` filters / `[skip ci]`) — a workflow skipped by filtering leaves its required check "Pending" and the PR can never merge; official guidance says not to use path or branch filtering on required workflows. GitHub documents a job-level `if:` restating the filter (a skipped job reports Success and satisfies the gate), but on its own it shares the skipped-reads-green hole above; the safe shape is that `if:` plus the fail-closed verdict job as the required check. The no-op twin-workflow (same workflow AND job name, inverse filter) is a community-known workaround, NOT in any current GitHub docs edition. Treat required-status UNKNOWN as required: when branch protection/rulesets are unreadable (the common case on repos you don't admin), assume every check this workflow produces may be required.

**Real-world example (better-auth)**: e2e.yml runs all 8 jobs on docs-only PRs.

---
