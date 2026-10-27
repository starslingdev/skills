<!-- Non-pattern text of the compiled ../optimization-patterns.md: the preamble (with the
Contents section and its category blurbs) and each `## Category N:` heading block, placed
before the first pattern of that category. Each section runs from its marker line to the
next marker. Edit here, then run build_catalog.py. -->
<!-- frame: preamble -->
# ci-speedup Pattern Catalog

This catalog defines every CI optimization pattern the `ci-speedup` skill can
detect. Each entry has a single METADATA block followed by the pattern's
anti-pattern, detection heuristic, and fix recipe.

**Catalog scope.** Patterns are organized into 14 categories (Caching,
Redundancy, Docker, Parallelization, Actions and Checkout, Conditional
Execution, Trigger and Scope, Release Workflow, Queue Times and Concurrency,
Timing Anomalies, Stack-Specific, Build Caching, Hidden Failures and Dead
Config). Each pattern carries an `impact` tier (HIGH/MEDIUM/LOW) and a
`detector` field naming the scanning strategy used. The scanner consumes
the METADATA blocks directly — adding a new pattern is a matter of writing
its catalog entry and (for novel detector types) extending the scanner.

**OPT-id assignment rule.** Each `OPT<n>` id is permanent and **never reused**,
even after a pattern is cut — a cut pattern's id stays retired so historical
reports, evals, and fix-strategy strings never collide with a different pattern.
Assign a **new** id to every new pattern (the next unused number; there are gaps
from retired/never-assigned ids, e.g. 10, 67). Do not renumber existing
patterns. `fix_strategy` slugs and the `_SIZING` keys are keyed on these ids, so
reuse would silently mis-size or mis-link old data.

## Contents

Patterns are `### OPT<n> — Title` blocks grouped into categories. This file is
large; **navigate by grep** — `OPT33`, `## Category 7`, or a keyword like
`turbo` / `concurrency` jumps straight to the entry. The categories
(grep `## Category N`):

1. Caching · 2. Redundancy · 3. Docker · 4. Parallelization · 5. Actions and
Checkout · 6. Conditional Execution · 7. Trigger and Scope · 8. Release Workflow ·
9. Queue Times and Concurrency · 10. Timing Anomalies · 11. Stack-Specific ·
12. Build Caching (Language-Agnostic) · 13. Hidden Failures and Dead Config ·
14. Structural / Critical-Path Levers

