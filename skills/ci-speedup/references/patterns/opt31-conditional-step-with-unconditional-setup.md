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

**Real-world example (blen-starter-kit)**: `ci.yml` `web-quality` job has `Install Playwright Chromium` (line 92, ~16s) followed by `Web smoke e2e` (line 95) gated on `if: env.CLERK_SECRET_KEY != '' && env.NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY != ''`. PRs from forks (no Clerk secrets) pay 16s for setup that's never consumed.

**Risk**: LOW. The fix is purely defensive — add the same `if:` to the upstream step. If the conditions diverge later, both steps still execute together (consumer just runs without setup, which would surface as a clear error).
