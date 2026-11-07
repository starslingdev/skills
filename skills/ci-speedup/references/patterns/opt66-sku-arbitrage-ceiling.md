---
pattern: OPT66
impact: LOW
class: data-driven
detector: manual
affected_files: '.github/workflows/*.yml,.github/workflows/*.yaml'
title_template: 'SKU Arbitrage Ceiling from Expensive Hosted Runners'
category: 13
status: removed
---

# OPT66 — SKU Arbitrage Ceiling from Expensive Hosted Runners

> **⚠️ REMOVED — pricing excision (2026-07-20).** OPT66 was a **dollar-only**
> pattern: it derived a published-rate *ceiling* (the $/mo you could avoid by
> moving a job to a cheaper same-core SKU) — never a credited saving. The
> 2026-07-20 pricing punt stripped every rate-derived surface from the skill, so
> the `actions-sku-arbitrage-ceiling` detector was deleted along with
> `scripts/billing.py` and `references/runner-rates.json`. Unlike the OPT49 /
> OPT51 CUTs, **no detector is retained** — the pattern has no meaning in
> a runner-minutes-only world and cannot be emitted. Per the retired-id rule (top
> of this file) the **id stays retired and is never reused**, so historical
> reports, evals, and fix-strategy strings never collide; the maintainers'
> pre-public development archive (#98 / #100) preserves the original detector
> and fix recipe for any future re-introduction.
> See CHANGELOG `[Unreleased] › Removed`.
