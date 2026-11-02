---
pattern: OPT76
impact: MEDIUM
class: static
detector: yaml-job-correlated
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml'
fix_strategy: submodule-lfs-checkout-payload
title_template: 'Submodule / Git LFS Checkout Payload'
category: 5
---

# OPT76 — Submodule / Git LFS Checkout Payload

**TL;DR**: The checkout clones every submodule, or downloads every Git LFS
object, for a job whose steps never read them — a fixed download paid on every
single run.

**Anti-pattern**: `actions/checkout` with `submodules: true` / `submodules:
recursive`, or `lfs: true` (equivalently a `git lfs pull` / `git lfs fetch` step
in a run block), in a job that never touches the submodule paths declared in
`.gitmodules` or the paths `.gitattributes` marks `filter=lfs`. The payload is
usually copied wholesale from one job that genuinely needs it (a release build,
a docs render) into every job in the workflow, so the lint job clones the vendor
tree and the unit-test job downloads the design assets. This is the sibling of
OPT28 (`fetch-depth: 0`): the same checkout step, a different payload — and it
is ranked and fixed separately because removing `fetch-depth: 0` does
nothing about a submodule or LFS clone, and vice versa.

**Detection heuristic**:

```bash
# 1. What payload does the repo actually declare?
cat .gitmodules                       # submodule paths
grep -n 'filter=lfs' .gitattributes   # LFS-tracked path patterns

# 2. Which jobs pull it?
grep -rn -e 'submodules:' -e 'lfs:' -e 'git lfs \(pull\|fetch\)' .github/workflows/

# 3. Which of those jobs never reference a declared path (the finding)?
```

A finding requires all three: a declared payload (step 1), a job that pulls it
(step 2), and **no** reference to any declared path anywhere in that job — its
`run` blocks, step and job-level `working-directory`, `strategy.matrix` values,
step `if:` and `name:`, job- and step-level `env:`, `with:` values, `uses:` refs,
and the body of every local composite action the job invokes, followed
transitively into the local actions those invoke (step 3). With no `.gitmodules` (or no
`filter=lfs` line), there is no declared path to prove unread, so nothing is
flagged. When a local composite action the job invokes cannot be read — at any depth in
that chain — the pattern fails **closed** and stays silent, the same conservative
stance OPT28 takes: the cost of a miss is a lost finding, never a fix that breaks
a job. A checkout that names a `repository:` other than this one is skipped
outright: it pulls that repo's submodules and LFS objects, about which this
repo's `.gitmodules` and `.gitattributes` say nothing.

Scoped, like OPT28, to workflows that run on `pull_request` / `push` /
`workflow_call`. A `workflow_dispatch`- or `schedule`-only helper runs ~0×/mo,
so its checkout payload is noise rather than a ranked optimization.

**Fix**: Pull the payload only in the jobs that read it. Drop `submodules:` /
`lfs:` from the other jobs' checkout, or scope the payload down where part of it
is genuinely needed — `submodules: false` plus a targeted `git submodule update
--init --depth 1 <path>` for the one submodule that is read, or `lfs: false`
plus `git lfs pull --include='<path>'` for the assets that are read. Where every
job in a cluster needs the same payload, the lever is OPT73 (a shared sub-step
across the critical-path cluster), not this pattern.

**Core evidence, and its honest limit**: the recipe rests on "this job does not
reference anything under the submodule / does not read the LFS-tracked paths."
The workflow YAML **cannot settle that on its own.** The detector proves only
that no path declared in `.gitmodules` / `.gitattributes` appears in the job's
own YAML or in the local composite actions it invokes. A build script, a
Makefile target, a test fixture, or a config file the job invokes can read the
payload without ever naming the path in CI config — and a `git submodule` /
`git lfs` consumer inside a container image is invisible here too. So the
finding is a **candidate, not a verdict**: before removing the payload, grep the
scripts the job actually runs for the declared paths, and confirm on one run
(the fix is trivially revertible — restore the `with:` key). If the submodule
carries build inputs resolved by path at build time, treat the payload as
load-bearing and skip the finding.

**Sizing**: no static default seconds. The cost is the repo's own payload — the
submodule tree's size and the LFS objects' bytes — which the workflow YAML never
reveals, so the catalog gives this pattern no `_SIZING` model and no modeled
saving; it renders **qualitatively** rather than carrying an invented number
(the same honest path any un-modeled pattern takes — the `_SIZING`
preamble in `collect_runs.py` states the rule: a pattern that isn't in the table
is sized as `None`/`None` and rendered qualitatively, because the scanner never
invents a number). Both axes therefore render empty. Where a reader wants the
number, the measurement is the job's own checkout step duration before and after
the change, per the rollout below — this pattern does not estimate it for them.

**Risk**: **MEDIUM**. For a submodule, removing a payload the job depends on
fails it loudly — a missing path, a missing file — which is why the rollout below
is cheap. LFS is the dangerous variant, and it fails **quietly**: with `lfs:`
dropped, a tracked file is still present as its ~130-byte pointer text, so a tool
reads the pointer and produces a wrong output instead of an error. Only drop
`lfs:` for jobs that read no tracked path at all, and check the job's output, not
just its exit code.

**Guardrail**: Never recommend dropping the payload for a job whose steps, or
whose invoked local actions, reference a declared path. Never recommend it at all
when the declared payload can't be read (no `.gitmodules` / `.gitattributes`) —
absence of a declaration is not evidence of unread payload.

**Rollout**: Change one job, re-run the workflow once, and compare that job's
checkout step duration before and after. Revert by restoring the single `with:`
key.
