---
pattern: OPT105
impact: MEDIUM
class: data-driven
detector: collect_runs
affected_files: '.husky/*,.githooks/*,lefthook.yml,lefthook.yaml,.pre-commit-config.yaml,.pre-commit-config.yml,package.json,.github/workflows/*.yml,.github/workflows/*.yaml'
fix_strategy: reuse-local-hook-work-by-input
title_template: 'Local Hook Work Is Repeated in Pull-Request CI'
category: 2
---

# OPT105 — Local Hook Work Is Repeated in Pull-Request CI

**TL;DR**: A local Git hook runs the same resolved type-check, lint, test, or
build command that pull-request CI runs again from scratch. The repeated CI
steps are a measured share of the job. Keep CI authoritative, but make its
second proof cheap by reusing work keyed to the inputs.

**Detection heuristic**: read static hook configuration from `.husky/`,
`.githooks/`, `lefthook.yml`, `.pre-commit-config.yaml`, or `simple-git-hooks`
in `package.json`. Resolve package scripts on both the hook and workflow sides,
then match literal leaf commands. Admit a finding only when the matched CI steps
have jobs-API timestamps, total at least 20 seconds at p50, and account for at
least 15% of the pull-request job. A missing, unreadable, dynamic, or different
hook stays quiet.

**Fix recipe — input-keyed reuse is the default**: make the CI rerun cheap
without trusting a claim from a developer machine. Depending on the matched
work, use `tsc --incremental` with a cached `.tsbuildinfo`, `eslint --cache`
with its cache file persisted, changed-file test selection such as
`--changedSince`, or a remote task cache such as Turborepo or Nx. Key reuse on
the source tree, lockfile, configuration, and tool versions. A cache miss or
changed input still performs the full check in CI.

**Optional, owner-approved alternative — trailer skip**: a hook may record a
commit trailer naming the checks it ran, but this is opt-in because it changes
the trust model. Bind the claim to the commit's tree hash, lockfile, and tool
versions so an amend, rebase, dependency change, or tool change invalidates it.
Honor it only for branches in the same repository, never pull requests from a
fork. Keep the full suite on the merge queue or, when there is no merge queue,
the default branch, so the merge gate still proves the work. State plainly that
a trailer can be written by hand and a hook can be bypassed with `--no-verify`:
accepting it is a trust decision for the repository owner, not a cache hit.

**Sizing**: wall-clock is the matched steps' measured p50, bounded by the
affected job's critical-path headroom. Runner-minutes are the same measured
step compute scaled by observed pull-request run volume and capped at the
affected job's measured completed-run compute. Both are ceilings until the
chosen input-keyed reuse is measured after adoption; the trailer alternative
gets no credit on the merge-gate run where the full suite remains mandatory.