- **Category 1 — Caching** (`OPT1`–): tool installs, build/test caches, dynamic cache keys, and the one pattern pointed the other way — a cache measured to cost more than it saves (`OPT79`), NuGet restores placed on a slower Windows disk (`OPT93`).
- **Category 2 — Redundancy**: duplicate env, repeated setup sequences, redundant build steps, repeated fixed setup across independent small jobs (`OPT77`).
- **Category 3 — Docker**: sleep-based readiness, over-broad `compose up`.
- **Category 4 — Parallelization**: needless `needs:` serialization, unsharded long jobs, a test worker count nobody measured (`OPT88`), the slowest platform compiling its own tests (`OPT89`).
- **Category 5 — Actions and Checkout**: stale action pins, repeated setup, full-history checkout, submodule / Git LFS checkout payload, checkout stalls on the tail (`OPT80`).
- **Category 6 — Conditional Execution**: merge_group step-vs-job conditions, draft-PR gating.
- **Category 7 — Trigger and Scope**: missing path filters, no `--filter` on PR turbo, cron frequency, every PR paying for coverage that could run after merge (`OPT90`).
- **Category 8 — Release Workflow**: release-path caching + redundancy.
- **Category 9 — Queue Times and Concurrency**: missing/!coarse concurrency groups.
- **Category 10 — Timing Anomalies**: failure-rate / bimodal duration signals (advisory), and the same job measured faster on another runner class (`OPT81`).
- **Category 11 — Stack-Specific**: turbo task outputs, unstable turbo env keys, type-aware ESLint on a slow lint job (`OPT82`).
- **Category 12 — Build Caching (language-agnostic)**: uncached compiled-language builds, a dependency compiled from source when a prebuilt package exists (`OPT86`), a compile nobody profiled (`OPT91`), a machine-wide .NET SDK reconciling unrelated workloads (`OPT94`).
- **Category 13 — Hidden Failures and Dead Config**: dead env vars, misconfigured caches.
- **Category 14 — Structural / Critical-Path Levers** (`OPT70`–`OPT75`, `OPT78`, `OPT83`–`OPT85`, `OPT87`): routed from the measured long pole (see ARCHITECTURE §11), not a flat grep. `OPT85` (tests run in many processes instead of one) and `OPT87` (tests wait on a slow dependency they do not verify) have no router yet: like `OPT74`, they are catalogued for application from the drilled pole's log and are reported as without a detector. `OPT78` is routed by the drill-time leaf detector rather than the structural router; `OPT83` and `OPT84` are routed by structural passes over the drilled poles' step decomposition plus the workflow YAML, not by the log leaves (`OPT84` also follows the step's `package.json` scripts).

(Catalog OPT-ids are the static scan; the `blocking_path.py` `_parse_log` **leaf
detectors** — prisma / vitest / turbo / playwright — are a separate set run over
the drilled long poles' captured logs, documented in ARCHITECTURE §12.3. `OPT78`
is the one catalog entry routed by that log-leaf set: it documents and reserves
the id for the `vitest-isolate-pool` leaf, and is not part of the static scan.)

---

<!-- frame: category 3 -->
## Category 3: Docker

---

<!-- frame: category 4 -->
## Category 4: Parallelization

---

<!-- frame: category 5 -->
## Category 5: Actions and Checkout

---

<!-- frame: category 7 -->
## Category 7: Trigger and Scope

---

<!-- frame: category 8 -->
## Category 8: Release Workflow

---

<!-- frame: category 10 -->
## Category 10: Timing Anomalies

---

<!-- frame: category 11 -->
## Category 11: Stack-Specific

> The patterns below apply only when the repo uses the listed tools.
> Skip the entire category if the repo's stack doesn't match.

---

<!-- frame: category 12 -->
## Category 12: Build Caching (Language-Agnostic)

---

<!-- frame: category 14 -->
## Category 14: Structural / Critical-Path Levers

These patterns are a **different class** from everything above. The catalog
patterns OPT1–OPT69, OPT76, OPT77, OPT79, OPT80, OPT81, OPT82, OPT86, OPT88–OPT91, OPT93 and OPT94 are *hygiene*: each is a named,
locally-checkable defect with
a mechanical, low-risk fix, detected by matching workflow YAML against the
catalog. OPT81, like OPT79 and OPT80, is derived from run history rather than matched against YAML, and its A2 advisory is risk MEDIUM. On real repos almost every hygiene hit moves **~0 developer
wall-clock** — the true bottleneck is usually a check that is *working as
intended* but is simply the slowest thing on the critical path, with no catalog
match.

Structural patterns attack exactly that gap. They are **not** detected by
declarative YAML matching; they are **routed** from the measured critical path
(`collect_runs.py` decomposes the long-pole job, cross-references required
checks, and finds shared/redundant work), and the final lever framing is written
by a per-candidate reasoning step. They carry an OPT-id so the report and tests
stay catalog-keyed, but the catalog is no longer the only thing that can produce
a finding.

Most of them (OPT70–OPT75) are routed by the deterministic structural router in
`collect_runs.py`. **OPT83** and **OPT84** are each routed by a structural pass over each drilled pole's
per-step decomposition and its workflow YAML (not by the log leaves) and emit a
finding with its own card; OPT84 also reads the repo's `package.json` scripts and tool versions. **OPT85** and **OPT87** have no router yet: like OPT74 they are listed as structural patterns without a detector, and are applied by reading the drilled pole's log and test code. **OPT78** is routed instead by a drill-time leaf detector over
the long pole's captured log (ARCHITECTURE §12.3), corroborated against the repo's
test-runner config. It emits no finding record, so the render boundary's `Risk`
row and banner never see it: its HIGH risk is stamped on the pole's drill-down
and its intent check, guardrail and rollout ride in its agent prompt. `scan.py`
reports it as having no critical-path router so its coverage is never overstated.

**The cost of the higher leverage is higher risk.** A hygiene fix at worst does
nothing. A structural change can **degrade correctness** — drop coverage, turn a
real failure into a false green, diverge from the shipped artifact. So every
structural pattern declares a **risk** rating, a **mandatory guardrail**, and a
**conservative rollout** in its METADATA, and the report ranks on savings AND
risk as **separate axes**: a high-savings/high-risk candidate can sit *below* a
boring safe one. A structural finding is NEVER presented as a safe quick win.

`risk` values: `LOW` (mechanical, reversible, no correctness exposure) <
`MEDIUM` (changes a signal/trigger; reversible but can drop a check developers
rely on) < `HIGH` (can silently change what is built/tested/shipped —
correctness exposure). The report's ranking demotes by risk so the rule is
visible and a future edit can't quietly re-promote a HIGH-risk lever into the
quick-wins list.

