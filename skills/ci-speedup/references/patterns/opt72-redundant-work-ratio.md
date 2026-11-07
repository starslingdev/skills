---
pattern: OPT72
impact: MEDIUM
class: structural
detector: critical-path
risk: MEDIUM
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml'
fix_strategy: redundant-work-ratio
title_template: 'Long pole spends most of its time on setup, not the actual work'
category: 14
---

# OPT72 — Redundant-Work Ratio (build/install ≫ payload)

**TL;DR**: Your slowest job spends most of its time building and installing, and only a little on the tests it actually exists to run.

**Anti-pattern**: A critical-path job whose `dominant_setup_or_build_step_time ÷ payload_step_time` ratio is high — the install/build steps dwarf the test/lint/scan step that is the job's actual purpose. Common shape: a monorepo job that `pnpm install && turbo run build` the whole tree, then runs one package's tests.

**Detection heuristic** (routed): from the step-duration decomposition of a critical-path job, compute `(sum of checkout+install+build+setup step p50) ÷ (sum of test+scan+package step p50)`. The router flags when this ratio exceeds 2× and the dominant step is itself a build or test step. The redundant build/install is the candidate to scope (OPT70) or warm-cache.

**Fix recipe**: Two paths, different risk. (a) **Scope the setup** to what the payload needs — only build the packages the tested package depends on (`turbo run build --filter=<pkg>...`). This inherits OPT70's HIGH-risk correctness guardrails (a missed transitive dep means testing against a stale build). (b) **Warm-cache the setup** — make the redundant build a cache restore (dependency cache, build cache keyed on inputs) so each run pays seconds, not minutes. Path (b) is the **safe default**: it removes the *cost* of the redundancy without removing the redundancy itself, so correctness is unchanged.

**Risk**: **MEDIUM** as written (warm-cache path is LOW; scope-the-setup path escalates to OPT70's HIGH). The report should prefer the cache path unless the user explicitly accepts OPT70's correctness rollout.

**Guardrail**: If recommending the scope path, carry OPT70's full guardrail (full-build fallback, output diff). If recommending the cache path, verify on ephemeral runners that the cache actually restores warm (an `actions/cache` hit that the tool re-validates from scratch is not a saving — see OPT8).

**Rollout**: Cache path — ship and measure warm-vs-cold step time over 5 PRs. Scope path — OPT70's parallel-run rollout.

---
