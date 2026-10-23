---
pattern: OPT51
impact: MEDIUM
class: data-driven
detector: manual
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml'
fix_strategy: install-to-test-ratio-50
title_template: 'Install-to-Test Ratio >50%'
category: 10
status: cut
---

# OPT51 — Install-to-Test Ratio >50%

> **⚠️ CUT — NOT auto-emitted.** This pattern's detector is retained for
> reference but is **not dispatched** by `collect_runs.py` — same flaw as OPT49.
> A high setup/total *ratio* is an OBSERVATION, not a verified lever: the
> detector credited `med_total * (med_ratio - 0.3)` as savings (assuming setup
> is reducible to 30% of the job) without ever proving the setup *is* reducible.
> A high ratio is just as often STRUCTURAL — a peer-dependency validator, a
> docs-lint, or a Docker/Playwright job is mostly install by nature and can't be
> cached away — so it sized large runner-min figures onto unrealizable savings,
> exactly the "a job is slow" observation the admission gate forbids. The
> **verified** setup signal is now carried by the cache family (proves a cold
> cache from the log), by **OPT73** (a shared setup step across the cluster,
> sized honestly), and by the artifact-handoff patterns (a concrete, realizable
> lever). The body below is historical reference only.

**TL;DR**: A job spends more time setting up (checkout + install) than actually running tests or building.

**Anti-pattern**: More than half of a job's runtime is spent on setup/install steps rather than actual test/lint/build work. Indicates caching problems or excessive setup.

**Detection heuristic**:

- Classify each step as "setup" (checkout, install, cache restore, Docker startup) or "work" (test, lint, build, type-check)
- Compute ratio: setup_time / total_time
- Flag if >50%

**Fix**: Improve caching, use artifact handoff from a setup job, or consolidate setup into a composite action.
