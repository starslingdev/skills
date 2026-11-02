---
pattern: OPT93
impact: MEDIUM
class: data-driven
detector: manual
risk: MEDIUM
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml,NuGet.Config'
fix_strategy: benchmark-nuget-cache-placement
title_template: 'NuGet restores to a slower disk on Windows runners'
category: 1
---

# OPT93 — NuGet Restores to a Slower Disk on Windows Runners

**Not auto-detected yet.** No audit phase emits this entry or claims to have checked it. Apply it manually to the audited repository's logs and comparable job measurements.

**Anti-pattern:** NuGet's effective package and HTTP cache paths sit on a demonstrably slower disk, and cold restore spends measurable time there. Default profile placement alone is not a defect. Logs establish where packages are restored and how long restore takes; whether another disk is faster is established only by the benchmark below. Preserve deliberately faster local or NVMe placement.

**Fix recipe:** on equivalent runner hardware and the same toolchain, benchmark `NUGET_PACKAGES` and `NUGET_HTTP_CACHE_PATH` under runner temp after verifying its disk. Explicitly print the effective paths, verify restore/build/test behavior, and measure cold and warm whole jobs separately. Moving both paths together cannot isolate either path's individual contribution. A cold pilot with remote caching disabled does not predict the production warm path.

If `actions/cache` persists the package cache, change its path and version the cache key while retaining OS, architecture and dependency scoping. Never remotely persist HTTP caches or credentials. Include remote restore/save transfer cost in the comparison; reject a change that shortens restore but lengthens the job.

**Sizing: uncredited.** No detector or sizing model. In the finding or PR description of the benchmark, report the audited repository's sample sizes, hardware and cache-state exclusions. No billing or production speedup claim follows from a small cold pilot.

**Risk: MEDIUM.** Changing paths without changing cache keys or validating actual disk placement can make the job slower or restore an incompatible cache.
