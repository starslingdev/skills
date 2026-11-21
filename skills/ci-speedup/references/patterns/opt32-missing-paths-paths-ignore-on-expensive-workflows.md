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

**Deploy/release overlap extension**: a workflow can already have `paths:` and
still be too broad. When the default branch is known, inspect a `push` deploy or
release workflow's ordered positive and negative patterns against test files in
the repository. Report it when the final matching pattern still includes a file
under `test`, `tests`, `__tests__`, `spec` or `specs` whose own name marks it
as a test (`*.test.*`, `*.spec.*`, `*_test.*`, `test_*.py`, `*_spec.rb`,
`conftest.py`, `*Test.java`). Patterns are matched with GitHub semantics (`*`
stays inside one path segment). Withhold this extension when the default
branch, path list, a glob construct or the repository test shape cannot be
read; when the test-named directory holds a package manifest; or when any
non-test file the filter selects imports from it, because then the deploy
builds from that directory. A deploy on another branch is not this finding.

**Fix**: Add `paths-ignore` for documentation, markdown files, and other non-code changes. Or use `paths` to restrict to relevant source directories.

For the deploy/release overlap extension, keep the existing positive list and
add a narrow negated test-directory pattern **after** the broader positive glob,
for example:

```yaml
paths:
  - 'worker/**'
  - '!worker/test/**'
```

GitHub evaluates path patterns in order, so placing the negative first lets the
later positive re-include the test path. One event cannot use both `paths` and
`paths-ignore`. Preserve every runtime, configuration, workflow and deployment-
helper path, then test test-only, runtime-only and mixed changes.

**Fail closed:** when a scoped workflow or job is a required check, a job-level `if:` alone is not safe: a failed detector skips the suite, and a skipped required check reads green. Add an always-running verdict job (`if: always()`) that `needs:` the detector, every intermediate job whose failure can suppress a suite, and the suites, and make that verdict the required check. Listing jobs in `needs:` is not an assertion: the verdict's step must inspect every dependency result and fail unless the detector succeeded AND each suite either succeeded or was skipped because the detector validly reported unchanged (its output exactly `'false'`). Reject `failure` and `cancelled` dependency results, and a `skipped` suite unless the detector validly reported unchanged, including an unchanged output written before failure. Keep `if: always()` rather than OPT46's `!cancelled()` here, because the verdict is the required check: a job skipped by its `if:` reports success, and a skipped required check does not block merging, so a head run cancelled without a replacement would pass with nothing validated. A strict "every suite succeeded" assertion is right only when a suite is never skipped on purpose; with intentional skips it turns every unchanged PR red. ci-secure's required-checks-skippable fact lists the assertion forms it can verify.

**Required-status-check caveat (the "Pending" landmine)**: if any check this workflow produces is a required status check, do NOT skip it with `paths`/`paths-ignore` (or `branches:` filters / `[skip ci]`) — a workflow skipped by filtering leaves its required check "Pending" and the PR can never merge; official guidance says not to use path or branch filtering on required workflows. GitHub documents a job-level `if:` restating the filter (a skipped job reports Success and satisfies the gate), but on its own it shares the skipped-reads-green hole above; the safe shape is that `if:` plus the fail-closed verdict job as the required check. The no-op twin-workflow (same workflow AND job name, inverse filter) is a community-known workaround, NOT in any current GitHub docs edition. Treat required-status UNKNOWN as required: when branch protection/rulesets are unreadable (the common case on repos you don't admin), assume every check this workflow produces may be required.

**Real-world example (better-auth)**: e2e.yml runs all 8 jobs on docs-only PRs.

**Deploy-path sizing**: runner-minute effect is a **saving**: the deploy jobs'
billed compute on test-only default-branch merges. Keep it unsized until those
merges' frequency and the affected jobs' measured compute are available; PR
changed-file sampling is not evidence for default-branch push frequency.

**Public deploy-path evidence**: [OpenClaw Crabbox pull request #2839](https://github.com/openclaw/crabbox/pull/2839)
uses the negated `'!worker/test/**'` pattern after the positive glob in the
same `paths:` list of a default-branch deploy workflow.

---
