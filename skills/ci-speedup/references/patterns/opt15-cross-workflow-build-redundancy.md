---
pattern: OPT15
impact: HIGH
class: static
detector: yaml-workflow-correlated
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml'
fix_strategy: cross-workflow-build-redundancy
title_template: 'Cross-Workflow Build Redundancy'
category: 2
---

# OPT15 — Cross-Workflow Build Redundancy

**TL;DR**: The same build runs in several separate workflows that all fire on one PR, doing identical work several times over.

**Anti-pattern**: The same build command (producing identical artifacts) appears in multiple independently-triggered workflows that all fire on the same PR push event. Each workflow rebuilds from scratch, multiplying build time by the number of workflows.

**Detection heuristic**:

1. List all workflows triggered by `pull_request` on the same branch.
2. For each, extract build step commands (normalized — strip env vars, paths).
3. Hash the commands. Count how many times the same build runs per PR push across all workflows.
4. Flag if >3 identical builds exist across workflows.

```bash
# Example: count cmake/build.sh invocations across all PR workflows
grep -rn 'build\.sh\|cmake\|cargo build\|cargo test\|maturin\|pip install' /tmp/workflows/*.yml | \
  grep -v '#' | sort
```

**Fix**: Extract the shared build into a dedicated workflow or composite action. Use `actions/cache` or `actions/upload-artifact` to share build outputs across workflows.

**Wall-clock warning (serial-gate check)**: The "build once, share artifact" fix (a dedicated prebuild job + downstream consumers that `needs:` it) removes parallel-overlapped compute — a real **runner-minute** win — but it inserts a **SERIAL stage** ahead of the fan-out that every consumer must wait on, so it **ADDS wall-clock**. In the baseline the N copies of the build run in parallel, so wall-clock pays for ONE; the patch collapses them behind one gated build plus an artifact download. Before recommending this pattern, run the critical-path check:

```
Δ wall-clock = + (new serial prebuild stage) + (artifact upload/download/extract on the critical consumer) − (build work removed from the long pole)
```

If `Δ wall-clock > 0` the finding is **wall-clock-NEGATIVE** → demote it to the runner-minute appendix and flag "do NOT ship for wall-clock"; never place it in Tier 1. **Worked proof**: the dedupe-pnpm-build parity benchmark measured **−1,950 runner-min/mo** but **+70–90s wall-clock/run** (build-job ~95s − ~40s removed from the critical consumer + ~29s download/extract ≈ +84s; net-negative on ~20% of runs from 190 MB concurrent download contention).

**ALWAYS pair this with the wall-clock-correct alternative**: warm the build cache (fix the Turbo/Next.js cache key) so each parallel build becomes a ~8s cache restore — the **same** redundancy removed (−33s/job), but with **NO serial gate**. This removes the *cost* of the redundancy instead of the redundancy itself, so the parallel copies stay cheap and wall-clock improves. See `wall-clock-methodology.md` §4 (serial-gate findings can be wall-clock-NEGATIVE) for the critical-path math.

---
