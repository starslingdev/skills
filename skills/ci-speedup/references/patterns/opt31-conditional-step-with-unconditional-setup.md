---
pattern: OPT31
impact: MEDIUM
class: static
detector: yaml-job-correlated
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml'
fix_strategy: conditional-step-with-unconditional-setup
title_template: 'Conditional Step With Unconditional Setup'
category: 6
---

# OPT31 — Conditional Step With Unconditional Setup

**Anti-pattern**: A step (typically a tool install, browser install, or large download) exists ONLY to support a downstream step that has an `if:` condition gating it. The downstream step is conditionally skipped (e.g., when a secret is missing, when a feature is disabled, on certain branches), but the upstream setup step runs unconditionally — paying the full setup cost for runs where the consumer never executes.

A second form occurs in an aggregate job: a shell-only step reads
`needs.<job>.result` values and fails when a producer was unsuccessful, but it
runs after checkout or a `setup-*` action. A failed dependency therefore pays
for setup before the result gate ends the job.

**Why this is missed by OPT1 (Unnecessary Tool Install)**: OPT1 catches the case where the tool is NEVER invoked in the job. OPT31 catches the case where the tool IS invoked, but only conditionally — and the install step is missing the same condition.

**Detection heuristic**:

1. For each job, walk steps in order. For every step that has an `if:` condition (especially conditions referencing `env.*`, `secrets.*`, branch names, or labels):
2. Look at the immediately preceding setup steps (within the same job, no `needs:` boundary). Identify the setup steps whose only consumer is this conditional step.
3. Examples of "setup-and-consumer" pairings:
   - `bunx playwright install` (setup) → `playwright test` or `e2e` (consumer)
   - `apt-get install <pkg>` → `<pkg> --version` or invocation
   - `pip install <test-only-package>` → only used in test invocation
   - `gh auth setup-git` → only used in conditional `gh` calls
4. Flag if the setup is unconditional but the consumer's `if:` would skip in some fraction of runs.
5. The savings = `setup_step_p50_seconds × P(consumer skips)`.

For the aggregate-job form, require a result step that reads only literal
`needs.*.result` values, uses shell control/output/test builtins, can exit
non-zero, and has no `continue-on-error`. Flag only checkout or `setup-*`
actions before the first such result gate. Withhold the finding when the gate
uses a repository script, another GitHub context, an unreadable expression, or
when setup already has a needs-result condition.

**Fix**: Copy the `if:` condition from the consumer step onto the setup step (or wrap both in a guard step that exits early). Example:

```yaml
# Before
- name: Install Playwright Chromium
  run: cd apps/web && bunx playwright install --with-deps chromium # always runs (~16s)

- name: Web smoke e2e
  if: env.CLERK_SECRET_KEY != ''
  run: cd apps/web && bunx playwright test smoke

# After
- name: Install Playwright Chromium
  if: env.CLERK_SECRET_KEY != '' # add same condition
  run: cd apps/web && bunx playwright install --with-deps chromium

- name: Web smoke e2e
  if: env.CLERK_SECRET_KEY != ''
  run: cd apps/web && bunx playwright test smoke
```

For a needs-result gate, move that unchanged gate to the first step, before
checkout and toolchain setup. Preserve its complete success predicate and test
successful, failed, cancelled, skipped and missing dependency results.

**Sizing**: Runner-minute effect is a **saving**: the affected setup steps'
measured billed compute on runs where the conditional consumer is skipped or a
dependency result is unsuccessful. Do not state a number until both the setup
time and that run share are measured. A result gate that ends a failed run
earlier normally does not reduce successful pull-request merge wait.

**Real-world example (blen-starter-kit)**: `ci.yml` `web-quality` job has `Install Playwright Chromium` (line 92, ~16s) followed by `Web smoke e2e` (line 95) gated on `if: env.CLERK_SECRET_KEY != '' && env.NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY != ''`. PRs from forks (no Clerk secrets) pay 16s for setup that's never consumed.

**Public evidence**: [OpenClaw pull request #2868](https://github.com/openclaw/crabbox/pull/2868)
moved a coverage producer-result gate ahead of checkout and Go setup so an
unsuccessful producer is reported before the aggregate job pays setup cost.

**Risk**: LOW. The conditional-consumer fix is defensive when both steps keep
the same condition. Moving a needs-result gate is safe only under the narrower
shell-only detection above; changing its predicate can instead hide a producer
failure, so preserve it byte-for-byte.
