---
pattern: OPT73
impact: HIGH
class: structural
detector: critical-path
risk: LOW
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml'
fix_strategy: shared-substep-floor
title_template: 'A shared step recurs across the whole cluster — fix it once, lower the floor'
category: 14
---

# OPT73 — Shared Sub-Step Across Critical-Path Jobs (cluster-floor lever)

**TL;DR**: The same named step re-runs in several jobs that execute at the same time (the evidence names the step and its cost in each job) — speeding up that one step lowers all of those jobs at once.

**Anti-pattern**: A normalized step (same name/category — e.g. `pnpm install`, `setup toolchain`, `build base image`, `restore deps`) that appears in ≥2 of the jobs in the critical-path cluster, each paying its full cost independently. Cutting one job's copy leaves the others gating the run.

**Detection heuristic** (routed): across the cluster jobs (the long pole plus every job within striking distance of it — the floor band), normalize step names (strip matrix args, lowercase, category-classify) and find a step category that recurs in ≥2 cluster jobs with material p50 in each. That step is a floor-lowering candidate.

**Fix recipe**: Make the shared step cheap **in every job that runs it** — a warm dependency/build cache keyed so all cluster jobs hit it, a prebuilt base image they all pull, or a `setup-*` `cache:` shared across jobs. The saving is credited across **every** cluster job containing the step (the floor drops by the per-job saving), not just the long pole — that's what makes it beat the floor. (Avoid the serial-gate trap: do NOT consolidate the shared step into one upstream job the others `needs:` — that adds wall-clock behind a serial gate, see OPT14/§4. Lower the floor by making each parallel copy cheap, not by serializing.) When the jobs repeating the step are few and the shared setup is large, the opposite move can win: merge them into one job and run their payloads as a `parallel:` step group inside it, so the setup is paid once (OPT77's fix covers the syntax, the runner requirement and the limits).

**Risk**: **LOW** — caching a shared setup step is mechanical and reversible, and changes no test/build semantics. (Escalates only if the "shared step" is itself a build whose caching could serve stale outputs — then carry a cache-key-correctness check.)

**Guardrail**: Verify the cache key captures the step's real inputs (lockfile, toolchain version, source the build reads) so a warm hit never serves stale artifacts. On ephemeral runners, confirm the cache restores warm (OPT8).

**Rollout**: Ship the shared cache, measure the floor (second-tallest job p50) before/after across 5 PRs — the wall-clock win shows only when the whole cluster comes down.

---
