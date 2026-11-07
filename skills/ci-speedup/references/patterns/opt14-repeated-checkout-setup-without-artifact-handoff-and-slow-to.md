---
pattern: OPT14
impact: MEDIUM
class: static
detector: yaml-job-correlated
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml'
fix_strategy: repeated-checkout-setup-without-artifact-handoff-and-slow-to
title_template: 'Repeated Checkout/Setup Without Artifact Handoff'
category: 2
aliases:
  - opt14--repeated-checkout-setup-without-artifact-handoff-and-slow-tool-replacement
  - opt14--repeated-checkoutsetup-without-artifact-handoff-and-slow-tool-replacement
alias_note: 'Alias anchors: this heading lost "(and Slow Tool Replacement)" when that half moved to OPT84; committed findings files, the example reports and the website still link both old slugs, so they stay.'
---

# OPT14 — Repeated Checkout/Setup Without Artifact Handoff

**TL;DR**: Multiple jobs each re-install and re-build from scratch instead of building once and passing the result to the others.

**Anti-pattern**: Every job checks out code and installs dependencies independently, even when a prior job already did the same work. No artifacts are passed between jobs.

**Detection heuristic**:

- Count checkout + install sequences across jobs in same workflow
- Check for `actions/upload-artifact` / `actions/download-artifact` usage

**Fix**: Use artifact handoff for built outputs, or extract the shared setup into a reusable composite action to at least reduce duplication.

**GUARDRAIL — same as OPT12 above.** When the proposed fix is "share artifacts from a prior workflow / job", the artifact-existence check is mandatory. See [OPT12 GUARDRAIL](#opt12--duplicated-setup-across-jobs) for the full procedure and required `evidence` text. Findings whose producer doesn't upload an artifact are capped at HIGH severity, and the saving must be sized from measured install dedup + cold-tail recovery, not from "downstream rebuilds from scratch" assumptions.

A slow lint, format or type-check tool on the long pole is [OPT84](#opt84--a-slow-step-on-the-long-pole-runs-a-tool-that-has-a-native-drop-in).

---
