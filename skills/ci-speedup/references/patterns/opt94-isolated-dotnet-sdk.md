---
pattern: OPT94
impact: MEDIUM
class: data-driven
detector: manual
risk: MEDIUM
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml,global.json'
fix_strategy: benchmark-isolated-dotnet-sdk
title_template: 'The .NET SDK reconciles unrelated machine-wide workloads'
category: 12
---

# OPT94 — The .NET SDK Reconciles Unrelated Machine-Wide Workloads

**Not auto-detected yet.** No audit phase emits this entry or claims to have checked it. Apply it manually using the audited repository's workload-installation logs and whole-job timings.

**Anti-pattern:** installing the required .NET workloads reconciles unrelated machine-wide workloads. Logs must identify that work and its measured cost; a slow setup step or a machine-wide SDK path alone is insufficient.

**Fix recipe:** benchmark a pinned SDK installed with `actions/setup-dotnet` and `DOTNET_INSTALL_DIR` under runner temp. Explicitly install and verify the pinned required workloads, selected `dotnet` executable path, SDK version and installation mode. Directory existence does not establish a usable installation. Preserve the build, tests and packaging checks, and state which release paths the comparison actually exercised.

Compare whole jobs on equivalent runner hardware and separate cold/warm states. A before/after observation across revisions does not attribute every second to SDK isolation; name simultaneous changes and the sample size.

**Windows Installer caution:** restoring MSI-managed workload directories does not recreate installer state. Do not suggest that shortcut without a verified installation/restoration contract. This is not a prohibition on relocatable SDK or Xcode caches. Installed-folder caching has no standalone entry here.

**Sizing: uncredited.** No detector or sizing model; only the audited repository's comparable measurements can establish a saving.

**Risk: MEDIUM.** SDK resolution and installer state affect correctness as well as startup time; keep required platform and release validation.
